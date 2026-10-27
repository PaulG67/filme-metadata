from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

from app.config import Settings, load_settings
from app.identify import (
    Candidate,
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
    is_collection_name,
    is_generic_dir,
    looks_like_collection,
    path_looks_like_collection,
    rank_candidates,
    same_work,
    systems_differ,
    _public_candidate,
)
from app.jellyfin import JellyfinClient, JellyfinError, candidate_from_jellyfin, has_primary_image, matches_compact
from app.plex import PlexClient, PlexError, PlexIndex
from app.posters import fetch_poster, poster_url
from app.tmdb import TmdbClient

log = logging.getLogger("filme-metadata")

LIBRARY_CACHE_SECONDS = 120
MAX_CHECK = 50
FIELD_KEYS = ["name", "year", "original", "tmdb", "imdb", "tvdb", "overview", "poster"]
ID_FIELDS = {"tmdb": "Tmdb", "imdb": "Imdb", "tvdb": "Tvdb"}


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
            "focus": None,
        }
        self._library_cache: tuple[float, list[dict]] | None = None
        self._load()

    def _lookup(self, item_id: str) -> dict | None:
        found = next((item for item in self._state["findings"] if item["item_id"] == item_id), None)
        if found is not None:
            return found
        focus = self._state.get("focus")
        if focus is not None and focus["item_id"] == item_id:
            return focus
        return None

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
                "focus": _public_finding(self._state["focus"]) if self._state.get("focus") else None,
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

    def apply(
        self,
        item_id: str,
        candidate_key: str,
        targets: list[str] | None = None,
        fields: list[str] | None = None,
        poster: str | None = None,
    ) -> dict:
        chosen = [target for target in (targets or ["jellyfin"]) if target in {"jellyfin", "plex"}]
        if not chosen:
            raise RuntimeError("Kein Ziel angegeben")
        picked = None
        if fields is not None and not candidate_key.startswith("side:"):
            picked = [field for field in FIELD_KEYS if field in fields]
            if not picked:
                raise RuntimeError("Nichts zum Übernehmen ausgewählt")
        chosen_poster = ""
        if poster:
            chosen_poster = poster_url(poster)
            if not chosen_poster:
                raise RuntimeError("Dieses Plakat lässt sich nicht laden")
            if picked is not None and "poster" not in picked:
                chosen_poster = ""
        with self._lock:
            if self._state["running"]:
                raise RuntimeError("Scan läuft noch")
            finding = self._lookup(item_id)
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
            written = candidate
            if picked is None:
                client.unlock(item_id)
                client.apply_remote(item_id, body)
                if kind == "series":
                    client.refresh(item_id, images=not chosen_poster)
                if chosen_poster:
                    client.download_poster(item_id, chosen_poster)
            else:
                url = ""
                if "poster" in picked:
                    url = chosen_poster or self._poster_for(client, settings, snapshot, candidate)
                client.unlock(item_id)
                changes = _field_changes(candidate, picked)
                if changes:
                    client.write_fields(item_id, changes)
                if url:
                    client.download_poster(item_id, url)
                written = _merged_values(_side_values(snapshot, "jellyfin"), candidate, picked)
            with self._lock:
                self._library_cache = None
                current = self._lookup(item_id)
                if current is not None:
                    _remember_written(self._state["accepted"], current, written, ["jellyfin"])
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
            written = candidate
            if picked is None:
                plex = PlexClient(settings.plex_baseurl, settings.plex_token, settings.verify_tls)
                try:
                    plex.match(plex_id, candidate.get("name") or "", candidate.get("year"), candidate.get("provider_ids") or {})
                    if chosen_poster:
                        plex.set_poster(plex_id, chosen_poster)
                finally:
                    plex.close()
            else:
                url = ""
                if "poster" in picked:
                    url = chosen_poster or self._poster_for(_client(settings), settings, snapshot, candidate)
                written = _merged_values(_side_values(snapshot, "plex"), candidate, picked)
                changes = _field_changes(candidate, picked)
                plex = PlexClient(settings.plex_baseurl, settings.plex_token, settings.verify_tls)
                try:
                    # Plex cannot store a single ID; a new ID means matching the whole edition.
                    if changes.get("ids"):
                        plex.match(plex_id, written.get("name") or "", written.get("year"), written.get("provider_ids") or {})
                    text = {key: value for key, value in changes.items() if key != "ids"}
                    if text:
                        plex.edit_fields(plex_id, kind, text)
                    if url:
                        plex.set_poster(plex_id, url)
                finally:
                    plex.close()
            with self._lock:
                current = self._lookup(item_id)
                if current is not None:
                    _remember_written(self._state["accepted"], current, written, ["plex"])
                    if not still_open(current):
                        self._state["findings"] = [item for item in self._state["findings"] if item["item_id"] != item_id]
                self._remember_stats()
                self._save()
        return {"ok": True, "name": written.get("name") or candidate["name"], "targets": chosen, "fields": picked}

    def _poster_for(self, client: JellyfinClient, settings: Settings, finding: dict, candidate: dict) -> str:
        url = poster_url(candidate.get("poster")) or self._resolve_poster(client, settings, finding, candidate)
        if not url:
            raise RuntimeError("Für diese Fassung ist kein Plakat zu finden")
        return url

    def accept(self, item_id: str, system: str) -> dict:
        systems = ["jellyfin", "plex"] if system == "both" else [system]
        if any(item not in {"jellyfin", "plex"} for item in systems):
            raise RuntimeError("Unbekanntes System")
        with self._lock:
            finding = self._lookup(item_id)
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

    def apply_sure(self, selections: dict | None = None, skip_collections: bool = False) -> dict:
        selections = selections or {}
        with self._lock:
            queued = []
            for item in self._state["findings"]:
                if item["status"] != "sure" or not item.get("candidates") or not item["candidates"][0].get("auto"):
                    continue
                if skip_collections and finding_is_collection(item):
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
                fields = selections.get(item_id)
                result = self.apply(item_id, key, targets, fields if isinstance(fields, list) else None)
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
        if not is_media_item(item):
            raise RuntimeError("Nur Filme, Serien und Folgen lassen sich prüfen")
        tmdb = TmdbClient(settings.tmdb_api_key) if settings.tmdb_api_key else None
        finding, _outcome = self._inspect(client, tmdb, item, None)
        clean = finding is None
        if clean:
            finding = _manual_finding(client, tmdb, item)
        if finding is None:
            return {"ok": True, "clean": True, "item": summary, "issue": None, "finding": None}
        with self._lock:
            if not clean:
                rest = [entry for entry in self._state["findings"] if entry["item_id"] != finding["item_id"]]
                self._state["findings"] = [finding, *rest]
                self._remember_stats()
                self._save()
            self._state["focus"] = finding
        return {"ok": True, "clean": clean, "item": summary, "issue": None, "finding": _public_finding(finding)}

    def research(self, item_id: str, term: str, year: int | None) -> dict:
        with self._lock:
            finding = self._lookup(item_id)
            if finding is None:
                raise KeyError("Eintrag nicht gefunden")
            kind = finding["kind"]
            query = term.strip() or finding.get("folder_title") or finding.get("jellyfin_name") or ""
        if not query:
            raise RuntimeError("Suchbegriff fehlt")
        settings = load_settings()
        client = _client(settings)
        tmdb = TmdbClient(settings.tmdb_api_key) if settings.tmdb_api_key else None
        episodes: list[tuple[int, int]] = []
        if kind == "series":
            try:
                episodes, _issues = _episode_facts(client.episodes(item_id))
            except JellyfinError as exc:
                log.warning("Folgen für die Suche nicht gelesen: %s", exc)
        found = _search(client, tmdb, kind, query, year, item_id)
        ranked = rank_candidates(query, year, episodes or None, found)
        public = []
        for candidate in ranked[:8]:
            entry = _public_candidate(candidate)
            entry["auto"] = False
            public.append(entry)
        with self._lock:
            current = self._lookup(item_id)
            if current is None:
                raise KeyError("Eintrag nicht gefunden")
            current["candidates"] = public
            current["searched"] = query + (f" ({year})" if year else "")
            if public:
                current["reason"] = f"{len(public)} Treffer für „{current['searched']}“. Die beste Fassung steht im Vergleich, die übrigen darunter."
            else:
                current["reason"] = f"Keine Treffer für „{current['searched']}“. Versuche einen anderen Suchbegriff."
            if current.get("status") == "manual":
                current["status"] = "review"
            self._save()
            return {"ok": True, "count": len(public), "finding": _public_finding(current)}

    def clear_focus(self) -> None:
        with self._lock:
            self._state["focus"] = None

    def poster_options(self, item_id: str, key: str) -> list[dict]:
        with self._lock:
            finding = self._lookup(item_id)
            if finding is None:
                raise KeyError("Eintrag nicht gefunden")
            candidate = dict(_poster_candidate(finding, key))
            snapshot = dict(finding)
        settings = load_settings()
        kind = "movie" if snapshot.get("kind") == "movie" else "series"
        ids = candidate.get("provider_ids") or {}
        options: list[dict] = []
        seen: set[str] = set()

        def add(url: str | None, thumb: str | None, language: str, source: str) -> None:
            clean = poster_url(url)
            if not clean or clean in seen:
                return
            seen.add(clean)
            options.append(
                {"url": clean, "thumb": poster_url(thumb) or clean, "language": language or "", "source": source}
            )

        add(candidate.get("poster"), None, "", "Vorschlag")
        if same_work(ids, snapshot.get("jellyfin_ids")):
            client = _client(settings)
            try:
                for image in client.remote_posters(item_id):
                    add(image.get("Url"), image.get("ThumbnailUrl"), image.get("Language") or "", image.get("ProviderName") or "Jellyfin")
            except JellyfinError as exc:
                log.warning("Plakate aus Jellyfin: %s", exc)
            finally:
                client.http.close()
        tmdb_id = str(ids.get("Tmdb") or "")
        if settings.tmdb_api_key and tmdb_id.isdigit():
            tmdb = TmdbClient(settings.tmdb_api_key)
            try:
                for image in tmdb.posters(kind, tmdb_id):
                    add(image["url"], image["thumb"], image["language"], image["source"])
            except Exception:
                log.warning("TMDB-Plakate nicht geladen", exc_info=True)
            finally:
                tmdb.http.close()
        return options[:36]

    def preview(self, item_id: str) -> dict:
        from app.listen import playback_source, save_clip

        settings, client, item, target_id, kind_hint, playable = self._playback(item_id)
        source = playback_source(client, item, kind_hint, playable)
        return {"ok": True, **save_clip(client, settings, target_id, source)}

    def browse(
        self,
        kind: str,
        term: str = "",
        missing_only: bool = False,
        open_only: bool = False,
        start: int = 0,
        limit: int = 60,
        refresh: bool = False,
    ) -> dict:
        now = time.monotonic()
        with self._lock:
            cached = self._library_cache
        if refresh or cached is None or now - cached[0] > LIBRARY_CACHE_SECONDS:
            client = _client(load_settings())
            try:
                items = [item for item in client.list_media() if is_media_item(item)]
            finally:
                client.http.close()
            cached = (now, items)
            with self._lock:
                self._library_cache = cached
        wanted = "Series" if kind == "series" else "Movie"
        with self._lock:
            open_ids = {item["item_id"] for item in self._state["findings"]}
            ignored = set(self._state["ignored"])
        query = term.strip()
        rows = []
        for item in cached[1]:
            if item.get("Type") != wanted:
                continue
            if query and not _library_matches(query, item):
                continue
            has_poster = has_primary_image(item)
            if missing_only and has_poster:
                continue
            if open_only and item["Id"] not in open_ids:
                continue
            rows.append(
                {
                    "id": item["Id"],
                    "kind": "series" if wanted == "Series" else "movie",
                    "name": item.get("Name") or "",
                    "year": _item_year(item),
                    "has_poster": has_poster,
                    "open": item["Id"] in open_ids,
                    "ignored": item["Id"] in ignored,
                }
            )
        start = max(0, start)
        return {"ok": True, "total": len(rows), "start": start, "items": rows[start : start + max(1, min(limit, 200))]}

    def check_items(self, item_ids: list[str]) -> dict:
        if not item_ids:
            raise RuntimeError("Nichts ausgewählt")
        if len(item_ids) > MAX_CHECK:
            raise RuntimeError(f"Höchstens {MAX_CHECK} Titel auf einmal prüfen")
        with self._lock:
            if self._state["running"]:
                raise RuntimeError("Scan läuft noch")
        settings = load_settings()
        client = _client(settings)
        tmdb = TmdbClient(settings.tmdb_api_key) if settings.tmdb_api_key else None
        plex_index = None
        if settings.plex_token:
            plex = PlexClient(settings.plex_baseurl, settings.plex_token, settings.verify_tls)
            try:
                plex_index = plex.library()
            except PlexError as exc:
                log.warning("Plex für Auswahl nicht geladen: %s", exc)
            finally:
                plex.close()
        found: list[str] = []
        clean: list[str] = []
        errors: list[str] = []
        for item_id in item_ids:
            try:
                item = client.media_item(item_id)
                if not is_media_item(item):
                    continue
                finding, _outcome = self._inspect(client, tmdb, item, plex_index)
            except (JellyfinError, RuntimeError, OSError) as exc:
                errors.append(f"{item_id}: {exc}")
                continue
            name = item.get("Name") or item_id
            if finding is None:
                clean.append(name)
                continue
            found.append(name)
            with self._lock:
                rest = [entry for entry in self._state["findings"] if entry["item_id"] != finding["item_id"]]
                self._state["findings"] = [finding, *rest]
                self._state["ignored"] = [entry for entry in self._state["ignored"] if entry != finding["item_id"]]
        with self._lock:
            self._remember_stats()
            self._save()
        return {"ok": True, "found": found, "clean": clean, "errors": errors}

    def poster(self, item_id: str, side: str, key: str, width: int = 480) -> tuple[bytes, str]:
        settings = load_settings()
        client = _client(settings)
        try:
            if side == "current":
                image = client.primary_image(item_id, width)
                if image is None:
                    raise KeyError("Kein Plakat")
                return image
            if side != "proposal":
                raise KeyError("Unbekannte Seite")
            with self._lock:
                finding = self._lookup(item_id)
                if finding is None:
                    raise KeyError("Eintrag nicht gefunden")
                candidate = _poster_candidate(finding, key)
                url = poster_url(candidate.get("poster"))
            if not url:
                url = self._resolve_poster(client, settings, finding, candidate)
                if url:
                    with self._lock:
                        current = self._lookup(item_id)
                        if current is not None:
                            _poster_candidate(current, key)["poster"] = url
                            self._save()
            if not url:
                raise KeyError("Kein Plakat")
            return fetch_poster(url)
        finally:
            client.http.close()

    def _resolve_poster(self, client: JellyfinClient, settings: Settings, finding: dict, candidate: dict) -> str:
        kind = "movie" if finding.get("kind") == "movie" else "series"
        ids = candidate.get("provider_ids") or {}
        try:
            results = client.remote_search_provider(
                kind,
                candidate.get("name") or "",
                candidate.get("year"),
                str(finding.get("item_id") or ""),
                ids,
            )
        except JellyfinError:
            results = []
        for result in results:
            url = poster_url(result.get("ImageUrl"))
            if url:
                return url
        tmdb_id = str(ids.get("Tmdb") or ids.get("tmdb") or "")
        if not settings.tmdb_api_key or not tmdb_id.isdigit():
            return ""
        tmdb = TmdbClient(settings.tmdb_api_key)
        try:
            described = tmdb.describe(kind, tmdb_id)
        except Exception:
            log.warning("TMDB-Plakat nicht geladen", exc_info=True)
            return ""
        finally:
            tmdb.http.close()
        return poster_url(described.get("poster"))

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
        if not path or not is_media_item(item):
            return None, "skip"
        kind = "series" if item.get("Type") == "Series" else "movie"
        if kind == "series":
            title, year, embedded = parse_series_path(path)
        else:
            title, year, embedded = parse_movie_path(path)
        if not title or (not year and is_collection_name(title)):
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
        if finding is None and not differ and (jelly_locked or has_primary_image(item)):
            return None, "ok"
        if finding is None and not differ:
            finding = _poster_finding(
                client,
                tmdb,
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
                embedded=embedded,
            )
            if finding is None:
                return None, "ok"
        elif finding is None:
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
                    "poster": poster_url(candidate.poster),
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
        self._state["findings"] = [item for item in data.get("findings") or [] if not is_collection_finding(item)]
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


