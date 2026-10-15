#!/usr/bin/env bash
# Weekly shooting-diary backup: dated JSON + SQLite copy in diary_backups/, JSON emailed to you.
# Needs DIARY_SMTP_USER and DIARY_SMTP_PASSWORD (Gmail app password) in the repo's .env.
# Cron (Sundays 03:17):  17 3 * * 0  /root/Flask_Telemetry/scripts/diary_backup.sh >> /var/log/diary_backup.log 2>&1
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$REPO_DIR/venv/bin/python}"
BACKUP_DIR="${BACKUP_DIR:-$REPO_DIR/diary_backups}"    # git-ignored
KEEP="${KEEP:-12}"                                       # copies kept on the server

echo "== $(date -Is) diary backup"
cd "$REPO_DIR"
"$PYTHON" -m diary backup "$BACKUP_DIR" --keep "$KEEP" --mail
