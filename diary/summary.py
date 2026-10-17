"""One-line summaries for the landing page (/diary/): diary, velocity and zeroes."""
import json
import time

from flask import jsonify

from . import bp, db, kv_get, login_required
from .chrono_stats import stats
from .zero import _load_labels, display_name


def diary_summary(conn):
    year = time.strftime("%Y")
    visits = rounds = 0
    last = None
    for r in conn.execute("SELECT date, data FROM sessions ORDER BY date DESC, id DESC"):
        s = json.loads(r["data"])
        if last is None:
            last = {"date": r["date"], "place": s.get("place") or ""}
        if r["date"].startswith(year):
            visits += 1
            rounds += sum(int(l.get("rounds") or 0) for l in s.get("lines", []) if str(l.get("rounds") or "0").isdigit())
    return {"year": year, "visits": visits, "rounds": rounds, "last": last}


def velocity_summary(conn):
    n = conn.execute("SELECT COUNT(*) FROM chrono_strings").fetchone()[0]
    r = conn.execute("SELECT s.id, s.date, s.gun_id, s.bullet_gr, l.name AS load, l.bullet_gr AS lgr "
                     "FROM chrono_strings s LEFT JOIN loads l ON l.id=s.load_id "
                     "ORDER BY s.date DESC, s.started_at DESC LIMIT 1").fetchone()
    last = None
    if r:
        v = [x[0] for x in conn.execute("SELECT v_ms FROM chrono_shots WHERE string_id=? AND NOT excluded", (r["id"],))]
        st = stats(v, r["bullet_gr"] or r["lgr"])
        last = {"date": r["date"], "load": r["load"], "gun_id": r["gun_id"], "avg": st["avg"], "sd": st["sd"], "n": st["n"]}
    return {"strings": n, "last": last}


def zero_summary(conn):
    out, loads = [], _load_labels(conn)
    for s in conn.execute("SELECT * FROM scopes WHERE NOT archived ORDER BY ord, name"):
        on = conn.execute("SELECT rifle, label, load_id FROM scope_zeros WHERE id=?", (s["mounted_zero_id"],)).fetchone() \
            if s["mounted_zero_id"] else None
        n = conn.execute("SELECT COUNT(*) FROM scope_zeros WHERE scope_id=? AND NOT archived", (s["id"],)).fetchone()[0]
        out.append({"name": s["name"], "positions": n, "on": display_name(dict(on), loads) if on else None,
                    "updated": s["updated"]})
    return out


@bp.get("/api/home")
@login_required
def api_home():
    conn = db()
    guns = {r["id"]: json.loads(r["data"]).get("name") for r in conn.execute("SELECT id, data FROM guns")}
    vel = velocity_summary(conn)
    if vel["last"]:
        vel["last"]["gun"] = guns.get(vel["last"].pop("gun_id"))
    name = (json.loads(kv_get("profile", conn) or "{}") or {}).get("name") or ""
    from . import current_user
    return jsonify(name=name, username=current_user()["username"], diary=diary_summary(conn), velocity=vel, zeroes=zero_summary(conn))
