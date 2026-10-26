import os
import secrets
import time
from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, request, send_from_directory
from werkzeug.exceptions import HTTPException

from modules import dashboards, currency, expenses, groups, guest, ocr, settlements, users
from modules.db import close_db, init_db
from modules.ocr import parse_receipt_text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BLUEPRINTS = [users.bp, groups.bp, expenses.bp, currency.bp, ocr.bp, settlements.bp, dashboards.bp, guest.bp]
FRIENDLY = {404: "Not found.", 405: "That action is not allowed here.", 413: "File is too large (max 8 MB)."}


def load_secret_key():
    if os.environ.get("XSPLIT_SECRET"):
        return os.environ["XSPLIT_SECRET"]
    path = os.path.join(BASE_DIR, ".secret_key")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    for _ in range(50):
        with open(path) as f:
            key = f.read().strip()
        if key:
            return key
        time.sleep(0.1)
    raise RuntimeError("Could not read .secret_key")


def create_app(db_path=None, testing=False, frontend_origin=None, database_url=None):
    frontend = (frontend_origin if frontend_origin is not None else os.environ.get("XSPLIT_FRONTEND_ORIGIN", "")).rstrip("/")
    app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"), static_url_path="/static")
    app.config.update(
        SECRET_KEY=secrets.token_hex(32) if testing else load_secret_key(),
        DB_PATH=db_path or os.environ.get("XSPLIT_DB") or os.path.join(BASE_DIR, "xsplit.db"),
        DATABASE_URL=database_url or (None if db_path else os.environ.get("DATABASE_URL")),
        UPLOAD_DIR=os.environ.get("XSPLIT_UPLOADS") or os.path.join(BASE_DIR, "uploads"),
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        TESTING=testing,
        FRONTEND_ORIGIN=frontend,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="None" if frontend else "Lax",
        SESSION_COOKIE_SECURE=bool(frontend) or os.environ.get("XSPLIT_HTTPS") == "1",
    )
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    init_db(app, currency.CURRENCIES)
    app.teardown_appcontext(close_db)
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)

    def trusted(origin):
        return origin == frontend or urlparse(origin).netloc == request.host

    @app.before_request
    def check_origin():
        origin = request.headers.get("Origin")
        if request.method == "OPTIONS" and request.path.startswith("/api/"):
            return ("", 204) if origin and trusted(origin) else (jsonify({"error": "Origin not allowed."}), 403)
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and origin and not trusted(origin):
            return jsonify({"error": "Requests from other websites are not allowed."}), 403

    @app.after_request
    def cors(resp):
        origin = request.headers.get("Origin")
        if frontend and origin == frontend:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            resp.headers["Access-Control-Max-Age"] = "600"
        resp.headers.add("Vary", "Origin")
        return resp

    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.route("/config.js")
    def config_js():
        return send_from_directory(app.static_folder, "config.js")

    @app.route("/guest/<token>")
    def old_guest_link(token):
        return redirect(f"/#guest={token}")

    @app.get("/api/meta")
    def meta():
        return jsonify({"currencies": [{"code": c, "name": n, "symbol": s} for c, n, s in currency.CURRENCIES],
                        "categories": expenses.CATEGORIES})

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    @app.errorhandler(HTTPException)
    def http_error(e):
        if request.path.startswith(("/api/", "/uploads/")):
            return jsonify({"error": FRIENDLY.get(e.code, e.description)}), e.code
        return e

    @app.errorhandler(Exception)
    def server_error(e):
        if isinstance(e, HTTPException):
            return http_error(e)
        app.logger.exception("Unhandled error")
        return jsonify({"error": "Something went wrong on the server. Nothing was saved; please try again."}), 500

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
