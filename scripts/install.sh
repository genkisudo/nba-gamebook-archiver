#!/usr/bin/env bash
# Create a virtualenv and install gbwatch. poppler-utils must already be present.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v pdftotext >/dev/null 2>&1 || ! command -v pdfinfo >/dev/null 2>&1; then
  echo "pdftotext and pdfinfo are required. Install them with: sudo apt-get install -y poppler-utils" >&2
  exit 1
fi

PY="${PYTHON:-python3}"
"$PY" -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e ".[dev]"
echo "installed. next: source .venv/bin/activate"
