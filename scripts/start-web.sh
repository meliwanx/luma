#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR/apps/web"
if [ ! -d node_modules ]; then npm ci; fi
exec npm run dev -- --host 127.0.0.1
