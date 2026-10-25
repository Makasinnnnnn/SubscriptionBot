#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"
if [ ! -f .env.production ]; then
    printf '%s\n' 'Missing .env.production; copy config/production.example.txt and configure it.' >&2
    exit 1
fi
export APP_ENV_FILE="$ROOT/.env.production"
chmod 600 .env.production
compose() {
    docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml "$@"
}
compose config --quiet
compose build
compose run --rm --no-deps app python -c "from app.core.config import get_settings; get_settings(); print('Configuration OK')"
compose up -d --wait postgres
sh scripts/backup_db.sh
compose stop app bot scheduler ops
compose run --rm --no-deps migrations
compose up -d --no-build --no-deps --wait app bot scheduler ops
compose ps
printf '%s\n' 'Started. Check http://127.0.0.1:8000/health/db'
