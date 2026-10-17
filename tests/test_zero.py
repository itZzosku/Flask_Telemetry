"""Scope zeroes: one-time note import, swap arithmetic, the API and backups. Synthetic data only,
plus one check that the note committed in notes/ still agrees with itself."""
import glob
import os

import pytest

import diary
from diary import zero, zero_note as zn

NOTE = """# Test Scope — Scope Zeroing Note

Scope: **Test Scope**, shared between two rifles. All values in **MRAD**.

**Reference: Rimfire = 0 / 0** (lowest elevation of the two). Zero stop is set **0.3 MRAD below** the Rimfire zero, so the Rimfire zero sits 0.3 up from the hard stop.

| Rifle | Caliber | Zero distance | Rail / mount |
|---|---|---|---|
| Rimfire | .22 LR | 50 m | **20 MOA canted rail** |
| AB-10 | .308 Win | 100 m | Flat rail |

---

## Dial positions (primary reference)

| Rifle / load | Elevation dial | Windage dial |
|---|---|---|
| **Rimfire** (reference) | **0** | **0** |
| AB-10 — 150 gr FMJ | **+2.3** | **1.1 LEFT** |
| AB-10 — 175 gr SMK | **+2.0** | **0.4 RIGHT** |

## Swap adjustments (derived from dial positions)

| Swap | Elevation | Windage |
|---|---|---|
| Rimfire → AB-10 (150 gr) | UP 2.3 | 1.1 LEFT |
| AB-10 (175 gr) → Rimfire | DOWN 2.0 | 0.4 LEFT |

**AB-10 load change (scope stays on):**

| Change | Elevation | Windage |
|---|---|---|
| 150 gr FMJ → 175 gr SMK | DOWN 0.3 | 1.5 RIGHT |

---

## Notes

- Something learned.

## To-do

- [ ] Check it

*Last updated: 2026-10-01 — both rifles zeroed.*
"""


# ------------------------------------------------------------------ pure functions
def test_parse_note():
    p = zn.parse_note(NOTE)
    s = p["scope"]
    assert s["name"] == "Test Scope" and s["unit"] == "MRAD" and s["click"] == 0.1
    assert s["zero_stop"] == -0.3 and s["updated"] == "2026-10-01" and s["status"] == "both rifles zeroed"
    assert s["notes"].startswith("## Notes") and "- [ ] Check it" in s["notes"] and "Last updated" not in s["notes"]
    z = p["zeros"]
    assert [x["rifle"] for x in z] == ["Rimfire", "AB-10", "AB-10"] and z[0]["is_ref"] and not z[1]["is_ref"]
    assert (z[1]["label"], z[1]["elev"], z[1]["wind"]) == ("150 gr FMJ", 2.3, -1.1)
    assert (z[2]["elev"], z[2]["wind"], z[2]["cal"], z[2]["distance_m"]) == (2.0, 0.4, ".308 Win", 100.0)
    assert z[0]["mount"] == "20 MOA canted rail"
    chk = zn.check_swaps(z, p["swaps"])
    assert chk["ok"] == 3 and chk["bad"] == []


def test_check_swaps_flags_a_wrong_row():
    p = zn.parse_note(NOTE.replace("| Rimfire → AB-10 (150 gr) | UP 2.3 |", "| Rimfire → AB-10 (150 gr) | UP 2.5 |"))
    chk = zn.check_swaps(p["zeros"], p["swaps"])
    assert chk["ok"] == 2 and len(chk["bad"]) == 1 and "UP 2.5" in chk["bad"][0]


def test_formatting_and_swap():
    assert zn.elev_dial(0.04) == "0" and zn.elev_dial(1.8) == "+1.8" and zn.elev_dial(-1.3) == "−1.3"
    assert zn.wind_dial(-3.6) == "3.6 LEFT" and zn.wind_dial(0.5) == "0.5 RIGHT"
    s = zn.swap({"elev": 4.5, "wind": -3.7}, {"elev": 1.8, "wind": -3.6})
    assert (s["elev"], s["wind"], s["elev_clicks"], s["wind_clicks"]) == ("DOWN 2.7", "0.1 RIGHT", 27, 1)
    assert zn.parse_wind("3.6 L") == -3.6 and zn.parse_elev("DOWN 2.7") == -2.7 and zn.parse_elev("**+1.8**") == 1.8
    assert zn.decimals(0.25) == 2 and zn.fmt(1.5, 0.25) == "1.50"


def test_repo_notes_are_consistent():
    """Every zeroing note in notes/ parses and its swap tables match its dial positions."""
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "notes")
    for path in glob.glob(os.path.join(root, "*.md")):
        with open(path, encoding="utf-8") as f:
            md = f.read()
        if "Scope Zeroing Note" not in md:
            continue
        p = zn.parse_note(md)
        assert p["zeros"], path
        chk = zn.check_swaps(p["zeros"], p["swaps"], p["scope"]["click"])
        assert chk["bad"] == [], (path, chk["bad"])


