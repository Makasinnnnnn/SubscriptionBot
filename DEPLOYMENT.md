# PropGate deployment

Пошаговое подключение `propgate.ru`, HTTPS и постоянного Crypto Pay webhook описано в
[`DOMAIN_SETUP.md`](DOMAIN_SETUP.md).
Ежедневные backup, monitoring и безопасная настройка SSH описаны в
[`SERVER_OPERATIONS.md`](SERVER_OPERATIONS.md).

## Production prerequisites

- Linux VPS with Docker Engine and Docker Compose;
- DNS records for `propgate.ru` pointing to the VPS;
- HTTPS reverse proxy such as Caddy or nginx;
- a production `.env` created from `config/production.example.txt`;
- Telegram bot administrator rights in every managed channel.

Cloudflare Quick Tunnel is suitable only for temporary local tests and is not a production
endpoint because its address changes after restart.

## Local production-like check

Create `.env.production` from `config/production.example.txt`, replace every placeholder and run:

```powershell
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml config
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml up -d --build
```

Production validation stops startup when secrets, configured legal URLs or support details are unsafe.
Disabled payment providers do not require credentials.

## First deployment

```bash
git clone <repository-url> /opt/propgate
cd /opt/propgate
cp config/production.example.txt .env.production
chmod 600 .env.production
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml up -d --build
```

Forward `https://propgate.ru` to `127.0.0.1:8000` with a reverse proxy. PostgreSQL and Redis
are not published on the host in the production override. Register provider webhook routes
under the permanent `https://propgate.ru` origin.

## Health and logs

```bash
curl -fsS https://propgate.ru/health
curl -fsS https://propgate.ru/health/db
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml ps
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml logs --tail 100 app bot scheduler ops
```

## Update and rollback

```bash
cd /opt/propgate
./scripts/backup_db.sh
git pull --ff-only
export APP_ENV_FILE=.env.production
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml up -d --build postgres redis migrations
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml up -d --build app bot scheduler ops
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml ps
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/health/db
```

The `0009_discount_promos` migration converts promo codes into payment
discounts and links them to invoices/redemptions. It includes `0008` when that
revision has not been applied yet. Always create and verify a backup before applying it.
Do not start the new bot or scheduler before the `migrations` service exits
successfully.

For rollback, stop application services, check out the previous Git revision and rebuild it.
Restore a pre-release dump only when the old code cannot use the upgraded schema:

```bash
FORCE_RESTORE=true ./scripts/restore_db.sh backups/subscription_YYYYMMDD_HHMMSS.dump
```

Restore is destructive. Keep the automatic safety backup and test restoration in a disposable
database first.

After this release, manually verify:

```bash
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml logs --tail 100 migrations app bot scheduler ops
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Open `/admin` and check tariff tabs, promo creation, support contacts,
reminder intervals and XLSX export. Then create a disposable discount promo,
verify the provider invoice amount and complete a test payment before checking
join requests for both channels.

## Backups, DNS and HTTPS

Backups are in `backups/`, excluded from Git and retained according to
`BACKUP_RETENTION_COUNT`. Copy encrypted backups outside the VPS. To migrate data, copy a
`.dump` file to the new server and use `restore_db.sh` after PostgreSQL starts.

Create DNS records only when the server IP is known. Issue TLS certificates with the reverse
proxy. Do not expose ports 5432 or 6379. Permit only SSH, HTTP and HTTPS in the host firewall.

Keep `.env.production` outside Git, readable only by the service account, and rotate any secret
that has ever appeared in a commit, screenshot, chat or shell history. The public webhook URL
contains `WEBHOOK_SECRET`; provider authentication is still verified independently (Crypto Pay
HMAC or a fresh YooKassa API lookup). Never expose the FastAPI port directly to the internet.
