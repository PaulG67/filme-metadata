from __future__ import annotations

import time

import httpx

from app.identify import Candidate

AUTH_HEADER = (
    'MediaBrowser Client="filme-metadata", Device="unraid", DeviceId="filme-metadata", Version="1.0.0"'
)


class JellyfinError(RuntimeError):
    pass


class JellyfinClient:
    def __init__(self, baseurl: str, token: str, verify: bool) -> None:
        self.baseurl = baseurl.rstrip("/")
        self.token = token
        self.headers = {
            "Authorization": AUTH_HEADER,
            "X-Emby-Authorization": AUTH_HEADER,
            "Accept": "application/json",
        }
        if token:
            self.headers["X-Emby-Token"] = token
            self.headers["X-Emby-Authorization"] = f'{AUTH_HEADER}, Token="{token}"'
        self.http = httpx.Client(timeout=60, verify=verify, follow_redirects=True)

    @classmethod
    def connect(cls, baseurl: str, token: str, username: str, password: str, verify: bool) -> JellyfinClient:
        if not baseurl:
            raise JellyfinError("JELLYFIN_BASEURL fehlt")
        if token:
            return cls(baseurl, token, verify)
        if not username:
            raise JellyfinError("Jellyfin API-Key oder Benutzername fehlt")
        access = cls.login(baseurl, username, password, verify)
        return cls(baseurl, access, verify)

    @staticmethod
    def login(baseurl: str, username: str, password: str, verify: bool) -> str:
        headers = {
            "Authorization": AUTH_HEADER,
            "X-Emby-Authorization": AUTH_HEADER,
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
        access = response.json().get("AccessToken")
        if not access:
            raise JellyfinError("Jellyfin-Login ohne AccessToken")
        return access

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
                    "Fields": "ProviderIds,Path,ProductionYear,OriginalTitle,PremiereDate",
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

    def media_item(self, item_id: str) -> dict:
        data = self._get(
            f"/Items/{item_id}",
            {"Fields": "MediaSources,RunTimeTicks,Path,ProviderIds,IndexNumber,ParentIndexNumber,ProductionYear"},
        )
        if not isinstance(data, dict) or not data.get("Id"):
            raise JellyfinError("Eintrag nicht gefunden")
        return data

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

    def refresh(self, item_id: str) -> None:
        self._request(
            "POST",
            f"/Items/{item_id}/Refresh",
            params={
                "Recursive": "true",
                "MetadataRefreshMode": "FullRefresh",
                "ImageRefreshMode": "FullRefresh",
                "ReplaceAllMetadata": "true",
                "ReplaceAllImages": "true",
            },
            timeout=180,
        )

    def unlock(self, item_id: str) -> None:
        item = self._get(f"/Items/{item_id}")
        if not isinstance(item, dict) or not item.get("LockData"):
            return
        item["LockData"] = False
        self._request("POST", f"/Items/{item_id}", json=item, timeout=60)

    def set_episode_index(self, item_id: str, season: int, episode: int) -> None:
        item = self._get(f"/Items/{item_id}")
        if not isinstance(item, dict):
            raise JellyfinError("Folge nicht gefunden")
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
        raw=item,
    )
