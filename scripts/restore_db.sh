#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BACKUP=${1:-}
FORCE=${FORCE_RESTORE:-false}
[ -f "$BACKUP" ] || { echo "Backup file not found." >&2; exit 1; }
case "$BACKUP" in *.dump|*.backup) ;; *) echo "Only .dump or .backup files are accepted." >&2; exit 1;; esac
BACKUP_DIR=$(CDPATH= cd -- "$(dirname -- "$BACKUP")" && pwd)
BACKUP="$BACKUP_DIR/$(basename -- "$BACKUP")"
if [ "$FORCE" != "true" ]; then
  printf 'Type RESTORE to continue: '
  read -r answer
  [ "$answer" = "RESTORE" ] || { echo "Restore cancelled."; exit 1; }
fi
"$(dirname "$0")/backup_db.sh"
cd "$ROOT"
if [ -f .env.production ]; then
  set -- --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml
else
  set -- -f docker-compose.yml
fi
docker compose "$@" exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null
CONTAINER=$(docker compose "$@" ps -q postgres)
docker cp "$BACKUP" "$CONTAINER:/tmp/restore.dump"
docker compose "$@" exec -T postgres pg_restore -l /tmp/restore.dump >/dev/null
docker compose "$@" stop app bot ops scheduler
trap 'docker compose "$@" exec -T postgres rm -f /tmp/restore.dump' EXIT
docker compose "$@" exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --single-transaction --exit-on-error /tmp/restore.dump'
docker compose "$@" exec -T postgres rm -f /tmp/restore.dump
trap - EXIT
docker compose "$@" start app bot ops scheduler
echo "Database restored from: $BACKUP"
