from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template, request

from app.excerpt import ExcerptError
from app.jellyfin import JellyfinError
from app.plex import PlexError
from app.scanner import Scanner

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

    @app.post("/api/listen")
    def listen():
        payload = request.get_json(silent=True) or {}
        item_id = str(payload.get("item_id") or "")
        if not item_id:
            return jsonify({"ok": False, "error": "item_id fehlt"}), 400
        try:
            return jsonify(scanner.listen(item_id))
        except ExcerptError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
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
