#!/usr/bin/env bash
# Start the daemon under nohup and rotate the log when it passes 10MB.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -x "$ROOT/.venv/bin/gbwatch" ]]; then
  echo "run scripts/install.sh first" >&2
  exit 1
fi

DATA_DIR="${DATA_DIR:-$ROOT/data}"
mkdir -p "$DATA_DIR"
LOG="$DATA_DIR/gbwatch.log"
PIDFILE="$DATA_DIR/gbwatch.pid"

if [[ -f "$PIDFILE" ]]; then
  old="$(cat "$PIDFILE" || true)"
  if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
    echo "already running pid $old"
    exit 0
  fi
fi

if [[ -f "$LOG" ]]; then
  size="$(stat -c%s "$LOG" 2>/dev/null || echo 0)"
  if [[ "$size" -gt 10485760 ]]; then
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    mv "$LOG" "$LOG.$ts"
    ls -1t "$LOG".* 2>/dev/null | tail -n +6 | xargs -r rm -f
  fi
fi

export DATA_DIR
# IA_ACCESS and IA_SECRET, when set in the environment, are picked up by the
# process. This script never prints them.
nohup "$ROOT/.venv/bin/gbwatch" run >>"$LOG" 2>&1 &
echo $! >"$PIDFILE"
echo "started pid $(cat "$PIDFILE") log $LOG"
