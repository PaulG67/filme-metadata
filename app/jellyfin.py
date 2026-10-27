from __future__ import annotations

import re
import time

import httpx

from app.identify import Candidate


def compact_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


def matches_compact(query: str, *names: str) -> bool:
    needle = compact_name(query)
    if len(needle) < 4:
        return False
    return any(needle in compact_name(name) for name in names if name)


def has_primary_image(item: dict) -> bool:
    tags = item.get("ImageTags") or {}
    return bool(isinstance(tags, dict) and tags.get("Primary"))


def clean_token(value: str) -> str:
    token = (value or "").strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        token = token[1:-1].strip()
    return token.replace("\r", "").replace("\n", "")


def authorization_header(token: str = "") -> str:
    parts = [
        'Client="filme-metadata"',
        'Device="unraid"',
        'DeviceId="filme-metadata"',
        'Version="1.0.0"',
    ]
    token = clean_token(token)
    if token:
        parts.append(f'Token="{token}"')
    return "MediaBrowser " + ", ".join(parts)


class JellyfinError(RuntimeError):
    pass


def pick_library_user(users: list, username: str = "") -> str:
    wanted = (username or "").strip().casefold()
    admins: list[str] = []
    for user in users or []:
        if not isinstance(user, dict) or not user.get("Id"):
            continue
        user_id = str(user["Id"])
        if wanted and str(user.get("Name") or "").casefold() == wanted:
            return user_id
        if (user.get("Policy") or {}).get("IsAdministrator"):
            admins.append(user_id)
    if admins:
        return admins[0]
    for user in users or []:
        if isinstance(user, dict) and user.get("Id"):
            return str(user["Id"])
    raise JellyfinError("Kein Jellyfin-Benutzer gefunden. Der API-Key muss ein Admin-Key sein.")