# ------------------------------------------------------------------ API
@pytest.fixture()
def client():
    from flask import Flask
    from werkzeug.security import generate_password_hash
    app = Flask(__name__)
    diary.init_app(app)
    conn = diary.connect()
    conn.executescript("DELETE FROM scope_log; DELETE FROM scope_zeros; DELETE FROM scopes; DELETE FROM guns;")
    diary.kv_set("password_hash", generate_password_hash("test-password-1"), conn)
    diary.upsert_gun(conn, "g-ab10", {"name": "Maker AB-10", "cal": ".308 Win"})
    diary.upsert_gun(conn, "g-rim", {"name": "Rimfire", "cal": ".22 LR"})
    conn.commit()
    conn.close()
    c = app.test_client()
    assert c.post("/diary/login", data={"password": "test-password-1"}).status_code == 302
    return c


H = {"X-Requested-With": "fetch"}


def _import(replace=False):
    conn = diary.connect()
    try:
        return zero.import_note(conn, NOTE, replace)
    finally:
        conn.commit()
        conn.close()


def _scope(c):
    return c.get("/diary/api/zero").get_json()[0]


def test_api_import_adjust_undo_mount_check(client):
    assert client.get("/diary/zeroes").status_code == 200
    assert client.get("/diary/zero").headers["Location"].endswith("/diary/zeroes")
    r = _import()
    assert r["zeros"] == 3 and r["check"]["ok"] == 3 and r["links"]["AB-10 — 150 gr FMJ"]["gun_id"] == "g-ab10"
    with pytest.raises(ValueError, match="exists"):
        _import()
    assert _import(replace=True)["created"] is False
    s = _scope(client)
    ab = next(z for z in s["zeros"] if z["label"] == "150 gr FMJ")
    assert s["ref_zero_id"] == s["zeros"][0]["id"] and s["zero_stop"] == -0.3

    # Range correction: UP 0.2, 0.1 RIGHT
    r = client.post(f"/diary/api/zero/zeros/{ab['id']}/adjust", json={"d_elev": 0.2, "d_wind": 0.1, "date": "2026-10-20"})
    assert r.get_json()["result"] == {"elev": 2.5, "wind": -1.0}
    assert client.post(f"/diary/api/zero/zeros/{ab['id']}/adjust", json={"d_elev": 0, "d_wind": 0}).status_code == 400
    s = _scope(client)
    ab = next(z for z in s["zeros"] if z["id"] == ab["id"])
    assert s["updated"] == "2026-10-20" and ab["undo_id"]
    assert s["log"][0]["kind"] == "adjust" and (s["log"][0]["d_elev"], s["log"][0]["d_wind"]) == (0.2, 0.1)

    # Undo restores the old position
    assert client.post(f"/diary/api/zero/log/{ab['undo_id']}/undo", json={}).status_code == 200
    ab = next(z for z in _scope(client)["zeros"] if z["id"] == ab["id"])
    assert (ab["elev"], ab["wind"]) == (2.3, -1.1)

    # Mount + check shot
    assert client.post(f"/diary/api/zero/scopes/{s['id']}/mount", json={"zero_id": ab["id"]}).status_code == 200
    assert client.post(f"/diary/api/zero/zeros/{ab['id']}/check", json={"ok": True}).status_code == 200
    s = _scope(client)
    assert s["mounted_zero_id"] == ab["id"] and next(z for z in s["zeros"] if z["id"] == ab["id"])["checks"]["n"] == 1


def test_api_set_rebase_and_delete(client):
    _import()
    s = _scope(client)
    rim, ab, smk = s["zeros"]
    # Editing the dial value logs a 'set'
    body = dict(ab, elev=2.4, wind=-1.2)
    assert client.put(f"/diary/api/zero/zeros/{ab['id']}", json=body).status_code == 200
    s = _scope(client)
    assert s["log"][0]["kind"] == "set" and s["log"][0]["prev_elev"] == 2.3
    # New reference: everything shifts, swaps stay the same
    before = zn.swap(next(z for z in s["zeros"] if z["id"] == rim["id"]), next(z for z in s["zeros"] if z["id"] == smk["id"]))
    assert client.post(f"/diary/api/zero/scopes/{s['id']}/reference", json={"zero_id": ab["id"]}).status_code == 200
    s = _scope(client)
    zs = {z["id"]: z for z in s["zeros"]}
    assert (zs[ab["id"]]["elev"], zs[ab["id"]]["wind"]) == (0, 0) and s["ref_zero_id"] == ab["id"]
    assert (zs[rim["id"]]["elev"], zs[rim["id"]]["wind"]) == (-2.4, 1.2) and s["zero_stop"] == -2.7
    assert zn.swap(zs[rim["id"]], zs[smk["id"]]) == before
    # Validation and CSRF guards
    assert client.put(f"/diary/api/zero/zeros/{ab['id']}", json=dict(ab, rifle="")).status_code == 400
    assert client.put(f"/diary/api/zero/zeros/{ab['id']}", json=dict(ab, elev=500)).status_code == 400
    assert client.put(f"/diary/api/zero/zeros/{ab['id']}", data="x").status_code == 415
    assert client.delete(f"/diary/api/zero/zeros/{smk['id']}").status_code == 403
    assert client.delete(f"/diary/api/zero/zeros/{smk['id']}", headers=H).status_code == 200
    assert len(_scope(client)["zeros"]) == 2
    assert client.delete(f"/diary/api/zero/scopes/{s['id']}", headers=H).status_code == 200
    assert client.get("/diary/api/zero").get_json() == []


