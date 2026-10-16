# Flask_Telemetry

## Ampumapäiväkirja (`/diary/`)

Password-protected shooting diary. Code lives in `diary/`; `main.py` only calls `diary.init_app(app)`.
Everything else on the site stays public as before.

Data is stored outside git in `diary_data/` (SQLite `diary.db`, password hash, cookie key).
`diary_data/` and `diary_seed*.json` are in `.gitignore` – this repo is public, never commit them.

### Phone app

The diary is an installable web app (manifest + service worker in `diary/static/`). Open `/diary/` over HTTPS:
Android Chrome → menu → **Install app** (or the page's *Asenna sovellus* button); iPhone Safari → Share →
**Add to Home Screen**. It then opens full-screen, starts without a connection using the last synced data,
and queues saves until the server is reachable again. Logging out clears the cached data.
After changing `diary/static/sw.js`, bump `VERSION` in it so phones drop the old cache.

### First-time setup on the server

```bash
pip install -r requirements.txt             # no new packages needed
python -m diary set-password                # prompts twice, min. 10 chars
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

### Admin commands

```bash
python -m diary set-password          # change password – logs out every device
python -m diary export backup.json    # full JSON backup
python -m diary import file.json [--replace]
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

Restore: `python -m diary import diary_backups/diary-YYYY-MM-DD.json --replace`
(or stop the app and copy the `.db` over `diary_data/diary.db`).

## Lähtönopeudet (`/diary/chrono`)

Muzzle-velocity data inside the diary (same login): Garmin Xero C1 ShotView exports, your own velocity
workbooks and Kestrel DROP weather logs. Open it from the diary header (*Nopeudet*).

- **Import** (*+ Tuo nopeudet*): pick files or a whole folder. ShotView `.xls`/`.xlsx`, velocity workbooks
  (`<gun>, <bullet>, <brass>, <charge> gr.xlsx`, one `DDMMYYYY` sheet per day) and Kestrel CSVs are
  recognised by content. Folder names give the powder (`N555`, `N133`, `N150` …) and the gun, in either
  order. Nothing is saved until you press *Tallenna valitut* in the preview.
- Shots are fingerprinted (date + time + speed): re-importing is harmless, and a raw ShotView session that
  overlaps a stored day only fills missing weather — fliers you removed in a workbook stay removed.
- **Statistics** are computed from included shots: average, sample SD (÷ n−1), Garmin's SD (÷ n), ES,
  95 % CI, KE. Days pool all strings of the same gun + load; the same load in two guns is tracked separately.
- Views: per-day list, trend of a load (average + CI, SD per day), charge ladder per gun, shot dot-plot with
  exclude/include per shot, manual weather per string.

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
