from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from app.config import Settings
from app.excerpt import (
    ExcerptError,
    clock,
    movie_hash,
    preview_offset,
    quote_match,
    sample_offsets,
)
from app.identify import candidate_key, jellyfin_provider_ids, norm_provider_ids
from app.posters import poster_url
from app.jellyfin import JellyfinClient, JellyfinError, candidate_from_jellyfin
from app.opensubtitles import OpenSubtitles

log = logging.getLogger("filme-metadata")
_model_lock = threading.Lock()
_model = None
_model_name = ""
_CLIP_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_CLIP_MAX_AGE = 900


def playback_source(client: JellyfinClient, item: dict, kind_hint: str | None, playable: dict | None) -> dict:
    if playable is not None:
        return playable
    if item.get("Type") == "Episode":
        return item
    if item.get("Type") == "Series" or kind_hint == "series":
        return _middle_episode(client, str(item.get("Id") or ""))
    return item


def clip_file(settings: Settings, item_id: str) -> Path | None:
    if not _CLIP_ID.fullmatch(item_id or ""):
        return None
    return settings.data_dir / "clips" / f"{item_id}.mp4"


def clip_label(item: dict, offset: int) -> str:
    name = item.get("Name") or "Ausschnitt"
    when = clock(offset)
    if item.get("Type") == "Episode":
        series = item.get("SeriesName") or ""
        season = item.get("ParentIndexNumber")
        episode = item.get("IndexNumber")
        index = f"S{int(season):02d}E{int(episode):02d} " if season and episode else ""
        prefix = f"{series} · " if series else ""
        return f"{prefix}{index}{name} · ab {when}"
    year = item.get("ProductionYear")
    title = f"{name} ({year})" if year else name
    return f"{title} · ab {when}"


def save_clip(
    client: JellyfinClient,
    settings: Settings,
    clip_id: str,
    source: dict,
    offset: int | None = None,
) -> dict:
    path = clip_file(settings, clip_id)
    if path is None:
        raise ExcerptError("Ungültige Kennung für den Ausschnitt")
    if not source.get("Id"):
        raise ExcerptError("Die Datei hat keine Jellyfin-Kennung")
    if shutil.which("ffmpeg") is None:
        raise ExcerptError("ffmpeg fehlt im Container")
    ticks = float(source.get("RunTimeTicks") or 0)
    duration = ticks / 10_000_000 if ticks else 0
    if offset is None:
        offset = preview_offset(duration, settings.excerpt_seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part.mp4")
    _extract_mp4(client, str(source["Id"]), int(offset), settings.excerpt_seconds, temporary)
    temporary.replace(path)
    info = {
        "clip_id": clip_id,
        "clip_offset": int(offset),
        "clip_label": clip_label(source, int(offset)),
        "source_id": str(source.get("Id") or ""),
    }
    path.with_suffix(".json").write_text(json.dumps(info), encoding="utf-8")
    return info


def fresh_clip(settings: Settings, clip_id: str, source_id: str) -> dict | None:
    info = _stored_clip(settings, clip_id)
    path = clip_file(settings, clip_id)
    if not info or path is None or not path.is_file():
        return None
    if time.time() - path.stat().st_mtime > _CLIP_MAX_AGE:
        return None
    if source_id and info.get("source_id") != source_id:
        return None
    return info


def _stored_clip(settings: Settings, clip_id: str) -> dict | None:
    path = clip_file(settings, clip_id)
    if path is None or not path.is_file() or path.stat().st_size < 1000:
        return None
    meta = path.with_suffix(".json")
    if not meta.is_file():
        return {"clip_id": clip_id, "clip_offset": 0, "clip_label": "Filmausschnitt", "source_id": ""}
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"clip_id": clip_id, "clip_offset": 0, "clip_label": "Filmausschnitt", "source_id": ""}
    if not isinstance(info, dict) or not info.get("clip_id"):
        return None
    return info


def _ensure_clip(client: JellyfinClient, settings: Settings, clip_id: str, source: dict) -> dict:
    try:
        found = fresh_clip(settings, clip_id, str(source.get("Id") or ""))
        if found:
            return found
        return save_clip(client, settings, clip_id, source)
    except ExcerptError as exc:
        log.warning("Videovorschau übersprungen: %s", exc)
        return {}


