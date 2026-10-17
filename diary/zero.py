"""Scope zeroes: one scope moved between rifles, a dial position per rifle / load.

Tables in the diary database:
  scopes       the scope: unit, click value, zero stop, reference position, mounted position, notes
  scope_zeros  dial positions (elev + = up, wind + = right) linked to a diary gun and optionally a load
  scope_log    every change, swap and check shot; prev_* lets the last change of a position be undone

The old Markdown note (notes/javelin-zeroing-note.md) is brought in once with `python -m diary zero-import`;
after that the database is the only copy. The page prints a PDF zeroing card.
"""
import os
import re
import secrets
import time

from flask import abort, jsonify, redirect, request, send_from_directory, url_for

from . import DATE_RE, HERE, ID_RE, bp, db, login_required
from . import zero_note as zn

SCHEMA = """
CREATE TABLE IF NOT EXISTS scopes (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'MRAD', click REAL NOT NULL DEFAULT 0.1,
    zero_stop REAL, ref_zero_id TEXT, mounted_zero_id TEXT, notes TEXT, status TEXT, updated TEXT,
    ord INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS scope_zeros (
    id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, rifle TEXT NOT NULL, gun_id TEXT, label TEXT, load_id TEXT,
    cal TEXT, distance_m REAL, mount TEXT, elev REAL NOT NULL DEFAULT 0, wind REAL NOT NULL DEFAULT 0,
    ord INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0, updated TEXT);
CREATE INDEX IF NOT EXISTS scope_zeros_scope ON scope_zeros(scope_id);
CREATE TABLE IF NOT EXISTS scope_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scope_id TEXT NOT NULL, zero_id TEXT, date TEXT, kind TEXT NOT NULL,
    elev REAL, wind REAL, d_elev REAL, d_wind REAL, prev_elev REAL, prev_wind REAL, ok INTEGER, note TEXT,
    created TEXT);
CREATE INDEX IF NOT EXISTS scope_log_scope ON scope_log(scope_id, id);
"""
UNITS = ("MRAD", "MOA")
LIMIT = 100.0          # no turret dials 100 MRAD / MOA from its reference


def init_zero(conn):
    conn.executescript(SCHEMA)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _today():
    return time.strftime("%Y-%m-%d")


def _slug(text, prefix):
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(text)).strip("-").lower()
    return (prefix + s)[:56].rstrip("-") + "-" + secrets.token_hex(2)


# ------------------------------------------------------------------ reads
def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args)]


def scope_rows(conn):
    return _rows(conn, "SELECT * FROM scopes ORDER BY archived, ord, name")


def zero_rows(conn, scope_id):
    return _rows(conn, "SELECT * FROM scope_zeros WHERE scope_id=? ORDER BY archived, ord, rowid", (scope_id,))


def _load_labels(conn):
    """load id → short load name (no calibre / brass): the rifle is already in the position's name."""
    from .chrono_loads import short_name
    try:
        return {r["id"]: short_name(dict(r)) for r in conn.execute("SELECT * FROM loads")}
    except Exception:                      # velocity tables not created yet
        return {}


def display_name(z, loads):
    """'T3X Super Varmint — GGG 55 gr': the typed label, else the linked velocity load, else just the rifle."""
    label = z.get("label") or (loads.get(z.get("load_id")) if z.get("load_id") else None)
    return zn.position_name(dict(rifle=z.get("rifle"), label=label))


def log_rows(conn, scope_id, limit=200, loads=None):
    loads = _load_labels(conn) if loads is None else loads
    rows = _rows(conn, "SELECT l.*, z.rifle, z.label, z.load_id FROM scope_log l LEFT JOIN scope_zeros z ON z.id=l.zero_id "
                       "WHERE l.scope_id=? ORDER BY l.date DESC, l.id DESC LIMIT ?", (scope_id, limit))
    for r in rows:
        r["position"] = display_name(r, loads) if r.get("rifle") else ""
    return rows


