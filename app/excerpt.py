"""Erkennung über den Dateiinhalt, nicht über den Dateinamen.

Der OpenSubtitles-Hash ist der Fingerabdruck der Datei (Anfang und Ende).
Trifft der nicht, wird ein gesprochener Ausschnitt mit den Untertiteln
der Kandidaten verglichen.
"""

from __future__ import annotations

import re
import struct

HASH_CHUNK = 65536
MIN_HASH_SIZE = HASH_CHUNK * 2

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "it", "you", "that",
    "this", "for", "on", "with", "was", "are", "be", "as", "at", "by", "from",
    "ich", "du", "und", "der", "die", "das", "ein", "eine", "ist", "nicht",
    "sie", "wir", "ihr", "zu", "den", "dem", "des", "auf", "mit", "von", "für",
    "im", "es", "sich", "auch", "wie", "aber", "wenn", "dann", "dass", "hat",
    "haben", "ein", "noch", "nur", "man", "so", "aus", "nach", "bei", "oder",
}


class ExcerptError(RuntimeError):
    pass


def movie_hash(head: bytes, tail: bytes, size: int) -> str:
    if size < MIN_HASH_SIZE:
        raise ExcerptError("Datei ist kürzer als 128 KB")
    if len(head) < HASH_CHUNK or len(tail) < HASH_CHUNK:
        raise ExcerptError("Dateiausschnitt unvollständig")
    total = size & 0xFFFFFFFFFFFFFFFF
    for chunk in (head[:HASH_CHUNK], tail[:HASH_CHUNK]):
        for (value,) in struct.iter_unpack("<q", chunk):
            total = (total + value) & 0xFFFFFFFFFFFFFFFF
    return f"{total:016x}"


def sample_offsets(duration: float, length: int = 20) -> list[int]:
    if duration <= length + 5:
        return [0]
    offsets: list[int] = []
    latest = max(0, int(duration - length - 1))
    for anchor in (0.22, 0.48, 0.72):
        offset = min(int(duration * anchor), latest)
        if offset not in offsets:
            offsets.append(offset)
    return offsets or [0]


def content_words(text: str) -> list[str]:
    cleaned = (text or "").lower().replace("'", "").replace("’", "").replace("`", "")
    cleaned = re.sub(r"[^\w\s]", " ", cleaned, flags=re.UNICODE)
    words = []
    for word in cleaned.split():
        if len(word) < 2 or word.isdigit() or word in STOPWORDS:
            continue
        words.append(word)
    return words


def srt_to_text(raw: str) -> str:
    lines: list[str] = []
    for line in (raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = re.sub(r"<[^>]+>", "", line).strip()
        if not stripped or stripped.isdigit() or "-->" in stripped:
            continue
        if re.fullmatch(r"\d{2}:\d{2}:\d{2}[,.]\d{3}", stripped):
            continue
        lines.append(stripped)
    return " ".join(lines)


def quote_match(transcript: str, subtitle_text: str) -> tuple[bool, str]:
    words = content_words(transcript)
    if len(words) < 4:
        return False, ""
    haystack = " ".join(content_words(subtitle_text))
    if not haystack:
        return False, ""
    for size in (6, 5, 4):
        if len(words) < size:
            continue
        for index in range(len(words) - size + 1):
            phrase = " ".join(words[index : index + size])
            if phrase in haystack:
                return True, phrase
    for index in range(len(words) - 2):
        chunk = words[index : index + 3]
        if all(len(word) >= 5 for word in chunk):
            phrase = " ".join(chunk)
            if phrase in haystack:
                return True, phrase
    return False, ""


def imdb_tt(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text in {"0", "none"}:
        return None
    if text.startswith("tt"):
        return text
    if text.isdigit():
        return f"tt{text}"
    return None


def imdb_number(value: object) -> str | None:
    tt = imdb_tt(value)
    if not tt:
        return None
    return tt[2:]


def parse_subtitle_hit(attributes: dict) -> dict | None:
    feature = attributes.get("feature_details") or {}
    feature_type = str(feature.get("feature_type") or attributes.get("feature_type") or "")
    parent_imdb = imdb_tt(attributes.get("parent_imdb_id") or feature.get("parent_imdb_id"))
    parent_tmdb = attributes.get("parent_tmdb_id") or feature.get("parent_tmdb_id")
    episode = feature_type.lower() == "episode" or bool(parent_imdb)
    if episode:
        if not parent_imdb and not parent_tmdb:
            return None
        name = (
            attributes.get("parent_title")
            or feature.get("parent_title")
            or feature.get("movie_name")
            or ""
        )
        imdb = parent_imdb
        tmdb = str(parent_tmdb) if parent_tmdb else None
        kind = "series"
    else:
        name = feature.get("movie_name") or feature.get("title") or ""
        imdb = imdb_tt(feature.get("imdb_id"))
        tmdb_raw = feature.get("tmdb_id")
        tmdb = str(tmdb_raw) if tmdb_raw else None
        kind = "movie"
    if not name and not imdb and not tmdb:
        return None
    year = feature.get("year") or attributes.get("year")
    try:
        year_number = int(year) if year else None
    except (TypeError, ValueError):
        year_number = None
    return {
        "kind": kind,
        "name": name,
        "year": year_number,
        "imdb": imdb,
        "tmdb": tmdb,
        "moviehash_match": bool(attributes.get("moviehash_match")),
    }


def clock(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"