def is_media_item(item: dict) -> bool:
    return item.get("Type") in {"Movie", "Series"}


def _library_matches(query: str, item: dict) -> bool:
    names = (item.get("Name") or "", item.get("OriginalTitle") or "")
    lowered = query.lower()
    if any(lowered in name.lower() for name in names):
        return True
    return matches_compact(query, *names, item.get("Path") or "")


def is_collection_finding(finding: dict) -> bool:
    title = finding.get("folder_title") or ""
    if is_collection_name(title) and not finding.get("folder_year"):
        return True
    top = (finding.get("candidates") or [{}])[0] or {}
    return is_collection_name(top.get("name") or "") and not is_collection_name(title)


def _poster_candidate(finding: dict, key: str) -> dict:
    candidates = [
        item for item in finding.get("candidates") or [] if not str(item.get("key") or "").startswith("side:")
    ]
    if key:
        chosen = next((item for item in candidates if item.get("key") == key), None)
        if chosen is not None:
            return chosen
    if not candidates:
        raise KeyError("Kein Vorschlag")
    return candidates[0]


def _poster_finding(
    client: JellyfinClient,
    tmdb: TmdbClient | None,
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
    embedded: dict,
) -> dict | None:
    found = _search(client, tmdb, kind, folder_title, folder_year, item_id)
    ranked = rank_candidates(folder_title, folder_year, None, found, embedded)
    chosen = next((item for item in ranked if same_work(jellyfin_ids, item.provider_ids)), None)
    if chosen is None and norm_provider_ids(jellyfin_ids):
        chosen = Candidate(
            name=jellyfin_name or folder_title,
            year=jellyfin_year or folder_year,
            provider_ids=norm_provider_ids(jellyfin_ids),
            source="jellyfin",
        )
    elif chosen is None and ranked:
        chosen = ranked[0]
    if chosen is None:
        return None
    public = _public_candidate(chosen)
    public["auto"] = False
    return {
        "item_id": item_id,
        "kind": kind,
        "path": path,
        "folder_title": folder_title,
        "folder_year": folder_year,
        "jellyfin_name": jellyfin_name,
        "jellyfin_year": jellyfin_year,
        "jellyfin_ids": jellyfin_provider_ids(norm_provider_ids(jellyfin_ids)),
        "jellyfin_original": jellyfin_original,
        "jellyfin_overview": jellyfin_overview,
        "jellyfin_accepted": False,
        "reason": "Kein Plakat in Jellyfin",
        "status": "poster",
        "missing_poster": True,
        "candidates": [public],
        "episode_issues": [],
        "episode_count": 0,
    }


