"""Velocity data (Garmin Xero / own logs / Kestrel) for the shooting diary.

Tables live in the diary's SQLite database. Statistics are computed on read from included shots,
so excluding a shot or editing a link never leaves stale figures.
"""
import hashlib
import json
import re
import secrets
import time
from collections import defaultdict

import os

from flask import abort, jsonify, redirect, request, send_from_directory, url_for

from . import DATE_RE, HERE, ID_RE, bp, db, kv_get, kv_set, login_required
from .chrono_import import (MAX_SHOTS_PER_STRING, fingerprint, parse_kestrel, parse_many, secs, sniff,
                            swap_rh_hpa)
from .chrono_stats import stats, temp_sensitivity
from . import chrono_loads

SCHEMA = """
CREATE TABLE IF NOT EXISTS loads (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'handload',
    cal TEXT, bullet TEXT, bullet_gr REAL, brass TEXT, powder TEXT, charge_gr REAL,
    notes TEXT DEFAULT '', archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chrono_strings (
    id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    started_at TEXT,
    gun_id TEXT, load_id TEXT, visit_id TEXT,
    name TEXT DEFAULT '',
    bullet_gr REAL,
    temp_c REAL, distance_m REAL,
    source TEXT NOT NULL,
    source_hash TEXT UNIQUE,
    unit_in TEXT DEFAULT 'm/s',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chrono_strings_date ON chrono_strings(date);
CREATE INDEX IF NOT EXISTS chrono_strings_gl ON chrono_strings(gun_id, load_id);
CREATE TABLE IF NOT EXISTS chrono_shots (
    string_id TEXT NOT NULL REFERENCES chrono_strings(id) ON DELETE CASCADE,
    n INTEGER NOT NULL,
    v_ms REAL NOT NULL,
    t TEXT,
    excluded INTEGER NOT NULL DEFAULT 0,
    cold_bore INTEGER NOT NULL DEFAULT 0,
    clean_bore INTEGER NOT NULL DEFAULT 0,
    note TEXT DEFAULT '',
    temp_c REAL, rh REAL, hpa REAL,
    fp TEXT UNIQUE,
    PRIMARY KEY (string_id, n)
);
CREATE INDEX IF NOT EXISTS chrono_shots_t ON chrono_shots(t);
"""
ALIAS_KEY, POWDERS_KEY = "chrono_gun_aliases", "chrono_powders"


def init_chrono(conn):
    conn.executescript(SCHEMA)
    chrono_loads.migrate(conn)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").casefold())


def slug(text, prefix="l-"):
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(text)).strip("-").lower()
    return (prefix + s)[:64].rstrip("-") or prefix + secrets.token_hex(4)


# ------------------------------------------------------------------ lookups
def guns(conn):
    return {r["id"]: json.loads(r["data"]) for r in conn.execute("SELECT id, data FROM guns")}


def aliases(conn):
    return json.loads(kv_get(ALIAS_KEY, conn) or "{}")


def known_powders(conn):
    return json.loads(kv_get(POWDERS_KEY, conn) or "[]")


def _tokens(text):
    """Words of a name, with letter-digit splits joined back: 'M-39 Kisa' / 'M_39' → {'m39', 'kisa'}."""
    words = re.findall(r"[a-z0-9]+", str(text or "").casefold())
    out, i = [], 0
    while i < len(words):
        w = words[i]
        if i + 1 < len(words) and w.isalpha() and len(w) <= 2 and words[i + 1].isdigit():
            w += words[i + 1]
            i += 1
        out.append(w)
        i += 1
    return set(out)


def _model(tokens):
    return {t for t in tokens if re.search(r"[a-z]", t) and re.search(r"\d", t)}


def resolve_gun(conn, candidates):
    """Folder names / file-name prefixes / ShotView titles → a diary gun.
    1) stored alias or gun name as an exact or prefix match (longest wins);
    2) otherwise the model token ('t3x', 't1x', 'm39') shared with exactly one best-matching gun name,
       so 'T3X Super Varmint' finds 'Tikka T3x Super Varmint' and 'M39 Kisa' finds 'Sako M39'."""
    gmap = guns(conn)
    names = {_norm(g.get("name")): gid for gid, g in gmap.items() if g.get("name")}
    names.update({_norm(a): gid for a, gid in aliases(conn).items()})
    names.pop("", None)
    best = None
    for cand in candidates:
        c = _norm(cand)
        if not c:
            continue
        for key, gid in names.items():
            if c == key or c.startswith(key):
                if best is None or len(key) > best[0]:
                    best = (len(key), gid)
    if best:
        return best[1]
    gtoks = {gid: _tokens(g.get("name")) for gid, g in gmap.items() if not g.get("archived")}
    for cand in candidates:
        ct = _tokens(cand)
        cm = _model(ct)
        if not cm:
            continue
        scored = sorted(((len(ct & gt), gid) for gid, gt in gtoks.items() if cm & _model(gt)), reverse=True)
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return scored[0][1]
    return None


