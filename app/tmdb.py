from __future__ import annotations

import time

import httpx

from app.identify import Candidate, norm_provider_ids

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


def _year(value: str | None) -> int | None:
    if not value or len(str(value)) < 4:
        return None
    try:
        return int(str(value)[:4])
    except ValueError:
        return None
