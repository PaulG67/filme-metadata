from __future__ import annotations

import re
import time

import httpx

from app.identify import Candidate, norm_provider_ids

_TMDB_ID = re.compile(r"^\d{1,12}$")
_IMDB_ID = re.compile(r"^tt\d{5,10}$")
_TRAILER_KEY = re.compile(r"^[A-Za-z0-9_-]{6,20}$")

BASE = "https://api.themoviedb.org/3"


class TmdbClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.http = httpx.Client(timeout=30, follow_redirects=True)

    def _get(self, path: str, params: dict) -> dict:
        query = {"api_key": self.api_key, **params}
        response = self.http.get(f"{BASE}{path}", params=query)
        if response.status_code == 429:
            time.sleep(2)
            response = self.http.get(f"{BASE}{path}", params=query)
        if response.status_code >= 400:
            raise RuntimeError(f"TMDB {path} -> {response.status_code}")
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"TMDB {path} ohne Objekt")
        return data

    def search(self, kind: str, query: str, year: int | None) -> list[Candidate]:
        years: list[int | None] = [year, None] if year else [None]
        found: list[Candidate] = []
        seen: set[str] = set()
        for release_year in years:
            for item in self._search_once(kind, query, release_year):
                tmdb_id = item.provider_ids.get("tmdb") or ""
                if not tmdb_id or tmdb_id in seen:
                    continue
                seen.add(tmdb_id)
                found.append(item)
                if len(found) >= 8:
                    return found
        return found

    def _search_once(self, kind: str, query: str, year: int | None) -> list[Candidate]:
        if kind == "movie":
            params: dict = {"query": query, "language": "de-DE", "include_adult": "false"}
            if year:
                params["year"] = year
            payload = self._get("/search/movie", params)
            rows = payload.get("results") or []
            return [self._movie(row) for row in rows[:8]]
        params = {"query": query, "language": "de-DE", "include_adult": "false"}
        if year:
            params["first_air_date_year"] = year
        payload = self._get("/search/tv", params)
        rows = payload.get("results") or []
        return [self._series(row) for row in rows[:8]]

    def _series(self, row: dict) -> Candidate:
        return Candidate(
            name=row.get("name") or "",
            original_name=row.get("original_name") or "",
            year=_year(row.get("first_air_date")),
            provider_ids={"tmdb": str(row.get("id"))},
            overview=row.get("overview") or "",
            source="tmdb",
        )

    def _movie(self, row: dict) -> Candidate:
        return Candidate(
            name=row.get("title") or "",
            original_name=row.get("original_title") or "",
            year=_year(row.get("release_date")),
            provider_ids={"tmdb": str(row.get("id"))},
            overview=row.get("overview") or "",
            source="tmdb",
        )

    def enrich(self, kind: str, candidate: Candidate) -> None:
        tmdb_id = candidate.provider_ids.get("tmdb")
        if not tmdb_id:
            return
        path = f"/movie/{tmdb_id}" if kind == "movie" else f"/tv/{tmdb_id}"
        data = self._get(path, {"append_to_response": "external_ids", "language": "de-DE"})
        external = data.get("external_ids") or {}
        ids = {"tmdb": str(tmdb_id)}
        if external.get("imdb_id"):
            ids["imdb"] = str(external["imdb_id"])
        if external.get("tvdb_id"):
            ids["tvdb"] = str(external["tvdb_id"])
        candidate.provider_ids = {**norm_provider_ids(ids), **candidate.provider_ids}
        if kind == "series":
            seasons = {
                int(season["season_number"]): int(season.get("episode_count") or 0)
                for season in data.get("seasons") or []
                if int(season.get("season_number") or 0) > 0 and int(season.get("episode_count") or 0) > 0
            }
            if seasons:
                candidate.seasons = seasons
        if not candidate.overview:
            candidate.overview = data.get("overview") or ""

    def resolve(self, kind: str, tmdb: str, imdb: str, tvdb: str) -> tuple[str, str, str]:
        if tmdb:
            return kind, tmdb, ""
        if imdb:
            found = self._from_find(imdb, "imdb_id", kind)
            if found:
                return found
        if tvdb:
            found = self._from_find(tvdb, "tvdb_id", kind)
            if found:
                return found
        return kind, "", ""

    def _from_find(self, external_id: str, source: str, kind: str) -> tuple[str, str, str] | None:
        data = self._get(f"/find/{external_id}", {"external_source": source, "language": "de-DE"})
        movie = (data.get("movie_results") or [None])[0]
        series = (data.get("tv_results") or [None])[0]
        chosen = movie if kind == "movie" else series
        chosen_kind = kind
        if not chosen:
            chosen = movie or series
            chosen_kind = "movie" if chosen is movie and movie else "series"
        if not chosen or not chosen.get("id"):
            return None
        title = chosen.get("title") or chosen.get("name") or ""
        return chosen_kind, str(chosen["id"]), title

    def describe(self, kind: str, tmdb_id: str) -> dict:
        path = f"/movie/{tmdb_id}" if kind == "movie" else f"/tv/{tmdb_id}"
        data = self._get(path, {"append_to_response": "videos,credits,external_ids", "language": "de-DE"})
        if not (data.get("overview") or "").strip():
            english = self._get(path, {"language": "en-US"})
            if english.get("overview"):
                data["overview"] = english["overview"]
        external = data.get("external_ids") or {}
        imdb = str(external.get("imdb_id") or "")
        tvdb = str(external.get("tvdb_id") or "")
        videos = ((data.get("videos") or {}).get("results")) or []
        cast = [
            person.get("name")
            for person in ((data.get("credits") or {}).get("cast") or [])
            if person.get("name")
        ][:6]
        runtime = data.get("runtime")
        if not runtime:
            lengths = [int(value) for value in (data.get("episode_run_time") or []) if value]
            runtime = lengths[0] if lengths else None
        poster = data.get("poster_path") or ""
        seasons = data.get("number_of_seasons")
        return {
            "name": data.get("title") or data.get("name") or "",
            "original": data.get("original_title") or data.get("original_name") or "",
            "year": _year(data.get("release_date") or data.get("first_air_date")),
            "overview": data.get("overview") or "",
            "runtime": int(runtime) if runtime else None,
            "genres": [item.get("name") for item in data.get("genres") or [] if item.get("name")],
            "seasons": int(seasons) if kind != "movie" and seasons else None,
            "cast": cast,
            "poster": f"https://image.tmdb.org/t/p/w342{poster}" if str(poster).startswith("/") else None,
            "trailer": pick_trailer(videos),
            "links": external_links(kind, tmdb_id, imdb, tvdb),
            "hint": "",
        }


