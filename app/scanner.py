from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from app.config import Settings, load_settings
from app.identify import (
    build_finding,
    merge_candidates,
    needs_review,
    parse_episode_index,
    parse_movie_path,
    parse_series_path,
    rank_candidates,
)
from app.jellyfin import JellyfinClient, JellyfinError, candidate_from_jellyfin
from app.tmdb import TmdbClient

log = logging.getLogger("filme-metadata")


class Scanner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = {
            "running": False,
            "progress": 0,
            "total": 0,
            "message": "Noch kein Scan",
            "error": None,
            "finished_at": None,
            "server_name": None,
            "stats": {},
            "findings": [],
            "ignored": [],
        }
        self._load()

    def status(self) -> dict:
        settings = load_settings()
        with self._lock:
            state = {
                "running": self._state["running"],
                "progress": self._state["progress"],
                "total": self._state["total"],
                "message": self._state["message"],
                "error": self._state["error"],
                "finished_at": self._state["finished_at"],
                "server_name": self._state["server_name"],
                "stats": dict(self._state["stats"]),
                "findings": [_public_finding(item) for item in self._state["findings"]],
                "ignored_count": len(self._state["ignored"]),
            }
        state["config"] = {
            "jellyfin_baseurl": settings.jellyfin_baseurl,
            "tmdb": bool(settings.tmdb_api_key),
            "has_token": bool(settings.jellyfin_token),
            "has_user": bool(settings.jellyfin_username),
        }
        return state

    def test_connection(self) -> dict:
        settings = load_settings()
        client = JellyfinClient.connect(
            settings.jellyfin_baseurl,
            settings.jellyfin_token,
            settings.jellyfin_username,
            settings.jellyfin_password,
            settings.verify_tls,
        )
        name = client.server_name()
        with self._lock:
            self._state["server_name"] = name
        return {"ok": True, "server_name": name}

    def start(self) -> None:
        with self._lock:
            if self._state["running"]:
                raise RuntimeError("Scan läuft bereits")
            self._state["running"] = True
            self._state["error"] = None
            self._state["progress"] = 0
            self._state["message"] = "Verbinde mit Jellyfin"
        thread = threading.Thread(target=self._run, name="scan", daemon=True)
        thread.start()

    def apply(self, item_id: str, candidate_key: str) -> dict:
        with self._lock:
            if self._state["running"]:
                raise RuntimeError("Scan läuft noch")
            finding = next((item for item in self._state["findings"] if item["item_id"] == item_id), None)
            if finding is None:
                raise KeyError("Eintrag nicht gefunden")
            candidate = next((item for item in finding["candidates"] if item["key"] == candidate_key), None)
            if candidate is None:
                raise KeyError("Treffer nicht gefunden")
            kind = finding["kind"]
            body = _apply_body(candidate)
        settings = load_settings()
        client = _client(settings)
        client.unlock(item_id)
        client.apply_remote(item_id, body)
        if kind == "series":
            client.refresh(item_id)
        with self._lock:
            self._state["findings"] = [item for item in self._state["findings"] if item["item_id"] != item_id]
            self._remember_stats()
            self._save()
        return {"ok": True, "name": candidate["name"]}

    def apply_sure(self) -> dict:
        with self._lock:
            queued = [
                (item["item_id"], item["candidates"][0]["key"])
                for item in self._state["findings"]
                if item["status"] == "sure" and item["candidates"] and item["candidates"][0]["auto"]
            ]
        done = []
        errors = []
        for item_id, key in queued:
            try:
                result = self.apply(item_id, key)
                done.append(result["name"])
            except Exception as exc:
                log.exception("Korrektur fehlgeschlagen")
                errors.append(str(exc))
        return {"ok": not errors, "applied": done, "errors": errors}

    def fix_episode(self, episode_id: str) -> dict:
        with self._lock:
            issue = None
            for finding in self._state["findings"]:
                for item in finding.get("episode_issues") or []:
                    if item["item_id"] == episode_id:
                        issue = item
                        break
            if issue is None:
                raise KeyError("Folge nicht gefunden")
            season = int(issue["expected_season"])
            episode = int(issue["expected_episode"])
        client = _client(load_settings())
        client.set_episode_index(episode_id, season, episode)
        with self._lock:
            for finding in self._state["findings"]:
                finding["episode_issues"] = [
                    item for item in finding.get("episode_issues") or [] if item["item_id"] != episode_id
                ]
            self._save()
        return {"ok": True}

    def ignore(self, item_id: str) -> None:
        with self._lock:
            if item_id not in self._state["ignored"]:
                self._state["ignored"].append(item_id)
            self._state["findings"] = [item for item in self._state["findings"] if item["item_id"] != item_id]
            self._remember_stats()
            self._save()

    def _run(self) -> None:
        try:
            settings = load_settings()
            client = _client(settings)
            server = client.server_name()
            tmdb = TmdbClient(settings.tmdb_api_key) if settings.tmdb_api_key else None
            with self._lock:
                self._state["server_name"] = server
                self._state["message"] = "Lade Bibliothek"
            media = client.list_media()
            findings = []
            ok = skipped = 0
            errors: list[str] = []
            ignored = set(self._state["ignored"])
            total = len(media)
            for index, item in enumerate(media, start=1):
                name = item.get("Name") or item.get("Id") or ""
                with self._lock:
                    self._state["progress"] = index
                    self._state["total"] = total
                    self._state["message"] = f"{index}/{total} {name}"
                if item.get("Id") in ignored:
                    continue
                try:
                    finding, outcome = self._inspect(client, tmdb, item)
                except Exception as exc:
                    log.exception("Eintrag fehlgeschlagen: %s", name)
                    errors.append(f"{name}: {exc}")
                    continue
                if outcome == "skip":
                    skipped += 1
                elif finding is None:
                    ok += 1
                else:
                    findings.append(finding)
            findings.sort(key=lambda item: (item["status"] != "sure", -_top_score(item), item["folder_title"].lower()))
            finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with self._lock:
                self._state["findings"] = findings
                self._state["finished_at"] = finished
                self._state["message"] = "Scan fertig"
                self._state["stats"] = {
                    "checked": total,
                    "ok": ok,
                    "skipped": skipped,
                    "findings": len(findings),
                    "sure": sum(1 for item in findings if item["status"] == "sure"),
                    "errors": errors[:12],
                }
                self._save()
        except JellyfinError as exc:
            log.error("Scan fehlgeschlagen: %s", exc)
            with self._lock:
                self._state["error"] = str(exc)
                self._state["message"] = "Scan fehlgeschlagen"
        except Exception as exc:
            log.exception("Scan fehlgeschlagen")
            with self._lock:
                self._state["error"] = str(exc)
                self._state["message"] = "Scan fehlgeschlagen"
        finally:
            with self._lock:
                self._state["running"] = False
                self._save()

    def _inspect(self, client: JellyfinClient, tmdb: TmdbClient | None, item: dict) -> tuple[dict | None, str]:
        path = item.get("Path") or ""
        if not path:
            return None, "skip"
        kind = "series" if item.get("Type") == "Series" else "movie"
        if kind == "series":
            title, year, embedded = parse_series_path(path)
        else:
            title, year, embedded = parse_movie_path(path)
        if not title:
            return None, "skip"
        jellyfin_year = _item_year(item)
        jellyfin_ids = item.get("ProviderIds") or {}
        extra_names = [item.get("OriginalTitle") or ""]
        if needs_review(title, year, item.get("Name") or "", jellyfin_year, jellyfin_ids, embedded, extra_names) is None:
            return None, "ok"
        episodes: list[tuple[int, int]] = []
        issues: list[dict] = []
        if kind == "series":
            episodes, issues = _episode_facts(client.episodes(item["Id"]))
        candidates = _search(client, tmdb, kind, title, year, item["Id"])
        finding = build_finding(
            folder_title=title,
            folder_year=year,
            jellyfin_name=item.get("Name") or "",
            jellyfin_year=jellyfin_year,
            jellyfin_ids=jellyfin_ids,
            candidates=candidates,
            episodes=episodes,
            embedded_ids=embedded,
            extra_names=extra_names,
            episode_issues=issues[:40],
        )
        if finding is None:
            return None, "ok"
        finding["item_id"] = item["Id"]
        finding["kind"] = kind
        finding["path"] = path
        finding["episode_count"] = len(episodes)
        return finding, "finding"

    def _remember_stats(self) -> None:
        findings = self._state["findings"]
        stats = dict(self._state["stats"])
        stats["findings"] = len(findings)
        stats["sure"] = sum(1 for item in findings if item["status"] == "sure")
        self._state["stats"] = stats

    def _load(self) -> None:
        path = load_settings().data_dir / "state.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self._state["findings"] = data.get("findings") or []
        self._state["ignored"] = data.get("ignored") or []
        self._state["stats"] = data.get("stats") or {}
        self._state["finished_at"] = data.get("finished_at")
        self._state["server_name"] = data.get("server_name")
        self._state["message"] = "Letzter Scan geladen" if self._state["findings"] or self._state["finished_at"] else "Noch kein Scan"

    def _save(self) -> None:
        path = load_settings().data_dir / "state.json"
        payload = {
            "findings": self._state["findings"],
            "ignored": self._state["ignored"],
            "stats": self._state["stats"],
            "finished_at": self._state["finished_at"],
            "server_name": self._state["server_name"],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _client(settings: Settings) -> JellyfinClient:
    return JellyfinClient.connect(
        settings.jellyfin_baseurl,
        settings.jellyfin_token,
        settings.jellyfin_username,
        settings.jellyfin_password,
        settings.verify_tls,
    )


def _item_year(item: dict) -> int | None:
    year = item.get("ProductionYear")
    if year:
        return int(year)
    premiere = item.get("PremiereDate")
    if not premiere:
        return None
    try:
        return int(str(premiere)[:4])
    except ValueError:
        return None


def _episode_facts(episodes: list[dict]) -> tuple[list[tuple[int, int]], list[dict]]:
    parsed: list[tuple[int, int]] = []
    issues: list[dict] = []
    for episode in episodes:
        index = parse_episode_index(episode.get("Path") or "")
        if index is None:
            continue
        season, number = index
        parsed.append(index)
        jellyfin_season = episode.get("ParentIndexNumber")
        jellyfin_episode = episode.get("IndexNumber")
        if jellyfin_season != season or jellyfin_episode != number:
            issues.append(
                {
                    "item_id": episode.get("Id"),
                    "name": episode.get("Name") or "",
                    "path": episode.get("Path") or "",
                    "expected_season": season,
                    "expected_episode": number,
                    "jellyfin_season": jellyfin_season,
                    "jellyfin_episode": jellyfin_episode,
                }
            )
    return parsed, issues


def _search(client: JellyfinClient, tmdb: TmdbClient | None, kind: str, title: str, year: int | None, item_id: str) -> list:
    found = []
    years = [year, None] if year else [None]
    seen_years: set[int | None] = set()
    for search_year in years:
        if search_year in seen_years:
            continue
        seen_years.add(search_year)
        try:
            for raw in client.remote_search(kind, title, search_year, item_id):
                found.append(candidate_from_jellyfin(raw))
        except JellyfinError as exc:
            log.warning("Remote-Suche %s: %s", title, exc)
    if tmdb is not None:
        try:
            found.extend(tmdb.search(kind, title, year))
        except Exception as exc:
            log.warning("TMDB-Suche %s: %s", title, exc)
    merged = merge_candidates(found)
    if tmdb is not None and merged:
        prelim = rank_candidates(title, year, None, merged)
        for candidate in prelim[:4]:
            try:
                tmdb.enrich(kind, candidate)
            except Exception as exc:
                log.warning("TMDB-Details %s: %s", candidate.name, exc)
        merged = merge_candidates(prelim)
    return merged


def _apply_body(candidate: dict) -> dict:
    raw = candidate.get("raw")
    if isinstance(raw, dict) and raw.get("ProviderIds"):
        body = dict(raw)
        body.pop("raw", None)
        return body
    body = {
        "Name": candidate.get("name") or "",
        "ProviderIds": candidate.get("provider_ids") or {},
    }
    if candidate.get("year"):
        body["ProductionYear"] = candidate["year"]
    if candidate.get("overview"):
        body["Overview"] = candidate["overview"]
    ids = candidate.get("provider_ids") or {}
    if ids.get("Tmdb"):
        body["SearchProviderName"] = "TheMovieDb"
    elif ids.get("Tvdb"):
        body["SearchProviderName"] = "TheTVDB"
    return body


def _top_score(finding: dict) -> float:
    candidates = finding.get("candidates") or []
    if not candidates:
        return 0.0
    return float(candidates[0].get("score") or 0)


def _public_finding(finding: dict) -> dict:
    return {
        **{key: value for key, value in finding.items() if key != "candidates"},
        "candidates": [{key: value for key, value in candidate.items() if key != "raw"} for candidate in finding.get("candidates") or []],
    }
