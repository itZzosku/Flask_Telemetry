"""Server-side admin for the shooting diary.

Accounts (each has its own data file):
  python -m diary user-add NAME [--admin]   new account without a password: they log in with an empty
                                            password within 7 days and choose one (--password: set it now)
  python -m diary user-list                 accounts, data files, last login
  python -m diary user-passwd NAME [--clear] set a new password, or --clear it so they choose one at next
                                            login (either logs that user out everywhere)
  python -m diary user-disable NAME / user-enable NAME / user-rename OLD NEW
  python -m diary user-delete NAME          remove the account and its data (final copy in diary_data/deleted/)

Data commands work on one account: add --user NAME (default: the first admin account, i.e. diary.db).
  python -m diary import seed.json        load sessions, guns + profile from JSON
  python -m diary import seed.json --replace   wipe sessions first
  python -m diary export backup.json      dump everything to JSON
  python -m diary backup DIR [--keep 12] [--mail]
                                          every account: dated JSON + SQLite copy into DIR, keep newest N
                                          per account, --mail: also email the JSONs (settings in .env)
  python -m diary assign-gun GUN_ID --cal ".223 Rem" [--type Kivääri] [--from 2024-04-17] [--to 2026-07-10]
                                          tie existing rows without a gun to GUN_ID (dates inclusive);
                                          --dry-run shows what would change, --list-guns shows ids
  python -m diary chrono-alias [NAME GUN_ID]   map a folder/file name (e.g. "KP-15") to a diary gun; no args lists
  python -m diary chrono-import PATH... [--gun ID] [--dry-run] [--keep-swaps] [--fix-dates]
                                          import ShotView exports, velocity workbooks and Kestrel CSVs
                                          (files or folders, searched recursively)
  python -m diary chrono-stats [--gun ID] [--load ID]   per-day velocity table
  python -m diary zero-import NOTE.md [--replace] [--dry-run]
                                          one-time import of an old Markdown zeroing note
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

from . import DATA_DIR, connect, init_db, kv_get, kv_set, upsert_gun, upsert_session, use_db
from . import chrono, users, zero


# ------------------------------------------------------------------ accounts
def _ask_password():
    pw = getpass("Password: ")
    if len(pw) < users.MIN_PASSWORD:
        sys.exit(f"Use at least {users.MIN_PASSWORD} characters.")
    if pw != getpass("Again: "):
        sys.exit("The passwords do not match.")
    return pw


def user_add(name, admin, with_password):
    try:
        users.check_name(name)
        if users.by_name(name):
            sys.exit(f"User {name} already exists.")
        u = users.create(name, _ask_password() if with_password else None, admin=admin)
    except ValueError as e:
        sys.exit(str(e))
    print(f"Created {u['username']}{' (admin)' if admin else ''} – data in {users.db_path(u)}")
    if not with_password:
        print(f"No password yet: {u['username']} logs in with an EMPTY password within {users.SETUP_DAYS} days "
              f"(until {u['setup_until'][:10]}) and chooses one.\n"
              "Until then anyone who knows the username could claim it – tell them soon.")


def user_list():
    rows = users.all_users()
    if not rows:
        print("No accounts. Create one: python -m diary user-add NAME --admin")
    for u in rows:
        flags = ", ".join(f for f in ("admin" if u["is_admin"] else "", "DISABLED" if u["disabled"] else "",
                                      ("no password – first login until " + u["setup_until"][:10]) if users.needs_setup(u)
                                      else ("no password, first-login window expired (user-passwd --clear)"
                                            if not u["pw_hash"] else "")) if f)
        print(f"{u['username']:<20} {u['db_file']:<24} last login {u['last_login'] or '–':<22} {flags}")


def user_passwd(name, clear=False):
    if not users.by_name(name):
        sys.exit(f"No user {name}.")
    if clear:
        users.clear_password(name)
        return print(f"{name} has no password now and is logged out everywhere. They log in with an EMPTY "
                     f"password within {users.SETUP_DAYS} days and choose a new one.")
    users.set_password(name, _ask_password())
    print(f"Password set for {name}. Their existing logins have been logged out.")


def _pick_user(name):
    """--user NAME → that account's data file for every data command; default: the first admin's."""
    users.migrate_single_user()
    if name:
        u = users.by_name(name)
        if not u:
            sys.exit(f"No user {name}. See: python -m diary user-list")
    else:
        admins = [u for u in users.all_users() if u["is_admin"]]
        u = admins[0] if admins else None
    if u:
        use_db(users.db_path(u))
        init_db(users.db_path(u))
    return u


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
    z = zero.import_rows(conn, data, replace)
    conn.commit()
    conn.close()
    extra = f", {c['chrono_strings']} strings / {c['chrono_shots']} velocities" if c["chrono_shots"] else ""
    if z["scope_zeros"]:
        extra += f", {z['scopes']} scopes / {z['scope_zeros']} zeroes"
    print(f"Imported {n} visits and {g} guns{extra} → {DATA_DIR}")


def export_json(path):
    from .summary import export_data
    conn = connect()
    data = export_data(conn)
    conn.close()
    sessions = data["sessions"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"Exported {len(sessions)} visits → {path}")


def backup(directory, keep):
    """Every account: diary-<user>-YYYY-MM-DD.json and .db into directory, newest `keep` per account.
    Returns [(username, json_path)]."""
    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    done = []
    accounts = users.all_users() or [None]          # no accounts yet: the plain diary.db
    for u in accounts:
        name = u["username"] if u else "diary"
        path = users.db_path(u) if u else os.path.join(DATA_DIR, "diary.db")
        if not os.path.exists(path):
            continue
        use_db(path)
        base = os.path.join(directory, f"diary-{name}-{stamp}")
        export_json(base + ".json")
        # sqlite3's backup API gives a consistent copy even while the app is running.
        src, dst = sqlite3.connect(path), sqlite3.connect(base + ".db")
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        for ext in (".json", ".db"):
            old = sorted(glob.glob(os.path.join(directory, f"diary-{name}-[0-9]*" + ext)))[:-keep]
            for p in old:
                os.remove(p)
        print(f"Backup {name}: {base}.json + .db (keeping the newest {keep})")
        done.append((name, base + ".json"))
    return done


def _summary(data):
    sessions = data.get("sessions", [])
    rounds = sum(int(l.get("rounds") or 0) for s in sessions for l in s.get("lines", []))
    return (f"Visits: {len(sessions)}, shooting days {len({s['date'] for s in sessions})}, shots {rounds}, "
            f"last visit {max((s['date'] for s in sessions), default='–')}\n"
            f"Guns: {len(data.get('guns', []))}\n"
            f"Velocity strings: {len(data.get('chrono_strings', []))}, shots {len(data.get('chrono_shots', []))}, "
            f"name aliases {len((data.get('chrono_settings') or {}).get('aliases', {}))}, "
            f"powders {len((data.get('chrono_settings') or {}).get('powders', []))}\n"
            f"Scopes: {len(data.get('scopes', []))}, zeroes {len(data.get('scope_zeros', []))}\n")


def mail_backup(files):
    """Email the backup JSONs (one per account) in one message. .env: DIARY_SMTP_USER, DIARY_SMTP_PASSWORD
    (Gmail app password), optional DIARY_BACKUP_TO (default: DIARY_SMTP_USER), DIARY_SMTP_HOST, DIARY_SMTP_PORT."""
    user = os.getenv("DIARY_SMTP_USER", "").strip().strip('"\'')
    # Google shows the app password in groups; copying can bring non-breaking spaces along.
    password = "".join(os.getenv("DIARY_SMTP_PASSWORD", "").strip().strip('"\'').split())
    if not user or not password:
        sys.exit("DIARY_SMTP_USER ja DIARY_SMTP_PASSWORD puuttuvat .env-tiedostosta.")
    to = os.getenv("DIARY_BACKUP_TO", user).strip()
    host = os.getenv("DIARY_SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("DIARY_SMTP_PORT", "587"))

    msg = EmailMessage()
    msg["Subject"] = f"Shooting diary backup {time.strftime('%Y-%m-%d')}"
    msg["From"] = user
    msg["To"] = to
    body = [f"Weekly backup of the shooting diary – {len(files)} account{'s' if len(files) != 1 else ''}.\n"]
    attachments = []
    for account, json_path in files:
        with open(json_path, "rb") as f:
            payload = f.read()
        name = os.path.basename(json_path)
        body.append(f"== {account} ({name})\n" + _summary(json.loads(payload)))
        attachments.append((payload, name))
    body.append("Restore one account on the server:\n"
                "  ./venv/bin/python -m diary import --user NAME diary-NAME-YYYY-MM-DD.json --replace\n"
                "Backups contain licence numbers – keep them to yourself.\n")
    msg.set_content("\n".join(body))
    for payload, name in attachments:
        msg.add_attachment(payload, maintype="application", subtype="json", filename=name)

    if not password.isascii() or ("gmail" in host and len(password) != 16):
        sys.exit(f"DIARY_SMTP_PASSWORD looks wrong: {len(password)} characters"
                 f"{'' if password.isascii() else ', contains special characters'}. "
                 "A Gmail app password is 16 letters – type it into .env by hand.")
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if os.getenv("DIARY_SMTP_STARTTLS", "1") != "0":
                smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        sys.exit(f"Login rejected ({e.smtp_code}). Check that DIARY_SMTP_USER is the same "
                 "Google account the app password was made for, and that the password is right.")
    except (smtplib.SMTPException, OSError) as e:
        sys.exit(f"Sending the e-mail failed: {type(e).__name__}: {e}")
    print(f"Sent: {', '.join(n for _, n in attachments)} → {to}")


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
        sys.exit(f"No gun {gun_id!r}. See the ids: python -m diary assign-gun --list-guns")
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
    print(f"{'[DRY RUN] ' if dry_run else ''}{gun.get('name', gun_id)}: {lines} rows / {visits} visits / "
          f"{rounds} shots{span}" + (f" (skipped {skipped} rows that already have a gun)" if skipped else ""))


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
            sys.exit(f"No gun {gun_id!r}. See the ids: python -m diary assign-gun --list-guns")
        al[name] = gun_id
        kv_set(chrono.ALIAS_KEY, json.dumps(al, ensure_ascii=False), conn)
    names = {gid: g.get("name", "") for gid, g in chrono.guns(conn).items()}
    for a, gid in sorted(al.items()):
        print(f"{a:<28} → {gid} ({names.get(gid, '?')})")
    if not al:
        print("No aliases. E.g.: python -m diary chrono-alias \"KP-15\" g-wwsd")
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
        flags = "".join({"weather_range": " ⇄weather", "date_outlier": " ?year", "date_corrected": " ✓date",
                         "check_mismatch": " ≠garmin", "no_load": ""}.get(x["code"], "") for x in w)
        st = s["stats"]
        fps = {fingerprint(s["date"], x["t"], x["v_ms"]) for x in s["shots"]} - {None}
        in_batch = bool(fps & seen)
        seen |= fps
        done = " (already imported)" if s["already_imported"] or s["existing_shots"] else (" (same as above)" if in_batch else "")
        print(f"{s['date']} {s['started_at'] or '     '}  {gnames.get(gid, gid or '?'):<20.20} {load:<52.52} "
              f"n={st['n']:<3} {_fmt(st['avg'])} SD {_fmt(st['sd'], 2)}{flags}{done}")
        if not (s["already_imported"] or s["existing_shots"] or in_batch):
            shots += st["n"]
            batch.append(dict(s, gun_id=gid, load_id=sug["load_id"], load=sug["load"], visit_id=sug["visit_id"]))
    for n in parsed["notes"]:
        if n["code"] not in ("empty",):
            print("  · " + n["text"])
    print(f"\n{len(files)} files, {len(strings)} strings, {shots} new shots"
          f", {parsed['weather_records']} Kestrel rows (weather on {parsed['weather_filled']} shots)"
          + (f", {len(unknown)} files skipped" if unknown else ""))
    if dry_run:
        print("[DRY RUN] nothing saved. Run without --dry-run to save.")
        conn.close()
        return
    res = chrono.commit_strings(conn, batch)
    conn.commit()
    conn.close()
    print(f"Saved {len(res['created'])} strings, {sum(c['shots'] for c in res['created'])} shots; "
          f"weather added to {res['merged_shots']} existing shots; skipped {len(res['skipped'])}.")


def chrono_print_stats(gun, load):
    conn = connect()
    lnames = {r["id"]: r["name"] for r in chrono.load_rows(conn)}
    gnames = {gid: g.get("name", gid) for gid, g in chrono.guns(conn).items()}
    for d in chrono.days(conn, gun, load):
        st = d["stats"]
        print(f"{d['date']}  {gnames.get(d['gun_id'], d['gun_id'] or '–'):<20.20} "
              f"{lnames.get(d["load_id"], d["load_id"] or "–"):<52.52} n={st['n']:<3} {_fmt(st['avg'])} "
              f"SD {_fmt(st['sd'], 2)} ES {_fmt(st['es'])} {str(st['ke_j']) + ' J' if st['ke_j'] else '':>7} "
              f"{_fmt(d['temp_c'])} °C")
    conn.close()


# ------------------------------------------------------------------ scope zeroes
def _dist(z):
    return f"{z['distance_m']:g} m" if z.get("distance_m") else ""


def zero_import(path, replace, dry_run):
    from . import zero_note as zn
    with open(path, encoding="utf-8") as f:
        md = f.read()
    try:
        p = zn.parse_note(md)
    except ValueError as e:
        sys.exit(str(e))
    sc = p["scope"]
    c = sc["click"]
    print(f"{sc['name']} · {sc['unit']} · click {c:g} · zero stop "
          f"{zn.elev_dial(sc['zero_stop'], c) if sc['zero_stop'] is not None else '–'} · updated {sc['updated'] or '–'}")
    for z in p["zeros"]:
        print(f"  {'*' if z['is_ref'] else ' '} {zn.position_name(z):<40.40} {zn.elev_dial(z['elev'], c):>6} "
              f"{zn.wind_dial(z['wind'], c):>11}   {z.get('cal') or ''} {_dist(z)}")
    chk = zn.check_swaps(p["zeros"], p["swaps"], c)
    print(f"Swap tables in the note: {chk['ok']} of {chk['ok'] + len(chk['bad'])} agree with the dial positions")
    for b in chk["bad"]:
        print("  ! " + b)
    if dry_run:
        print("Dry run – nothing saved.")
        return
    conn = connect()
    try:
        res = zero.import_note(conn, md, replace)
    except ValueError as e:
        if str(e).startswith("exists:"):
            sys.exit(f"{sc['name']} is already in the database – use --replace to overwrite it "
                     "(its change history is replaced too).")
        sys.exit(str(e))
    conn.commit()
    gnames = {gid: g.get("name", gid) for gid, g in chrono.guns(conn).items()}
    lnames = {r["id"]: r["name"] for r in chrono.load_rows(conn)}
    for name, l in res["links"].items():
        print(f"  {name:<40.40} gun: {gnames.get(l['gun_id'], '– (pick on the page)')}"
              + (f" · load: {lnames.get(l['load_id'])}" if l["load_id"] else ""))
    conn.close()
    print(f"{'Created' if res['created'] else 'Replaced'} {sc['name']} with {res['zeros']} positions.")


def main():
    p = argparse.ArgumentParser(prog="python -m diary")
    sub = p.add_subparsers(dest="cmd", required=True)
    u = argparse.ArgumentParser(add_help=False)
    u.add_argument("--user", help="account whose data to use (default: the first admin account)")
    _add = sub.add_parser
    sub.add_parser = lambda name, **kw: _add(name, parents=[u], **kw)   # --user on every command
    sub.add_parser("set-password")
    ua = sub.add_parser("user-add")
    ua.add_argument("name")
    ua.add_argument("--admin", action="store_true")
    ua.add_argument("--password", action="store_true", help="set the password now instead of on first login")
    sub.add_parser("user-list")
    sub.add_parser("user-delete").add_argument("name")
    for cmd in ("user-passwd", "user-disable", "user-enable"):
        sub.add_parser(cmd).add_argument("name")
    sub.choices["user-passwd"].add_argument("--clear", action="store_true",
                                            help="remove the password: they choose a new one at next login")
    ur = sub.add_parser("user-rename")
    ur.add_argument("old")
    ur.add_argument("new")
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
    zi = sub.add_parser("zero-import")
    zi.add_argument("path")
    zi.add_argument("--replace", action="store_true", help="overwrite a scope with the same name")
    zi.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    init_db()
    users.migrate_single_user()          # first run after accounts came in: the old diary becomes an account
    if a.cmd == "user-add":
        return user_add(a.name, a.admin, a.password)
    if a.cmd == "user-list":
        return user_list()
    if a.cmd in ("user-passwd", "set-password"):
        name = getattr(a, "name", None) or a.user or (_pick_user(None) or {}).get("username")
        if not name:
            sys.exit("No accounts yet. Create one: python -m diary user-add NAME --admin")
        return user_passwd(name, getattr(a, "clear", False))
    if a.cmd == "user-delete":
        try:
            if input(f"Delete {a.name} and all their data? A final copy is kept. Type the username: ").strip() != a.name:
                sys.exit("Not deleted.")
            keep = users.delete(a.name)
        except ValueError as e:
            sys.exit(str(e))
        return print(f"Deleted {a.name}. Final copy in {keep}")
    if a.cmd in ("user-disable", "user-enable", "user-rename"):
        try:
            if a.cmd == "user-rename":
                users.rename(a.old, a.new)
            else:
                users.set_disabled(a.name, a.cmd == "user-disable")
        except ValueError as e:
            sys.exit(str(e))
        return print("Done.")
    if a.cmd != "backup":
        who = _pick_user(a.user)
        if who and a.cmd not in ("chrono-stats",):
            print(f"[account: {who['username']}]", file=sys.stderr)
    if a.cmd == "import":
        import_json(a.path, a.replace)
    elif a.cmd == "assign-gun":
        if a.list_guns or not a.gun_id:
            list_guns()
        elif not a.cal:
            sys.exit("Give --cal, e.g. --cal \".223 Rem\"")
        else:
            assign_gun(a.gun_id, a.cal, a.typ, a.date_from, a.date_to, a.dry_run)
    elif a.cmd == "chrono-alias":
        if a.name and not a.gun_id:
            sys.exit("Give the gun id too, e.g.: python -m diary chrono-alias \"KP-15\" g-wwsd")
        chrono_alias(a.name, a.gun_id)
    elif a.cmd == "chrono-import":
        chrono_import(a.paths, a.gun, a.dry_run, a.keep_swaps, a.fix_dates)
    elif a.cmd == "chrono-stats":
        chrono_print_stats(a.gun, a.load)
    elif a.cmd == "zero-import":
        zero_import(a.path, a.replace, a.dry_run)
    elif a.cmd == "backup":
        files = backup(a.directory, max(1, a.keep))
        if a.mail and files:
            mail_backup(files)
    else:
        export_json(a.path)


if __name__ == "__main__":
    main()
