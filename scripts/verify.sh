#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR/backend"
"${PYTHON_BIN:-.venv/bin/python}" -m unittest discover -s tests
cd "$ROOT_DIR/apps/web"
npm run build
echo "Luma verification passed"