def load_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM loads")]


def load_spec(hints, bullet_gr, cal):
    """Proposed load (structured fields + generated name) from file-name/folder hints, or None."""
    return chrono_loads.from_hints(hints, bullet_gr, cal)


def match_load(rows, spec):
    """Same generated name (+ calibre when both know it) → the same load."""
    if not spec:
        return None
    key = _norm(spec["name"])
    for r in rows:
        if _norm(r["name"]) == key and r["kind"] == spec["kind"] and (
                not r.get("cal") or not spec.get("cal") or _norm(r["cal"]) == _norm(spec["cal"])):
            return r["id"]
    return None


def upsert_load(conn, lid, body):
    vals = chrono_loads.clean(body)
    cols = ",".join(vals)
    conn.execute(f"INSERT INTO loads(id,{cols}) VALUES(?{',?' * len(vals)}) "
                 f"ON CONFLICT(id) DO UPDATE SET " + ",".join(f"{k}=excluded.{k}" for k in vals),
                 (lid, *vals.values()))
    return vals


def ensure_load(conn, spec):
    spec = chrono_loads.clean(spec)
    lid = match_load(load_rows(conn), spec)
    if lid:
        return lid
    lid = slug(spec["name"])
    if conn.execute("SELECT 1 FROM loads WHERE id=?", (lid,)).fetchone():
        lid = slug(spec["name"])[:55] + "-" + secrets.token_hex(3)
    upsert_load(conn, lid, spec)
    return lid


def merge_loads(conn, src, dst):
    """Move every string of load `src` to `dst` and delete `src`."""
    n = conn.execute("UPDATE chrono_strings SET load_id=? WHERE load_id=?", (dst, src)).rowcount
    conn.execute("DELETE FROM loads WHERE id=?", (src,))
    return n


def visits_on(conn, date):
    return [(r["id"], json.loads(r["data"])) for r in
            conn.execute("SELECT id, data FROM sessions WHERE date=?", (date,))]


# ------------------------------------------------------------------ preview
def preview(conn, parsed):
    """Add stats, link suggestions and duplicate checks to parsed strings (nothing is written)."""
    gmap, lrows = guns(conn), load_rows(conn)
    # Shot time + velocity → date, from ShotView exports in this upload and shots already stored
    raw_dates = defaultdict(set)
    for s in parsed["strings"]:
        if s["source"] == "shotview" and s["date"]:
            for x in s["shots"]:
                if x["t"]:
                    raw_dates[(x["t"], round(x["v_ms"], 1))].add(s["date"])
    for s in parsed["strings"]:
        vs = [x["v_ms"] for x in s["shots"]]
        s["stats"] = stats(vs, s.get("bullet_gr"))
        h = s.get("hints") or {}
        warnings = s.setdefault("warnings", [])

        # Year typos: the same shots in a raw export (or the DB) on another date → that date wins
        votes = defaultdict(int)
        for x in s["shots"]:
            if not x["t"]:
                continue
            key = (x["t"], round(x["v_ms"], 1))
            for d in raw_dates.get(key, ()):
                votes[d] += 1
            for r in conn.execute(
                    "SELECT DISTINCT st.date FROM chrono_shots sh JOIN chrono_strings st ON st.id=sh.string_id "
                    "WHERE sh.t=? AND abs(sh.v_ms-?)<0.05", key):
                votes[r["date"]] += 1
        if votes and s["source"] != "shotview":
            d, cnt = max(votes.items(), key=lambda kv: kv[1])
            if d != s["date"] and cnt >= max(2, len(vs) / 2):
                warnings.append({"code": "date_corrected", "text": f"date {s['date']} → {d}: the same shots are in a ShotView export"})
                s["date_original"], s["date"] = s["date"], d

        # Gun, visit, load
        cands = list(h.get("path") or []) + [h.get("gun_text"), s.get("file", "").rsplit("/", 1)[-1]]
        gun_id = resolve_gun(conn, [c for c in cands if c])
        day_visits = visits_on(conn, s["date"]) if s["date"] else []
        visit_id = day_visits[0][0] if len(day_visits) == 1 else None
        if not gun_id and visit_id:
            ids = {l.get("gunId") for l in day_visits[0][1].get("lines", []) if l.get("gunId")}
            gun_id = ids.pop() if len(ids) == 1 else None
        cal = gmap.get(gun_id, {}).get("cal") if gun_id else None
        spec = load_spec(h, s.get("bullet_gr"), cal)
        load_id = match_load(lrows, spec)
        if not spec and s.get("bullet_gr"):
            same = [r["id"] for r in lrows if not r["archived"] and r["bullet_gr"]
                    and abs(r["bullet_gr"] - s["bullet_gr"]) < 0.6 and (not cal or r["cal"] in (None, cal))]
            load_id = same[0] if len(same) == 1 else None
        s["suggest"] = {"gun_id": gun_id, "visit_id": visit_id, "load_id": load_id,
                        "load": None if load_id else spec}
        if not load_id and not spec:
            warnings.append({"code": "no_load", "text": "load not recognised – pick one or leave it empty"})

        # Already imported?
        fps = [fingerprint(s["date"], x["t"], x["v_ms"]) for x in s["shots"]]
        have = sum(1 for f in fps if f and conn.execute("SELECT 1 FROM chrono_shots WHERE fp=?", (f,)).fetchone())
        s["existing_shots"] = have
        s["already_imported"] = bool(fps) and have == len(fps)
    return parsed