def recognize(
    client: JellyfinClient,
    settings: Settings,
    item_id: str,
    candidates: list[dict],
    kind_hint: str | None,
    playable: dict | None = None,
) -> dict:
    subtitles = OpenSubtitles(
        settings.opensubtitles_api_key,
        settings.opensubtitles_username,
        settings.opensubtitles_password,
    )
    item = playable or client.media_item(item_id)
    kind = "series" if item.get("Type") in {"Episode", "Series"} or kind_hint == "series" else "movie"
    source = playback_source(client, item, kind_hint, playable)
    hit = _hash_hit(client, subtitles, source)
    if hit:
        note = "Datei-Fingerabdruck trifft diese Fassung"
        candidate = _candidate_from_hit(client, item_id, hit, note, kind)
        return {
            "message": f"Per Datei-Fingerabdruck erkannt: {candidate['name']}",
            "transcript": "",
            "candidate": candidate,
            "promote_key": None,
            "note": note,
            "kind": hit["kind"],
            "path": source.get("Path") or item.get("Path") or "",
            "jellyfin_name": item.get("Name") or "",
            "jellyfin_year": item.get("ProductionYear"),
            **_ensure_clip(client, settings, item_id, source),
        }
    season, episode = _season_episode(source)
    transcript, offset, clip = _hear(client, settings, source, item_id)
    matched: list[tuple[dict, str]] = []
    for candidate in candidates[:4]:
        text = _subtitle_for_candidate(subtitles, candidate, kind, season, episode)
        found, phrase = quote_match(transcript, text)
        if found:
            matched.append((candidate, phrase))
    shown = transcript[:240]
    if len(matched) == 1:
        candidate, phrase = matched[0]
        note = f"Dialog ab {clock(offset)} steht in den Untertiteln: „{phrase}“"
        return {
            "message": f"Per Ausschnitt erkannt: {candidate.get('name')}. „{shown}“",
            "transcript": shown,
            "candidate": None,
            "promote_key": candidate.get("key"),
            "note": note,
            "kind": kind,
            "path": source.get("Path") or "",
            "jellyfin_name": item.get("Name") or "",
            "jellyfin_year": item.get("ProductionYear"),
            **clip,
        }
    if len(matched) > 1:
        names = ", ".join(item[0].get("name") or "?" for item in matched)
        raise ExcerptError(f"Der Satz passt zu mehreren Treffern: {names}", clip=clip or None)
    if not candidates:
        raise ExcerptError(
            "Die Datei ist bei OpenSubtitles unbekannt, und es gibt keine Titel-Kandidaten "
            f"für den Dialogvergleich. Gehörter Text: „{shown}“",
            clip=clip or None,
        )
    raise ExcerptError(
        f"Keiner der Kandidaten enthält diesen Dialog. Gehörter Text: „{shown}“",
        clip=clip or None,
    )


def _hash_hit(client: JellyfinClient, subtitles: OpenSubtitles, item: dict) -> dict | None:
    size = _media_size(item)
    if not size or size < 131072:
        return None
    try:
        head, tail = client.read_edges(item["Id"], size)
        file_hash = movie_hash(head, tail, size)
    except (JellyfinError, ExcerptError) as exc:
        log.warning("Fingerabdruck übersprungen: %s", exc)
        return None
    log.info("OpenSubtitles-Hash für %s", item.get("Name") or item.get("Id"))
    return subtitles.by_hash(file_hash)