def _manual_finding(client: JellyfinClient, tmdb: TmdbClient | None, item: dict) -> dict | None:
    path = item.get("Path") or ""
    kind = "series" if item.get("Type") == "Series" else "movie"
    if path:
        title, year, embedded = parse_series_path(path) if kind == "series" else parse_movie_path(path)
    else:
        title, year, embedded = "", None, {}
    jellyfin_name = item.get("Name") or ""
    finding = _poster_finding(
        client,
        tmdb,
        item_id=item["Id"],
        kind=kind,
        path=path,
        folder_title=title or jellyfin_name,
        folder_year=year or _item_year(item),
        jellyfin_name=jellyfin_name,
        jellyfin_year=_item_year(item),
        jellyfin_ids=item.get("ProviderIds") or {},
        jellyfin_original=(item.get("OriginalTitle") or "").strip(),
        jellyfin_overview=(item.get("Overview") or "").strip()[:400],
        embedded=embedded,
    )
    if finding is None:
        return None
    missing = not has_primary_image(item)
    finding["manual"] = True
    finding["missing_poster"] = missing
    finding["status"] = "poster" if missing else "manual"
    finding["reason"] = (
        "Kein Plakat in Jellyfin. Wähle unten eins aus."
        if missing
        else "Die Angaben passen. Unten kannst du trotzdem ein anderes Plakat wählen."
    )
    return finding


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
        "has_poster": has_primary_image(item),
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
    if not is_collection_name(title):
        found = [candidate for candidate in found if not is_collection_name(candidate.name)]
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


