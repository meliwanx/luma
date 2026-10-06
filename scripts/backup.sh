#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"
BACKUP_ROOT="${1:-$ROOT_DIR/backups}"
mkdir -p "$BACKUP_ROOT"
BACKUP_DIR="$(mktemp -d "$BACKUP_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"

restart_luma=false
cleanup() {
  status=$?
  trap - EXIT
  if [ "$restart_luma" = true ]; then
    docker compose start luma || status=1
  fi
  if [ "$status" -ne 0 ]; then
    echo "Backup failed; incomplete files remain in $BACKUP_DIR" >&2
  fi
  exit "$status"
}
trap cleanup EXIT

if docker compose ps --status running --services | grep -qx luma; then
  restart_luma=true
  # Stop writes so the database and local files describe the same snapshot.
  docker compose stop luma
fi
docker compose exec -T postgres sh -c \
  'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-privileges' \
  > "$BACKUP_DIR/database.dump"
docker compose run --rm --no-deps -T --user 0 --entrypoint tar luma \
  -C /opt/luma/backend/data -czf - . > "$BACKUP_DIR/files.tar.gz"
(
  cd "$BACKUP_DIR"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum database.dump files.tar.gz > SHA256SUMS
  else
    shasum -a 256 database.dump files.tar.gz > SHA256SUMS
  fi
)
echo "Backup written to $BACKUP_DIR"
