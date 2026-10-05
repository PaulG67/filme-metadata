from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template, request

from app.jellyfin import JellyfinError
from app.scanner import Scanner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("filme-metadata")


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
        except (JellyfinError, OSError, RuntimeError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/scan")
    def scan():
        try:
            scanner.start()
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        return jsonify({"ok": True})

    @app.post("/api/apply")
    def apply():
        payload = request.get_json(silent=True) or {}
        try:
            result = scanner.apply(str(payload.get("item_id") or ""), str(payload.get("key") or ""))
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except (JellyfinError, RuntimeError, OSError) as exc:
            log.exception("Übernehmen fehlgeschlagen")
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify(result)

    @app.post("/api/apply-sure")
    def apply_sure():
        try:
            return jsonify(scanner.apply_sure())
        except (JellyfinError, RuntimeError, OSError) as exc:
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
