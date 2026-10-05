from __future__ import annotations

import io
import zipfile

import httpx

from app.excerpt import ExcerptError, imdb_number, parse_subtitle_hit, srt_to_text

API = "https://api.opensubtitles.com/api/v1"
USER_AGENT = "filme-metadata v1.0.0"


class OpenSubtitles:
    def __init__(self, api_key: str, username: str = "", password: str = "") -> None:
        if not api_key:
            raise ExcerptError(
                "OpenSubtitles-API-Key fehlt. Kostenlosen Key auf opensubtitles.com anlegen "
                "und als OPENSUBTITLES_API_KEY setzen."
            )
        self.username = username
        self.password = password
        self._token: str | None = None
        self.http = httpx.Client(
            timeout=40,
            follow_redirects=True,
            headers={
                "Api-Key": api_key,
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )

    def by_hash(self, moviehash: str) -> dict | None:
        rows = self._rows({"moviehash": moviehash, "languages": "de,en"})
        for row in rows:
            attributes = row.get("attributes") or {}
            if not attributes.get("moviehash_match"):
                continue
            hit = parse_subtitle_hit(attributes)
            if hit and (hit.get("imdb") or hit.get("tmdb")):
                hit["moviehash_match"] = True
                return hit
        return None

    def movie_text(self, imdb: str | None, tmdb: str | None) -> str:
        params: dict[str, str] = {"languages": "de", "type": "movie"}
        if imdb_number(imdb):
            params["imdb_id"] = imdb_number(imdb) or ""
        elif tmdb:
            params["tmdb_id"] = str(tmdb)
        else:
            return ""
        return self._text(params)

    def episode_text(self, imdb: str | None, tmdb: str | None, season: int | None, episode: int | None) -> str:
        if not season or not episode:
            return ""
        params: dict[str, str] = {
            "languages": "de",
            "type": "episode",
            "season_number": str(season),
            "episode_number": str(episode),
        }
        if imdb_number(imdb):
            params["parent_imdb_id"] = imdb_number(imdb) or ""
        elif tmdb:
            params["parent_tmdb_id"] = str(tmdb)
        else:
            return ""
        return self._text(params)

    def _text(self, params: dict[str, str]) -> str:
        rows = self._rows(params)
        if not rows:
            fallback = dict(params)
            fallback["languages"] = "en"
            rows = self._rows(fallback)
        if not rows:
            return ""
        row = max(rows, key=lambda item: int((item.get("attributes") or {}).get("download_count") or 0))
        files = (row.get("attributes") or {}).get("files") or []
        if not files:
            return ""
        return self._download(int(files[0]["file_id"]))

    def _rows(self, params: dict[str, str]) -> list[dict]:
        response = self.http.get(f"{API}/subtitles", params={key: value for key, value in params.items() if value})
        self._raise(response)
        data = response.json()
        return list(data.get("data") or [])

    def _login(self) -> None:
        if self._token:
            return
        if not self.username:
            raise ExcerptError(
                "OpenSubtitles-Benutzer fehlt. Für den Dialog-Abgleich Benutzername und Passwort setzen."
            )
        response = self.http.post(
            f"{API}/login",
            json={"username": self.username, "password": self.password},
        )
        self._raise(response)
        token = response.json().get("token")
        if not token:
            raise ExcerptError("OpenSubtitles-Login ohne Token")
        self._token = token

    def _download(self, file_id: int) -> str:
        self._login()
        response = self.http.post(
            f"{API}/download",
            headers={"Authorization": f"Bearer {self._token}"},
            json={"file_id": file_id},
        )
        self._raise(response)
        link = response.json().get("link")
        if not link:
            raise ExcerptError("Untertitel ohne Download-Link")
        body = self.http.get(link, headers={"Accept": "*/*"})
        if body.status_code >= 400:
            raise ExcerptError(f"Untertitel-Download fehlgeschlagen ({body.status_code})")
        return srt_to_text(decode_subtitle(body.content))

    def _raise(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        message = ""
        try:
            payload = response.json()
            message = str(payload.get("message") or payload.get("error") or "")
        except Exception:
            message = response.text[:180]
        if response.status_code == 429:
            raise ExcerptError("OpenSubtitles-Limit erreicht. Später erneut versuchen.")
        raise ExcerptError(message or f"OpenSubtitles-Fehler {response.status_code}")


def decode_subtitle(data: bytes) -> str:
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            name = next((item for item in archive.namelist() if item.lower().endswith(".srt")), "")
            if not name:
                raise ExcerptError("Untertitel-Archiv ohne SRT")
            data = archive.read(name)
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")
