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

# Held by the daemon (fd 9 is inherited). A reused pid must not look like us.
exec 9>"$DATA_DIR/gbwatch.lock"
if ! flock -n 9; then
  echo "already running"
  exit 0
fi

export DATA_DIR
# IA_ACCESS and IA_SECRET, when set in the environment, are picked up by the
# process. This script never prints them.
# The daemon writes gbwatch.log itself. stdout is discarded.
# stderr is appended so a crash before logging is still visible.
nohup "$ROOT/.venv/bin/gbwatch" run >/dev/null 2>>"$LOG" &
echo $! >"$PIDFILE"
echo "started pid $(cat "$PIDFILE") log $LOG"