# ------------------------------------------------------------------ commit
def _source_hash(date, shots):
    h = hashlib.sha256()
    for x in shots:
        h.update(f"{date}|{x.get('t') or ''}|{float(x['v_ms']):.2f};".encode())
    return h.hexdigest()


def _f(v, lo=None, hi=None):
    if v is None or v == "":
        return None
    v = float(v)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ValueError("range")
    return v


def commit_strings(conn, items):
    """Insert strings; shots already stored (same fingerprint) only fill empty weather."""
    result = {"created": [], "merged_shots": 0, "skipped": []}
    gun_ids = set(guns(conn))
    for it in items:
        if it.get("skip"):
            continue
        date = str(it.get("date") or "")
        shots = it.get("shots") or []
        if not DATE_RE.match(date) or not isinstance(shots, list) or not shots or len(shots) > MAX_SHOTS_PER_STRING:
            raise ValueError(f"bad_string:{it.get('key', '')}")
        if it.get("swap_rh_hpa"):
            swap_rh_hpa(it)
        gun_id = it.get("gun_id") or None
        if gun_id and gun_id not in gun_ids:
            raise ValueError("unknown_gun")
        load_id = it.get("load_id") or None
        if not load_id and isinstance(it.get("load"), dict):
            load_id = ensure_load(conn, it["load"])
        if load_id and not conn.execute("SELECT 1 FROM loads WHERE id=?", (load_id,)).fetchone():
            raise ValueError("unknown_load")
        manual = {k: _f(it.get(k)) for k in ("temp_c", "rh", "hpa")}

        clean = []
        for i, x in enumerate(shots):
            v = _f(x.get("v_ms"), 1, 2000)
            if v is None:
                raise ValueError("bad_velocity")
            shot = {"n": int(x.get("n") or i + 1), "v_ms": v, "t": x.get("t") or None,
                    "excluded": 1 if x.get("excluded") else 0, "cold_bore": 1 if x.get("cold_bore") else 0,
                    "clean_bore": 1 if x.get("clean_bore") else 0, "note": str(x.get("note") or "")[:200]}
            for k in ("temp_c", "rh", "hpa"):
                val = _f(x.get(k))
                shot[k] = val if val is not None else manual[k]
            shot["fp"] = fingerprint(date, shot["t"], v)
            clean.append(shot)
        # Two rows with the same time and speed inside one string (copied row) stay distinct
        counts = defaultdict(int)
        for shot in clean:
            if shot["fp"]:
                counts[shot["fp"]] += 1
                if counts[shot["fp"]] > 1:
                    shot["fp"] += f"#{counts[shot['fp']]}"

        shash = _source_hash(date, clean)
        if conn.execute("SELECT 1 FROM chrono_strings WHERE source_hash=?", (shash,)).fetchone():
            result["skipped"].append({"key": it.get("key"), "why": "already_imported"})
            continue
        # A string that shares any shot with stored data is already curated elsewhere (e.g. a workbook
        # day built from this raw export, fliers removed): only fill its empty weather, add nothing.
        overlap = 0
        for shot in clean:
            row = shot["fp"] and conn.execute("SELECT string_id, n, temp_c, rh, hpa FROM chrono_shots WHERE fp=?",
                                              (shot["fp"],)).fetchone()
            if not row:
                continue
            overlap += 1
            sets = {k: shot[k] for k in ("temp_c", "rh", "hpa") if row[k] is None and shot[k] is not None}
            if sets:
                conn.execute("UPDATE chrono_shots SET " + ",".join(f"{k}=?" for k in sets)
                             + " WHERE string_id=? AND n=?", (*sets.values(), row["string_id"], row["n"]))
                result["merged_shots"] += 1
                _refresh_temp(conn, row["string_id"])
        if overlap:
            result["skipped"].append({"key": it.get("key"), "why": "shots_exist", "overlap": overlap,
                                      "dropped": len(clean) - overlap})
            continue
        new = clean
        temps = [x["temp_c"] for x in new if x["temp_c"] is not None]
        sid = "c-" + secrets.token_hex(6)
        conn.execute(
            "INSERT INTO chrono_strings(id,date,started_at,gun_id,load_id,visit_id,name,bullet_gr,temp_c,"
            "distance_m,source,source_hash,unit_in,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, date, it.get("started_at") or None, gun_id, load_id, it.get("visit_id") or None,
             str(it.get("name") or "")[:120], _f(it.get("bullet_gr")),
             round(sum(temps) / len(temps), 1) if temps else manual["temp_c"], _f(it.get("distance_m")),
             str(it.get("source") or "manual")[:20], shash, it.get("unit_in") or "m/s",
             str(it.get("notes") or "")[:500], _now()))
        seen = set()
        for x in new:
            while x["n"] in seen:
                x["n"] += 1000
            seen.add(x["n"])
            conn.execute("INSERT INTO chrono_shots(string_id,n,v_ms,t,excluded,cold_bore,clean_bore,note,"
                         "temp_c,rh,hpa,fp) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (sid, x["n"], x["v_ms"], x["t"], x["excluded"], x["cold_bore"], x["clean_bore"],
                          x["note"], x["temp_c"], x["rh"], x["hpa"], x["fp"]))
        result["created"].append({"key": it.get("key"), "id": sid, "shots": len(new)})
    return result


