#!/usr/bin/env bash
# Keep the daemon up. scripts/run.sh exits immediately when a copy is already running.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
while true; do
  "$ROOT/scripts/run.sh" || true
  sleep 300
done
