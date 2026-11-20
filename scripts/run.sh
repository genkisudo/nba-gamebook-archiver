#!/usr/bin/env bash
# Start the daemon under nohup. The process rotates gbwatch.log itself.
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

export DATA_DIR
# IA_ACCESS and IA_SECRET, when set in the environment, are picked up by the
# process. This script never prints them.
# The daemon writes gbwatch.log itself. stdout is discarded.
# stderr is appended so a crash before logging is still visible.
nohup "$ROOT/.venv/bin/gbwatch" run >/dev/null 2>>"$LOG" &
echo $! >"$PIDFILE"
echo "started pid $(cat "$PIDFILE") log $LOG"
