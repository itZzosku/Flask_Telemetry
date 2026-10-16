"""Server-side admin for the shooting diary.

  python -m diary set-password            set or change the login password
  python -m diary import seed.json        load sessions, guns + profile from JSON
  python -m diary import seed.json --replace   wipe sessions first
  python -m diary export backup.json      dump everything to JSON
  python -m diary backup DIR [--keep 12] [--mail]
                                          dated JSON + SQLite copy into DIR, keep newest N,
                                          --mail: also email the JSON (settings in .env)
  python -m diary assign-gun GUN_ID --cal ".223 Rem" [--type Kivääri] [--from 2024-04-17] [--to 2026-07-10]
                                          tie existing rows without a gun to GUN_ID (dates inclusive);
                                          --dry-run shows what would change, --list-guns shows ids
  python -m diary chrono-alias [NAME GUN_ID]   map a folder/file name (e.g. "KP-15") to a diary gun; no args lists
  python -m diary chrono-import PATH... [--gun ID] [--dry-run] [--keep-swaps] [--fix-dates]
                                          import ShotView exports, velocity workbooks and Kestrel CSVs
                                          (files or folders, searched recursively)
  python -m diary chrono-stats [--gun ID] [--load ID]   per-day velocity table
"""
import argparse
import glob
import json
import os
import sqlite3
import sys
import smtplib
import time
from email.message import EmailMessage
from getpass import getpass

from werkzeug.security import generate_password_hash

from . import DATA_DIR, DB_PATH, connect, init_db, kv_get, kv_set, upsert_gun, upsert_session
from . import chrono


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
    c = chrono.import_rows(conn, data, replace)
    conn.commit()
    conn.close()
    extra = f", {c['chrono_strings']} sarjaa / {c['chrono_shots']} nopeutta" if c["chrono_shots"] else ""
    print(f"Tuotu {n} käyntiä ja {g} asetta{extra} → {DATA_DIR}")


def export_json(path):
    conn = connect()
    sessions = [dict(json.loads(r["data"]), id=r["id"])
                for r in conn.execute("SELECT id, data FROM sessions ORDER BY date, id")]
    guns = [dict(json.loads(r["data"]), id=r["id"])
            for r in conn.execute("SELECT id, data FROM guns ORDER BY id")]
    profile = json.loads(kv_get("profile", conn) or "{}")
    velocity = chrono.export_rows(conn)
    conn.close()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"profile": profile, "guns": guns, "sessions": sessions, **velocity}, f,
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
    return base + ".json"