def apply_day_weather(conn, date, values, gun_id=None, overwrite=False):
    """Manual weather for a whole day (optionally one gun): fills empty fields unless overwrite."""
    vals = {k: _f(values.get(k)) for k in ("temp_c", "rh", "hpa") if values.get(k) not in (None, "")}
    if not vals:
        return 0
    q = "SELECT id FROM chrono_strings WHERE date=?" + (" AND gun_id=?" if gun_id else "")
    ids = [r["id"] for r in conn.execute(q, (date, gun_id) if gun_id else (date,))]
    n = 0
    for sid in ids:
        touched = 0
        for k, v in vals.items():
            cond = "" if overwrite else f" AND {k} IS NULL"
            touched = max(touched, conn.execute(f"UPDATE chrono_shots SET {k}=? WHERE string_id=?{cond}",
                                                (v, sid)).rowcount)
        n += touched                      # shots, not field updates
        _refresh_temp(conn, sid)
    return n


def merge_weather_records(conn, records, window_min=10):
    """Kestrel records onto stored shots (empty fields only)."""
    by_date = defaultdict(list)
    for r in records:
        by_date[r["date"]].append((secs(r["t"]), r))
    n, touched = 0, set()
    for date, recs in by_date.items():
        for row in conn.execute("SELECT sh.string_id, sh.n, sh.t, sh.temp_c, sh.rh, sh.hpa FROM chrono_shots sh "
                                "JOIN chrono_strings st ON st.id=sh.string_id WHERE st.date=? AND sh.t IS NOT NULL",
                                (date,)).fetchall():
            ts = secs(row["t"])
            dist, rec = min(((abs(ts - x), r) for x, r in recs), key=lambda p: p[0])
            if dist > window_min * 60:
                continue
            sets = {k: rec[k] for k in ("temp_c", "rh", "hpa") if row[k] is None and rec.get(k) is not None}
            if sets:
                conn.execute("UPDATE chrono_shots SET " + ",".join(f"{k}=?" for k in sets)
                             + " WHERE string_id=? AND n=?", (*sets.values(), row["string_id"], row["n"]))
                n += 1
                touched.add(row["string_id"])
    for sid in touched:
        _refresh_temp(conn, sid)
    return n


