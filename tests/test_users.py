"""Accounts: every account has its own database file, so one user can never see or change another's data.
Synthetic data only."""
import os
import sqlite3

import pytest
from flask import Flask
from werkzeug.security import generate_password_hash

import diary
from diary import users

H = {"X-Requested-With": "fetch"}
NOTE = "osku-only secret note"


def _app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test"
    diary.init_app(app)
    return app


def _login(app, name, pw):
    c = app.test_client()
    r = c.post("/diary/login", data={"username": name, "password": pw})
    assert r.status_code == 302, r.get_data(as_text=True)[:300]
    return c


@pytest.fixture()
def two():
    """osku (diary.db, admin) with data; mikko (users/mikko.db) fresh."""
    users.ensure("osku", "osku-password-1", admin=True, db_file="diary.db")
    m = users.by_name("mikko")
    if m:
        p = users.db_path(m)
        with users.uconnect() as c:
            c.execute("DELETE FROM users WHERE username='mikko'")
        if os.path.exists(p):
            os.remove(p)
    users.create("mikko", "mikko-password-1")
    app = _app()
    o = _login(app, "osku", "osku-password-1")
    o.put("/diary/api/guns/g-osku", json={"name": "Osku Rifle", "cal": ".308 Win"})
    o.put("/diary/api/sessions/s-osku", json={"date": "2026-10-01", "place": NOTE, "lines": [{"rounds": 20, "gunId": "g-osku"}]})
    o.put("/diary/api/profile", json={"name": "Osku", "sra": "123456"})
    o.put("/diary/api/chrono/loads/l-osku", json={"kind": "handload", "cal": ".308 Win", "bullet_model": "Scenar", "bullet_gr": 155})
    o.put("/diary/api/zero/scopes/s-osku", json={"name": "Osku Scope"})
    o.put("/diary/api/zero/zeros/z-osku", json={"scope_id": "s-osku", "rifle": "Osku Rifle", "elev": 1.0, "wind": 0})
    return app, o, _login(app, "mikko", "mikko-password-1")


def test_every_api_route_needs_login():
    app = _app()
    anon = app.test_client()
    for rule in app.url_map.iter_rules():
        if "/diary/api/" not in rule.rule:
            continue
        url = rule.rule.replace("<int:log_id>", "1")
        for arg in rule.arguments:
            url = url.replace(f"<{arg}>", "x")
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            r = anon.open(url, method=method, json={}, headers=H)
            assert r.status_code == 401, (method, rule.rule, r.status_code)


def test_users_see_only_their_own_data(two):
    app, o, m = two
    assert m.get("/diary/api/account").get_json() == {"username": "mikko", "is_admin": False}
    assert m.get("/diary/api/sessions").get_json() == []
    assert m.get("/diary/api/guns").get_json() == []
    assert m.get("/diary/api/profile").get_json() == {}
    assert m.get("/diary/api/chrono/loads").get_json() == []
    assert m.get("/diary/api/chrono/strings").get_json() == []
    assert m.get("/diary/api/zero").get_json() == []
    home = m.get("/diary/api/home").get_json()
    assert home["diary"]["visits"] == 0 and home["zeroes"] == [] and home["velocity"]["strings"] == 0
    for r in (m.get("/diary/api/sessions"), m.get("/diary/api/profile"), m.get("/diary/api/home")):
        assert NOTE not in r.get_data(as_text=True) and "123456" not in r.get_data(as_text=True)
    # osku still sees everything
    assert NOTE in [s.get("place") for s in o.get("/diary/api/sessions").get_json()]
    assert "Osku Scope" in [z["name"] for z in o.get("/diary/api/home").get_json()["zeroes"]]


