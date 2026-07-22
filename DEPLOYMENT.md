# PropGate deployment

## Production prerequisites

- Linux VPS with Docker Engine and Docker Compose;
- DNS records for `propgate.ru` pointing to the VPS;
- HTTPS reverse proxy such as Caddy or nginx;
- a production `.env` created from `.env.production.example`;
- Telegram bot administrator rights in every managed channel.

Cloudflare Quick Tunnel is suitable only for temporary local tests and is not a production
endpoint because its address changes after restart.

## Local production-like check

Create `.env.production` from `.env.production.example`, replace every placeholder and run:

```powershell
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml config
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml up -d --build
```

Production validation stops startup when secrets, legal URLs or support details are unsafe.
Disabled payment providers do not require credentials.

## First deployment

```bash
git clone <repository-url> /opt/propgate
cd /opt/propgate
cp .env.production.example .env.production
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
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml logs --tail 100 app bot scheduler
```

## Update and rollback

```bash
./scripts/backup_db.sh
git pull --ff-only
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml up -d --build
```

For rollback, stop application services, check out the previous Git revision and rebuild it.
Restore a pre-release dump only when the old code cannot use the upgraded schema:

```bash
FORCE_RESTORE=true ./scripts/restore_db.sh backups/subscription_YYYYMMDD_HHMMSS.dump
```

Restore is destructive. Keep the automatic safety backup and test restoration in a disposable
database first.

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