def _refresh_temp(conn, sid):
    r = conn.execute("SELECT avg(temp_c) a FROM chrono_shots WHERE string_id=? AND temp_c IS NOT NULL", (sid,)).fetchone()
    if r["a"] is not None:
        conn.execute("UPDATE chrono_strings SET temp_c=? WHERE id=?", (round(r["a"], 1), sid))


# ------------------------------------------------------------------ reads
def _shots_by_string(conn, where="", args=()):
    out = defaultdict(list)
    for r in conn.execute("SELECT sh.* FROM chrono_shots sh JOIN chrono_strings st ON st.id=sh.string_id "
                          + where + " ORDER BY sh.string_id, sh.n", args):
        out[r["string_id"]].append(dict(r))
    return out


def list_strings(conn, f=None):
    f = f or {}
    where, args = [], []
    for key, col in (("gun", "gun_id"), ("load", "load_id"), ("date", "date")):
        if f.get(key):
            where.append(f"st.{col}=?")
            args.append(f[key])
    if f.get("from"):
        where.append("st.date>=?"); args.append(f["from"])
    if f.get("to"):
        where.append("st.date<=?"); args.append(f["to"])
    w = (" WHERE " + " AND ".join(where)) if where else ""
    shots = _shots_by_string(conn, w, args)
    lmap = {r["id"]: r for r in load_rows(conn)}
    out = []
    for r in conn.execute("SELECT * FROM chrono_strings st" + w + " ORDER BY date DESC, started_at DESC", args):
        s = dict(r)
        inc = [x["v_ms"] for x in shots.get(s["id"], []) if not x["excluded"]]
        bgr = s["bullet_gr"] or (lmap.get(s["load_id"]) or {}).get("bullet_gr")
        s["stats"] = stats(inc, bgr)
        s["shot_count"] = len(shots.get(s["id"], []))
        out.append(s)
    return out


def get_string(conn, sid):
    r = conn.execute("SELECT * FROM chrono_strings WHERE id=?", (sid,)).fetchone()
    if not r:
        return None
    s = dict(r)
    s["shots"] = _shots_by_string(conn, " WHERE st.id=?", (sid,)).get(sid, [])
    load = conn.execute("SELECT bullet_gr FROM loads WHERE id=?", (s["load_id"],)).fetchone() if s["load_id"] else None
    s["stats"] = stats([x["v_ms"] for x in s["shots"] if not x["excluded"]],
                       s["bullet_gr"] or (load["bullet_gr"] if load else None))
    return s


def days(conn, gun=None, load=None, cold_bore=True):
    """Pooled per day + gun + load, newest first."""
    groups = defaultdict(lambda: {"strings": [], "v": [], "temps": []})
    f = {"gun": gun, "load": load}
    shots = None
    for s in list_strings(conn, f):
        g = groups[(s["date"], s["gun_id"], s["load_id"])]
        g["strings"].append(s["id"])
    if groups:
        shots = _shots_by_string(conn)
    lmap = {r["id"]: r for r in load_rows(conn)}
    out = []
    for (date, gid, lid), g in groups.items():
        for sid in g["strings"]:
            for x in shots.get(sid, []):
                if x["excluded"] or (not cold_bore and x["cold_bore"]):
                    continue
                g["v"].append(x["v_ms"])
                if x["temp_c"] is not None:
                    g["temps"].append(x["temp_c"])
        st = stats(g["v"], (lmap.get(lid) or {}).get("bullet_gr"))
        out.append({"date": date, "gun_id": gid, "load_id": lid, "strings": g["strings"], "stats": st,
                    "temp_c": round(sum(g["temps"]) / len(g["temps"]), 1) if g["temps"] else None})
    # Δ vs the load's all-time pooled average (per gun)
    alltime = {}
    for d in out:
        alltime.setdefault((d["gun_id"], d["load_id"]), []).append(d)
    for key, ds in alltime.items():
        v = [x for sid in sum((d["strings"] for d in ds), []) for x in
             [s["v_ms"] for s in shots.get(sid, []) if not s["excluded"]]]
        mean = sum(v) / len(v) if v else None
        for d in ds:
            d["delta"] = round(d["stats"]["avg"] - mean, 2) if mean is not None and d["stats"]["avg"] is not None else None
    out.sort(key=lambda d: d["date"], reverse=True)
    return out


def trend(conn, gun, load):
    ds = sorted(days(conn, gun, load), key=lambda d: d["date"])
    pts = [{"date": d["date"], "temp_c": d["temp_c"], **d["stats"]} for d in ds]
    return {"points": pts, "temp_sensitivity": temp_sensitivity([(d["temp_c"], d["stats"]["avg"]) for d in ds])}


