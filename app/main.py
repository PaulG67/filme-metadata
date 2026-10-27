from __future__ import annotations

import logging

from pathlib import Path

import httpx
from flask import Flask, Response, jsonify, render_template, request, send_file

from app.excerpt import ExcerptError
from app.jellyfin import JellyfinError
from app.plex import PlexError
from app.scanner import Scanner
from app.tmdb import external_links, lookup_title

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("filme-metadata")


def _targets(value) -> list[str]:
    if value is None:
        return ["jellyfin"]
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    return [part for part in value if part in {"jellyfin", "plex"}]


def _fields(value) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [str(part) for part in value]


def create_app() -> Flask:
    app = Flask(__name__)
    scanner = Scanner()

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/icon.svg")
    def icon():
        path = Path(__file__).resolve().parents[1] / "icon.svg"
        return send_file(path, mimetype="image/svg+xml", max_age=3600)

    @app.get("/api/status")
    def status():
        return jsonify(scanner.status())

    @app.post("/api/test")
    def test_connection():
        try:
            return jsonify(scanner.test_connection())
        except (JellyfinError, PlexError, OSError, RuntimeError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/scan")
    def scan():
        try:
            scanner.start()
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        return jsonify({"ok": True})

    @app.get("/api/poster/<item_id>")
    def poster(item_id: str):
        if not item_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for ch in item_id):
            return Response(status=404)
        side = str(request.args.get("side") or "current")
        key = str(request.args.get("key") or "")
        raw_width = str(request.args.get("w") or "")
        width = min(480, max(120, int(raw_width))) if raw_width.isdigit() else 480
        try:
            data, mime = scanner.poster(item_id, side, key, width)
        except KeyError:
            return Response(status=404)
        except (JellyfinError, OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
            log.warning("Plakat nicht geladen: %s", exc)
            return Response(status=404)
        return Response(data, mimetype=mime, headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/title")
    def title_detail():
        from app.config import load_settings

        kind = "movie" if request.args.get("kind") == "movie" else "series"
        tmdb = str(request.args.get("tmdb") or "")
        imdb = str(request.args.get("imdb") or "")
        tvdb = str(request.args.get("tvdb") or "")
        name = str(request.args.get("name") or "")[:180]
        year = None
        raw_year = str(request.args.get("year") or "")
        if raw_year.isdigit():
            year = int(raw_year)
        try:
            return jsonify(lookup_title(load_settings().tmdb_api_key, kind, tmdb, imdb, tvdb, name, year))
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc), "links": external_links(kind, tmdb, imdb, tvdb)}), 400

    @app.get("/api/library")
    def library():
        term = str(request.args.get("q") or "")
        try:
            return jsonify({"ok": True, "items": scanner.search_library(term)})
        except (JellyfinError, OSError, RuntimeError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/focus")
    def focus():
        payload = request.get_json(silent=True) or {}
        item_id = str(payload.get("item_id") or "")
        if not item_id:
            return jsonify({"ok": False, "error": "item_id fehlt"}), 400
        try:
            return jsonify(scanner.focus(item_id))
        except (JellyfinError, RuntimeError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/browse")
    def browse():
        args = request.args
        start = str(args.get("start") or "0")
        limit = str(args.get("limit") or "60")
        try:
            return jsonify(
                scanner.browse(
                    "movie" if args.get("kind") == "movie" else "series",
                    str(args.get("q") or "")[:120],
                    args.get("missing") == "1",
                    args.get("open") == "1",
                    int(start) if start.isdigit() else 0,
                    int(limit) if limit.isdigit() else 60,
                    args.get("refresh") == "1",
                )
            )
        except (JellyfinError, OSError, RuntimeError, httpx.HTTPError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/check")
    def check():
        payload = request.get_json(silent=True) or {}
        raw = payload.get("item_ids")
        item_ids = [str(value) for value in raw if str(value)] if isinstance(raw, list) else []
        try:
            return jsonify(scanner.check_items(item_ids))
        except (JellyfinError, PlexError, RuntimeError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/focus/search")
    def focus_search():
        payload = request.get_json(silent=True) or {}
        raw_year = str(payload.get("year") or "").strip()
        year = int(raw_year) if raw_year.isdigit() and 1880 <= int(raw_year) <= 2100 else None
        try:
            return jsonify(
                scanner.research(str(payload.get("item_id") or ""), str(payload.get("term") or "")[:160], year)
            )
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except (JellyfinError, RuntimeError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/focus/clear")
    def clear_focus():
        scanner.clear_focus()
        return jsonify({"ok": True})

    @app.get("/api/posters/<item_id>")
    def poster_options(item_id: str):
        try:
            options = scanner.poster_options(item_id, str(request.args.get("key") or ""))
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except (JellyfinError, OSError, RuntimeError, httpx.HTTPError) as exc:
            log.warning("Plakatauswahl nicht geladen: %s", exc)
            return jsonify({"ok": False, "error": "Plakate nicht geladen"}), 400
        return jsonify({"ok": True, "options": options})

    @app.post("/api/apply")
    def apply():
        payload = request.get_json(silent=True) or {}
        try:
            result = scanner.apply(
                str(payload.get("item_id") or ""),
                str(payload.get("key") or ""),
                _targets(payload.get("targets")),
                _fields(payload.get("fields")),
                str(payload.get("poster") or "") or None,
            )
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except (JellyfinError, PlexError, RuntimeError, OSError) as exc:
            log.exception("Übernehmen fehlgeschlagen")
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify(result)

    @app.post("/api/accept")
    def accept():
        payload = request.get_json(silent=True) or {}
        item_id = str(payload.get("item_id") or "")
        system = str(payload.get("system") or "")
        if not item_id or not system:
            return jsonify({"ok": False, "error": "item_id oder system fehlt"}), 400
        try:
            return jsonify(scanner.accept(item_id, system))
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/apply-sure")
    def apply_sure():
        try:
            payload = request.get_json(silent=True) or {}
            selections = payload.get("selections")
            if not isinstance(selections, dict):
                selections = {}
            return jsonify(
                scanner.apply_sure(
                    {str(key): _fields(value) for key, value in selections.items()},
                    payload.get("skip_collections") is True,
                )
            )
        except (JellyfinError, PlexError, RuntimeError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/episode")
    def episode():
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(scanner.fix_episode(str(payload.get("item_id") or "")))
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except (JellyfinError, RuntimeError, OSError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/clip")
    def clip_create():
        payload = request.get_json(silent=True) or {}
        item_id = str(payload.get("item_id") or "")
        if not item_id:
            return jsonify({"ok": False, "error": "item_id fehlt"}), 400
        try:
            return jsonify(scanner.preview(item_id))
        except ExcerptError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except (JellyfinError, RuntimeError, OSError) as exc:
            log.exception("Filmausschnitt fehlgeschlagen")
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/clip/<item_id>")
    def clip_file(item_id: str):
        from app.config import load_settings
        from app.listen import clip_file as clip_path

        path = clip_path(load_settings(), item_id)
        if path is None or not path.is_file():
            return jsonify({"ok": False, "error": "Kein Ausschnitt vorhanden"}), 404
        return send_file(path, mimetype="video/mp4", conditional=True, max_age=0)

    @app.post("/api/listen")
    def listen():
        payload = request.get_json(silent=True) or {}
        item_id = str(payload.get("item_id") or "")
        if not item_id:
            return jsonify({"ok": False, "error": "item_id fehlt"}), 400
        try:
            return jsonify(scanner.listen(item_id))
        except ExcerptError as exc:
            body = {"ok": False, "error": str(exc)}
            if exc.clip:
                body.update(exc.clip)
            return jsonify(body), 400
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except (JellyfinError, RuntimeError, OSError) as exc:
            log.exception("Ausschnitt fehlgeschlagen")
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/ignore")
    def ignore():
        payload = request.get_json(silent=True) or {}
        item_id = str(payload.get("item_id") or "")
        if not item_id:
            return jsonify({"ok": False, "error": "item_id fehlt"}), 400
        scanner.ignore(item_id)
        return jsonify({"ok": True})

    return app


app = create_app()
