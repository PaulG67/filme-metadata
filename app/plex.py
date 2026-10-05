"""Plex-Bibliothek lesen und eine falsche Zuordnung per Match korrigieren.

Filme und Serien werden über den Ordnernamen verbunden, nicht über den angezeigten Titel.
Sonst fände Hotel Inhumans auf Jellyfin die Serie Marvel's Inhumans auf Plex nicht.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

import httpx

from app.identify import GENERIC_DIRS, jellyfin_provider_ids, norm_provider_ids, normalize
from app.jellyfin import clean_token

PLEX_HEADERS = {
    "Accept": "application/xml",
    "X-Plex-Product": "filme-metadata",
    "X-Plex-Version": "1.0.0",
    "X-Plex-Client-Identifier": "filme-metadata",
    "X-Plex-Platform": "Linux",
    "X-Plex-Device": "Docker",
    "X-Plex-Device-Name": "filme-metadata",
}

_SEASON = re.compile(r"(?i)^(season|staffel)\s*\d+$|^s\d{1,2}$")
_VIDEO = re.compile(r"(?i)\.(mkv|mp4|avi|m4v|ts|wmv|mov|mpg|mpeg|iso)$")


class PlexError(RuntimeError):
    pass


def path_key(path: str) -> str:
    text = (path or "").replace("\\", "/").lower()
    while "//" in text:
        text = text.replace("//", "/")
    return text.rstrip("/")


def folder_keys(path: str) -> set[str]:
    parts = [part for part in path_key(path).split("/") if part]
    if parts and _VIDEO.search(parts[-1]):
        parts = parts[:-1]
    while parts and (_SEASON.match(parts[-1]) or parts[-1] in GENERIC_DIRS):
        parts = parts[:-1]
    if not parts:
        return set()
    return {parts[-1]}


def direct_guid(ids: dict | None) -> str | None:
    normalized = norm_provider_ids(ids)
    if normalized.get("imdb"):
        return f"imdb://{normalized['imdb']}"
    if normalized.get("tmdb"):
        return f"tmdb://{normalized['tmdb']}"
    if normalized.get("tvdb"):
        return f"tvdb://{normalized['tvdb']}"
    return None


def pick_match_guid(results: list[str], ids: dict | None) -> str | None:
    wanted = [value for value in norm_provider_ids(ids).values() if value]
    for guid in results:
        lowered = guid.lower()
        if any(value in lowered for value in wanted):
            return guid
    return direct_guid(ids)


class PlexIndex:
    def __init__(self, items: list[dict]) -> None:
        self.items = items

    def match(self, *, kind: str, path: str, folder_title: str, folder_year: int | None) -> dict | None:
        keys = folder_keys(path)
        by_folder = [item for item in self.items if item["kind"] == kind and keys and (keys & item["folders"])]
        chosen = _single(by_folder)
        if chosen:
            return chosen
        titled = []
        for item in self.items:
            if item["kind"] != kind:
                continue
            if folder_year and item.get("year") and int(item["year"]) != int(folder_year):
                continue
            if normalize(item.get("name") or "") == normalize(folder_title):
                titled.append(item)
        return _single(titled)


class PlexClient:
    def __init__(self, baseurl: str, token: str, verify: bool) -> None:
        self.baseurl = baseurl.rstrip("/")
        self.token = clean_token(token)
        self.http = httpx.Client(timeout=120, verify=verify, follow_redirects=True)

    def close(self) -> None:
        self.http.close()

    def _headers(self) -> dict[str, str]:
        headers = dict(PLEX_HEADERS)
        if self.token:
            headers["X-Plex-Token"] = self.token
        return headers

    def _request(self, method: str, path: str, params: dict | None = None) -> httpx.Response:
        try:
            response = self.http.request(method, f"{self.baseurl}{path}", params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise PlexError(f"Plex nicht erreichbar unter {self.baseurl}") from exc
        if response.status_code == 401:
            raise PlexError("Plex lehnt den Token ab (401). X-Plex-Token aus den Plex-Einstellungen eintragen.")
        if response.status_code >= 400:
            detail = response.text[:200].replace("\n", " ")
            raise PlexError(f"Plex {method} {path} -> {response.status_code} {detail}")
        return response

    def server_name(self) -> str:
        root = ET.fromstring(self._request("GET", "/").text)
        name = root.attrib.get("friendlyName") or "Plex"
        version = root.attrib.get("version") or ""
        return f"{name} {version}".strip()

    def library(self) -> PlexIndex:
        root = ET.fromstring(self._request("GET", "/library/sections").text)
        items: list[dict] = []
        for directory in root.findall("Directory"):
            library_type = directory.attrib.get("type")
            key = directory.attrib.get("key")
            if library_type not in {"movie", "show"} or not key:
                continue
            kind = "movie" if library_type == "movie" else "series"
            container_type = "1" if kind == "movie" else "2"
            section = ET.fromstring(
                self._request(
                    "GET",
                    f"/library/sections/{key}/all",
                    {"type": container_type, "includeGuids": "1"},
                ).text
            )
            nodes = section.findall("Video" if kind == "movie" else "Directory")
            paths = self._show_paths(key) if kind == "series" else {}
            for node in nodes:
                item = _item_from_node(node, kind, paths)
                if item:
                    items.append(item)
        return PlexIndex(items)

    def _show_paths(self, section_key: str) -> dict[str, str]:
        try:
            root = ET.fromstring(
                self._request("GET", f"/library/sections/{section_key}/all", {"type": "4"}).text
            )
        except PlexError:
            return {}
        paths: dict[str, str] = {}
        for node in root.findall("Video"):
            parent = node.attrib.get("grandparentRatingKey")
            if not parent or parent in paths:
                continue
            part = node.find(".//Part")
            if part is not None and part.attrib.get("file"):
                paths[parent] = part.attrib["file"]
        return paths

    def search_guids(self, rating_key: str, name: str, year: int | None) -> list[str]:
        params = {"manual": "1", "title": name or ""}
        if year:
            params["year"] = str(year)
        try:
            root = ET.fromstring(self._request("GET", f"/library/metadata/{rating_key}/matches", params).text)
        except PlexError:
            return []
        guids = []
        for node in root.iter():
            guid = node.attrib.get("guid")
            if guid and guid not in guids:
                guids.append(guid)
        return guids

    def match(self, rating_key: str, name: str, year: int | None, ids: dict) -> None:
        guid = pick_match_guid(self.search_guids(rating_key, name, year), ids)
        if not guid:
            raise PlexError("Keine IMDb-, TMDB- oder TVDB-ID zum Übernehmen auf Plex")
        params = {"guid": guid, "name": name or ""}
        if year:
            params["year"] = str(year)
        self._request("PUT", f"/library/metadata/{rating_key}/match", params)
        try:
            self._request("GET", f"/library/metadata/{rating_key}/refresh")
        except PlexError:
            return


def _single(items: list[dict]) -> dict | None:
    if len(items) == 1:
        return items[0]
    return None


def _guid_map(node: ET.Element) -> dict[str, str]:
    raw: dict[str, str] = {}
    for guid in list(node.findall("Guid")) + list(node.findall("guid")):
        gid = guid.attrib.get("id") or ""
        if "://" not in gid:
            continue
        provider, value = gid.split("://", 1)
        provider = provider.lower()
        if "imdb" in provider:
            raw["imdb"] = value.split("?")[0]
        elif "tmdb" in provider or "themoviedb" in provider:
            raw["tmdb"] = value.split("?")[0]
        elif "tvdb" in provider or "thetvdb" in provider:
            raw["tvdb"] = value.split("?")[0]
    return jellyfin_provider_ids(norm_provider_ids(raw))


def _item_from_node(node: ET.Element, kind: str, show_paths: dict[str, str]) -> dict | None:
    rating_key = node.attrib.get("ratingKey")
    if not rating_key:
        return None
    paths = []
    for location in node.findall("Location"):
        if location.attrib.get("path"):
            paths.append(location.attrib["path"])
    part = node.find(".//Part")
    if part is not None and part.attrib.get("file"):
        paths.append(part.attrib["file"])
    if show_paths.get(rating_key):
        paths.append(show_paths[rating_key])
    folders: set[str] = set()
    for path in paths:
        folders |= folder_keys(path)
    year_text = node.attrib.get("year")
    try:
        year = int(year_text) if year_text else None
    except ValueError:
        year = None
    return {
        "item_id": rating_key,
        "kind": kind,
        "name": node.attrib.get("title") or "",
        "original": (node.attrib.get("originalTitle") or "").strip(),
        "year": year,
        "ids": _guid_map(node),
        "overview": (node.attrib.get("summary") or "").strip()[:400],
        "path": paths[0] if paths else "",
        "folders": folders,
    }
