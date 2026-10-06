#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${API_BASE_URL:-}" ]]; then
  echo "错误：必须设置 API_BASE_URL。" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

FLUTTER_BIN="$(command -v flutter)"
BUILD_NAME="$(awk '/^version:/ {split($2, version, "[+]"); print version[1]; exit}' pubspec.yaml)"
BUILD_NUMBER="${BUILD_NUMBER:-$(awk '/^version:/ {split($2, version, "[+]"); print version[2]; exit}' pubspec.yaml)}"
if [[ ! "$BUILD_NUMBER" =~ ^[0-9]+$ ]]; then
  echo "错误：BUILD_NUMBER 必须是数字，或在 pubspec.yaml 的 version 中指定构建号。" >&2
  exit 1
fi

exec "$FLUTTER_BIN" build ipa --release \
  "--dart-define=API_BASE_URL=$API_BASE_URL" \
  "--dart-define=APP_VERSION=$BUILD_NAME+$BUILD_NUMBER" \
  "--build-number=$BUILD_NUMBER" \
  --export-options-plist=ios/ExportOptions.plist
