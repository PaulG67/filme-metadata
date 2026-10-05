from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    jellyfin_baseurl: str
    jellyfin_token: str
    jellyfin_username: str
    jellyfin_password: str
    tmdb_api_key: str
    opensubtitles_api_key: str
    opensubtitles_username: str
    opensubtitles_password: str
    whisper_model: str
    excerpt_seconds: int
    data_dir: Path
    port: int
    verify_tls: bool


def load_settings() -> Settings:
    raw_data = os.environ.get("DATA_DIR", "").strip()
    data_dir = Path(raw_data) if raw_data else Path(__file__).resolve().parents[1] / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        jellyfin_baseurl=os.environ.get("JELLYFIN_BASEURL", "http://172.17.0.1:8096").strip().rstrip("/"),
        jellyfin_token=os.environ.get("JELLYFIN_TOKEN", "").strip(),
        jellyfin_username=os.environ.get("JELLYFIN_USERNAME", "").strip(),
        jellyfin_password=os.environ.get("JELLYFIN_PASSWORD", ""),
        tmdb_api_key=os.environ.get("TMDB_API_KEY", "").strip(),
        opensubtitles_api_key=os.environ.get("OPENSUBTITLES_API_KEY", "").strip(),
        opensubtitles_username=os.environ.get("OPENSUBTITLES_USERNAME", "").strip(),
        opensubtitles_password=os.environ.get("OPENSUBTITLES_PASSWORD", ""),
        whisper_model=os.environ.get("WHISPER_MODEL", "small").strip() or "small",
        excerpt_seconds=max(8, min(45, int(os.environ.get("EXCERPT_SECONDS", "20") or "20"))),
        data_dir=data_dir,
        port=int(os.environ.get("PORT", "8792") or "8792"),
        verify_tls=os.environ.get("SSL_BYPASS", "false").strip().lower() not in {"1", "true", "yes"},
    )