def _middle_episode(client: JellyfinClient, series_id: str) -> dict:
    episodes = [item for item in client.episodes(series_id) if item.get("Path") or item.get("Id")]
    if not episodes:
        raise ExcerptError("Die Serie hat keine Folgen mit einer Datei")
    episodes.sort(key=lambda item: (item.get("ParentIndexNumber") or 0, item.get("IndexNumber") or 0))
    chosen = episodes[len(episodes) // 2]
    return client.media_item(chosen["Id"])


def _season_episode(item: dict) -> tuple[int | None, int | None]:
    from app.identify import parse_episode_index

    season = item.get("ParentIndexNumber")
    episode = item.get("IndexNumber")
    if season and episode:
        return int(season), int(episode)
    parsed = parse_episode_index(item.get("Path") or "")
    if not parsed:
        return None, None
    return parsed


def _hear(client: JellyfinClient, settings: Settings, item: dict, clip_id: str) -> tuple[str, int, dict]:
    if not settings.opensubtitles_username:
        raise ExcerptError(
            "Die Datei ist bei OpenSubtitles nicht bekannt. Für den Dialog-Ausschnitt "
            "OPENSUBTITLES_USERNAME und OPENSUBTITLES_PASSWORD setzen.",
            clip=_stored_clip(settings, clip_id),
        )
    if shutil.which("ffmpeg") is None:
        raise ExcerptError("ffmpeg fehlt im Container", clip=_stored_clip(settings, clip_id))
    ticks = float(item.get("RunTimeTicks") or 0)
    duration = ticks / 10_000_000 if ticks else 0
    offsets = sample_offsets(duration, settings.excerpt_seconds)
    last_text = ""
    saved = fresh_clip(settings, clip_id, str(item.get("Id") or ""))
    order = list(offsets)
    if saved and saved.get("clip_offset") in order:
        order.remove(saved["clip_offset"])
        order.insert(0, int(saved["clip_offset"]))
    elif saved:
        order.insert(0, int(saved["clip_offset"]))
    for offset in order:
        media = clip_file(settings, clip_id)
        reuse = (
            saved is not None
            and int(saved.get("clip_offset") or -1) == int(offset)
            and media is not None
            and media.is_file()
        )
        if reuse:
            info = saved
        else:
            info = save_clip(client, settings, clip_id, item, offset)
            media = clip_file(settings, clip_id)
            saved = info
        if media is None or not media.is_file():
            continue
        last_text = _transcribe_media(media, settings)
        words = [part for part in last_text.split() if len(part) > 2]
        if len(words) >= 8:
            return last_text.strip(), offset, info
    if len(last_text.split()) < 4:
        raise ExcerptError("Im Ausschnitt war kein erkennbarer Dialog", clip=saved)
    return last_text.strip(), order[-1], saved or {}


def _extract_wav(client: JellyfinClient, item_id: str, offset: int, seconds: int, dest: Path) -> None:
    token = (client.token or "").replace("\r", "").replace("\n", "")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(offset),
        "-t",
        str(seconds),
        "-headers",
        client.ffmpeg_headers(),
        "-i",
        client.static_stream_url(item_id),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        "-y",
        str(dest),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=90, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ExcerptError("Ton-Ausschnitt hat zu lange gedauert") from exc
    if completed.returncode != 0 or not dest.exists() or dest.stat().st_size < 1000:
        detail = completed.stderr.decode("utf-8", "replace")[-240:].replace(token, "***")
        raise ExcerptError(f"Ton-Ausschnitt fehlgeschlagen: {detail}".strip())


def _extract_mp4(client: JellyfinClient, item_id: str, offset: int, seconds: int, dest: Path) -> None:
    token = (client.token or "").replace("\r", "").replace("\n", "")
    shared = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(offset),
        "-t",
        str(seconds),
        "-headers",
        client.ffmpeg_headers(),
        "-i",
        client.static_stream_url(item_id),
        "-vf",
        "scale=-2:480",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-y",
        str(dest),
    ]
    with_audio = shared[:-2] + ["-c:a", "aac", "-ac", "2", "-b:a", "96k", "-y", str(dest)]
    picture = shared[:-2] + ["-an", "-y", str(dest)]
    error = _run_ffmpeg(with_audio, token, dest, 150, "Filmausschnitt")
    if error is None:
        return
    error = _run_ffmpeg(picture, token, dest, 150, "Filmausschnitt")
    if error is not None:
        raise ExcerptError(error)