def ladder(conn, gun):
    """One point per load (handloads with a charge) for this gun, pooled over every day."""
    shots = _shots_by_string(conn, " WHERE st.gun_id=?", (gun,))
    by_load = defaultdict(lambda: {"v": [], "days": set()})
    for r in conn.execute("SELECT id, date, load_id FROM chrono_strings WHERE gun_id=? AND load_id IS NOT NULL", (gun,)):
        g = by_load[r["load_id"]]
        g["v"] += [x["v_ms"] for x in shots.get(r["id"], []) if not x["excluded"]]
        g["days"].add(r["date"])
    out = []
    for lrow in load_rows(conn):
        if lrow["id"] not in by_load or lrow["kind"] != "handload" or lrow["charge_gr"] is None:
            continue
        g = by_load[lrow["id"]]
        out.append({"load_id": lrow["id"], "group": chrono_loads.ladder_group(lrow), "name": lrow["name"],
                    "charge_gr": lrow["charge_gr"], "days": sorted(g["days"]), **stats(g["v"], lrow["bullet_gr"])})
    out.sort(key=lambda p: (p["group"], p["charge_gr"]))
    return out


def load_summary(conn, lid):
    """All-time and last-10-days figures per gun for one load, plus temperature sensitivity."""
    ds = days(conn, load=lid)
    per = defaultdict(list)
    for d in ds:
        per[d["gun_id"]].append(d)
    shots = _shots_by_string(conn, " WHERE st.load_id=?", (lid,))
    lrow = conn.execute("SELECT bullet_gr FROM loads WHERE id=?", (lid,)).fetchone()
    bgr = lrow["bullet_gr"] if lrow else None
    out = []
    for gid, items in per.items():
        items.sort(key=lambda d: d["date"])
        allv = [x["v_ms"] for d in items for sid in d["strings"] for x in shots.get(sid, []) if not x["excluded"]]
        lastv = [x["v_ms"] for d in items[-10:] for sid in d["strings"] for x in shots.get(sid, []) if not x["excluded"]]
        out.append({"gun_id": gid, "days": len(items), "all": stats(allv, bgr), "last10": stats(lastv, bgr),
                    "temp_sensitivity": temp_sensitivity([(d["temp_c"], d["stats"]["avg"]) for d in items])})
    return out


def export_rows(conn):
    return {"loads": load_rows(conn),
            "chrono_strings": [dict(r) for r in conn.execute("SELECT * FROM chrono_strings ORDER BY date, id")],
            "chrono_shots": [dict(r) for r in conn.execute("SELECT * FROM chrono_shots ORDER BY string_id, n")]}


def import_rows(conn, data, replace=False):
    if replace and any(k in data for k in ("loads", "chrono_strings", "chrono_shots")):
        conn.executescript("DELETE FROM chrono_shots; DELETE FROM chrono_strings; DELETE FROM loads;")
    for table, key in (("loads", "id"), ("chrono_strings", "id"), ("chrono_shots", None)):
        for row in data.get(table, []):
            cols = list(row)
            conn.execute(f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                         [row[c] for c in cols])
    chrono_loads.migrate(conn)          # backups from before the structured load fields
    return {t: len(data.get(t, [])) for t in ("loads", "chrono_strings", "chrono_shots")}


