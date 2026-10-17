"""Shooting diary – login-protected blueprint at /diary, one account per person.

Data and secrets live in DIARY_DATA_DIR (default: <repo>/diary_data, git-ignored):
  users.db          accounts (see users.py)
  diary.db          the first account's data; others in users/<name>.db – every account its own file
  secret_key        signs the login cookie (created on first start)

Create the first account on the server:  python -m diary user-add NAME --admin
(An existing single-password diary becomes an admin account – "osku", or DIARY_ADMIN_USER – with the same
password on first start.)
"""
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import timedelta
from functools import wraps

from flask import (Blueprint, abort, g, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(HERE), ".env"))
except ImportError:
    pass
DATA_DIR = os.getenv("DIARY_DATA_DIR", os.path.join(os.path.dirname(HERE), "diary_data"))
DB_PATH = os.path.join(DATA_DIR, "diary.db")
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROFILE_KEYS = ("name", "sra", "ipsc", "hunterNo")

bp = Blueprint("diary", __name__, template_folder="templates", static_folder="static",
               static_url_path="/static")


# ------------------------------------------------------------------ setup
def init_app(app):
    """Register the blueprint and the cookie/proxy settings it needs."""
    os.makedirs(DATA_DIR, exist_ok=True)
    init_db()
    from . import users
    users.migrate_single_user()
    for u in users.all_users():              # schema / migrations for every account's file
        init_db(users.db_path(u))
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY") or _secret_key_file()
    app.config.update(
        SESSION_COOKIE_NAME="diary_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Secure by default (site is reached over HTTPS via the proxy).
        # Set DIARY_COOKIE_SECURE=0 to log in over plain http on the LAN.
        SESSION_COOKIE_SECURE=_cookie_secure(),
        PERMANENT_SESSION_LIFETIME=PERMANENT_LIFETIME,
    )
    # Nginx Proxy Manager sits in front: trust one hop of X-Forwarded-* so the
    # login rate limit sees the real client IP. Set DIARY_BEHIND_PROXY=0 if not proxied.
    if os.getenv("DIARY_BEHIND_PROXY", "1") != "0":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.register_blueprint(bp, url_prefix="/diary")


PERMANENT_LIFETIME = timedelta(days=int(os.getenv("DIARY_LOGIN_DAYS", "90")))


def _cookie_secure():
    return os.getenv("DIARY_COOKIE_SECURE", "1") != "0"


