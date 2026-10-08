#!/usr/bin/env bash
# Generate iOS, Android and desktop icons from one 1024 PNG.
# Writes only to --out. It does not replace icons committed in this repository.
#
#   scripts/brand-assets.sh --png icon-1024.png --out /path/to/private/brand \
#       [--svg logo.svg] [--tray tray.png]
#
# --svg or --tray supplies the menu-bar / notification icons. Without either,
# those icons are resized from the PNG. See docs/extending.md.
set -euo pipefail

usage() {
  echo "用法：scripts/brand-assets.sh --png 1024.png --out 目录 [--svg 标志.svg] [--tray 托盘.png]" >&2
}

PNG=""
SVG=""
TRAY=""
OUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --png)
      PNG="${2:-}"
      shift 2
      ;;
    --svg)
      SVG="${2:-}"
      shift 2
      ;;
    --tray)
      TRAY="${2:-}"
      shift 2
      ;;
    --out)
      OUT="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "错误：未知参数 $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$PNG" || -z "$OUT" ]]; then
  echo "错误：必须同时提供 --png 和 --out。" >&2
  usage
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
args=(--png "$PNG" --out "$OUT")
if [[ -n "$SVG" ]]; then
  args+=(--svg "$SVG")
fi
if [[ -n "$TRAY" ]]; then
  args+=(--tray "$TRAY")
fi

exec python3 "$SCRIPT_DIR/brand_assets.py" "${args[@]}"