def external_links(kind: str, tmdb: str = "", imdb: str = "", tvdb: str = "") -> dict[str, str]:
    section = "movie" if kind == "movie" else "tv"
    tvdb_section = "movie" if kind == "movie" else "series"
    links: dict[str, str] = {}
    if _TMDB_ID.fullmatch(tmdb or ""):
        links["tmdb"] = f"https://www.themoviedb.org/{section}/{tmdb}"
    if _IMDB_ID.fullmatch(imdb or ""):
        links["imdb"] = f"https://www.imdb.com/title/{imdb}"
    if _TMDB_ID.fullmatch(tvdb or ""):
        links["tvdb"] = f"https://www.thetvdb.com/dereferrer/{tvdb_section}/{tvdb}"
    return links


def clean_provider_id(kind: str, value: str) -> str:
    text = (value or "").strip()
    if kind == "imdb":
        if text.isdigit():
            text = f"tt{text}"
        return text if _IMDB_ID.fullmatch(text) else ""
    return text if _TMDB_ID.fullmatch(text) else ""


def pick_trailer(videos: list[dict]) -> str | None:
    usable = []
    for video in videos or []:
        key = str(video.get("key") or "")
        if video.get("site") != "YouTube" or not _TRAILER_KEY.fullmatch(key):
            continue
        if video.get("type") not in {"Trailer", "Teaser"}:
            continue
        usable.append(video)
    if not usable:
        return None

    def rank(video: dict) -> tuple:
        language = video.get("iso_639_1") or ""
        language_rank = 0 if language == "de" else 1 if language == "en" else 2
        type_rank = 0 if video.get("type") == "Trailer" else 1
        official_rank = 0 if video.get("official") else 1
        return (type_rank, official_rank, language_rank)

    usable.sort(key=rank)
    return str(usable[0]["key"])


def lookup_title(api_key: str, kind: str, tmdb: str = "", imdb: str = "", tvdb: str = "", name: str = "", year: int | None = None) -> dict:
    kind = "movie" if kind == "movie" else "series"
    tmdb = clean_provider_id("tmdb", tmdb)
    imdb = clean_provider_id("imdb", imdb)
    tvdb = clean_provider_id("tvdb", tvdb)
    links = external_links(kind, tmdb, imdb, tvdb)
    base = {
        "ok": True,
        "name": name,
        "original": "",
        "year": year,
        "overview": "",
        "runtime": None,
        "genres": [],
        "seasons": None,
        "cast": [],
        "poster": None,
        "trailer": None,
        "links": links,
        "hint": "",
    }
    if not api_key:
        base["hint"] = "Für Beschreibung, Besetzung und Trailer einen TMDB-API-Key setzen. Die Links öffnen die Datenbank auch ohne Key."
        return base
    client = TmdbClient(api_key)
    try:
        resolved_kind, resolved_id, preview = client.resolve(kind, tmdb, imdb, tvdb)
        if not resolved_id:
            base["hint"] = "Zu dieser ID gibt es bei TMDB keinen Treffer."
            return base
        described = client.describe(resolved_kind, resolved_id)
    finally:
        client.http.close()
    if preview and not described.get("name"):
        described["name"] = preview
    described["links"] = {**links, **described.get("links", {})}
    described["ok"] = True
    if not described.get("name"):
        described["name"] = name
    if not described.get("year"):
        described["year"] = year
    if not described.get("trailer"):
        described["hint"] = "Kein Trailer hinterlegt."
    return described


def _year(value: str | None) -> int | None:
    if not value or len(str(value)) < 4:
        return None
    try:
        return int(str(value)[:4])
    except ValueError:
        return None
