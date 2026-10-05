from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

MAX_POSTER_BYTES = 8_000_000


def _blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def poster_url(value: str | None) -> str:
    text = (value or "").strip()
    if text.startswith("http://image.tmdb.org/"):
        text = "https://" + text[len("http://") :]
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".local"):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return text
    if _blocked(address):
        return ""
    return text


def _host_is_public(host: str) -> bool:
    if not poster_url(f"https://{host}/"):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    seen = False
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return False
        seen = True
        if _blocked(address):
            return False
    return seen


def tmdb_poster(path: str | None) -> str:
    if path and str(path).startswith("/") and ".." not in str(path):
        return f"https://image.tmdb.org/t/p/w500{path}"
    return ""


def fetch_poster(url: str) -> tuple[bytes, str]:
    safe = poster_url(url)
    host = urlparse(safe).hostname or ""
    if not safe or not _host_is_public(host):
        raise ValueError("Plakat-Adresse ungültig")
    with httpx.Client(timeout=20, follow_redirects=True) as http:
        response = http.get(safe)
    final_host = urlparse(str(response.url)).hostname or ""
    if poster_url(str(response.url)) == "" or not _host_is_public(final_host) or response.status_code >= 400:
        raise ValueError("Plakat nicht geladen")
    content_type = (response.headers.get("content-type") or "image/jpeg").split(";", 1)[0].strip().lower()
    if not content_type.startswith("image/"):
        raise ValueError("Antwort ist kein Bild")
    data = response.content
    if not data or len(data) > MAX_POSTER_BYTES:
        raise ValueError("Plakat zu groß oder leer")
    return data, content_type