def full(conn, scope_id):
    s = conn.execute("SELECT * FROM scopes WHERE id=?", (scope_id,)).fetchone()
    if not s:
        return None
    s = dict(s)
    loads = _load_labels(conn)
    s["zeros"] = zero_rows(conn, scope_id)
    s["log"] = log_rows(conn, scope_id, loads=loads)
    last = {}
    for e in s["log"]:                         # newest first: the first entry per position may be undone
        if e["zero_id"] and e["zero_id"] not in last and e["kind"] in ("adjust", "set", "check", "new"):
            last[e["zero_id"]] = e["id"]
    checks = {}
    for e in s["log"]:
        if e["kind"] == "check" and e["zero_id"]:
            c = checks.setdefault(e["zero_id"], {"n": 0, "last": None, "ok": None})
            c["n"] += 1
            if c["last"] is None:
                c["last"], c["ok"] = e["date"], e["ok"]
    for z in s["zeros"]:
        z["name"] = display_name(z, loads)
        z["undo_id"] = last.get(z["id"])
        z["checks"] = checks.get(z["id"], {"n": 0, "last": None, "ok": None})
    return s


# ------------------------------------------------------------------ writes
def _log(conn, scope_id, zero_id, kind, date=None, **kw):
    cols = ["scope_id", "zero_id", "date", "kind", "created"] + list(kw)
    conn.execute(f"INSERT INTO scope_log({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                 [scope_id, zero_id, date or _today(), kind, _now(), *kw.values()])
    conn.execute("UPDATE scopes SET updated=? WHERE id=? AND (updated IS NULL OR updated<?)",
                 (date or _today(), scope_id, date or _today()))


def _num(v, lo=-LIMIT, hi=LIMIT, name="value"):
    if v is None or v == "":
        return None
    try:
        v = float(str(v).replace(",", ".").replace(zn.MINUS, "-"))
    except ValueError:
        raise ValueError("bad_" + name)
    if not lo <= v <= hi:
        raise ValueError("bad_" + name)
    return v


def _txt(v, n=120):
    v = " ".join(str(v or "").split())[:n]
    return v or None


def _date(v):
    v = str(v or "").strip()
    if not v:
        return _today()
    if not DATE_RE.match(v):
        raise ValueError("bad_date")
    return v


def save_scope(conn, sid, b):
    unit = b.get("unit") if b.get("unit") in UNITS else "MRAD"
    vals = {"name": _txt(b.get("name"), 60), "unit": unit,
            "click": _num(b.get("click"), 0.01, 5, "click") or (0.25 if unit == "MOA" else 0.1),
            "zero_stop": _num(b.get("zero_stop"), name="zero_stop"),
            "notes": str(b.get("notes") or "")[:20000] or None, "status": _txt(b.get("status"), 200),
            "ord": int(b.get("ord") or 0), "archived": 1 if b.get("archived") else 0}
    if not vals["name"]:
        raise ValueError("name_required")
    if conn.execute("SELECT 1 FROM scopes WHERE id=?", (sid,)).fetchone():
        conn.execute("UPDATE scopes SET " + ",".join(f"{k}=?" for k in vals) + " WHERE id=?", (*vals.values(), sid))
    else:
        vals.update(id=sid, updated=_today())
        conn.execute(f"INSERT INTO scopes({','.join(vals)}) VALUES({','.join('?' * len(vals))})", list(vals.values()))
    return sid


def save_zero(conn, zid, b):
    """Create or edit a position. A changed elev / wind is logged as 'set' (or 'new')."""
    old = conn.execute("SELECT * FROM scope_zeros WHERE id=?", (zid,)).fetchone()
    scope_id = old["scope_id"] if old else b.get("scope_id")
    s = conn.execute("SELECT * FROM scopes WHERE id=?", (scope_id,)).fetchone()
    if not s:
        raise ValueError("bad_scope")
    vals = {"rifle": _txt(b.get("rifle"), 60), "gun_id": _txt(b.get("gun_id"), 64),
            "label": _txt(b.get("label"), 80), "load_id": _txt(b.get("load_id"), 64), "cal": _txt(b.get("cal"), 40),
            "distance_m": _num(b.get("distance_m"), 1, 3000, "distance_m"), "mount": _txt(b.get("mount"), 80),
            "archived": 1 if b.get("archived") else 0}
    if not vals["rifle"]:
        raise ValueError("rifle_required")
    elev, wind = _num(b.get("elev"), name="elev"), _num(b.get("wind"), name="wind")
    elev = zn.rnd(elev if elev is not None else (old["elev"] if old else 0), s["click"])
    wind = zn.rnd(wind if wind is not None else (old["wind"] if old else 0), s["click"])
    date, note = _date(b.get("date")), _txt(b.get("note"), 300)
    if old:
        conn.execute("UPDATE scope_zeros SET " + ",".join(f"{k}=?" for k in vals) + ",elev=?,wind=?,updated=? WHERE id=?",
                     (*vals.values(), elev, wind, date, zid))
        if not (zn.is_zero(elev - old["elev"], s["click"]) and zn.is_zero(wind - old["wind"], s["click"])):
            _log(conn, scope_id, zid, "set", date, elev=elev, wind=wind, d_elev=zn.rnd(elev - old["elev"], s["click"]),
                 d_wind=zn.rnd(wind - old["wind"], s["click"]), prev_elev=old["elev"], prev_wind=old["wind"], note=note)
    else:
        ordv = conn.execute("SELECT COALESCE(MAX(ord),0)+1 FROM scope_zeros WHERE scope_id=?", (scope_id,)).fetchone()[0]
        conn.execute("INSERT INTO scope_zeros(id,scope_id," + ",".join(vals) + ",elev,wind,ord,updated) VALUES("
                     + ",".join("?" * (len(vals) + 6)) + ")", (zid, scope_id, *vals.values(), elev, wind, ordv, date))
        _log(conn, scope_id, zid, "new", date, elev=elev, wind=wind, note=note)
    _drop_moved_reference(conn, scope_id, date)
    return zid


def _drop_moved_reference(conn, scope_id, date):
    """A reference position that no longer reads 0 / 0 isn't one: the turret's own zero becomes the
    reference (a virtual point no rifle has to match). Values stay as they are – they are dial readings."""
    s = conn.execute("SELECT s.ref_zero_id, s.click, z.elev, z.wind, z.rifle, z.label FROM scopes s "
                     "JOIN scope_zeros z ON z.id=s.ref_zero_id WHERE s.id=?", (scope_id,)).fetchone()
    if s and not (zn.is_zero(s["elev"], s["click"]) and zn.is_zero(s["wind"], s["click"])):
        set_turret_reference(conn, scope_id, date, note=f"{zn.position_name(dict(s))} moved off 0 / 0")


def set_turret_reference(conn, scope_id, date=None, note=None):
    """0 / 0 = the turret's own zero, not any rifle. Nothing shifts."""
    if not conn.execute("SELECT 1 FROM scopes WHERE id=?", (scope_id,)).fetchone():
        return None
    conn.execute("UPDATE scopes SET ref_zero_id=NULL WHERE id=?", (scope_id,))
    _log(conn, scope_id, None, "reference", _date(date), d_elev=0, d_wind=0, note=note)
    return True


def adjust(conn, zid, d_elev, d_wind, date=None, note=None):
    """Dialled correction at the range: the position moves by (d_elev, d_wind)."""
    z = conn.execute("SELECT z.*, s.click FROM scope_zeros z JOIN scopes s ON s.id=z.scope_id WHERE z.id=?",
                     (zid,)).fetchone()
    if not z:
        return None
    de, dw = zn.rnd(d_elev or 0, z["click"]), zn.rnd(d_wind or 0, z["click"])
    if zn.is_zero(de, z["click"]) and zn.is_zero(dw, z["click"]):
        raise ValueError("no_change")
    elev, wind = zn.rnd(z["elev"] + de, z["click"]), zn.rnd(z["wind"] + dw, z["click"])
    if abs(elev) > LIMIT or abs(wind) > LIMIT:
        raise ValueError("bad_value")
    date = _date(date)
    conn.execute("UPDATE scope_zeros SET elev=?, wind=?, updated=? WHERE id=?", (elev, wind, date, zid))
    _log(conn, z["scope_id"], zid, "adjust", date, elev=elev, wind=wind, d_elev=de, d_wind=dw,
         prev_elev=z["elev"], prev_wind=z["wind"], note=_txt(note, 300))
    _drop_moved_reference(conn, z["scope_id"], date)
    return {"elev": elev, "wind": wind}


def check(conn, zid, ok=True, date=None, note=None):
    z = conn.execute("SELECT * FROM scope_zeros WHERE id=?", (zid,)).fetchone()
    if not z:
        return None
    _log(conn, z["scope_id"], zid, "check", _date(date), elev=z["elev"], wind=z["wind"], ok=1 if ok else 0,
         note=_txt(note, 300))
    return True


def mount(conn, scope_id, zid, date=None, note=None):
    """The scope went onto this position's rifle (logged, shown as 'now on')."""
    z = conn.execute("SELECT * FROM scope_zeros WHERE id=? AND scope_id=?", (zid, scope_id)).fetchone()
    if not z:
        return None
    date = _date(date)
    # A rifle carries one scope: any other scope recorded on the same rifle comes off it.
    off = []
    for o in conn.execute("SELECT s.id, s.name, z.id AS zid, z.gun_id, z.rifle FROM scopes s JOIN scope_zeros z "
                          "ON z.id=s.mounted_zero_id WHERE s.id<>?", (scope_id,)).fetchall():
        same = (o["gun_id"] == z["gun_id"]) if (o["gun_id"] and z["gun_id"]) else \
            (o["rifle"] or "").casefold() == (z["rifle"] or "").casefold()
        if same:
            conn.execute("UPDATE scopes SET mounted_zero_id=NULL WHERE id=?", (o["id"],))
            _log(conn, o["id"], o["zid"], "unmount", date, note=f"{z['rifle']} now carries {_scope_name(conn, scope_id)}")
            off.append(o["name"])
    conn.execute("UPDATE scopes SET mounted_zero_id=? WHERE id=?", (zid, scope_id))
    _log(conn, scope_id, zid, "swap", date, elev=z["elev"], wind=z["wind"], note=_txt(note, 300))
    return {"unmounted": off}


def _scope_name(conn, sid):
    r = conn.execute("SELECT name FROM scopes WHERE id=?", (sid,)).fetchone()
    return r["name"] if r else "another scope"


def rebase(conn, scope_id, zid, date=None):
    """Make a position the 0 / 0 reference: every position and the zero stop shift by its values.
    Only the turret labels change — no scope is physically moved."""
    s = conn.execute("SELECT * FROM scopes WHERE id=?", (scope_id,)).fetchone()
    z = conn.execute("SELECT * FROM scope_zeros WHERE id=? AND scope_id=?", (zid, scope_id)).fetchone()
    if not s or not z:
        return None
    de, dw, c = z["elev"], z["wind"], s["click"]
    for r in conn.execute("SELECT id, elev, wind FROM scope_zeros WHERE scope_id=?", (scope_id,)).fetchall():
        conn.execute("UPDATE scope_zeros SET elev=?, wind=? WHERE id=?",
                     (zn.rnd(r["elev"] - de, c), zn.rnd(r["wind"] - dw, c), r["id"]))
    zs = None if s["zero_stop"] is None else zn.rnd(s["zero_stop"] - de, c)
    conn.execute("UPDATE scopes SET ref_zero_id=?, zero_stop=? WHERE id=?", (zid, zs, scope_id))
    # History stays readable in the new frame
    conn.execute("UPDATE scope_log SET elev=elev-?, wind=wind-?, prev_elev=prev_elev-?, prev_wind=prev_wind-? "
                 "WHERE scope_id=?", (de, dw, de, dw, scope_id))
    _log(conn, scope_id, zid, "reference", _date(date), elev=0, wind=0, d_elev=de, d_wind=dw)
    return True


def undo(conn, log_id):
    """Undo the newest adjust / set / check / new of a position."""
    e = conn.execute("SELECT * FROM scope_log WHERE id=?", (log_id,)).fetchone()
    if not e or e["kind"] not in ("adjust", "set", "check", "new"):
        return None
    newer = conn.execute("SELECT 1 FROM scope_log WHERE zero_id=? AND kind IN ('adjust','set','check','new') "
                         "AND (date>? OR (date=? AND id>?))", (e["zero_id"], e["date"], e["date"], e["id"])).fetchone()
    if newer:
        raise ValueError("not_latest")
    if e["kind"] == "new":
        _delete_zero(conn, e["zero_id"])
        return True
    if e["kind"] in ("adjust", "set"):
        conn.execute("UPDATE scope_zeros SET elev=?, wind=? WHERE id=?", (e["prev_elev"], e["prev_wind"], e["zero_id"]))
    conn.execute("DELETE FROM scope_log WHERE id=?", (log_id,))
    return True


def _delete_zero(conn, zid):
    conn.execute("DELETE FROM scope_zeros WHERE id=?", (zid,))
    conn.execute("DELETE FROM scope_log WHERE zero_id=?", (zid,))
    conn.execute("UPDATE scopes SET ref_zero_id=NULL WHERE ref_zero_id=?", (zid,))
    conn.execute("UPDATE scopes SET mounted_zero_id=NULL WHERE mounted_zero_id=?", (zid,))


def delete_scope(conn, sid):
    for t, col in (("scope_log", "scope_id"), ("scope_zeros", "scope_id"), ("scopes", "id")):
        conn.execute(f"DELETE FROM {t} WHERE {col}=?", (sid,))


# ------------------------------------------------------------------ import the Markdown note
def _guess_load(conn, gun_id, label):
    """'GGG 55 gr' → the one load this gun has fired with a 55 gr bullet (velocity data), if exactly one."""
    if not gun_id or not label:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*gr\b", label, re.I)
    if not m:
        return None
    try:
        rows = conn.execute("SELECT DISTINCT l.id FROM loads l JOIN chrono_strings s ON s.load_id=l.id "
                            "WHERE s.gun_id=? AND abs(l.bullet_gr-?)<0.6", (gun_id, float(m.group(1).replace(",", ".")))).fetchall()
    except Exception:            # no velocity tables yet
        return None
    return rows[0][0] if len(rows) == 1 else None


def import_note(conn, md, replace=False):
    """Create (or with replace=True, overwrite) a scope from the Markdown note.
    Returns {"scope_id", "created", "zeros", "check": {"ok", "bad"}, "links": {...}}."""
    from .chrono import resolve_gun
    p = zn.parse_note(md)
    sc = p["scope"]
    if not sc["name"]:
        raise ValueError("No scope name (# <Scope> — Scope Zeroing Note) found.")
    old = conn.execute("SELECT id FROM scopes WHERE lower(name)=lower(?)", (sc["name"],)).fetchone()
    if old and not replace:
        raise ValueError(f"exists:{old['id']}")
    sid = old["id"] if old else _slug(sc["name"], "s-")
    if old:
        delete_scope(conn, sid)
    date = sc["updated"] or _today()
    conn.execute("INSERT INTO scopes(id,name,unit,click,zero_stop,notes,status,updated) VALUES(?,?,?,?,?,?,?,?)",
                 (sid, sc["name"], sc["unit"], sc["click"], sc["zero_stop"], sc["notes"] or None, sc["status"], date))
    links, ref = {}, None
    for i, z in enumerate(p["zeros"]):
        zid = _slug(zn.position_name(z), "z-")
        gun = resolve_gun(conn, [z["rifle"]])
        load = _guess_load(conn, gun, z.get("label"))
        links[zn.position_name(z)] = {"gun_id": gun, "load_id": load}
        conn.execute("INSERT INTO scope_zeros(id,scope_id,rifle,gun_id,label,load_id,cal,distance_m,mount,elev,wind,ord,updated)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (zid, sid, z["rifle"], gun, z.get("label"), load, z.get("cal"), z.get("distance_m"), z.get("mount"),
                      zn.rnd(z["elev"], sc["click"]), zn.rnd(z["wind"], sc["click"]), i, date))
        _log(conn, sid, zid, "import", date, elev=z["elev"], wind=z["wind"], note="Imported from the note")
        if z.get("is_ref"):
            ref = zid
    conn.execute("UPDATE scopes SET ref_zero_id=? WHERE id=?", (ref, sid))
    return {"scope_id": sid, "created": not old, "zeros": len(p["zeros"]),
            "check": zn.check_swaps(p["zeros"], p["swaps"], sc["click"]), "links": links}