def test_users_cannot_change_each_others_data(two):
    app, o, m = two
    # Ids that exist only in osku's file are simply not there for mikko
    assert m.get("/diary/api/chrono/loads/l-osku").status_code == 404
    assert m.post("/diary/api/zero/zeros/z-osku/adjust", json={"d_elev": 5, "d_wind": 0}).status_code == 404
    assert m.post("/diary/api/zero/scopes/s-osku/mount", json={"zero_id": "z-osku"}).status_code == 404
    m.delete("/diary/api/guns/g-osku", headers=H)
    m.delete("/diary/api/sessions/s-osku", headers=H)
    m.delete("/diary/api/zero/scopes/s-osku", headers=H)
    # Same ids written by mikko land in mikko's file
    m.put("/diary/api/guns/g-osku", json={"name": "Mikko Rifle"})
    m.put("/diary/api/profile", json={"name": "Mikko"})
    og = {g["id"]: g["name"] for g in o.get("/diary/api/guns").get_json()}
    assert og["g-osku"] == "Osku Rifle" and "Mikko Rifle" not in og.values()
    assert o.get("/diary/api/profile").get_json()["name"] == "Osku"
    assert any(s["id"] == "s-osku" for s in o.get("/diary/api/sessions").get_json())
    sc = next(s for s in o.get("/diary/api/zero").get_json() if s["id"] == "s-osku")
    assert sc["zeros"][0]["elev"] == 1.0
    assert [g["name"] for g in m.get("/diary/api/guns").get_json()] == ["Mikko Rifle"]
    # And the files really are separate
    path = users.db_path(users.by_name("mikko"))
    names = [r[0] for r in sqlite3.connect(path).execute("SELECT data FROM guns")]
    assert len(names) == 1 and "Mikko Rifle" in names[0]


def test_login_rules_and_password_change(two):
    app, o, m = two
    c = app.test_client()
    assert c.post("/diary/login", data={"username": "mikko", "password": "osku-password-1"}).status_code == 200
    assert c.post("/diary/login", data={"username": "nobody", "password": "x" * 12}).status_code == 200
    # Change password: wrong current / too short refused; then other devices are logged out, this one isn't
    m2 = _login(app, "mikko", "mikko-password-1")
    assert m.put("/diary/api/account/password", json={"current": "wrong-password", "new": "x" * 12}).status_code == 403
    assert m.put("/diary/api/account/password", json={"current": "mikko-password-1", "new": "short"}).status_code == 400
    assert m.put("/diary/api/account/password", json={"current": "mikko-password-1", "new": "mikko-new-pass-2"}).status_code == 200
    assert m.get("/diary/api/guns").status_code == 200
    assert m2.get("/diary/api/guns").status_code == 401
    assert o.get("/diary/api/guns").status_code == 200            # other accounts untouched
    _login(app, "mikko", "mikko-new-pass-2")
    # Disabling an account cuts its existing logins too
    users.set_disabled("mikko", True)
    assert m.get("/diary/api/guns").status_code == 401
    assert app.test_client().post("/diary/login", data={"username": "mikko", "password": "mikko-new-pass-2"}).status_code == 200
    users.set_disabled("mikko", False)