def mail_backup(json_path):
    """Email the backup JSON. .env: DIARY_SMTP_USER, DIARY_SMTP_PASSWORD (Gmail app password),
    optional DIARY_BACKUP_TO (default: DIARY_SMTP_USER), DIARY_SMTP_HOST, DIARY_SMTP_PORT."""
    user = os.getenv("DIARY_SMTP_USER", "").strip().strip('"\'')
    # Google shows the app password in groups; copying can bring non-breaking spaces along.
    password = "".join(os.getenv("DIARY_SMTP_PASSWORD", "").strip().strip('"\'').split())
    if not user or not password:
        sys.exit("DIARY_SMTP_USER ja DIARY_SMTP_PASSWORD puuttuvat .env-tiedostosta.")
    to = os.getenv("DIARY_BACKUP_TO", user).strip()
    host = os.getenv("DIARY_SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("DIARY_SMTP_PORT", "587"))

    with open(json_path, "rb") as f:
        payload = f.read()
    data = json.loads(payload)
    sessions = data.get("sessions", [])
    days = len({s["date"] for s in sessions})
    rounds = sum(int(l.get("rounds") or 0) for s in sessions for l in s.get("lines", []))
    last = max((s["date"] for s in sessions), default="–")
    name = os.path.basename(json_path)

    msg = EmailMessage()
    msg["Subject"] = f"Ampumapäiväkirja varmuuskopio {time.strftime('%Y-%m-%d')}"
    msg["From"] = user
    msg["To"] = to
    msg.set_content(
        f"Viikoittainen varmuuskopio ampumapäiväkirjasta.\n\n"
        f"Käyntejä: {len(sessions)}\nHarrastuskertoja: {days}\nLaukauksia: {rounds}\n"
        f"Aseita: {len(data.get('guns', []))}\nViimeisin käynti: {last}\n"
        f"Nopeussarjoja: {len(data.get('chrono_strings', []))}, laukauksia {len(data.get('chrono_shots', []))}\n\n"
        f"Palautus palvelimella:\n  ./venv/bin/python -m diary import {name} --replace\n")
    msg.add_attachment(payload, maintype="application", subtype="json", filename=name)

    if not password.isascii() or ("gmail" in host and len(password) != 16):
        sys.exit(f"DIARY_SMTP_PASSWORD näyttää väärältä: {len(password)} merkkiä"
                 f"{'' if password.isascii() else ', sisältää erikoismerkkejä'}. "
                 "Gmailin sovellussalasana on 16 kirjainta – kirjoita se .env-tiedostoon käsin.")
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if os.getenv("DIARY_SMTP_STARTTLS", "1") != "0":
                smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        sys.exit(f"Kirjautuminen hylättiin ({e.smtp_code}). Tarkista, että DIARY_SMTP_USER on sama "
                 "Google-tili, jolle sovellussalasana tehtiin, ja että salasana on oikein.")
    except (smtplib.SMTPException, OSError) as e:
        sys.exit(f"Sähköpostin lähetys epäonnistui: {type(e).__name__}: {e}")
    print(f"Lähetetty: {name} → {to}")


def _norm(v):
    return " ".join(str(v or "").split()).casefold()


def list_guns():
    conn = connect()
    for r in conn.execute("SELECT id, data FROM guns ORDER BY id"):
        g = json.loads(r["data"])
        print(f"{r['id']:<16} {g.get('name', '')}  ({g.get('type', '')}, {g.get('cal', '')})")
    conn.close()


def assign_gun(gun_id, cal, typ, date_from, date_to, dry_run):
    conn = connect()
    row = conn.execute("SELECT data FROM guns WHERE id=?", (gun_id,)).fetchone()
    if not row:
        sys.exit(f"Asetta {gun_id!r} ei löydy. Katso tunnukset: python -m diary assign-gun --list-guns")
    gun = json.loads(row["data"])
    lines = visits = rounds = skipped = 0
    first = last = None
    for r in conn.execute("SELECT id, date, data FROM sessions ORDER BY date, id").fetchall():
        if (date_from and r["date"] < date_from) or (date_to and r["date"] > date_to):
            continue
        body, changed = json.loads(r["data"]), False
        for l in body.get("lines", []):
            if _norm(l.get("cal")) != _norm(cal) or (typ and _norm(l.get("type")) != _norm(typ)):
                continue
            if l.get("gunId"):
                skipped += 1          # already tied to a gun – leave it alone
                continue
            l["gunId"], l["gun"] = gun_id, gun.get("name", "")
            lines += 1
            rounds += int(l.get("rounds") or 0)
            changed = True
        if changed:
            visits += 1
            first = first or r["date"]
            last = r["date"]
            if not dry_run:
                upsert_session(conn, r["id"], body)
    if not dry_run:
        conn.commit()
    conn.close()
    span = f", {first} – {last}" if first else ""
    print(f"{'[KOE] ' if dry_run else ''}{gun.get('name', gun_id)}: {lines} riviä / {visits} käyntiä / "
          f"{rounds} laukausta{span}" + (f" (ohitettu {skipped} riviä, joilla on jo ase)" if skipped else ""))


# ------------------------------------------------------------------ velocity data
VEL_EXT = (".xls", ".xlsx", ".csv", "")
MAX_FILE = 5 * 1024 * 1024


def _collect(paths):
    """Files under the given paths; relpath keeps the folder names (powder / rifle) for hints."""
    out = []
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isfile(p):
            out.append((p, os.path.basename(os.path.dirname(p)) + "/" + os.path.basename(p)))
            continue
        base = os.path.dirname(p)
        for root, _dirs, files in os.walk(p):
            for name in sorted(files):
                full = os.path.join(root, name)
                ext = os.path.splitext(name)[1].lower()
                if ext in VEL_EXT and os.path.getsize(full) <= MAX_FILE and not name.startswith("~$"):
                    out.append((full, os.path.relpath(full, base)))
    return out


def chrono_alias(name, gun_id):
    conn = connect()
    al = chrono.aliases(conn)
    if name:
        if gun_id not in chrono.guns(conn):
            sys.exit(f"Asetta {gun_id!r} ei löydy. Katso tunnukset: python -m diary assign-gun --list-guns")
        al[name] = gun_id
        kv_set(chrono.ALIAS_KEY, json.dumps(al, ensure_ascii=False), conn)
    names = {gid: g.get("name", "") for gid, g in chrono.guns(conn).items()}
    for a, gid in sorted(al.items()):
        print(f"{a:<28} → {gid} ({names.get(gid, '?')})")
    if not al:
        print("Ei aliaksia. Esim: python -m diary chrono-alias \"KP-15\" g-wwsd")
    conn.close()


def _fmt(v, nd=1):
    return "–" if v is None else f"{v:.{nd}f}"


def chrono_import(paths, gun, dry_run, keep_swaps, fix_dates):
    from .chrono_import import fingerprint, parse_many, swap_rh_hpa
    conn = connect()
    files = _collect(paths)
    items = []
    for full, rel in files:
        with open(full, "rb") as f:
            items.append((os.path.basename(full), f.read(), rel))
    parsed = chrono.preview(conn, parse_many(items, chrono.known_powders(conn)))
    unknown = [e for e in parsed["errors"]]
    lnames = {r["id"]: r["name"] for r in chrono.load_rows(conn)}
    gnames = {gid: g.get("name", gid) for gid, g in chrono.guns(conn).items()}
    # Workbooks first: they carry loads and weather; raw exports then only add what is missing.
    strings = sorted(parsed["strings"], key=lambda s: (s["source"] != "workbook", s["date"] or "", s["started_at"] or ""))
    batch, shots, seen = [], 0, set()
    for s in strings:
        sug = s["suggest"]
        w = s["warnings"]
        if not keep_swaps and any(x["code"] == "weather_range" and x.get("swap") for x in w):
            swap_rh_hpa(s)
        if fix_dates:
            for x in w:
                if x["code"] == "date_outlier" and x.get("suggest"):
                    s["date"] = x["suggest"]
        gid = gun or sug["gun_id"]
        load = sug["load_id"] and lnames.get(sug["load_id"]) or (sug["load"] or {}).get("name") or "–"
        flags = "".join({"weather_range": " ⇄sää", "date_outlier": " ?vuosi", "date_corrected": " ✓päivä",
                         "check_mismatch": " ≠garmin", "no_load": ""}.get(x["code"], "") for x in w)
        st = s["stats"]
        fps = {fingerprint(s["date"], x["t"], x["v_ms"]) for x in s["shots"]} - {None}
        in_batch = bool(fps & seen)
        seen |= fps
        done = " (jo tuotu)" if s["already_imported"] or s["existing_shots"] else (" (sama kuin yllä)" if in_batch else "")
        print(f"{s['date']} {s['started_at'] or '     '}  {gnames.get(gid, gid or '?'):<20.20} {load:<52.52} "
              f"n={st['n']:<3} {_fmt(st['avg'])} SD {_fmt(st['sd'], 2)}{flags}{done}")
        if not (s["already_imported"] or s["existing_shots"] or in_batch):
            shots += st["n"]
            batch.append(dict(s, gun_id=gid, load_id=sug["load_id"], load=sug["load"], visit_id=sug["visit_id"]))
    for n in parsed["notes"]:
        if n["code"] not in ("empty",):
            print("  · " + n["text"])
    print(f"\n{len(files)} tiedostoa, {len(strings)} sarjaa, {shots} uutta laukausta"
          f", Kestrel-rivejä {parsed['weather_records']} ({parsed['weather_filled']} laukaukseen sää)"
          + (f", {len(unknown)} ohitettua tiedostoa" if unknown else ""))
    if dry_run:
        print("[KOE] mitään ei tallennettu. Aja ilman --dry-run tallentaaksesi.")
        conn.close()
        return
    res = chrono.commit_strings(conn, batch)
    conn.commit()
    conn.close()
    print(f"Tallennettu {len(res['created'])} sarjaa, {sum(c['shots'] for c in res['created'])} laukausta; "
          f"{res['merged_shots']} olemassa olevaan laukaukseen lisätty sää; ohitettu {len(res['skipped'])}.")


def chrono_print_stats(gun, load):
    conn = connect()
    lnames = {r["id"]: r["name"] for r in chrono.load_rows(conn)}
    gnames = {gid: g.get("name", gid) for gid, g in chrono.guns(conn).items()}
    for d in chrono.days(conn, gun, load):
        st = d["stats"]
        print(f"{d['date']}  {gnames.get(d['gun_id'], d['gun_id'] or '–'):<20.20} "
              f"{lnames.get(d["load_id"], d["load_id"] or "–"):<52.52} n={st['n']:<3} {_fmt(st['avg'])} "
              f"SD {_fmt(st['sd'], 2)} ES {_fmt(st['es'])}  {_fmt(d['temp_c'])} °C")
    conn.close()


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
    b.add_argument("--mail", action="store_true", help="email the JSON after the backup")
    g = sub.add_parser("assign-gun")
    g.add_argument("gun_id", nargs="?")
    g.add_argument("--cal")
    g.add_argument("--type", dest="typ")
    g.add_argument("--from", dest="date_from", help="YYYY-MM-DD, inclusive")
    g.add_argument("--to", dest="date_to", help="YYYY-MM-DD, inclusive")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--list-guns", action="store_true")
    ca = sub.add_parser("chrono-alias")
    ca.add_argument("name", nargs="?")
    ca.add_argument("gun_id", nargs="?")
    ci = sub.add_parser("chrono-import")
    ci.add_argument("paths", nargs="+")
    ci.add_argument("--gun", help="gun id for every string (overrides folder/file names)")
    ci.add_argument("--dry-run", action="store_true")
    ci.add_argument("--keep-swaps", action="store_true", help="don't swap RH/pressure that look swapped")
    ci.add_argument("--fix-dates", action="store_true", help="apply suggested year fixes")
    cs = sub.add_parser("chrono-stats")
    cs.add_argument("--gun")
    cs.add_argument("--load")
    a = p.parse_args()
    init_db()
    if a.cmd == "set-password":
        set_password()
    elif a.cmd == "import":
        import_json(a.path, a.replace)
    elif a.cmd == "assign-gun":
        if a.list_guns or not a.gun_id:
            list_guns()
        elif not a.cal:
            sys.exit("Anna --cal, esim. --cal \".223 Rem\"")
        else:
            assign_gun(a.gun_id, a.cal, a.typ, a.date_from, a.date_to, a.dry_run)
    elif a.cmd == "chrono-alias":
        if a.name and not a.gun_id:
            sys.exit("Anna myös aseen tunnus, esim: python -m diary chrono-alias \"KP-15\" g-wwsd")
        chrono_alias(a.name, a.gun_id)
    elif a.cmd == "chrono-import":
        chrono_import(a.paths, a.gun, a.dry_run, a.keep_swaps, a.fix_dates)
    elif a.cmd == "chrono-stats":
        chrono_print_stats(a.gun, a.load)
    elif a.cmd == "backup":
        json_path = backup(a.directory, max(1, a.keep))
        if a.mail:
            mail_backup(json_path)
    else:
        export_json(a.path)


if __name__ == "__main__":
    main()
