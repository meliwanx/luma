#!/usr/bin/env bash
set -euo pipefail
umask 077

if [ "$#" -ne 2 ] || [ "$2" != --yes ]; then
  echo "Usage: $0 BACKUP_DIRECTORY --yes" >&2
  echo "Replaces the current database and local files; all sessions will be invalidated." >&2
  exit 2
fi
BACKUP_DIR="$(cd "$1" && pwd)"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"
for name in database.dump files.tar.gz SHA256SUMS; do
  if [ ! -f "$BACKUP_DIR/$name" ]; then
    echo "Missing backup file: $name" >&2
    exit 2
  fi
done
(
  cd "$BACKUP_DIR"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -c SHA256SUMS
  else
    shasum -a 256 -c SHA256SUMS
  fi
)

# Validate before changing any data. Archives contain only relative files and
# directories; links, devices and traversal paths are never accepted.
docker compose run --rm --no-deps -T --entrypoint python luma -c '
import sys, tarfile
from pathlib import PurePosixPath
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
    for member in archive:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
            raise SystemExit("Unsafe file archive")
' < "$BACKUP_DIR/files.tar.gz"

restart_luma=false
restore_complete=false
cleanup() {
  status=$?
  trap - EXIT
  if [ "$restore_complete" = true ] && [ "$restart_luma" = true ]; then
    docker compose start luma || status=1
  elif [ "$status" -ne 0 ]; then
    echo "Restore failed; keep Luma stopped until the backup is fully restored." >&2
  fi
  exit "$status"
}
trap cleanup EXIT

if docker compose ps --status running --services | grep -qx luma; then
  restart_luma=true
  docker compose stop luma
fi
docker compose up -d --wait postgres redis
docker compose exec -T postgres sh -c \
  'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges --exit-on-error' \
  < "$BACKUP_DIR/database.dump"
docker compose run --rm --no-deps -T --user 0 --entrypoint python luma -c '
import os, shutil, sys, tarfile
from pathlib import Path
root = Path("/opt/luma/backend/data")
root.mkdir(parents=True, exist_ok=True)
for child in root.iterdir():
    if child.is_dir() and not child.is_symlink():
        shutil.rmtree(child)
    else:
        child.unlink()
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
    archive.extractall(root, filter="data")
for parent, directories, files in os.walk(root):
    os.chown(parent, 10001, 10001)
    for name in files:
        os.chown(os.path.join(parent, name), 10001, 10001)
' < "$BACKUP_DIR/files.tar.gz"
docker compose exec -T redis sh -c \
  'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli -n "${REDIS_DB:-0}" FLUSHDB >/dev/null'
restore_complete=true
echo "Backup restored; users must log in again."
