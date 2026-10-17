#!/usr/bin/env bash
# Update the site from GitHub main and restart it — only if the new code passes its checks.
#
#   ./scripts/update.sh            pull, check, restart
#   ./scripts/update.sh --check    show what would come in, change nothing
#   ./scripts/update.sh --restart  just restart (no pull)
#
# Steps: refuse if there are local edits → snapshot diary.db → git pull → pip install if requirements.txt
# changed → compile + tests (pytest, if installed in the venv) → on failure roll back to the old commit and
# keep the old version running → restart → check the site answers.
#
# Restart: set RESTART_CMD (e.g. in the environment or below), or SERVICE for a systemd unit.
#   RESTART_CMD="systemctl restart flask-telemetry.service" ./scripts/update.sh
# Without either, the script tries systemd unit $SERVICE (default flask-telemetry.service) and otherwise tells
# you to restart by hand.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$REPO_DIR/venv/bin/python}"
PIP="${PIP:-$REPO_DIR/venv/bin/pip}"
SERVICE="${SERVICE:-flask-telemetry.service}"
RESTART_CMD="${RESTART_CMD:-}"
BRANCH="${BRANCH:-main}"
DATA_DIR="${DIARY_DATA_DIR:-$REPO_DIR/diary_data}"
SNAP_DIR="${SNAP_DIR:-$REPO_DIR/diary_backups/pre-update}"   # git-ignored (diary_backups/)
SNAP_KEEP="${SNAP_KEEP:-5}"
PORT="${PORT:-5000}"
HEALTH_URL="${HEALTH_URL:-http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT/diary/login}"

say()  { printf '\n== %s\n' "$*"; }
fail() { printf '\n!! %s\n' "$*" >&2; exit 1; }

cd "$REPO_DIR"
MODE="${1:-}"

restart() {
  if [ -n "$RESTART_CMD" ]; then
    say "Restarting: $RESTART_CMD"; eval "$RESTART_CMD"
  elif command -v systemctl >/dev/null && systemctl cat "$SERVICE" >/dev/null 2>&1; then
    say "Restarting systemd unit $SERVICE"; systemctl restart "$SERVICE"
  else
    say "Restart the site yourself now (no RESTART_CMD set and no systemd unit '$SERVICE')."
    return 0
  fi
  # Give it a moment, then check it answers
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    if curl -fsS -o /dev/null "$HEALTH_URL" 2>/dev/null; then say "Site answers at $HEALTH_URL"; return 0; fi
  done
  fail "No answer from $HEALTH_URL after the restart – check the service log."
}

if [ "$MODE" = "--restart" ]; then restart; exit 0; fi

# 1. Local edits would be overwritten or block the pull
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  git status --short --untracked-files=no
  fail "Local changes in tracked files. Commit or undo them first (git checkout -- <file>), or reset with: git reset --hard origin/$BRANCH"
fi
[ "$(git rev-parse --abbrev-ref HEAD)" = "$BRANCH" ] || fail "Not on $BRANCH (git checkout $BRANCH)."

say "Fetching origin/$BRANCH"
git fetch --quiet origin "$BRANCH"
OLD="$(git rev-parse HEAD)"
NEW="$(git rev-parse "origin/$BRANCH")"
if [ "$OLD" = "$NEW" ]; then
  say "Already up to date ($(git log -1 --format='%h %s'))."
  exit 0
fi
git merge-base --is-ancestor "$OLD" "$NEW" || fail "Local $BRANCH has commits that aren't on GitHub – sort that out by hand."

echo "Coming in:"
git log --format='  %h %s' "$OLD..$NEW"
if [ "$MODE" = "--check" ]; then exit 0; fi

# 2. Snapshot the database before new code (and its migrations) touches it
if [ -f "$DATA_DIR/diary.db" ]; then
  mkdir -p "$SNAP_DIR"
  SNAP="$SNAP_DIR/diary-$(date +%Y%m%d-%H%M%S)-${OLD:0:7}.db"
  "$PYTHON" - "$DATA_DIR/diary.db" "$SNAP" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
src.close(); dst.close()
PY
  say "Database snapshot: $SNAP"
  ls -1t "$SNAP_DIR"/diary-*.db 2>/dev/null | tail -n +"$((SNAP_KEEP + 1))" | xargs -r rm --
fi

rollback() {
  printf '\n!! %s – rolling back to %s\n' "$1" "${OLD:0:7}" >&2
  git reset --quiet --hard "$OLD"
  if [ "$REQ_CHANGED" = 1 ]; then "$PIP" install --quiet -r requirements.txt || true; fi
  fail "Update aborted. The old version is still the one on disk; nothing was restarted."
}

# 3. Pull
say "Updating ${OLD:0:7} → ${NEW:0:7}"
git merge --quiet --ff-only "$NEW"

REQ_CHANGED=0
if ! git diff --quiet "$OLD" "$NEW" -- requirements.txt; then
  REQ_CHANGED=1
  say "requirements.txt changed – installing"
  "$PIP" install --quiet -r requirements.txt || rollback "pip install failed"
fi

# 4. Checks: everything compiles, the app imports, tests pass (tests use a throwaway data folder)
say "Checking"
"$PYTHON" -m compileall -q diary main.py >/dev/null || rollback "Python files don't compile"
DIARY_DATA_DIR="$(mktemp -d)" "$PYTHON" -c "import diary, flask; a = flask.Flask('x'); diary.init_app(a)" \
  || rollback "The app doesn't start"
if "$PYTHON" -c "import pytest" 2>/dev/null; then
  "$PYTHON" -m pytest -q tests || rollback "Tests failed"
else
  echo "  (pytest not installed in the venv – skipping tests; install with: $PIP install pytest)"
fi

# 5. Restart
restart
say "Updated to $(git log -1 --format='%h %s')"