def finding_is_collection(finding: dict) -> bool:
    top = (finding.get("candidates") or [{}])[0] or {}
    return path_looks_like_collection(finding.get("path") or "") or looks_like_collection(
        finding.get("jellyfin_name") or "",
        finding.get("folder_title") or "",
        top.get("name") or "",
    )


def _public_finding(finding: dict) -> dict:
    return {
        **{key: value for key, value in finding.items() if key != "candidates"},
        "collection": finding_is_collection(finding),
        "candidates": [{key: value for key, value in candidate.items() if key != "raw"} for candidate in finding.get("candidates") or []],
    }


def still_open(finding: dict) -> bool:
    if finding.get("missing_poster") and not finding.get("jellyfin_accepted"):
        return True
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
        finding["missing_poster"] = False
    plex = finding.get("plex")
    if "plex" in targets and plex:
        accepted["plex"][plex["item_id"]] = signature
        plex["accepted"] = True
        plex["name"] = candidate.get("name") or plex.get("name")
        plex["year"] = candidate.get("year")
        plex["ids"] = ids
        plex["overview"] = candidate.get("overview") or ""
        plex["original"] = candidate.get("original_name") or ""


def _side_values(finding: dict, system: str) -> dict:
    if system == "plex":
        source = finding.get("plex") or {}
        if not source:
            raise KeyError("Kein Plex-Eintrag")
        return {
            "name": source.get("name") or "",
            "year": source.get("year"),
            "original_name": source.get("original") or "",
            "provider_ids": dict(source.get("ids") or {}),
            "overview": source.get("overview") or "",
        }
    return {
        "name": finding.get("jellyfin_name") or "",
        "year": finding.get("jellyfin_year"),
        "original_name": finding.get("jellyfin_original") or "",
        "provider_ids": dict(finding.get("jellyfin_ids") or {}),
        "overview": finding.get("jellyfin_overview") or "",
    }