def test_single_user_diary_becomes_an_account(tmp_path, monkeypatch):
    """A diary from before accounts (diary.db + one password) turns into the admin account 'osku' with the
    same password and data on first start."""
    monkeypatch.setattr(diary, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(diary, "DB_PATH", str(tmp_path / "diary.db"))
    diary.init_db()
    conn = diary.connect()
    diary.kv_set("password_hash", generate_password_hash("old-single-pass"), conn)
    diary.upsert_gun(conn, "g-old", {"name": "Old Rifle"})
    conn.commit()
    conn.close()
    app = _app()
    u = users.by_name("osku")
    assert u and u["is_admin"] and u["db_file"] == "diary.db"
    c = _login(app, "osku", "old-single-pass")
    assert [g["name"] for g in c.get("/diary/api/guns").get_json()] == ["Old Rifle"]
    # Next account gets its own file
    assert users.create("buddy", "buddy-password")["db_file"] == os.path.join("users", "buddy.db")


def test_new_account_chooses_password_on_first_login():
    """user-add without a password: empty password once → must choose one → logged in with it."""
    for n in ("newbie", "late"):
        if users.by_name(n):
            with users.uconnect() as c:
                c.execute("DELETE FROM users WHERE username=?", (n,))
    users.create("newbie")
    app = _app()
    c = app.test_client()
    # Not logged in yet: the empty password only opens the "choose your password" step
    r = c.post("/diary/login", data={"username": "newbie", "password": ""})
    assert r.status_code == 200 and b"Choose your password" in r.data
    assert c.get("/diary/api/guns").status_code == 401
    r = c.post("/diary/first-password", data={"password": "short", "password2": "short"})
    assert b"at least 10" in r.data and c.get("/diary/api/guns").status_code == 401
    r = c.post("/diary/first-password", data={"password": "newbie-pass-1", "password2": "other-pass-11"})
    assert b"match" in r.data
    r = c.post("/diary/first-password", data={"password": "newbie-pass-1", "password2": "newbie-pass-1"})
    assert r.status_code == 302 and c.get("/diary/api/account").get_json()["username"] == "newbie"
    # From now on the empty password is just wrong, the chosen one works
    assert b"Choose your password" not in app.test_client().post(
        "/diary/login", data={"username": "newbie", "password": ""}).data
    _login(app, "newbie", "newbie-pass-1")
    # Without the setup step, /first-password does nothing
    assert app.test_client().post("/diary/first-password", data={"password": "x" * 12, "password2": "x" * 12}).status_code == 302
    assert users.verify("newbie", "x" * 12) is None
    # The window closes: an unclaimed account can't be taken with an empty password after SETUP_DAYS
    users.create("late", setup_days=-1)
    r = app.test_client().post("/diary/login", data={"username": "late", "password": ""})
    assert b"Choose your password" not in r.data
    # --clear: back to choosing a new one, and existing logins end
    n = _login(app, "newbie", "newbie-pass-1")
    users.clear_password("newbie")
    assert n.get("/diary/api/guns").status_code == 401
    assert b"Choose your password" in app.test_client().post("/diary/login", data={"username": "newbie", "password": ""}).data


def test_admin_manages_accounts_in_the_app(two):
    app, o, m = two
    for n in ("buddy2",):
        if users.by_name(n):
            with users.uconnect() as c:
                c.execute("DELETE FROM users WHERE username=?", (n,))
    # Only admins
    assert m.get("/diary/api/admin/users").status_code == 403
    assert m.post("/diary/api/admin/users", json={"username": "sneaky"}).status_code == 403
    assert users.by_name("sneaky") is None
    assert m.get("/diary/api/home").get_json()["is_admin"] is False
    assert o.get("/diary/api/home").get_json()["is_admin"] is True
    # Add: no password, first login chooses it
    r = o.post("/diary/api/admin/users", json={"username": "Buddy2"})
    assert r.status_code == 200 and r.get_json()["user"]["setup_open"] is True
    assert o.post("/diary/api/admin/users", json={"username": "buddy2"}).status_code == 400      # exists
    assert o.post("/diary/api/admin/users", json={"username": "x"}).status_code == 400           # bad name
    assert o.post("/diary/api/admin/users", data="username=evil").status_code == 415             # JSON only (CSRF)
    names = [u["username"] for u in o.get("/diary/api/admin/users").get_json()]
    assert "buddy2" in names and "osku" in names
    assert b"Choose your password" in app.test_client().post("/diary/login", data={"username": "buddy2", "password": ""}).data
    # Disable / reset someone else, never yourself
    assert o.post("/diary/api/admin/users/osku/disable", json={}).status_code == 400
    assert o.post("/diary/api/admin/users/mikko/disable", json={}).status_code == 200
    assert m.get("/diary/api/guns").status_code == 401
    assert o.post("/diary/api/admin/users/mikko/enable", json={}).status_code == 200
    assert o.post("/diary/api/admin/users/mikko/clear-password", json={}).get_json()["user"]["setup_open"] is True
    assert users.verify("mikko", "mikko-password-1") is None
    assert o.post("/diary/api/admin/users/nobody/disable", json={}).status_code == 404
    assert o.post("/diary/api/admin/users/mikko/explode", json={}).status_code == 404