def test_new_scope_and_position(client):
    assert client.put("/diary/api/zero/scopes/s-new", json={"name": "New", "unit": "MOA", "zero_stop": -1}).status_code == 200
    assert client.put("/diary/api/zero/zeros/z-a", json={"scope_id": "s-new", "rifle": "Rimfire", "gun_id": "g-rim",
                                                         "elev": 0.6, "wind": -0.3}).status_code == 200
    s = _scope(client)
    assert s["click"] == 0.25 and s["ref_zero_id"] == "z-a" and s["log"][0]["kind"] == "new"
    assert (s["zeros"][0]["elev"], s["zeros"][0]["wind"]) == (0.5, -0.25)      # snapped to the 0.25 MOA click


def test_backup_round_trip(client):
    _import()
    conn = diary.connect()
    data = zero.export_rows(conn)
    assert len(data["scopes"]) == 1 and len(data["scope_zeros"]) == 3 and len(data["scope_log"]) == 3
    conn.executescript("DELETE FROM scope_log; DELETE FROM scope_zeros; DELETE FROM scopes;")
    assert zero.import_rows(conn, data, replace=True) == {"scopes": 1, "scope_zeros": 3, "scope_log": 3}
    conn.commit()
    conn.close()
    s = _scope(client)
    assert s["name"] == "Test Scope" and len(s["zeros"]) == 3


def test_pages_and_home_summary(client):
    for path in ("/diary/", "/diary/log", "/diary/velocity", "/diary/zeroes"):
        assert client.get(path).status_code == 200, path
    assert client.get("/diary/chrono?shared=2").headers["Location"].endswith("/diary/velocity?shared=2")
    _import()
    conn = diary.connect()
    s = zero.full(conn, zero.scope_rows(conn)[0]["id"])
    zero.mount(conn, s["id"], s["zeros"][1]["id"])
    conn.commit()
    conn.close()
    h = client.get("/diary/api/home").get_json()
    assert h["zeroes"] == [{"name": "Test Scope", "positions": 3, "on": "AB-10 — 150 gr FMJ", "updated": h["zeroes"][0]["updated"]}]
    assert h["velocity"]["strings"] >= 0 and "visits" in h["diary"]
    import flask
    app = flask.Flask(__name__)
    diary.init_app(app)
    anon = app.test_client()
    assert anon.get("/diary/").status_code == 302 and anon.get("/diary/api/home").status_code == 401


def test_second_scope_takes_the_rifle(client):
    """Two scopes: mounting B on a rifle that carries A takes A off it; other rifles are untouched."""
    _import()
    a = _scope(client)
    ab_a = next(z for z in a["zeros"] if z["label"] == "150 gr FMJ")
    assert client.post(f"/diary/api/zero/scopes/{a['id']}/mount", json={"zero_id": ab_a["id"]}).status_code == 200
    assert client.put("/diary/api/zero/scopes/s-b", json={"name": "Second", "unit": "MRAD"}).status_code == 200
    client.put("/diary/api/zero/zeros/z-b1", json={"scope_id": "s-b", "rifle": "AB-10", "gun_id": "g-ab10", "elev": 0, "wind": 0})
    client.put("/diary/api/zero/zeros/z-b2", json={"scope_id": "s-b", "rifle": "Rimfire", "gun_id": "g-rim", "elev": 1, "wind": 0})
    r = client.post("/diary/api/zero/scopes/s-b/mount", json={"zero_id": "z-b2"}).get_json()
    assert r["result"]["unmounted"] == []                                   # Rimfire was free
    r = client.post("/diary/api/zero/scopes/s-b/mount", json={"zero_id": "z-b1"}).get_json()
    assert r["result"]["unmounted"] == ["Test Scope"]
    scopes = {s["id"]: s for s in client.get("/diary/api/zero").get_json()}
    assert scopes[a["id"]]["mounted_zero_id"] is None and scopes["s-b"]["mounted_zero_id"] == "z-b1"
    assert scopes[a["id"]]["log"][0]["kind"] == "unmount"
    # Positions stay per scope
    assert len(scopes[a["id"]]["zeros"]) == 3 and len(scopes["s-b"]["zeros"]) == 2
    h = client.get("/diary/api/home").get_json()["zeroes"]
    assert {(z["name"], z["on"]) for z in h} == {("Test Scope", None), ("Second", "AB-10")}
