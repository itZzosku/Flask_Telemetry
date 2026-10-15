# Flask_Telemetry

## Ampumapäiväkirja (`/diary/`)

Password-protected shooting diary. Code lives in `diary/`; `main.py` only calls `diary.init_app(app)`.
Everything else on the site stays public as before.

Data is stored outside git in `diary_data/` (SQLite `diary.db`, password hash, cookie key).
`diary_data/` and `diary_seed*.json` are in `.gitignore` – this repo is public, never commit them.

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

Import files may contain `profile`, `guns` and `sessions`; each part is optional and upserted by id.
Guns are for your own round counts only – PDF/CSV exports drop gun names and merge rows of the same
type, calibre, toimintatapa and lisätietoja within a visit.

Back up `diary_data/diary.db` (or a `python -m diary export` dump) with the rest of the server.

### Weekly backup by email

`scripts/diary_backup.sh` runs `python -m diary backup diary_backups --keep 12 --mail`: a dated JSON +
SQLite copy on the server (newest 12 kept) and the JSON emailed to your Gmail.

Add to the server's `.env` (never commit it):

```
DIARY_SMTP_USER=you@gmail.com
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
