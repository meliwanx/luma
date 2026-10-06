#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR/backend"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ] && ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Set PYTHON_BIN to a Python environment with backend/requirements.txt installed." >&2
  exit 1
fi
exec "$PYTHON_BIN" -m uvicorn app.main:app --reload --host 127.0.0.1 --port "${PORT:-8787}"
