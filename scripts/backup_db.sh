#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BACKUP_DIR="$ROOT/backups"
RETENTION_COUNT="${BACKUP_RETENTION_COUNT:-14}"
cd "$ROOT"
if [ -f .env.production ]; then
  set -- --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml
else
  set -- -f docker-compose.yml
fi
mkdir -p "$BACKUP_DIR"
STAMP=$(date -u +%Y%m%d_%H%M%S)
NAME="subscription_${STAMP}.dump"
CONTAINER_PATH="/tmp/$NAME"
docker compose "$@" exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null
docker compose "$@" exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "$1"' -- "$CONTAINER_PATH"
CONTAINER=$(docker compose "$@" ps -q postgres)
docker cp "$CONTAINER:$CONTAINER_PATH" "$BACKUP_DIR/$NAME"
docker compose "$@" exec -T postgres pg_restore -l "$CONTAINER_PATH" >/dev/null
docker compose "$@" exec -T postgres rm -f "$CONTAINER_PATH"
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'subscription_*.dump' -printf '%T@ %p\n' |
  sort -rn | awk -v keep="$RETENTION_COUNT" 'NR > keep {sub(/^[^ ]+ /, ""); print}' |
  while IFS= read -r file; do [ -n "$file" ] && rm -f -- "$file"; done
printf 'Backup created and verified: %s\n' "$BACKUP_DIR/$NAME"
