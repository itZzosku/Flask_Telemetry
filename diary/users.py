"""Accounts. Each user has their own SQLite database file, so one user's pages and API calls can only ever
open that user's data — isolation doesn't depend on every query remembering a user filter.

  DATA_DIR/users.db          accounts: username, password hash, admin flag, database file
  DATA_DIR/diary.db          the first (admin) account's data — the single-user database from before
  DATA_DIR/users/<name>.db   every other account

Accounts are created on the server: python -m diary user-add NAME. A new account has no password: the person
logs in with an empty password once (within SETUP_DAYS) and must choose their own before anything else.
"""
import os
import re
import sqlite3
import time

from werkzeug.security import check_password_hash, generate_password_hash

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
MIN_PASSWORD = 10
SETUP_DAYS = 7          # how long a new account can be claimed with an empty password
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    pw_hash TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    db_file TEXT NOT NULL UNIQUE,
    disabled INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    last_login TEXT
);
"""


def _data_dir():
    from . import DATA_DIR
    return DATA_DIR


def users_db_path():
    return os.path.join(_data_dir(), "users.db")


def uconnect():
    conn = sqlite3.connect(users_db_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if "setup_until" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
        conn.execute("ALTER TABLE users ADD COLUMN setup_until TEXT")   # empty-password first login allowed until
    return conn


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def db_path(user):
    """Absolute path of a user's data file (user: row/dict with db_file)."""
    return os.path.join(_data_dir(), user["db_file"])


def all_users(include_disabled=True):
    with uconnect() as c:
        rows = c.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [dict(r) for r in rows if include_disabled or not r["disabled"]]


def get(uid):
    if uid is None:
        return None
    with uconnect() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(r) if r else None


def by_name(username):
    with uconnect() as c:
        r = c.execute("SELECT * FROM users WHERE username=?", (str(username or "").strip().lower(),)).fetchone()
    return dict(r) if r else None


def check_name(username):
    name = str(username or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError("Username: 2–32 characters, lower-case letters, digits, - and _ (starting with a letter or digit).")
    return name


def check_password(pw):
    if len(pw or "") < MIN_PASSWORD:
        raise ValueError(f"Use at least {MIN_PASSWORD} characters.")
    return pw


def _setup_deadline(days):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days * 86400))


def create(username, password=None, admin=False, db_file=None, setup_days=SETUP_DAYS):
    """New account with an empty database (schema created). db_file defaults to users/<name>.db.
    password=None: no password yet – first login with an empty password, then they choose one."""
    from . import init_db
    name = check_name(username)
    if password is not None:
        check_password(password)
    migrate_single_user()                 # an old single-password diary.db becomes its own account first
    if by_name(name):
        raise ValueError(f"User {name} already exists.")
    # The very first account uses diary.db (where a single-user diary always lived); the rest users/<name>.db
    db_file = db_file or ("diary.db" if not all_users() else os.path.join("users", name + ".db"))
    os.makedirs(os.path.dirname(os.path.join(_data_dir(), db_file)) or _data_dir(), exist_ok=True)
    with uconnect() as c:
        c.execute("INSERT INTO users(username,pw_hash,is_admin,db_file,created,setup_until) VALUES(?,?,?,?,?,?)",
                  (name, generate_password_hash(password) if password is not None else None, 1 if admin else 0,
                   db_file, _now(), None if password is not None else _setup_deadline(setup_days)))
    init_db(os.path.join(_data_dir(), db_file))
    return by_name(name)


def set_password(username, password):
    check_password(password)
    with uconnect() as c:
        n = c.execute("UPDATE users SET pw_hash=?, setup_until=NULL WHERE username=?",
                      (generate_password_hash(password), str(username).lower())).rowcount
    if not n:
        raise ValueError(f"No user {username}.")


def clear_password(username, setup_days=SETUP_DAYS):
    """Back to 'no password': logged out everywhere; next login with an empty password picks a new one."""
    with uconnect() as c:
        n = c.execute("UPDATE users SET pw_hash=NULL, setup_until=? WHERE username=?",
                      (_setup_deadline(setup_days), str(username).lower())).rowcount
    if not n:
        raise ValueError(f"No user {username}.")


def needs_setup(user):
    """Account without a password whose first-login window is still open."""
    return bool(user and not user["pw_hash"] and not user["disabled"] and user.get("setup_until")
                and user["setup_until"] >= _now())


def set_disabled(username, disabled):
    with uconnect() as c:
        n = c.execute("UPDATE users SET disabled=? WHERE username=?", (1 if disabled else 0, str(username).lower())).rowcount
    if not n:
        raise ValueError(f"No user {username}.")


def rename(old, new):
    """Only the login name changes; the data file keeps its name."""
    new = check_name(new)
    if by_name(new):
        raise ValueError(f"User {new} already exists.")
    with uconnect() as c:
        n = c.execute("UPDATE users SET username=? WHERE username=?", (new, str(old).lower())).rowcount
    if not n:
        raise ValueError(f"No user {old}.")


def verify_setup(username, password):
    """First login of a new account: empty password while the setup window is open → the user row."""
    u = by_name(username)
    return u if (password or "") == "" and needs_setup(u) else None


def verify(username, password):
    """The user row when name + password match an enabled account, else None."""
    u = by_name(username)
    if not u or u["disabled"] or not u["pw_hash"]:
        check_password_hash(generate_password_hash("x"), password or "")   # same work either way
        return None
    return u if check_password_hash(u["pw_hash"], password or "") else None


def touch_login(uid):
    with uconnect() as c:
        c.execute("UPDATE users SET last_login=? WHERE id=?", (_now(), uid))


def stamp(user):
    """Part of the password hash goes into the login cookie: a password change logs out that user's devices."""
    return (user.get("pw_hash") or "")[-12:]


def migrate_single_user(name=None):
    """First start after the update: the single-password diary becomes the admin account on the same
    data file (diary.db), with the same password. Returns the new user or None."""
    from . import DB_PATH
    if all_users() or not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT value FROM kv WHERE key='password_hash'").fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    if not row:
        return None
    # "osku" matches the username the old login page already sent, so saved passwords keep working
    name = check_name(name or os.getenv("DIARY_ADMIN_USER", "osku"))
    with uconnect() as c:
        c.execute("INSERT INTO users(username,pw_hash,is_admin,db_file,created) VALUES(?,?,1,'diary.db',?)",
                  (name, row[0], _now()))
    return by_name(name)


def ensure(username, password, admin=False, db_file=None):  # noqa: E302
    """Create the account, or reset its password if it exists (setup scripts and tests)."""
    if by_name(username):
        set_password(username, password)
        return by_name(username)
    return create(username, password, admin=admin, db_file=db_file)
