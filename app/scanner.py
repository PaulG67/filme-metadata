from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from app.config import Settings, load_settings
from app.identify import (
    build_finding,
    candidate_key,
    jellyfin_provider_ids,
    merge_candidates,
    metadata_signature,
    needs_review,
    norm_provider_ids,
    parse_episode_index,
    parse_movie_path,
    parse_series_path,
    parse_title_and_ids,
    is_generic_dir,
    rank_candidates,
    same_work,
    systems_differ,
)
from app.jellyfin import JellyfinClient, JellyfinError, candidate_from_jellyfin
from app.plex import PlexClient, PlexError, PlexIndex
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
            "accepted": {"jellyfin": {}, "plex": {}},
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
                "accepted_count": sum(len(bucket) for bucket in self._state["accepted"].values()),
            }
        state["config"] = {
            "jellyfin_baseurl": settings.jellyfin_baseurl,
            "plex_baseurl": settings.plex_baseurl,
            "plex": bool(settings.plex_token),
            "tmdb": bool(settings.tmdb_api_key),
            "excerpt": bool(settings.opensubtitles_api_key),
            "has_token": bool(settings.jellyfin_token),
            "has_user": bool(settings.jellyfin_username),
        }
        return state

    def test_connection(self) -> dict:
        settings = load_settings()
        names = []
        errors = []
        try:
            client = JellyfinClient.connect(
                settings.jellyfin_baseurl,
                settings.jellyfin_token,
                settings.jellyfin_username,
                settings.jellyfin_password,
                settings.verify_tls,
            )
            names.append(client.server_name())
        except (JellyfinError, OSError, RuntimeError) as exc:
            errors.append(str(exc))
        if settings.plex_token:
            plex = PlexClient(settings.plex_baseurl, settings.plex_token, settings.verify_tls)
            try:
                names.append(plex.server_name())
            except (PlexError, OSError, RuntimeError) as exc:
                errors.append(str(exc))
            finally:
                plex.close()
        if errors:
            raise RuntimeError("; ".join(errors))
        name = " · ".join(names) or "verbunden"
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

    def apply(self, item_id: str, candidate_key: str, targets: list[str] | None = None) -> dict:
        chosen = [target for target in (targets or ["jellyfin"]) if target in {"jellyfin", "plex"}]
        if not chosen:
            raise RuntimeError("Kein Ziel angegeben")
        with self._lock:
            if self._state["running"]:
                raise RuntimeError("Scan läuft noch")
            finding = next((item for item in self._state["findings"] if item["item_id"] == item_id), None)
            if finding is None:
                raise KeyError("Eintrag nicht gefunden")
            if candidate_key == "side:jellyfin":
                candidate = _side_candidate(finding, "jellyfin")
            elif candidate_key == "side:plex":
                candidate = _side_candidate(finding, "plex")
            else:
                candidate = next((item for item in finding["candidates"] if item["key"] == candidate_key), None)
            if candidate is None:
                raise KeyError("Treffer nicht gefunden")
            kind = finding["kind"]
            plex_id = (finding.get("plex") or {}).get("item_id")
            body = _apply_body(candidate)
            snapshot = dict(finding)
        settings = load_settings()
        if "jellyfin" in chosen:
            if snapshot.get("jellyfin_accepted"):
                raise RuntimeError("Jellyfin ist als stimmend markiert")
            client = _client(settings)
            client.unlock(item_id)
            client.apply_remote(item_id, body)
            if kind == "series":
                client.refresh(item_id)
            with self._lock:
                current = next((item for item in self._state["findings"] if item["item_id"] == item_id), None)
                if current is not None:
                    _remember_written(self._state["accepted"], current, candidate, ["jellyfin"])
                    if not still_open(current):
                        self._state["findings"] = [item for item in self._state["findings"] if item["item_id"] != item_id]
                self._remember_stats()
                self._save()
        if "plex" in chosen:
            if not plex_id:
                raise RuntimeError("Kein passender Plex-Eintrag")
            if (snapshot.get("plex") or {}).get("accepted"):
                raise RuntimeError("Plex ist als stimmend markiert")
            if not settings.plex_token:
                raise PlexError("PLEX_TOKEN fehlt")
            plex = PlexClient(settings.plex_baseurl, settings.plex_token, settings.verify_tls)
            try:
                plex.match(plex_id, candidate.get("name") or "", candidate.get("year"), candidate.get("provider_ids") or {})
            finally:
                plex.close()
            with self._lock:
                current = next((item for item in self._state["findings"] if item["item_id"] == item_id), None)
                if current is not None:
                    _remember_written(self._state["accepted"], current, candidate, ["plex"])
                    if not still_open(current):
                        self._state["findings"] = [item for item in self._state["findings"] if item["item_id"] != item_id]
                self._remember_stats()
                self._save()
        return {"ok": True, "name": candidate["name"], "targets": chosen}

    def accept(self, item_id: str, system: str) -> dict:
        systems = ["jellyfin", "plex"] if system == "both" else [system]
        if any(item not in {"jellyfin", "plex"} for item in systems):
            raise RuntimeError("Unbekanntes System")
        with self._lock:
            finding = next((item for item in self._state["findings"] if item["item_id"] == item_id), None)
            if finding is None:
                raise KeyError("Eintrag nicht gefunden")
            if "plex" in systems and not finding.get("plex"):
                raise RuntimeError("Kein passender Plex-Eintrag")
            for target in systems:
                _mark_accepted(self._state["accepted"], finding, target)
            if not still_open(finding):
                self._state["findings"] = [item for item in self._state["findings"] if item["item_id"] != item_id]
            self._remember_stats()
            self._save()
        return {"ok": True}

    def apply_sure(self) -> dict:
        with self._lock:
            queued = []
            for item in self._state["findings"]:
                if item["status"] != "sure" or not item.get("candidates") or not item["candidates"][0].get("auto"):
                    continue
                candidate = item["candidates"][0]
                targets = []
                if not item.get("jellyfin_accepted"):
                    targets.append("jellyfin")
                plex = item.get("plex") or {}
                if plex and not plex.get("accepted") and not same_work(candidate.get("provider_ids"), plex.get("ids")):
                    targets.append("plex")
                if targets:
                    queued.append((item["item_id"], candidate["key"], targets))
        done = []
        errors = []
        for item_id, key, targets in queued:
            try:
                result = self.apply(item_id, key, targets)
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

    def search_library(self, term: str) -> list[dict]:
        client = _client(load_settings())
        return [library_hit(item) for item in client.search_items(term)]

    def focus(self, item_id: str) -> dict:
        settings = load_settings()
        client = _client(settings)
        item = client.media_item(item_id)
        summary = library_hit(item)
        if item.get("Type") == "Episode":
            return {"ok": True, "clean": episode_mismatch(item) is None, "item": summary, "issue": episode_mismatch(item)}
        tmdb = TmdbClient(settings.tmdb_api_key) if settings.tmdb_api_key else None
        finding, _outcome = self._inspect(client, tmdb, item, None)
        if finding is None:
            return {"ok": True, "clean": True, "item": summary, "issue": None}
        with self._lock:
            rest = [entry for entry in self._state["findings"] if entry["item_id"] != finding["item_id"]]
            self._state["findings"] = [finding, *rest]
            self._remember_stats()
            self._save()
        return {"ok": True, "clean": False, "item": summary, "issue": None}

    def preview(self, item_id: str) -> dict:
        from app.listen import playback_source, save_clip

        settings, client, item, target_id, kind_hint, playable = self._playback(item_id)
        source = playback_source(client, item, kind_hint, playable)
        return {"ok": True, **save_clip(client, settings, target_id, source)}

    def listen(self, item_id: str) -> dict:
        from app.excerpt import ExcerptError
        from app.listen import recognize

        settings, client, item, target_id, kind_hint, playable = self._playback(item_id)
        with self._lock:
            if self._state["running"]:
                raise RuntimeError("Scan läuft noch")
            finding = next((entry for entry in self._state["findings"] if entry["item_id"] == target_id), None)
            candidates = [dict(entry) for entry in (finding or {}).get("candidates") or []]
            for candidate in candidates:
                candidate.pop("raw", None)
        if not candidates:
            candidates = self._listen_candidates(client, settings, item)
        try:
            payload = recognize(client, settings, target_id, candidates, kind_hint, playable)
        except ExcerptError:
            raise
        with self._lock:
            self._store_listen(target_id, payload)
            self._remember_stats()
            self._save()
        return {
            "ok": True,
            "message": payload["message"],
            "clip_id": payload.get("clip_id"),
            "clip_label": payload.get("clip_label"),
            "clip_offset": payload.get("clip_offset"),
        }

    def _playback(self, item_id: str):
        settings = load_settings()
        client = _client(settings)
        item = client.media_item(item_id)
        target_id = item_id
        kind_hint = "movie"
        playable = None
        if item.get("Type") == "Episode":
            target_id = str(item.get("SeriesId") or "")
            if not target_id:
                raise RuntimeError("Die Folge ist keiner Serie zugeordnet")
            kind_hint = "series"
            playable = item
        elif item.get("Type") == "Series":
            kind_hint = "series"
        return settings, client, item, target_id, kind_hint, playable

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
            plex_index: PlexIndex | None = None
            if settings.plex_token:
                plex = PlexClient(settings.plex_baseurl, settings.plex_token, settings.verify_tls)
                try:
                    with self._lock:
                        self._state["message"] = "Lade Plex"
                    plex_index = plex.library()
                except PlexError as exc:
                    errors.append(str(exc))
                finally:
                    plex.close()
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
                    finding, outcome = self._inspect(client, tmdb, item, plex_index)
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

    def _inspect(
        self,
        client: JellyfinClient,
        tmdb: TmdbClient | None,
        item: dict,
        plex_index: PlexIndex | None,
    ) -> tuple[dict | None, str]:
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
        jellyfin_name = item.get("Name") or ""
        jelly_signature = metadata_signature(jellyfin_name, jellyfin_year, jellyfin_ids)
        jelly_locked = self._is_accepted("jellyfin", item["Id"], jelly_signature)
        plex_item = None
        if plex_index is not None:
            plex_item = plex_index.match(kind=kind, path=path, folder_title=title, folder_year=year)
        plex_locked = False
        if plex_item is not None:
            plex_locked = self._is_accepted(
                "plex",
                plex_item["item_id"],
                metadata_signature(plex_item["name"], plex_item.get("year"), plex_item.get("ids")),
            )
        reason = None if jelly_locked else needs_review(
            title, year, jellyfin_name, jellyfin_year, jellyfin_ids, embedded, extra_names
        )
        finding = None
        if reason:
            episodes: list[tuple[int, int]] = []
            issues: list[dict] = []
            if kind == "series":
                episodes, issues = _episode_facts(client.episodes(item["Id"]))
            candidates = _search(client, tmdb, kind, title, year, item["Id"])
            finding = build_finding(
                folder_title=title,
                folder_year=year,
                jellyfin_name=jellyfin_name,
                jellyfin_year=jellyfin_year,
                jellyfin_ids=jellyfin_ids,
                candidates=candidates,
                episodes=episodes,
                embedded_ids=embedded,
                extra_names=extra_names,
                episode_issues=issues[:40],
            )
            if finding is not None:
                finding["item_id"] = item["Id"]
                finding["kind"] = kind
                finding["path"] = path
                finding["episode_count"] = len(episodes)
                finding["jellyfin_overview"] = (item.get("Overview") or "").strip()[:400]
                finding["jellyfin_original"] = (item.get("OriginalTitle") or "").strip()
                finding["jellyfin_accepted"] = False
        plex_public = _public_plex(plex_item) if plex_item else None
        if plex_public is not None:
            plex_public["accepted"] = plex_locked
        differ = None
        if plex_item is not None and not plex_locked:
            differ = systems_differ(
                jellyfin_name,
                jellyfin_year,
                jellyfin_ids,
                plex_item["name"],
                plex_item.get("year"),
                plex_item.get("ids"),
            )
        if finding is None:
            if not differ:
                return None, "ok"
            finding = _split_finding(
                item_id=item["Id"],
                kind=kind,
                path=path,
                folder_title=title,
                folder_year=year,
                jellyfin_name=jellyfin_name,
                jellyfin_year=jellyfin_year,
                jellyfin_ids=jellyfin_ids,
                jellyfin_original=(item.get("OriginalTitle") or "").strip(),
                jellyfin_overview=(item.get("Overview") or "").strip()[:400],
                jellyfin_accepted=jelly_locked,
                reason=differ,
            )
        elif differ:
            finding["reason"] = f"{finding['reason']}; {differ}"
        candidate_ids = ((finding.get("candidates") or [{}])[0] or {}).get("provider_ids")
        if plex_public is not None and (differ or not same_work(candidate_ids, plex_public.get("ids"))):
            finding["plex"] = plex_public
        if not still_open(finding):
            return None, "ok"
        return finding, "finding"

    def _listen_candidates(self, client: JellyfinClient, settings: Settings, item: dict) -> list[dict]:
        title, year = _listen_query(item)
        if not title:
            return []
        kind = "series" if item.get("Type") in {"Series", "Episode"} else "movie"
        tmdb = TmdbClient(settings.tmdb_api_key) if settings.tmdb_api_key else None
        search_id = str(item.get("SeriesId") or item.get("Id") or "")
        found = _search(client, tmdb, kind, title, year, search_id)
        public = []
        for candidate in found[:4]:
            public.append(
                {
                    "key": candidate_key(candidate.provider_ids, candidate.name, candidate.year),
                    "name": candidate.name,
                    "year": candidate.year,
                    "original_name": candidate.original_name,
                    "provider_ids": jellyfin_provider_ids(candidate.provider_ids),
                    "overview": (candidate.overview or "")[:400],
                    "auto": False,
                    "notes": [],
                    "score": candidate.score,
                }
            )
        return public

    def _is_accepted(self, system: str, item_id: str, signature: str) -> bool:
        return self._state["accepted"].get(system, {}).get(item_id) == signature

    def _store_listen(self, item_id: str, payload: dict) -> None:
        finding = next((item for item in self._state["findings"] if item["item_id"] == item_id), None)
        note = payload.get("note") or ""
        if payload.get("promote_key") and finding is not None:
            chosen = next((item for item in finding["candidates"] if item.get("key") == payload["promote_key"]), None)
            if chosen is None:
                raise KeyError("Treffer nicht mehr vorhanden")
            for item in finding["candidates"]:
                item["auto"] = False
            chosen["auto"] = True
            chosen["score"] = 1
            notes = [note] + [item for item in (chosen.get("notes") or []) if item != note]
            chosen["notes"] = notes
            rest = [item for item in finding["candidates"] if item is not chosen]
            finding["candidates"] = [chosen, *rest]
            finding["status"] = "sure"
            finding["reason"] = note
            _apply_excerpt(finding, payload)
            return
        candidate = payload.get("candidate")
        if not candidate:
            if finding is not None:
                _apply_excerpt(finding, payload)
            return
        if finding is None:
            created = {
                "item_id": item_id,
                "kind": payload.get("kind") or "movie",
                "path": payload.get("path") or "",
                "folder_title": candidate.get("name") or "",
                "folder_year": candidate.get("year"),
                "jellyfin_name": payload.get("jellyfin_name") or "",
                "jellyfin_year": payload.get("jellyfin_year"),
                "jellyfin_ids": {},
                "reason": note,
                "status": "sure",
                "candidates": [candidate],
                "episode_issues": [],
            }
            _apply_excerpt(created, payload)
            self._state["findings"].insert(0, created)
            return
        for item in finding["candidates"]:
            item["auto"] = False
        others = [item for item in finding["candidates"] if item.get("key") != candidate.get("key")]
        finding["candidates"] = [candidate, *others][:6]
        finding["status"] = "sure"
        finding["reason"] = note
        _apply_excerpt(finding, payload)

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
        accepted = data.get("accepted") or {}
        self._state["accepted"] = {
            "jellyfin": dict(accepted.get("jellyfin") or {}),
            "plex": dict(accepted.get("plex") or {}),
        }
        self._state["stats"] = data.get("stats") or {}
        self._state["finished_at"] = data.get("finished_at")
        self._state["server_name"] = data.get("server_name")
        self._state["message"] = "Letzter Scan geladen" if self._state["findings"] or self._state["finished_at"] else "Noch kein Scan"

    def _save(self) -> None:
        path = load_settings().data_dir / "state.json"
        payload = {
            "findings": self._state["findings"],
            "ignored": self._state["ignored"],
            "accepted": self._state["accepted"],
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


def _apply_excerpt(finding: dict, payload: dict) -> None:
    finding["excerpt_transcript"] = payload.get("transcript") or ""
    if payload.get("clip_id"):
        finding["excerpt_clip"] = True
        finding["excerpt_offset"] = payload.get("clip_offset")
        finding["excerpt_label"] = payload.get("clip_label") or ""


def library_hit(item: dict) -> dict:
    kind = {"Movie": "movie", "Series": "series", "Episode": "episode"}.get(item.get("Type") or "", "movie")
    return {
        "id": item.get("Id"),
        "name": item.get("Name") or "",
        "year": _item_year(item),
        "type": kind,
        "series": item.get("SeriesName") or "",
        "season": item.get("ParentIndexNumber"),
        "episode": item.get("IndexNumber"),
        "path": item.get("Path") or "",
    }


def episode_mismatch(item: dict) -> dict | None:
    expected = parse_episode_index(item.get("Path") or "")
    if expected is None:
        return None
    season, number = expected
    if item.get("ParentIndexNumber") == season and item.get("IndexNumber") == number:
        return None
    return {
        "item_id": item.get("Id"),
        "expected_season": season,
        "expected_episode": number,
        "jellyfin_season": item.get("ParentIndexNumber"),
        "jellyfin_episode": item.get("IndexNumber"),
    }


def _listen_query(item: dict) -> tuple[str, int | None]:
    from pathlib import PurePosixPath

    path = (item.get("Path") or "").replace("\\", "/")
    if item.get("Type") == "Episode":
        for parent in list(PurePosixPath(path).parents)[:4]:
            title, year, _ids = parse_title_and_ids(parent.name, allow_bare_year=False)
            if title and year and not is_generic_dir(title):
                return title, year
        return item.get("SeriesName") or "", None
    if item.get("Type") == "Series":
        title, year, _ids = parse_series_path(path)
        return title or item.get("Name") or "", year or _item_year(item)
    title, year, _ids = parse_movie_path(path)
    return title or item.get("Name") or "", year or _item_year(item)


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
    if ids.get("Tmdb") or ids.get("Imdb"):
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


def still_open(finding: dict) -> bool:
    plex = finding.get("plex")
    jelly_open = not finding.get("jellyfin_accepted")
    if not plex:
        return jelly_open
    plex_open = not plex.get("accepted")
    if not jelly_open and not plex_open:
        return False
    differ = systems_differ(
        finding.get("jellyfin_name") or "",
        finding.get("jellyfin_year"),
        finding.get("jellyfin_ids"),
        plex.get("name") or "",
        plex.get("year"),
        plex.get("ids"),
    )
    if differ is None:
        return False
    return jelly_open or plex_open


def _mark_accepted(accepted: dict, finding: dict, system: str) -> None:
    if system == "plex":
        plex = finding.get("plex") or {}
        if not plex.get("item_id"):
            raise RuntimeError("Kein passender Plex-Eintrag")
        accepted["plex"][plex["item_id"]] = metadata_signature(plex.get("name") or "", plex.get("year"), plex.get("ids"))
        plex["accepted"] = True
        return
    accepted["jellyfin"][finding["item_id"]] = metadata_signature(
        finding.get("jellyfin_name") or "",
        finding.get("jellyfin_year"),
        finding.get("jellyfin_ids"),
    )
    finding["jellyfin_accepted"] = True


def _remember_written(accepted: dict, finding: dict, candidate: dict, targets: list[str]) -> None:
    signature = metadata_signature(candidate.get("name") or "", candidate.get("year"), candidate.get("provider_ids"))
    ids = candidate.get("provider_ids") or {}
    if "jellyfin" in targets:
        accepted["jellyfin"][finding["item_id"]] = signature
        finding["jellyfin_accepted"] = True
        finding["jellyfin_name"] = candidate.get("name") or finding.get("jellyfin_name")
        finding["jellyfin_year"] = candidate.get("year")
        finding["jellyfin_ids"] = ids
        finding["jellyfin_overview"] = candidate.get("overview") or ""
        finding["jellyfin_original"] = candidate.get("original_name") or ""
    plex = finding.get("plex")
    if "plex" in targets and plex:
        accepted["plex"][plex["item_id"]] = signature
        plex["accepted"] = True
        plex["name"] = candidate.get("name") or plex.get("name")
        plex["year"] = candidate.get("year")
        plex["ids"] = ids
        plex["overview"] = candidate.get("overview") or ""
        plex["original"] = candidate.get("original_name") or ""


def _side_candidate(finding: dict, system: str) -> dict:
    if system == "plex":
        source = finding.get("plex") or {}
        if not source:
            raise KeyError("Kein Plex-Eintrag")
        name = source.get("name") or ""
        year = source.get("year")
        ids = source.get("ids") or {}
        original = source.get("original") or ""
        overview = source.get("overview") or ""
    else:
        name = finding.get("jellyfin_name") or ""
        year = finding.get("jellyfin_year")
        ids = finding.get("jellyfin_ids") or {}
        original = finding.get("jellyfin_original") or ""
        overview = finding.get("jellyfin_overview") or ""
    if not norm_provider_ids(ids):
        raise RuntimeError("Keine IMDb-, TMDB- oder TVDB-ID zum Übernehmen")
    return {
        "key": f"side:{system}",
        "name": name,
        "year": year,
        "original_name": original,
        "provider_ids": ids,
        "overview": overview,
        "auto": False,
        "raw": None,
    }


def _public_plex(item: dict) -> dict:
    return {
        "item_id": item["item_id"],
        "name": item.get("name") or "",
        "original": item.get("original") or "",
        "year": item.get("year"),
        "ids": item.get("ids") or {},
        "overview": item.get("overview") or "",
        "path": item.get("path") or "",
        "accepted": False,
    }


def _split_finding(
    *,
    item_id: str,
    kind: str,
    path: str,
    folder_title: str,
    folder_year: int | None,
    jellyfin_name: str,
    jellyfin_year: int | None,
    jellyfin_ids: dict,
    jellyfin_original: str,
    jellyfin_overview: str,
    jellyfin_accepted: bool,
    reason: str,
) -> dict:
    ids = jellyfin_provider_ids(norm_provider_ids(jellyfin_ids))
    candidates = []
    if ids:
        candidates.append(
            {
                "key": "side:jellyfin",
                "name": jellyfin_name,
                "year": jellyfin_year,
                "original_name": jellyfin_original,
                "provider_ids": ids,
                "overview": jellyfin_overview,
                "auto": False,
                "score": 1,
                "notes": ["Diese Jellyfin-Angaben nach Plex übernehmen"],
            }
        )
    return {
        "item_id": item_id,
        "kind": kind,
        "path": path,
        "folder_title": folder_title,
        "folder_year": folder_year,
        "jellyfin_name": jellyfin_name,
        "jellyfin_year": jellyfin_year,
        "jellyfin_ids": ids,
        "jellyfin_original": jellyfin_original,
        "jellyfin_overview": jellyfin_overview,
        "jellyfin_accepted": jellyfin_accepted,
        "reason": reason,
        "status": "split",
        "candidates": candidates,
        "episode_issues": [],
        "episode_count": 0,
    }
