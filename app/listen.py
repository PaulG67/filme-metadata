from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from app.config import Settings
from app.excerpt import (
    ExcerptError,
    clock,
    movie_hash,
    quote_match,
    sample_offsets,
)
from app.identify import candidate_key, jellyfin_provider_ids, norm_provider_ids
from app.jellyfin import JellyfinClient, JellyfinError, candidate_from_jellyfin
from app.opensubtitles import OpenSubtitles

log = logging.getLogger("filme-metadata")
_model_lock = threading.Lock()
_model = None
_model_name = ""


def recognize(
    client: JellyfinClient,
    settings: Settings,
    item_id: str,
    candidates: list[dict],
    kind_hint: str | None,
) -> dict:
    subtitles = OpenSubtitles(
        settings.opensubtitles_api_key,
        settings.opensubtitles_username,
        settings.opensubtitles_password,
    )
    item = client.media_item(item_id)
    playable = item
    kind = "movie"
    if item.get("Type") == "Series" or kind_hint == "series":
        kind = "series"
        if item.get("Type") == "Series":
            playable = _middle_episode(client, item_id)
    hit = _hash_hit(client, subtitles, playable)
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
            "path": playable.get("Path") or item.get("Path") or "",
            "jellyfin_name": item.get("Name") or "",
            "jellyfin_year": item.get("ProductionYear"),
        }
    season, episode = _season_episode(playable)
    transcript, offset = _hear(client, settings, playable)
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
            "path": playable.get("Path") or "",
            "jellyfin_name": item.get("Name") or "",
            "jellyfin_year": item.get("ProductionYear"),
        }
    if len(matched) > 1:
        names = ", ".join(item[0].get("name") or "?" for item in matched)
        raise ExcerptError(f"Der Satz passt zu mehreren Treffern: {names}")
    if not candidates:
        raise ExcerptError(
            "Die Datei ist bei OpenSubtitles unbekannt, und es gibt keine Titel-Kandidaten "
            f"für den Dialogvergleich. Gehörter Text: „{shown}“"
        )
    raise ExcerptError(f"Keiner der Kandidaten enthält diesen Dialog. Gehörter Text: „{shown}“")


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


def _hear(client: JellyfinClient, settings: Settings, item: dict) -> tuple[str, int]:
    if not settings.opensubtitles_username:
        raise ExcerptError(
            "Die Datei ist bei OpenSubtitles nicht bekannt. Für den Dialog-Ausschnitt "
            "OPENSUBTITLES_USERNAME und OPENSUBTITLES_PASSWORD setzen."
        )
    if shutil.which("ffmpeg") is None:
        raise ExcerptError("ffmpeg fehlt im Container")
    ticks = float(item.get("RunTimeTicks") or 0)
    duration = ticks / 10_000_000 if ticks else 0
    offsets = sample_offsets(duration, settings.excerpt_seconds)
    last_text = ""
    with tempfile.TemporaryDirectory(prefix="excerpt-") as folder:
        wav = Path(folder) / "clip.wav"
        for offset in offsets:
            _extract_wav(client, item["Id"], offset, settings.excerpt_seconds, wav)
            last_text = _transcribe(wav, settings)
            words = [part for part in last_text.split() if len(part) > 2]
            if len(words) >= 8:
                return last_text.strip(), offset
    if len(last_text.split()) < 4:
        raise ExcerptError("Im Ausschnitt war kein erkennbarer Dialog")
    return last_text.strip(), offsets[-1]


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
        f"X-Emby-Token: {token}\r\n",
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
