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
