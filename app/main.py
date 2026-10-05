from __future__ import annotations

import logging

from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

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

    @app.post("/api/apply")
    def apply():
        payload = request.get_json(silent=True) or {}
        try:
            result = scanner.apply(
                str(payload.get("item_id") or ""),
                str(payload.get("key") or ""),
                _targets(payload.get("targets")),
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
            return jsonify(scanner.apply_sure())
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
