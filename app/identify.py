"""Identität eines Films oder einer Folge ist eine Provider-ID, kein Titel.

Der Ordner (Sonarr/Radarr/Jellyfin-Namensschema) sagt, was in der Bibliothek liegt.
TMDB/TVDB/IMDb sagen, welcher Datensatz das ist. Ein gemeinsames Wort wie
„Inhumans“ reicht nicht, um zwei Serien gleichzusetzen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import PurePosixPath

AUTO_SCORE = 0.82
AUTO_MARGIN = 0.15
SUSPECT_TITLE = 0.80

ARTICLES = {"the", "a", "an", "der", "die", "das", "ein", "eine"}

GENERIC_DIRS = {
    "movies",
    "movie",
    "filme",
    "film",
    "films",
    "video",
    "videos",
    "media",
    "4k",
    "uhd",
    "1080p",
    "720p",
    "serien",
    "serie",
    "series",
    "tv",
    "tvshows",
    "tv-shows",
    "shows",
}

_EMBEDDED_ID = re.compile(
    r"[\{\[]\s*(tvdb|tmdb|imdb)(?:id)?\s*[-:=]\s*(tt\d+|\d+)\s*[\}\]]",
    re.IGNORECASE,
)
_PAREN_YEAR = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]\s*$")
_BARE_YEAR = re.compile(r"(?:^|\s)((?:19|20)\d{2})\s*$")
_EPISODE = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:s(\d{1,2})[ ._-]*e(\d{1,3})|(\d{1,2})x(\d{2,3}))(?:[^a-z0-9]|$)"
)
_QUALITY = re.compile(
    r"(?i)\b(1080p|720p|2160p|480p|4k|uhd|bluray|blu-ray|bdrip|brrip|webrip|"
    r"web-dl|webdl|hdtv|hdrip|dvdrip|x264|x265|h\.264|h\.265|h264|h265|hevc|"
    r"aac|dts|truehd|atmos|remux|proper|repack|extended|amzn|nf|dsnp|german|dl)\b"
)


def normalize(title: str) -> str:
    text = (title or "").lower().replace("'", "").replace("’", "").replace("`", "")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def word_pairs(title: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in re.split(r"\s+", (title or "").strip()):
        display = re.sub(r"^[^\w]+|[^\w]+$", "", part, flags=re.UNICODE)
        if not display:
            continue
        token = normalize(display)
        if not token or token.isdigit() or token in ARTICLES:
            continue
        pairs.append((display, token))
    return pairs


def content_tokens(title: str) -> set[str]:
    return {token for _, token in word_pairs(title)}


def title_score(query: str, candidate: str) -> float:
    left = normalize(query)
    right = normalize(candidate)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    query_tokens = content_tokens(query)
    candidate_tokens = content_tokens(candidate)
    ratio = SequenceMatcher(None, left, right).ratio()
    if not query_tokens or not candidate_tokens:
        return ratio * 0.4
    overlap = query_tokens & candidate_tokens
    if not overlap:
        return ratio * 0.35
    precision = len(overlap) / len(candidate_tokens)
    recall = len(overlap) / len(query_tokens)
    f1 = 2 * precision * recall / (precision + recall)
    return 0.7 * f1 + 0.3 * ratio


def best_title_score(query: str, *names: str) -> float:
    scores = [title_score(query, name) for name in names if name and name.strip()]
    return max(scores) if scores else 0.0


def year_score(expected: int | None, candidate: int | None) -> float | None:
    if not expected or not candidate:
        return None
    diff = abs(int(expected) - int(candidate))
    if diff == 0:
        return 1.0
    if diff == 1:
        return 0.45
    return 0.0


def norm_provider_ids(raw: dict | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (raw or {}).items():
        if value is None or str(value).strip() == "":
            continue
        norm_key = re.sub(r"[^a-z]", "", str(key).lower())
        if norm_key in {"imdb", "imdbid"}:
            norm_key = "imdb"
        elif norm_key in {"tmdb", "tmdbid", "themoviedb"}:
            norm_key = "tmdb"
        elif norm_key in {"tvdb", "tvdbid", "thetvdb"}:
            norm_key = "tvdb"
        else:
            continue
        text = str(value).strip()
        if norm_key == "imdb":
            text = text.lower()
            if text.isdigit():
                text = f"tt{text}"
            if not text.startswith("tt"):
                continue
        out[norm_key] = text
    return out


def jellyfin_provider_ids(ids: dict[str, str]) -> dict[str, str]:
    labels = {"tmdb": "Tmdb", "imdb": "Imdb", "tvdb": "Tvdb"}
    return {labels[key]: value for key, value in norm_provider_ids(ids).items() if key in labels}


def same_work(left: dict | None, right: dict | None) -> bool:
    a = norm_provider_ids(left)
    b = norm_provider_ids(right)
    return any(a.get(key) and a.get(key) == b.get(key) for key in ("imdb", "tmdb", "tvdb"))


def candidate_key(ids: dict[str, str], name: str, year: int | None) -> str:
    normalized = norm_provider_ids(ids)
    for key in ("imdb", "tmdb", "tvdb"):
        if normalized.get(key):
            return f"{key}:{normalized[key]}"
    return f"name:{normalize(name)}:{year or ''}"


def strip_embedded_ids(name: str) -> tuple[str, dict[str, str]]:
    found: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        found[match.group(1).lower()] = match.group(2)
        return " "

    cleaned = _EMBEDDED_ID.sub(replace, name or "")
    return cleaned, norm_provider_ids(found)


def parse_title_and_ids(name: str, allow_bare_year: bool = False) -> tuple[str, int | None, dict[str, str]]:
    cleaned, ids = strip_embedded_ids(name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-_")
    year: int | None = None
    match = _PAREN_YEAR.search(cleaned)
    if match:
        year = int(match.group(1))
        cleaned = cleaned[: match.start()].strip(" .-_")
    elif allow_bare_year:
        bare = _BARE_YEAR.search(cleaned)
        if bare:
            year = int(bare.group(1))
            cleaned = cleaned[: bare.start()].strip(" .-_")
    return cleaned, year, ids


def clean_media_stem(stem: str) -> tuple[str, bool]:
    dotted = "." in stem
    text = stem.replace(".", " ").replace("_", " ")
    text = _QUALITY.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-")
    return text, dotted


def is_generic_dir(name: str) -> bool:
    return normalize(name) in GENERIC_DIRS


def parse_series_path(path: str) -> tuple[str, int | None, dict[str, str]]:
    folder = PurePosixPath((path or "").replace("\\", "/")).name
    return parse_title_and_ids(folder, allow_bare_year=False)


def parse_movie_path(path: str) -> tuple[str, int | None, dict[str, str]]:
    pure = PurePosixPath((path or "").replace("\\", "/"))
    stem, dotted = clean_media_stem(pure.stem)
    file_title, file_year, file_ids = parse_title_and_ids(stem, allow_bare_year=dotted)
    parent_title, parent_year, parent_ids = ("", None, {})
    if pure.parent.name and pure.parent.name not in {"/", "."}:
        parent_title, parent_year, parent_ids = parse_title_and_ids(pure.parent.name, allow_bare_year=False)
    ids = {**parent_ids, **file_ids}
    if parent_title and not is_generic_dir(parent_title) and parent_year:
        return parent_title, parent_year, ids
    if file_title and file_year:
        return file_title, file_year, ids
    if parent_title and not is_generic_dir(parent_title):
        return parent_title, parent_year, ids
    return file_title, file_year, ids


def parse_episode_index(path: str) -> tuple[int, int] | None:
    name = PurePosixPath((path or "").replace("\\", "/")).name
    match = _EPISODE.search(name)
    if not match:
        return None
    if match.group(1) and match.group(2):
        return int(match.group(1)), int(match.group(2))
    return int(match.group(3)), int(match.group(4))


def episode_score(
    episodes: list[tuple[int, int]] | None,
    seasons: dict[int, int] | None,
) -> tuple[float | None, str]:
    if not episodes or not seasons:
        return None, ""
    by_season: dict[int, set[int]] = {}
    for season, episode in episodes:
        if season > 0 and episode > 0:
            by_season.setdefault(season, set()).add(episode)
    if not by_season:
        return None, ""
    usable = {number: count for number, count in seasons.items() if number > 0 and count > 0}
    scores: list[float] = []
    notes: list[str] = []
    impossible = False
    for season, numbers in sorted(by_season.items()):
        count = usable.get(season)
        if count is None:
            impossible = True
            notes.append(f"Staffel {season} gibt es bei diesem Treffer nicht")
            continue
        if max(numbers) > count:
            impossible = True
            notes.append(f"Datei geht bis S{season:02d}E{max(numbers):02d}, der Treffer hat nur {count} Folgen")
            continue
        complete = len(numbers) == count and set(range(1, count + 1)) <= numbers
        if complete:
            scores.append(1.0)
            notes.append(f"Staffel {season}: {count}/{count} Folgen")
        else:
            scores.append(0.55)
            notes.append(f"Staffel {season}: {len(numbers)} von {count} Folgen lokal")
    if impossible:
        return 0.0, "; ".join(notes)
    if not scores:
        return 0.0, "; ".join(notes)
    return sum(scores) / len(scores), "; ".join(notes)


def _token_diff(query: str, candidate: str) -> tuple[list[str], list[str]]:
    query_words = {token: display for display, token in word_pairs(query)}
    candidate_words = {token: display for display, token in word_pairs(candidate)}
    missing = [query_words[token] for token in query_words.keys() - candidate_words.keys()]
    extra = [candidate_words[token] for token in candidate_words.keys() - query_words.keys()]
    return missing, extra


@dataclass
class Candidate:
    name: str
    year: int | None = None
    provider_ids: dict[str, str] = field(default_factory=dict)
    overview: str = ""
    source: str = ""
    seasons: dict[int, int] | None = None
    original_name: str = ""
    title_score: float = 0.0
    year_score: float | None = None
    episode_score: float | None = None
    score: float = 0.0
    auto: bool = False
    pinned: bool = False
    notes: list[str] = field(default_factory=list)
    raw: dict | None = None

    def __post_init__(self) -> None:
        self.provider_ids = norm_provider_ids(self.provider_ids)
        if self.year is not None:
            self.year = int(self.year)


def _same_candidate(left: Candidate, right: Candidate) -> bool:
    if same_work(left.provider_ids, right.provider_ids):
        return True
    same_name = normalize(left.name) and normalize(left.name) == normalize(right.name)
    return bool(same_name and left.year and right.year and left.year == right.year)


def merge_candidates(candidates: list[Candidate]) -> list[Candidate]:
    groups: list[Candidate] = []
    for candidate in candidates:
        current = next((group for group in groups if _same_candidate(group, candidate)), None)
        if current is None:
            groups.append(candidate)
            continue
        current.provider_ids = {**candidate.provider_ids, **current.provider_ids}
        if candidate.raw and not current.raw:
            current.raw = candidate.raw
        if candidate.seasons and not current.seasons:
            current.seasons = candidate.seasons
        if candidate.overview and not current.overview:
            current.overview = candidate.overview
        if candidate.original_name and not current.original_name:
            current.original_name = candidate.original_name
        if candidate.year and not current.year:
            current.year = candidate.year
        if len(candidate.name or "") > len(current.name or ""):
            current.name = candidate.name
        sources = {part for part in (current.source, candidate.source) if part}
        current.source = "+".join(sorted(sources))
    return groups


def _combined_score(title: float, year: float | None, episode: float | None) -> float:
    parts = [(title, 0.50)]
    if year is not None:
        parts.append((year, 0.35))
    if episode is not None:
        parts.append((episode, 0.20))
    weight = sum(item[1] for item in parts)
    return sum(value * item_weight for value, item_weight in parts) / weight


def _can_auto(best: Candidate, runner_up: Candidate | None) -> bool:
    if best.year_score == 0 or best.episode_score == 0:
        return False
    if best.score < AUTO_SCORE:
        return False
    other = runner_up.score if runner_up else 0.0
    if best.score - other < AUTO_MARGIN:
        return False
    if best.year_score is None and best.title_score < 0.92:
        if best.episode_score is None or best.episode_score < 0.95:
            return False
    return True


def _matches_embedded(candidate: Candidate, embedded: dict[str, str]) -> bool:
    return any(
        candidate.provider_ids.get(key) and candidate.provider_ids.get(key) == value
        for key, value in embedded.items()
    )


def rank_candidates(
    folder_title: str,
    folder_year: int | None,
    episodes: list[tuple[int, int]] | None,
    candidates: list[Candidate],
    embedded_ids: dict[str, str] | None = None,
) -> list[Candidate]:
    embedded = norm_provider_ids(embedded_ids)
    for candidate in candidates:
        names = [candidate.name, candidate.original_name]
        candidate.title_score = best_title_score(folder_title, *names)
        compared = candidate.name
        if candidate.original_name and title_score(folder_title, candidate.original_name) > title_score(folder_title, candidate.name):
            compared = candidate.original_name
        missing, extra = _token_diff(folder_title, compared)
        candidate.year_score = year_score(folder_year, candidate.year)
        ep_score, ep_note = episode_score(episodes, candidate.seasons)
        candidate.episode_score = ep_score
        candidate.pinned = bool(embedded) and _matches_embedded(candidate, embedded)
        notes: list[str] = []
        if candidate.pinned:
            notes.append("ID steht im Ordnernamen")
        if candidate.title_score >= 0.98:
            notes.append("Titel stimmt mit dem Ordner überein")
        elif missing or extra:
            if missing:
                notes.append("Fehlende Wörter: " + ", ".join(missing))
            if extra:
                notes.append("Zusätzliche Wörter: " + ", ".join(extra))
        if folder_year and candidate.year:
            if candidate.year_score == 1:
                notes.append(f"Jahr {candidate.year} stimmt")
            elif candidate.year_score == 0:
                notes.append(f"Jahr {candidate.year} widerspricht dem Ordner ({folder_year})")
            else:
                notes.append(f"Jahr {candidate.year} weicht um 1 von {folder_year} ab")
        if ep_note:
            notes.append(ep_note)
        candidate.notes = notes
        candidate.score = 1.0 if candidate.pinned else _combined_score(
            candidate.title_score, candidate.year_score, candidate.episode_score
        )
        candidate.auto = False
    ranked = sorted(candidates, key=lambda item: (item.pinned, item.score, item.title_score), reverse=True)
    if ranked:
        runner_up = ranked[1] if len(ranked) > 1 else None
        ranked[0].auto = _can_auto(ranked[0], runner_up)
    return ranked


def needs_review(
    folder_title: str,
    folder_year: int | None,
    jellyfin_name: str,
    jellyfin_year: int | None,
    jellyfin_ids: dict | None,
    embedded_ids: dict[str, str] | None = None,
    extra_names: list[str] | None = None,
) -> str | None:
    if not folder_title:
        return None
    embedded = norm_provider_ids(embedded_ids)
    current = norm_provider_ids(jellyfin_ids)
    for key, value in embedded.items():
        if current.get(key) and current[key] != value:
            label = key.upper() if key != "tmdb" else "TMDB"
            return f"Im Ordner steht {label} {value}, Jellyfin hat {current[key]}"
    names = [jellyfin_name, *(extra_names or [])]
    score = best_title_score(folder_title, *names)
    reasons: list[str] = []
    if score < SUSPECT_TITLE:
        shown = jellyfin_name or "ohne Titel"
        reasons.append(f"Ordner „{folder_title}“, Jellyfin zeigt „{shown}“")
    if folder_year and jellyfin_year and abs(int(folder_year) - int(jellyfin_year)) > 1:
        reasons.append(f"Ordnerjahr {folder_year}, Jellyfin {jellyfin_year}")
    if not reasons:
        return None
    return "; ".join(reasons)


def build_finding(
    *,
    folder_title: str,
    folder_year: int | None,
    jellyfin_name: str,
    jellyfin_year: int | None,
    jellyfin_ids: dict | None,
    candidates: list[Candidate],
    episodes: list[tuple[int, int]] | None = None,
    embedded_ids: dict[str, str] | None = None,
    extra_names: list[str] | None = None,
    episode_issues: list[dict] | None = None,
) -> dict | None:
    reason = needs_review(
        folder_title,
        folder_year,
        jellyfin_name,
        jellyfin_year,
        jellyfin_ids,
        embedded_ids,
        extra_names,
    )
    if reason is None:
        return None
    embedded = norm_provider_ids(embedded_ids)
    ranked = rank_candidates(folder_title, folder_year, episodes, list(candidates), embedded)
    if embedded and not any(item.pinned for item in ranked):
        synthetic = Candidate(
            name=folder_title,
            year=folder_year,
            provider_ids=embedded,
            source="ordner",
            notes=["ID aus dem Ordnernamen, kein Suchtreffer"],
            title_score=1.0,
            score=1.0,
            pinned=True,
            auto=True,
        )
        ranked.insert(0, synthetic)
        for item in ranked[1:]:
            item.auto = False
    if ranked and same_work(jellyfin_ids, ranked[0].provider_ids) and ranked[0].year_score != 0:
        return None
    public = [_public_candidate(item) for item in ranked[:6]]
    if not public:
        status = "no_match"
    elif public[0]["auto"]:
        status = "sure"
    else:
        status = "review"
    return {
        "folder_title": folder_title,
        "folder_year": folder_year,
        "jellyfin_name": jellyfin_name,
        "jellyfin_year": jellyfin_year,
        "jellyfin_ids": jellyfin_provider_ids(norm_provider_ids(jellyfin_ids)),
        "reason": reason,
        "status": status,
        "candidates": public,
        "episode_issues": episode_issues or [],
    }


def _public_candidate(candidate: Candidate) -> dict:
    return {
        "key": candidate_key(candidate.provider_ids, candidate.name, candidate.year),
        "name": candidate.name,
        "original_name": candidate.original_name,
        "year": candidate.year,
        "provider_ids": jellyfin_provider_ids(candidate.provider_ids),
        "overview": (candidate.overview or "")[:400],
        "source": candidate.source,
        "score": round(candidate.score, 3),
        "title_score": round(candidate.title_score, 3),
        "year_score": None if candidate.year_score is None else round(candidate.year_score, 3),
        "episode_score": None if candidate.episode_score is None else round(candidate.episode_score, 3),
        "auto": candidate.auto,
        "notes": list(candidate.notes),
        "raw": candidate.raw,
    }