# ------------------------------------------------------------------ backup
TABLES = ("scopes", "scope_zeros", "scope_log")


def export_rows(conn):
    return {t: _rows(conn, f"SELECT * FROM {t} ORDER BY rowid") for t in TABLES}


def import_rows(conn, data, replace=False):
    if replace and any(t in data for t in TABLES):
        conn.executescript("DELETE FROM scope_log; DELETE FROM scope_zeros; DELETE FROM scopes;")
    for t in TABLES:
        for row in data.get(t, []):
            cols = list(row)
            conn.execute(f"INSERT OR REPLACE INTO {t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                         [row[c] for c in cols])
    return {t: len(data.get(t, [])) for t in TABLES}


# ------------------------------------------------------------------ routes
@bp.get("/zeroes")
@login_required
def page_zero():
    resp = send_from_directory(os.path.join(HERE, "static"), "zero.html", max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.get("/zero")
def old_zero_url():
    return redirect(url_for("diary.page_zero"), code=301)


def _body():
    if not request.is_json:              # JSON only: a cross-site form can't send it (CSRF)
        abort(415)
    b = request.get_json(silent=True)
    if not isinstance(b, dict):
        abort(400)
    return b


def _id(v):
    if not ID_RE.match(v or ""):
        abort(400)
    return v


def _ok(fn):
    try:
        res = fn()
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if res is None:
        return jsonify(error="not_found"), 404
    db().commit()
    return jsonify(ok=True, result=res if res is not True else None)


@bp.get("/api/zero")
@login_required
def api_zero():
    conn = db()
    return jsonify([full(conn, s["id"]) for s in scope_rows(conn)])


@bp.put("/api/zero/scopes/<sid>")
@login_required
def api_put_scope(sid):
    b = _body()
    return _ok(lambda: save_scope(db(), _id(sid), b))


@bp.delete("/api/zero/scopes/<sid>")
@login_required
def api_delete_scope(sid):
    if request.headers.get("X-Requested-With") != "fetch":
        abort(403)
    return _ok(lambda: delete_scope(db(), _id(sid)) or True)


@bp.put("/api/zero/zeros/<zid>")
@login_required
def api_put_zero(zid):
    b = _body()
    return _ok(lambda: save_zero(db(), _id(zid), b))


@bp.delete("/api/zero/zeros/<zid>")
@login_required
def api_delete_zero(zid):
    if request.headers.get("X-Requested-With") != "fetch":
        abort(403)
    return _ok(lambda: _delete_zero(db(), _id(zid)) or True)


@bp.post("/api/zero/zeros/<zid>/adjust")
@login_required
def api_adjust(zid):
    b = _body()
    return _ok(lambda: adjust(db(), _id(zid), _num(b.get("d_elev"), name="d_elev"), _num(b.get("d_wind"), name="d_wind"),
                              b.get("date"), b.get("note")))


@bp.post("/api/zero/zeros/<zid>/check")
@login_required
def api_check(zid):
    b = _body()
    return _ok(lambda: check(db(), _id(zid), b.get("ok", True) is not False, b.get("date"), b.get("note")))


@bp.post("/api/zero/scopes/<sid>/mount")
@login_required
def api_mount(sid):
    b = _body()
    return _ok(lambda: mount(db(), _id(sid), _id(b.get("zero_id")), b.get("date"), b.get("note")))


@bp.post("/api/zero/scopes/<sid>/reference")
@login_required
def api_reference(sid):
    b = _body()
    if not b.get("zero_id"):                       # turret zero (virtual point) is the reference
        return _ok(lambda: set_turret_reference(db(), _id(sid), b.get("date")))
    return _ok(lambda: rebase(db(), _id(sid), _id(b.get("zero_id")), b.get("date")))


@bp.post("/api/zero/log/<int:log_id>/undo")
@login_required
def api_undo(log_id):
    _body()
    return _ok(lambda: undo(db(), log_id))