class JellyfinClient:
    def __init__(self, baseurl: str, token: str, verify: bool, username: str = "") -> None:
        self.baseurl = baseurl.rstrip("/")
        self.token = clean_token(token)
        self.username = (username or "").strip()
        self._user_id = ""
        self.headers = {
            "Authorization": authorization_header(self.token),
            "Accept": "application/json",
        }
        self.http = httpx.Client(timeout=60, verify=verify, follow_redirects=True)

    def ffmpeg_headers(self) -> str:
        return f"Authorization: {authorization_header(self.token)}\r\n"

    @classmethod
    def connect(cls, baseurl: str, token: str, username: str, password: str, verify: bool) -> JellyfinClient:
        if not baseurl:
            raise JellyfinError("JELLYFIN_BASEURL fehlt")
        token = clean_token(token)
        if token:
            return cls(baseurl, token, verify, username)
        if not username:
            raise JellyfinError("Jellyfin API-Key oder Benutzername fehlt")
        access, user_id = cls.login(baseurl, username, password, verify)
        client = cls(baseurl, access, verify, username)
        client._user_id = user_id
        return client

    @staticmethod
    def login(baseurl: str, username: str, password: str, verify: bool) -> tuple[str, str]:
        headers = {
            "Authorization": authorization_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30, verify=verify, follow_redirects=True) as http:
            response = http.post(
                f"{baseurl.rstrip('/')}/Users/AuthenticateByName",
                json={"Username": username, "Pw": password or ""},
                headers=headers,
            )
        if response.status_code >= 400:
            raise JellyfinError(
                f"Jellyfin-Login fehlgeschlagen ({response.status_code}). "
                "Besser einen Admin-API-Key als JELLYFIN_TOKEN setzen."
            )
        payload = response.json()
        access = payload.get("AccessToken")
        if not access:
            raise JellyfinError("Jellyfin-Login ohne AccessToken")
        user_id = str((payload.get("User") or {}).get("Id") or "")
        return access, user_id

    def _request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None, timeout: float = 60) -> httpx.Response:
        url = f"{self.baseurl}{path}"
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.http.request(method, url, json=json, params=params, headers=self.headers, timeout=timeout)
            except httpx.HTTPError as exc:
                last_error = exc
                time.sleep(0.4 * (attempt + 1))
                continue
            if response.status_code >= 500 and attempt < 2:
                time.sleep(0.4 * (attempt + 1))
                continue
            if response.status_code == 401:
                raise JellyfinError(
                    "Jellyfin lehnt den API-Key ab (401). "
                    "Im Dashboard unter API-Keys einen Schlüssel anlegen und nur diesen Wert eintragen."
                )
            if response.status_code >= 400:
                detail = response.text[:300].replace("\n", " ")
                raise JellyfinError(f"{method} {path} -> {response.status_code} {detail}")
            return response
        raise JellyfinError(f"Jellyfin nicht erreichbar unter {self.baseurl}") from last_error

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        return self._request("GET", path, params=params).json()

    def server_name(self) -> str:
        info = self._get("/System/Info")
        if not isinstance(info, dict):
            return "Jellyfin"
        name = info.get("ServerName") or "Jellyfin"
        version = info.get("Version") or ""
        return f"{name} {version}".strip()

    def list_media(self) -> list[dict]:
        items: list[dict] = []
        start = 0
        while True:
            page = self._get(
                "/Items",
                {
                    "Recursive": "true",
                    "IncludeItemTypes": "Series,Movie",
                    "Fields": "ProviderIds,Path,ProductionYear,OriginalTitle,PremiereDate,Overview,ImageTags",
                    "StartIndex": str(start),
                    "Limit": "200",
                },
            )
            if not isinstance(page, dict):
                break
            chunk = page.get("Items") or []
            items.extend(chunk)
            start += len(chunk)
            total = int(page.get("TotalRecordCount") or 0)
            if not chunk or start >= total:
                break
        return items

    def search_items(self, term: str) -> list[dict]:
        query = term.strip()
        if len(query) < 2:
            return []
        page = self._get(
            "/Items",
            {
                "SearchTerm": query,
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Series,Episode",
                "Fields": "Path,ProductionYear,PremiereDate,IndexNumber,ParentIndexNumber,SeriesName,OriginalTitle,ImageTags",
                "Limit": "20",
                "EnableTotalRecordCount": "false",
            },
        )
        hits = [item for item in ((page.get("Items") or []) if isinstance(page, dict) else []) if item.get("Id")]
        if hits or " " in query:
            return hits
        matched = []
        for item in self.list_media():
            if matches_compact(query, item.get("Name") or "", item.get("OriginalTitle") or "", item.get("Path") or ""):
                matched.append(item)
            if len(matched) >= 20:
                break
        return matched

    def library_user_id(self) -> str:
        if self._user_id:
            return self._user_id
        try:
            users = self._get("/Users")
        except JellyfinError as exc:
            raise JellyfinError(
                "Jellyfin-Benutzer nicht lesbar. Der API-Key muss ein Admin-Key sein, "
                "damit ein Film oder eine Serie geschrieben werden kann."
            ) from exc
        if not isinstance(users, list):
            raise JellyfinError("Jellyfin-Benutzerliste fehlt")
        self._user_id = pick_library_user(users, self.username)
        return self._user_id

    def item_record(self, item_id: str, fields: str = "") -> dict:
        params = {"userId": self.library_user_id()}
        if fields:
            params["Fields"] = fields
        data = self._get(f"/Items/{item_id}", params)
        if not isinstance(data, dict) or not data.get("Id"):
            raise JellyfinError("Eintrag nicht gefunden")
        return data

    def media_item(self, item_id: str) -> dict:
        return self.item_record(
            item_id,
            "MediaSources,RunTimeTicks,Path,ProviderIds,IndexNumber,ParentIndexNumber,ProductionYear,Overview,OriginalTitle,SeriesName,SeriesId,PremiereDate,ImageTags",
        )

    def primary_image(self, item_id: str) -> tuple[bytes, str] | None:
        response = self.http.get(
            f"{self.baseurl}/Items/{item_id}/Images/Primary",
            headers=self.headers,
            params={"maxWidth": "480", "quality": "90"},
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise JellyfinError(f"Plakat nicht lesbar ({response.status_code})")
        content_type = (response.headers.get("content-type") or "image/jpeg").split(";", 1)[0].strip().lower()
        if not content_type.startswith("image/") or not response.content:
            return None
        return response.content, content_type

    def static_stream_url(self, item_id: str) -> str:
        return f"{self.baseurl}/Videos/{item_id}/stream?Static=true"

    def read_edges(self, item_id: str, size: int) -> tuple[bytes, bytes]:
        head = self._read_range(item_id, 0, 65536)
        tail_start = max(0, size - 65536)
        tail = self._read_range(item_id, tail_start, 65536)
        return head, tail

    def _read_range(self, item_id: str, start: int, length: int) -> bytes:
        end = start + length - 1
        headers = dict(self.headers)
        headers["Range"] = f"bytes={start}-{end}"
        try:
            with self.http.stream(
                "GET",
                self.static_stream_url(item_id),
                headers=headers,
                timeout=40,
            ) as response:
                if response.status_code != 206:
                    raise JellyfinError("Jellyfin gibt die Datei nicht abschnittsweise frei")
                chunks: list[bytes] = []
                remaining = length
                for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    take = chunk[:remaining]
                    chunks.append(take)
                    remaining -= len(take)
                    if remaining <= 0:
                        break
        except JellyfinError:
            raise
        except Exception as exc:
            raise JellyfinError(f"Dateiausschnitt fehlgeschlagen: {exc}") from exc
        data = b"".join(chunks)
        if len(data) < length:
            raise JellyfinError("Dateiausschnitt unvollständig")
        return data[:length]

    def remote_search_provider(
        self,
        kind: str,
        name: str,
        year: int | None,
        item_id: str,
        provider_ids: dict[str, str],
    ) -> list[dict]:
        info: dict = {"Name": name or "", "ProviderIds": provider_ids}
        if year:
            info["Year"] = int(year)
        path = "/Items/RemoteSearch/Series" if kind == "series" else "/Items/RemoteSearch/Movie"
        data = self._request("POST", path, json={"SearchInfo": info, "ItemId": item_id}, timeout=90).json()
        if isinstance(data, list):
            return data
        return []

    def episodes(self, series_id: str) -> list[dict]:
        items: list[dict] = []
        start = 0
        while True:
            page = self._get(
                f"/Shows/{series_id}/Episodes",
                {
                    "Fields": "Path,IndexNumber,ParentIndexNumber",
                    "StartIndex": str(start),
                    "Limit": "200",
                },
            )
            if not isinstance(page, dict):
                break
            chunk = page.get("Items") or []
            items.extend(chunk)
            start += len(chunk)
            total = int(page.get("TotalRecordCount") or 0)
            if not chunk or start >= total:
                break
        return items

    def remote_search(self, kind: str, name: str, year: int | None, item_id: str) -> list[dict]:
        info: dict = {"Name": name}
        if year:
            info["Year"] = int(year)
        path = "/Items/RemoteSearch/Series" if kind == "series" else "/Items/RemoteSearch/Movie"
        data = self._request("POST", path, json={"SearchInfo": info, "ItemId": item_id}, timeout=90).json()
        if isinstance(data, list):
            return data
        return []

    def apply_remote(self, item_id: str, result: dict) -> None:
        self._request(
            "POST",
            f"/Items/RemoteSearch/Apply/{item_id}",
            params={"replaceAllImages": "true"},
            json=result,
            timeout=180,
        )

    def refresh(self, item_id: str, images: bool = True) -> None:
        self._request(
            "POST",
            f"/Items/{item_id}/Refresh",
            params={
                "Recursive": "true",
                "MetadataRefreshMode": "FullRefresh",
                "ImageRefreshMode": "FullRefresh",
                "ReplaceAllMetadata": "true",
                "ReplaceAllImages": "true" if images else "false",
            },
            timeout=180,
        )

    def remote_posters(self, item_id: str) -> list[dict]:
        data = self._get(
            f"/Items/{item_id}/RemoteImages",
            {"type": "Primary", "limit": "40", "includeAllLanguages": "false"},
        )
        images = data.get("Images") if isinstance(data, dict) else None
        return [image for image in images or [] if isinstance(image, dict)]

    def unlock(self, item_id: str) -> None:
        item = self.item_record(item_id)
        if not item.get("LockData"):
            return
        item["LockData"] = False
        self._request("POST", f"/Items/{item_id}", json=item, timeout=60)

    def write_fields(self, item_id: str, values: dict) -> None:
        item = self.item_record(item_id)
        if "name" in values and values["name"]:
            item["Name"] = values["name"]
        if "year" in values:
            item["ProductionYear"] = int(values["year"]) if values["year"] else None
        if "original" in values:
            item["OriginalTitle"] = values["original"] or ""
        if "overview" in values:
            item["Overview"] = values["overview"] or ""
        if values.get("ids"):
            ids = dict(item.get("ProviderIds") or {})
            for key, value in values["ids"].items():
                if value:
                    ids[key] = str(value)
                else:
                    ids.pop(key, None)
            item["ProviderIds"] = ids
        self._request("POST", f"/Items/{item_id}", json=item, timeout=60)

    def download_poster(self, item_id: str, url: str) -> None:
        self._request(
            "POST",
            f"/Items/{item_id}/RemoteImages/Download",
            params={"type": "Primary", "imageUrl": url},
            timeout=120,
        )

    def set_episode_index(self, item_id: str, season: int, episode: int) -> None:
        item = self.item_record(item_id)
        item["ParentIndexNumber"] = season
        item["IndexNumber"] = episode
        self._request("POST", f"/Items/{item_id}", json=item, timeout=60)


def candidate_from_jellyfin(item: dict) -> Candidate:
    year = item.get("ProductionYear")
    if not year and item.get("PremiereDate"):
        try:
            year = int(str(item["PremiereDate"])[:4])
        except ValueError:
            year = None
    return Candidate(
        name=item.get("Name") or "",
        original_name=item.get("OriginalTitle") or "",
        year=int(year) if year else None,
        provider_ids=item.get("ProviderIds") or {},
        overview=item.get("Overview") or "",
        source="jellyfin",
        poster=(item.get("ImageUrl") or "").strip(),
        raw=item,
    )
