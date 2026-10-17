# Flask_Telemetry

## Shooting diary (`/diary/`)

Password-protected shooting diary. Code lives in `diary/`; `main.py` only calls `diary.init_app(app)`.
Everything else on the site stays public as before.

Pages (one login, a switcher at the top of each): `/diary/` landing page with a summary of each section,
`/diary/log` diary, `/diary/velocity` muzzle velocities, `/diary/zeroes` scope zeroes. The old
`/diary/chrono` and `/diary/zero` addresses redirect. Long-press the installed app icon for shortcuts.

Data is stored outside git in `diary_data/`: `users.db` (accounts), one SQLite file per account (`diary.db` for
the first one, `users/<name>.db` for the rest) and the cookie key.
`diary_data/` and `diary_seed*.json` are in `.gitignore` – this repo is public, never commit them.

### Phone app

The diary is an installable web app (manifest + service worker in `diary/static/`). Open `/diary/` over HTTPS:
Android Chrome → menu → **Install app** (or the page's *Install app* button); iPhone Safari → Share →
**Add to Home Screen**. It then opens full-screen, starts without a connection using the last synced data,
and queues saves until the server is reachable again. Logging out clears the cached data.
After changing `diary/static/sw.js`, bump `VERSION` in it so phones drop the old cache.

### First-time setup on the server

```bash
pip install -r requirements.txt             # no new packages needed
python -m diary user-add NAME --admin --password   # first account; prompts twice, min. 10 chars
python -m diary import diary_seed.json      # one-time import of existing entries
rm diary_seed.json                          # contains licence numbers
python main.py
```

Open `https://<your-domain>/diary/`. The login lasts 90 days per device.

### Settings (`.env`, all optional)

| Variable | Default | Purpose |
|---|---|---|
| `DIARY_DATA_DIR` | `./diary_data` | Where the database and secrets live |
| `DIARY_COOKIE_SECURE` | `1` | Login cookie only over HTTPS. Set `0` to log in via `http://<LAN-IP>:5000` |
| `DIARY_BEHIND_PROXY` | `1` | Trust one `X-Forwarded-For` hop (Nginx Proxy Manager) |
| `DIARY_LOGIN_DAYS` | `90` | How long a login stays valid |
| `FLASK_SECRET_KEY` | auto | Cookie signing key; generated into `diary_data/secret_key` if unset |

### Accounts

Every person has their own account and their own database file – one user's pages, API calls, offline
queue and backups never touch another's data. Accounts are created on the server without a password: the
person logs in with their username and an **empty password** (within 7 days) and must choose their own
before anything else. Later they change it on the landing page (*Password*).

```bash
python -m diary user-add mikko             # new account, no password yet – tell them to log in soon
python -m diary user-add NAME --password   # …or set the password now (asks twice, min. 10 chars)
python -m diary user-add NAME --admin      # an admin account (admins are just flagged for now)
python -m diary user-list                  # accounts, data files, last login, unclaimed accounts
python -m diary user-passwd mikko          # new password – logs that user out on every device
python -m diary user-passwd mikko --clear  # forgot it: no password again, they choose a new one at next login
python -m diary user-disable mikko         # blocks login and ends their sessions (data kept); user-enable
python -m diary user-rename old new        # login name only; the data file stays
```

A diary from before accounts becomes the admin account **osku** (or `DIARY_ADMIN_USER`) on first start, with
the same password and data (`diary.db`).

### Admin commands

Data commands work on one account: add `--user NAME` (default: the first admin account).

```bash
python -m diary export backup.json --user mikko    # full JSON backup of one account
python -m diary import file.json [--replace] --user mikko
```

Tie old rows to a gun afterwards (only rows without a gun; dates inclusive; try `--dry-run` first):

```bash
python -m diary assign-gun --list-guns
python -m diary assign-gun g-wwsd --cal ".223 Rem" --from 2024-04-17 --to 2026-07-10 --dry-run
```

Import files may contain `profile`, `guns` and `sessions`; each part is optional and upserted by id.
Guns are for your own round counts only – PDF/CSV exports drop gun names and merge rows of the same
type, calibre, toimintatapa and lisätietoja within a visit.

Back up `diary_data/diary.db` (or a `python -m diary export` dump) with the rest of the server.

### Updating the server

```bash
./venv/bin/pip install pytest          # once: lets the update run the tests before restarting
./scripts/update.sh --check            # what would come in
./scripts/update.sh                    # pull + check + restart
```

`update.sh` refuses to run over local edits, snapshots every database (accounts and each account's data) to
`diary_backups/pre-update/` (newest 5 kept), pulls `main`, installs `requirements.txt` if it changed, compiles, starts the app once in a
throwaway folder and runs the tests. If anything fails it rolls back to the previous commit and leaves
the running site alone; otherwise it restarts and checks the site answers. Restart command:
`RESTART_CMD="…"` or a systemd unit `SERVICE` (default `flask-telemetry.service`); `./scripts/update.sh --restart`
only restarts.

### Weekly backup by email

`scripts/diary_backup.sh` runs `python -m diary backup diary_backups --keep 12 --mail`: a dated JSON +
SQLite copy on the server (newest 12 kept) and the JSON emailed to your Gmail.

Add to the server's `.env` (never commit it):

```
DIARY_SMTP_USER=you@gmail.com            # must be the Gmail address the app password belongs to
DIARY_SMTP_PASSWORD=abcdefghijklmnop     # Gmail app password, myaccount.google.com/apppasswords
# DIARY_BACKUP_TO=other@address          # optional, defaults to DIARY_SMTP_USER
```

```bash
./scripts/diary_backup.sh   # test once – check your inbox
crontab -e                  # add: 17 3 * * 0  /root/Flask_Telemetry/scripts/diary_backup.sh >> /var/log/diary_backup.log 2>&1
```

Backups contain your licence numbers – they only go to your own mailbox.

Every account is backed up: `diary-<user>-YYYY-MM-DD.json` + `.db`, newest 12 per account, and all JSONs go in
the one e-mail. Restore one account: `python -m diary import --user NAME diary_backups/diary-NAME-YYYY-MM-DD.json --replace`
(or stop the app and copy the `.db` over `diary_data/diary.db`).

## Language

The pages, the command line and the backup e-mail are in English. Firearm types and firing modes are stored
in the Finnish legal wording (Ampuma-aselaki: *Kivääri*, *2 - lippaallinen kertatuli* …) and shown in English;
*Export* writes a PDF or a CSV in Finnish (the shooting-activity record for a licence application) or in
English, for the last 2 years, a year or the whole diary; the last choice is remembered.

## Muzzle velocity (`/diary/velocity`)

Muzzle-velocity data inside the diary (same login): Garmin Xero C1 ShotView exports, your own velocity
workbooks and Kestrel DROP weather logs. Open it from the diary header (*Velocity*).

- **Share from ShotView** (Android, installed app): ShotView → session → Share → *Shooting Diary* opens the
  import preview with the file. After updating, remove and re-add the app to the home screen once so Android
  learns the share target. Install it with **Chrome** (menu → *Install app*): Vivaldi and other browsers only make
  a home-screen shortcut, which Android never lists as a share target. Not available on iPhone.
- **Import** (*+ Import velocities*): pick files or a whole folder. ShotView `.xls`/`.xlsx`, velocity workbooks
  (`<gun>, <bullet>, <brass>, <charge> gr.xlsx`, one `DDMMYYYY` sheet per day) and Kestrel CSVs are
  recognised by content. Folder names give the powder (`N555`, `N133`, `N150` …) and the gun, in either
  order. Nothing is saved until you press *Save selected* in the preview.
- Shots are fingerprinted (date + time + speed): re-importing is harmless, and a raw ShotView session that
  overlaps a stored day only fills missing weather — fliers you removed in a workbook stay removed.
- **Statistics** are computed from included shots: average, sample SD (÷ n−1), Garmin's SD (÷ n), ES,
  95 % CI, KE. Days pool all strings of the same gun + load; the same load in two guns is tracked separately.
- Views: per-day list, trend of a load (average + CI, SD per day), charge ladder per gun, shot dot-plot with
  exclude/include per shot, manual weather per string.
- **Loads**: like guns, each load is a set of fields — handload: bullet maker, bullet,
  weight, brass, powder, charge, primer, COAL; factory: maker, product, bullet weight. The name is generated,
  e.g. `6.5 CM · Lapua Scenar 139 gr · N555 44.0 gr · Lapua brass` or `.22 LR · SK Long Range Match 40 gr`.
  The calibre is part of the load: the same 123 gr FMJ in .308 Win and 7.62x53R are two loads. Saving a load
  whose name matches another offers to merge them. Loads saved before this were converted on first start.

```bash
pip install -r requirements.txt                    # adds xlrd + openpyxl
python -m diary chrono-alias "KP-15" g-wwsd        # folder/file name → diary gun (no args lists them)
python -m diary chrono-import Ballistiikkataulukot/Velocity "Ballistiikkataulukot/Garmin Xero C1" \
                              "Ballistiikkataulukot/Kestrel Drop D3" --dry-run
python -m diary chrono-import …same paths…         # commit; RH/pressure that look swapped are fixed,
                                                   # --keep-swaps to leave them, --fix-dates for year typos
python -m diary chrono-stats --gun g-t3x           # per-day table
```

Velocity tables (`loads`, `chrono_strings`, `chrono_shots`) live in `diary.db` and are part of
`export`/`import`/`backup`. `*.xls`/`*.xlsx` are git-ignored — never commit your logs. Tests use synthetic data:
`python -m pytest tests`.

## Scope zeroes (`/diary/zeroes`)

One scope moved between rifles: a dial position (elevation + up, windage left/right) per rifle and load, in
the scope's own frame where the reference position is 0 / 0. Open it from the diary or velocity header
(*Zeroes*). The database is the only copy; there is no Markdown file to keep in sync.

- **Swap**: *Scope is on* → *Move to* shows what to dial (e.g. `DOWN 2.7` / `0.1 RIGHT`, in clicks too), the
  target's turret readings and its distance above the zero stop. *Mounted on …* records the swap, then asks
  for the check shot: *On zero ✓* logs it, *Off – adjust…* opens the correction.
- **Cap readings**: with the turret turn sizes set in *Scope settings* (e.g. 10 MRAD), the swap also says where
  the caps stop if they read 0 / 0 now — elevation numbered one way (DOWN 2.9 → `7.1`, one turn below 0), windage
  both ways to the half-turn mark (`1 L … 4 L, 5, 4 R … 1 R`) with a warning when a move passes that mark.
- **Adjust zero**: tap UP/DOWN and LEFT/RIGHT by clicks for what you corrected at the range; the position
  moves by that much and the change is logged with date and note. *Undo* reverts the newest change of a
  position. Exact values can also be typed under *Details* (logged as “set”).
- **0 / 0 reference**: by default 0 / 0 is the turret's own zero — values are what the dials read, and no
  rifle has to sit at 0 / 0. *Scope settings → 0 / 0 reference* can instead pick a position: everything is
  relabelled so it reads 0 / 0 (swaps don't change). If that position is later re-zeroed off 0 / 0, the
  reference goes back to the turret zero by itself.
- **Several scopes**: *+ Scope* adds another; each has its own positions, zero stop, reference and history.
  The chips show where each scope is. Mounting a scope on a rifle takes any other scope off that rifle (logged),
  and a position whose rifle carries a different scope says so. Old scopes can be archived.
- **Offline**: with no connection at the range, adjustments, check shots, mounts, position edits and notes are
  kept on the phone, shown straight away (marked *not sent yet*) and sent in order when the connection is back
  (or with *Send now*). Each carries an id, so a change that is resent is applied once. Undo, reference changes
  and deletes need a connection.
- **Notes**: free Markdown (`## headings`, bullets, `- [ ]` to-dos that can be ticked on the page).
- **Zeroing card (PDF)**: one A4 page for the range bag — dial positions with distance above the zero stop and
  last check shot, every swap in both directions with click counts, rifles and mounts, notes, recent changes.

An old Markdown zeroing note (the `notes/javelin-zeroing-note.md` layout) is brought in once on the server:

```bash
python -m diary zero-import notes/javelin-zeroing-note.md --dry-run   # parse + check the note's swap tables
python -m diary zero-import notes/javelin-zeroing-note.md             # --replace to overwrite (history too)
```

Rifle names are matched to diary guns the same way as velocity folders (aliases, then the model, e.g. `T3X`).
Zeroes (`scopes`, `scope_zeros`, `scope_log`) are part of `export`/`import`/`backup`; so are the velocity
folder→gun aliases and the powder list (`chrono_settings`), so the emailed JSON alone restores everything.
