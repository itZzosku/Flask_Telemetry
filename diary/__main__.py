"""Server-side admin for the shooting diary.

  python -m diary set-password            set or change the login password
  python -m diary import seed.json        load sessions + profile from JSON
  python -m diary import seed.json --replace   wipe sessions first
  python -m diary export backup.json      dump everything to JSON
"""
import argparse
import json
import sys
from getpass import getpass

from werkzeug.security import generate_password_hash

from . import DATA_DIR, connect, init_db, kv_get, kv_set, upsert_session


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
    if data.get("profile"):
        kv_set("profile", json.dumps(data["profile"], ensure_ascii=False), conn)
    conn.commit()
    conn.close()
    print(f"Tuotu {n} käyntiä → {DATA_DIR}")


def export_json(path):
    conn = connect()
    sessions = [dict(json.loads(r["data"]), id=r["id"])
                for r in conn.execute("SELECT id, data FROM sessions ORDER BY date, id")]
    profile = json.loads(kv_get("profile", conn) or "{}")
    conn.close()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"profile": profile, "sessions": sessions}, f, ensure_ascii=False, indent=1)
    print(f"Viety {len(sessions)} käyntiä → {path}")


def main():
    p = argparse.ArgumentParser(prog="python -m diary")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("set-password")
    i = sub.add_parser("import")
    i.add_argument("path")
    i.add_argument("--replace", action="store_true")
    e = sub.add_parser("export")
    e.add_argument("path")
    a = p.parse_args()
    init_db()
    if a.cmd == "set-password":
        set_password()
    elif a.cmd == "import":
        import_json(a.path, a.replace)
    else:
        export_json(a.path)


if __name__ == "__main__":
    main()
