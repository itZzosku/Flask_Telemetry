#!/usr/bin/env bash
# Weekly shooting-diary backup: local dated copy + upload to Google Drive with rclone.
# Cron (Sundays 03:17):  17 3 * * 0  /root/Flask_Telemetry/scripts/diary_backup.sh >> /var/log/diary_backup.log 2>&1
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$REPO_DIR/venv/bin/python}"
BACKUP_DIR="${BACKUP_DIR:-$REPO_DIR/diary_backups}"    # git-ignored
REMOTE="${REMOTE:-gdrive:Ampumapaivakirja}"              # rclone remote:folder
KEEP="${KEEP:-12}"                                       # local copies to keep

echo "== $(date -Is) diary backup"
cd "$REPO_DIR"
"$PYTHON" -m diary backup "$BACKUP_DIR" --keep "$KEEP"
# copy (not sync): Drive keeps every weekly file even after local pruning
rclone copy "$BACKUP_DIR" "$REMOTE" --include "diary-*" --log-level NOTICE
echo "== uploaded to $REMOTE"
