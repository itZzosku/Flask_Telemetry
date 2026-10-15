"""Server-side admin for the shooting diary.

  python -m diary set-password            set or change the login password
  python -m diary import seed.json        load sessions, guns + profile from JSON
  python -m diary import seed.json --replace   wipe sessions first
  python -m diary export backup.json      dump everything to JSON
  python -m diary backup DIR [--keep 12]  dated JSON + SQLite copy into DIR, keep newest N
"""
import argparse
import glob
import json
import os
import sqlite3
import sys
import time
from getpass import getpass

from werkzeug.security import generate_password_hash

from . import DATA_DIR, DB_PATH, connect, init_db, kv_get, kv_set, upsert_gun, upsert_session


def set_password():
    pw = getpass("Uusi salasana: ")
    if len(pw) < 10:
        sys.exit("Käytä vähintään 10 merkkiä.")
    if pw != getpass("Uudestaan: "):
        sys.exit("Salasanat eivät täsmää.")
    conn = connect()
    kv_set("password_hash", generate_password_hash(pw), conn)
    conn.close()
    print("Salasana asetettu. Kaikki vanhat kirjautumiset on kirjattu ulos.")


def import_json(path, replace):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    conn = connect()
    if replace:
        conn.execute("DELETE FROM sessions")
    n = 0
    for s in data.get("sessions", []):
        s = dict(s)
        sid = s.pop("id")
        upsert_session(conn, sid, s)
        n += 1
    g = 0
    for gun in data.get("guns", []):
        gun = dict(gun)
        upsert_gun(conn, gun.pop("id"), gun)
        g += 1
    if data.get("profile"):
        kv_set("profile", json.dumps(data["profile"], ensure_ascii=False), conn)
    conn.commit()
    conn.close()
    print(f"Tuotu {n} käyntiä ja {g} asetta → {DATA_DIR}")


def export_json(path):
    conn = connect()
    sessions = [dict(json.loads(r["data"]), id=r["id"])
                for r in conn.execute("SELECT id, data FROM sessions ORDER BY date, id")]
    guns = [dict(json.loads(r["data"]), id=r["id"])
            for r in conn.execute("SELECT id, data FROM guns ORDER BY id")]
    profile = json.loads(kv_get("profile", conn) or "{}")
    conn.close()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"profile": profile, "guns": guns, "sessions": sessions}, f,
                  ensure_ascii=False, indent=1)
    print(f"Viety {len(sessions)} käyntiä → {path}")


def backup(directory, keep):
    """Write diary-YYYY-MM-DD.json and .db into directory, prune to the newest `keep`."""
    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    base = os.path.join(directory, f"diary-{stamp}")
    export_json(base + ".json")
    # sqlite3's backup API gives a consistent copy even while the app is running.
    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(base + ".db")
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    for ext in (".json", ".db"):
        old = sorted(glob.glob(os.path.join(directory, "diary-*" + ext)))[:-keep]
        for path in old:
            os.remove(path)
    print(f"Varmuuskopio: {base}.json + .db (säilytetään {keep} uusinta)")


def main():
    p = argparse.ArgumentParser(prog="python -m diary")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("set-password")
    i = sub.add_parser("import")
    i.add_argument("path")
    i.add_argument("--replace", action="store_true")
    e = sub.add_parser("export")
    e.add_argument("path")
    b = sub.add_parser("backup")
    b.add_argument("directory")
    b.add_argument("--keep", type=int, default=12)
    a = p.parse_args()
    init_db()
    if a.cmd == "set-password":
        set_password()
    elif a.cmd == "import":
        import_json(a.path, a.replace)
    elif a.cmd == "backup":
        backup(a.directory, max(1, a.keep))
    else:
        export_json(a.path)


if __name__ == "__main__":
    main()
