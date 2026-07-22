# Local acceptance checklist

## Automated

```powershell
scripts\test.cmd
scripts\docker_compose.cmd up -d --build postgres redis migrations app bot scheduler
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/health/db
```

The suite covers isolated database models, payment provider contracts, webhook signatures and
idempotency, subscription lifecycle, admin scopes, legal acceptance/versioning, payment
deduplication and admin notification preferences/deduplication. It uses fake providers and does
not contact real payment systems or send Telegram messages.

Create and verify a Docker PostgreSQL backup separately:

```powershell
scripts\backup_db.ps1
```

Restore must be tested against a disposable database or Docker volume. Never restore the
working database in an unattended test.

## Manual Telegram acceptance

- create and edit a tariff from `/admin`;
- verify legal-document links and accept the current version;
- complete a dev payment and verify invite delivery;
- grant, extend and revoke access;
- verify scheduler expiration with a short subscription;
- inspect payment, subscription and error notifications;
- change `LEGAL_VERSION` and verify that consent is requested again.

## Requires external systems

- a real Telegram Stars payment and refund;
- a real invite link and actual member removal;
- Crypto Pay and YooKassa invoices/webhooks;
- public DNS, HTTPS and uptime checks outside the local network.