def _secret_key_file():
    path = os.path.join(DATA_DIR, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


# ------------------------------------------------------------------ database
_default_db = None          # the command line picks an account's file with --user (see __main__)


def use_db(path):
    global _default_db
    _default_db = path


def connect(path=None):
    conn = sqlite3.connect(path or _default_db or DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db():
    """The logged-in user's database – the only data a request can reach."""
    if "diary_db" not in g:
        user = g.get("diary_user")
        if not user:
            raise RuntimeError("db() outside a logged-in request")
        from .users import db_path
        g.diary_db = connect(db_path(user))
    return g.diary_db


@bp.teardown_app_request
def _close_db(_exc):
    conn = g.pop("diary_db", None)
    if conn is not None:
        conn.close()


def init_db(path=None):
    os.makedirs(os.path.dirname(path or DB_PATH), exist_ok=True)
    conn = connect(path or DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            data TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sessions_date ON sessions(date);
        CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS guns (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        """
    )
    from .chrono import init_chrono
    init_chrono(conn)
    from .zero import init_zero
    init_zero(conn)
    conn.commit()
    conn.close()


def kv_get(key, conn=None):
    row = (conn or db()).execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(key, value, conn=None):
    conn = conn or db()
    conn.execute("INSERT INTO kv(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def upsert_gun(conn, gid, body):
    conn.execute("INSERT INTO guns(id,data) VALUES(?,?) "
                 "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                 (gid, json.dumps(body, ensure_ascii=False)))


def upsert_session(conn, sid, body):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        "INSERT INTO sessions(id,date,data,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET date=excluded.date, data=excluded.data, "
        "updated_at=excluded.updated_at",
        (sid, body["date"], json.dumps(body, ensure_ascii=False), now))


# ------------------------------------------------------------------ auth
_fails = {}            # ip -> [count, first_fail_time]
_fails_lock = threading.Lock()
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60


def _too_many(ip):
    with _fails_lock:
        count, first = _fails.get(ip, (0, 0))
        if time.time() - first >= LOCK_SECONDS:
            _fails.pop(ip, None)
            return False
        return count >= MAX_FAILS


def _note_fail(ip):
    with _fails_lock:
        entry = _fails.setdefault(ip, [0, time.time()])
        entry[0] += 1


def current_user():
    """The logged-in, enabled account whose password hasn't changed since this cookie was issued."""
    from . import users
    if "diary_user" not in g:
        u = users.get(session.get("diary_uid")) if session.get("diary_auth") else None
        ok = u and not u["disabled"] and users.stamp(u) and session.get("diary_pw") == users.stamp(u)
        g.diary_user = u if ok else None
    return g.diary_user


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user():
            return view(*args, **kwargs)
        if request.blueprint == "diary" and "/api/" in request.path:
            return jsonify(error="login_required"), 401
        return redirect(url_for("diary.login"))
    return wrapper


@bp.route("/login", methods=["GET", "POST"])
def login():
    from . import users
    users.migrate_single_user()              # a diary set up before accounts existed
    if not users.all_users(include_disabled=False):
        return render_template("diary_login.html", disabled=True,
                               error="No accounts yet. Run on the server: python -m diary user-add NAME --admin"), 503
    error, username = None, ""
    if request.method == "POST":
        ip = request.remote_addr or "?"
        username = request.form.get("username", "").strip().lower()[:32]
        if _too_many(ip):
            error = "Too many wrong attempts. Wait 15 minutes."
        else:
            u = users.verify(username, request.form.get("password", ""))
            if u:
                with _fails_lock:
                    _fails.pop(ip, None)
                session.clear()
                session.permanent = True
                session["diary_auth"] = True
                session["diary_uid"] = u["id"]
                session["diary_pw"] = users.stamp(u)
                users.touch_login(u["id"])
                resp = redirect(url_for("diary.home"))
                # Readable by the pages (not a secret): keeps each account's offline queue apart on a shared phone
                resp.set_cookie("diary_user", u["username"], max_age=int(PERMANENT_LIFETIME.total_seconds()),
                                path="/diary", samesite="Lax", secure=_cookie_secure(), httponly=False)
                return resp
            _note_fail(ip)
            time.sleep(1)
            error = "Wrong username or password."
    return render_template("diary_login.html", error=error, disabled=False, username=username)


@bp.post("/logout")
def logout():
    session.clear()
    resp = redirect(url_for("diary.login"))
    resp.delete_cookie("diary_user", path="/diary")
    return resp


# ------------------------------------------------------------------ pages
def static_page(name):
    resp = send_from_directory(os.path.join(HERE, "static"), name, max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.get("/")
@login_required
def home():
    """Landing page: Diary, Velocity and Zeroes with a one-line summary of each."""
    return static_page("home.html")


@bp.get("/log")
@login_required
def page():
    return static_page("diary.html")


@bp.get("/manifest.webmanifest")
def manifest():
    # Always fresh: Android reads the share target from it when the app is installed.
    resp = send_from_directory(os.path.join(HERE, "static"), "manifest.webmanifest",
                               mimetype="application/manifest+json", max_age=0)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.get("/sw.js")
def service_worker():
    # Served from /diary/ (not /diary/static/) so it may control the whole app.
    resp = send_from_directory(os.path.join(HERE, "static"), "sw.js",
                               mimetype="application/javascript", max_age=0)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.after_request
def _headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if "/api/" in request.path:
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ API
def _json_body():
    # A JSON content type can't be sent by a cross-site form, which blocks CSRF.
    if not request.is_json:
        abort(415)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        abort(400)
    return body


@bp.get("/api/sessions")
@login_required
def list_sessions():
    rows = db().execute("SELECT id, data FROM sessions ORDER BY date DESC, id DESC").fetchall()
    return jsonify([dict(json.loads(r["data"]), id=r["id"]) for r in rows])


@bp.put("/api/sessions/<sid>")
@login_required
def put_session(sid):
    if not ID_RE.match(sid):
        abort(400)
    body = _json_body()
    body.pop("id", None)
    if not DATE_RE.match(str(body.get("date", ""))):
        return jsonify(error="bad_date"), 400
    if not isinstance(body.get("lines", []), list):
        return jsonify(error="bad_lines"), 400
    upsert_session(db(), sid, body)
    db().commit()
    return jsonify(ok=True)


@bp.delete("/api/sessions/<sid>")
@login_required
def delete_session(sid):
    if request.headers.get("X-Requested-With") != "fetch":
        abort(403)
    db().execute("DELETE FROM sessions WHERE id=?", (sid,))
    db().commit()
    return jsonify(ok=True)


@bp.get("/api/account")
@login_required
def get_account():
    u = current_user()
    return jsonify(username=u["username"], is_admin=bool(u["is_admin"]))


@bp.put("/api/account/password")
@login_required
def put_password():
    """Change your own password; your other devices are logged out, this one stays in."""
    from . import users
    body = _json_body()
    u = current_user()
    if not users.verify(u["username"], str(body.get("current", ""))):
        time.sleep(1)
        return jsonify(error="wrong_password"), 403
    try:
        users.set_password(u["username"], str(body.get("new", "")))
    except ValueError:
        return jsonify(error="too_short", min=users.MIN_PASSWORD), 400
    session["diary_pw"] = users.stamp(users.get(u["id"]))
    return jsonify(ok=True)


@bp.get("/api/profile")
@login_required
def get_profile():
    return jsonify(json.loads(kv_get("profile") or "{}"))


@bp.put("/api/profile")
@login_required
def put_profile():
    body = _json_body()
    kv_set("profile", json.dumps({k: str(body.get(k, ""))[:200] for k in PROFILE_KEYS},
                                 ensure_ascii=False))
    return jsonify(ok=True)


GUN_KEYS = {"name": str, "type": str, "cal": str, "mode": str, "startCount": int,
            "archived": bool, "order": int, "acquired": str}


@bp.get("/api/guns")
@login_required
def list_guns():
    rows = db().execute("SELECT id, data FROM guns").fetchall()
    return jsonify([dict(json.loads(r["data"]), id=r["id"]) for r in rows])


@bp.put("/api/guns/<gid>")
@login_required
def put_gun(gid):
    if not ID_RE.match(gid):
        abort(400)
    body = _json_body()
    gun = {}
    for key, typ in GUN_KEYS.items():
        val = body.get(key)
        if val is None:
            continue
        try:
            gun[key] = typ(val) if typ is not str else str(val)[:120]
        except (TypeError, ValueError):
            return jsonify(error="bad_" + key), 400
    if not gun.get("name", "").strip():
        return jsonify(error="name_required"), 400
    if gun.get("acquired") and not DATE_RE.match(gun["acquired"]):
        return jsonify(error="bad_acquired"), 400
    upsert_gun(db(), gid, gun)
    db().commit()
    return jsonify(ok=True)


@bp.delete("/api/guns/<gid>")
@login_required
def delete_gun(gid):
    if request.headers.get("X-Requested-With") != "fetch":
        abort(403)
    db().execute("DELETE FROM guns WHERE id=?", (gid,))
    db().commit()
    return jsonify(ok=True)


# Velocity data (Garmin Xero / own logs / Kestrel): routes register on the same blueprint.
from . import chrono  # noqa: E402,F401
# Scope zeroes (dial positions per rifle, swap calculator, PDF zeroing card).
from . import zero  # noqa: E402,F401
# Landing-page summary.
from . import summary  # noqa: E402,F401