def _side_candidate(finding: dict, system: str) -> dict:
    values = _side_values(finding, system)
    if not norm_provider_ids(values["provider_ids"]):
        raise RuntimeError("Keine IMDb-, TMDB- oder TVDB-ID zum Übernehmen")
    return {"key": f"side:{system}", **values, "auto": False, "raw": None}


def _field_changes(candidate: dict, picked: list[str]) -> dict:
    changes: dict = {}
    if "name" in picked:
        changes["name"] = candidate.get("name") or ""
    if "year" in picked:
        changes["year"] = candidate.get("year")
    if "original" in picked:
        changes["original"] = candidate.get("original_name") or ""
    if "overview" in picked:
        changes["overview"] = candidate.get("overview") or ""
    ids = candidate.get("provider_ids") or {}
    picked_ids = {key: ids.get(key) or "" for field, key in ID_FIELDS.items() if field in picked}
    if picked_ids:
        changes["ids"] = picked_ids
    return changes


def _merged_values(current: dict, candidate: dict, picked: list[str]) -> dict:
    merged = {**current, "provider_ids": dict(current.get("provider_ids") or {})}
    changes = _field_changes(candidate, picked)
    if changes.get("name"):
        merged["name"] = changes["name"]
    if "year" in changes:
        merged["year"] = changes["year"]
    if "original" in changes:
        merged["original_name"] = changes["original"]
    if "overview" in changes:
        merged["overview"] = changes["overview"]
    for key, value in (changes.get("ids") or {}).items():
        if value:
            merged["provider_ids"][key] = value
        else:
            merged["provider_ids"].pop(key, None)
    return merged


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