# ------------------------------------------------------------------ routes
@bp.get("/chrono")
@login_required
def page_chrono():
    resp = send_from_directory(os.path.join(HERE, "static"), "chrono.html", max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.post("/chrono/share")
def share_fallback():
    """Reached only when the service worker isn't running yet; the page explains what to do."""
    return redirect(url_for("diary.page_chrono", shared="nosw"), code=303)


def _fetch_only():
    if request.headers.get("X-Requested-With") != "fetch":
        abort(403)


def _body():
    if not request.is_json:
        abort(415)
    b = request.get_json(silent=True)
    if not isinstance(b, (dict, list)):
        abort(400)
    return b


@bp.post("/api/chrono/parse")
@login_required
def api_parse():
    _fetch_only()                       # multipart can't use the JSON-only CSRF guard
    files = request.files.getlist("files")
    paths = request.form.getlist("paths")
    if not files or len(files) > 100:
        return jsonify(error="no_files"), 400
    items = []
    for i, f in enumerate(files):
        data = f.read(5 * 1024 * 1024 + 1)
        if len(data) > 5 * 1024 * 1024:
            return jsonify(error="file_too_large", file=f.filename), 413
        items.append((f.filename or f"file{i}", data, paths[i] if i < len(paths) and paths[i] else f.filename))
    parsed = parse_many(items, known_powders(db()))
    return jsonify(preview(db(), parsed))


@bp.post("/api/chrono/commit")
@login_required
def api_commit():
    body = _body()
    items = body.get("strings") if isinstance(body, dict) else None
    if not isinstance(items, list) or len(items) > 500:
        abort(400)
    try:
        res = commit_strings(db(), items)
    except ValueError as e:
        db().rollback()
        return jsonify(error=str(e)), 400
    db().commit()
    return jsonify(res)


@bp.get("/api/chrono/strings")
@login_required
def api_strings():
    return jsonify(list_strings(db(), {k: request.args.get(k) for k in ("gun", "load", "date", "from", "to")}))


@bp.get("/api/chrono/strings/<sid>")
@login_required
def api_string(sid):
    s = get_string(db(), sid)
    return jsonify(s) if s else (jsonify(error="not_found"), 404)


@bp.patch("/api/chrono/strings/<sid>")
@login_required
def api_patch_string(sid):
    b = _body()
    conn = db()
    if not conn.execute("SELECT 1 FROM chrono_strings WHERE id=?", (sid,)).fetchone():
        return jsonify(error="not_found"), 404
    sets = {}
    for k in ("gun_id", "load_id", "visit_id", "name", "notes", "started_at"):
        if k in b:
            sets[k] = (str(b[k])[:500] if b[k] not in (None, "") else None)
    for k in ("temp_c", "distance_m", "bullet_gr"):
        if k in b:
            try:
                sets[k] = _f(b[k])
            except (TypeError, ValueError):
                return jsonify(error="bad_" + k), 400
    if "date" in b:
        if not DATE_RE.match(str(b["date"])):
            return jsonify(error="bad_date"), 400
        sets["date"] = b["date"]
    if sets:
        conn.execute("UPDATE chrono_strings SET " + ",".join(f"{k}=?" for k in sets) + " WHERE id=?",
                     (*sets.values(), sid))
    for x in b.get("shots", []) if isinstance(b.get("shots"), list) else []:
        if isinstance(x, dict) and "n" in x:
            conn.execute("UPDATE chrono_shots SET excluded=? WHERE string_id=? AND n=?",
                         (1 if x.get("excluded") else 0, sid, int(x["n"])))
    if b.get("weather"):
        w = b["weather"]
        for k in ("temp_c", "rh", "hpa"):
            if w.get(k) not in (None, ""):
                cond = "" if w.get("overwrite") else f" AND {k} IS NULL"
                conn.execute(f"UPDATE chrono_shots SET {k}=? WHERE string_id=?{cond}", (_f(w[k]), sid))
        if w.get("swap_rh_hpa"):
            conn.execute("UPDATE chrono_shots SET rh=hpa, hpa=rh WHERE string_id=?", (sid,))
        _refresh_temp(conn, sid)
    conn.commit()
    return jsonify(get_string(conn, sid))


@bp.delete("/api/chrono/strings/<sid>")
@login_required
def api_delete_string(sid):
    _fetch_only()
    conn = db()
    conn.execute("DELETE FROM chrono_shots WHERE string_id=?", (sid,))
    conn.execute("DELETE FROM chrono_strings WHERE id=?", (sid,))
    conn.commit()
    return jsonify(ok=True)


@bp.post("/api/chrono/weather")
@login_required
def api_weather():
    """JSON {date, temp_c, rh, hpa, gun_id?, overwrite?} for manual day weather,
    or multipart Kestrel CSV files merged onto stored shots."""
    conn = db()
    if request.files:
        _fetch_only()
        recs, errors = [], []
        for f in request.files.getlist("files"):
            data = f.read(5 * 1024 * 1024)
            if sniff(data) == "kestrel":
                recs += parse_kestrel(data)
            else:
                errors.append(f.filename)
        n = merge_weather_records(conn, recs)
        conn.commit()
        return jsonify(records=len(recs), shots_filled=n, not_kestrel=errors)
    b = _body()
    if not DATE_RE.match(str(b.get("date", ""))):
        return jsonify(error="bad_date"), 400
    try:
        n = apply_day_weather(conn, b["date"], b, b.get("gun_id"), bool(b.get("overwrite")))
    except ValueError:
        return jsonify(error="bad_value"), 400
    conn.commit()
    return jsonify(shots_filled=n)


@bp.get("/api/chrono/days")
@login_required
def api_days():
    return jsonify(days(db(), request.args.get("gun"), request.args.get("load"),
                        request.args.get("cold", "1") != "0"))


@bp.get("/api/chrono/trend")
@login_required
def api_trend():
    gun, load = request.args.get("gun"), request.args.get("load")
    if not load:
        return jsonify(error="load_required"), 400
    return jsonify(trend(db(), gun, load))


@bp.get("/api/chrono/ladder")
@login_required
def api_ladder():
    gun = request.args.get("gun")
    if not gun:
        return jsonify(error="gun_required"), 400
    return jsonify(ladder(db(), gun))


def load_usage(conn):
    """Per load: shots, days, guns and last date (for the load list)."""
    out = {}
    for r in conn.execute(
            "SELECT st.load_id lid, count(sh.n) n, count(DISTINCT st.date) d, max(st.date) last, "
            "group_concat(DISTINCT st.gun_id) guns FROM chrono_strings st "
            "JOIN chrono_shots sh ON sh.string_id=st.id AND sh.excluded=0 WHERE st.load_id IS NOT NULL "
            "GROUP BY st.load_id"):
        out[r["lid"]] = {"shots": r["n"], "days": r["d"], "last": r["last"],
                         "guns": [g for g in (r["guns"] or "").split(",") if g]}
    return out


@bp.get("/api/chrono/loads")
@login_required
def api_loads():
    use = load_usage(db())
    return jsonify([dict(r, use=use.get(r["id"], {"shots": 0, "days": 0, "last": None, "guns": []}))
                    for r in load_rows(db())])


@bp.get("/api/chrono/loads/<lid>")
@login_required
def api_load(lid):
    r = db().execute("SELECT * FROM loads WHERE id=?", (lid,)).fetchone()
    if not r:
        return jsonify(error="not_found"), 404
    return jsonify(dict(r, per_gun=load_summary(db(), lid)))


@bp.put("/api/chrono/loads/<lid>")
@login_required
def api_put_load(lid):
    """Save a load. Another load with the same generated name → 409 with its id, so the page can offer
    to merge the two instead of keeping duplicates."""
    if not ID_RE.match(lid):
        abort(400)
    conn = db()
    try:
        vals = chrono_loads.clean(_body())
    except (TypeError, ValueError) as e:
        return jsonify(error=str(e) if str(e).startswith("bad_") else "bad_value"), 400
    twin = next((r for r in load_rows(conn) if r["id"] != lid and _norm(r["name"]) == _norm(vals["name"])
                 and r["kind"] == vals["kind"]), None)
    if twin and not (isinstance(request.json, dict) and request.json.get("allow_duplicate")):
        return jsonify(error="duplicate", other=twin["id"], name=vals["name"]), 409
    upsert_load(conn, lid, vals)
    conn.commit()
    return jsonify(dict(conn.execute("SELECT * FROM loads WHERE id=?", (lid,)).fetchone()))


@bp.post("/api/chrono/loads/<lid>/merge")
@login_required
def api_merge_load(lid):
    b = _body()
    into = b.get("into") if isinstance(b, dict) else None
    conn = db()
    if not into or into == lid or not conn.execute("SELECT 1 FROM loads WHERE id=?", (into,)).fetchone():
        return jsonify(error="bad_target"), 400
    n = merge_loads(conn, lid, into)
    conn.commit()
    return jsonify(moved=n, into=into)


@bp.delete("/api/chrono/loads/<lid>")
@login_required
def api_delete_load(lid):
    _fetch_only()
    conn = db()
    if conn.execute("SELECT 1 FROM chrono_strings WHERE load_id=?", (lid,)).fetchone():
        return jsonify(error="load_in_use"), 409
    conn.execute("DELETE FROM loads WHERE id=?", (lid,))
    conn.commit()
    return jsonify(ok=True)


@bp.get("/api/chrono/settings")
@login_required
def api_settings():
    return jsonify(aliases=aliases(db()), powders=known_powders(db()))


@bp.put("/api/chrono/settings")
@login_required
def api_put_settings():
    b = _body()
    gids = set(guns(db()))
    al = b.get("aliases", {})
    if not isinstance(al, dict) or any(v not in gids for v in al.values()):
        return jsonify(error="bad_aliases"), 400
    pw = b.get("powders", [])
    if not isinstance(pw, list):
        return jsonify(error="bad_powders"), 400
    kv_set(ALIAS_KEY, json.dumps({str(k)[:60]: v for k, v in al.items()}, ensure_ascii=False))
    kv_set(POWDERS_KEY, json.dumps([str(p)[:30] for p in pw], ensure_ascii=False))
    return jsonify(ok=True)