def _transcribe_media(media: Path, settings: Settings) -> str:
    with tempfile.TemporaryDirectory(prefix="excerpt-") as folder:
        wav = Path(folder) / "clip.wav"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(media),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            "-y",
            str(wav),
        ]
        error = _run_ffmpeg(command, "", wav, 60, "Ton aus dem Ausschnitt")
        if error is not None:
            raise ExcerptError(error)
        return _transcribe(wav, settings)


def _run_ffmpeg(command: list[str], token: str, dest: Path, timeout: int, label: str) -> str | None:
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return f"{label} hat zu lange gedauert"
    if completed.returncode != 0 or not dest.exists() or dest.stat().st_size < 1000:
        detail = completed.stderr.decode("utf-8", "replace")[-240:].replace(token, "***") if token else completed.stderr.decode("utf-8", "replace")[-240:]
        return f"{label} fehlgeschlagen: {detail}".strip()
    return None


def _transcribe(wav: Path, settings: Settings) -> str:
    global _model, _model_name
    with _model_lock:
        if _model is None or _model_name != settings.whisper_model:
            from faster_whisper import WhisperModel

            root = str(settings.data_dir / "models")
            _model = WhisperModel(
                settings.whisper_model,
                device="cpu",
                compute_type="int8",
                download_root=root,
            )
            _model_name = settings.whisper_model
        model = _model
    segments, _info = model.transcribe(str(wav), vad_filter=True, beam_size=1)
    return " ".join(segment.text.strip() for segment in segments).strip()


def _subtitle_for_candidate(
    subtitles: OpenSubtitles,
    candidate: dict,
    kind: str,
    season: int | None,
    episode: int | None,
) -> str:
    ids = candidate.get("provider_ids") or {}
    imdb = ids.get("Imdb") or ids.get("imdb")
    tmdb = ids.get("Tmdb") or ids.get("tmdb")
    try:
        if kind == "series":
            return subtitles.episode_text(imdb, tmdb, season, episode)
        return subtitles.movie_text(imdb, tmdb)
    except ExcerptError:
        raise
    except Exception as exc:
        log.warning("Untertitel %s: %s", candidate.get("name"), exc)
        return ""


def _candidate_from_hit(client: JellyfinClient, item_id: str, hit: dict, note: str, fallback_kind: str) -> dict:
    ids: dict[str, str] = {}
    if hit.get("imdb"):
        ids["imdb"] = hit["imdb"]
    if hit.get("tmdb"):
        ids["tmdb"] = str(hit["tmdb"])
    normalized = norm_provider_ids(ids)
    display = jellyfin_provider_ids(normalized)
    kind = hit.get("kind") or fallback_kind
    raw = None
    name = hit.get("name") or ""
    year = hit.get("year")
    try:
        results = client.remote_search_provider(kind, name, year, item_id, display)
        raw = _matching_raw(results, normalized) or (results[0] if results else None)
    except JellyfinError as exc:
        log.warning("Remote-Suche nach Fingerabdruck: %s", exc)
    if raw:
        mapped = candidate_from_jellyfin(raw)
        if mapped.name:
            name = mapped.name
        if mapped.year:
            year = mapped.year
        normalized = {**normalized, **mapped.provider_ids}
        display = jellyfin_provider_ids(normalized)
    return {
        "key": candidate_key(normalized, name, year),
        "name": name,
        "original_name": "",
        "year": year,
        "provider_ids": display,
        "overview": "",
        "poster": poster_url(mapped.poster) if raw else "",
        "source": "ausschnitt",
        "score": 1.0,
        "title_score": 1.0,
        "year_score": None,
        "episode_score": None,
        "auto": True,
        "notes": [note],
        "raw": raw,
    }


def _matching_raw(results: list[dict], ids: dict[str, str]) -> dict | None:
    for result in results:
        found = norm_provider_ids(result.get("ProviderIds") or {})
        if ids.get("imdb") and found.get("imdb") == ids["imdb"]:
            return result
        if ids.get("tmdb") and found.get("tmdb") == ids["tmdb"]:
            return result
    return None


def _media_size(item: dict) -> int | None:
    sources = item.get("MediaSources") or []
    if not sources:
        return None
    size = sources[0].get("Size")
    try:
        value = int(size)
    except (TypeError, ValueError):
        return None
    return value or None
