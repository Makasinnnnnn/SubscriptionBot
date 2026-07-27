# Local acceptance checklist

## Automated

```powershell
scripts\test.cmd
scripts\docker_compose.cmd up -d --build postgres redis migrations app bot scheduler ops
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/health/db
```

The suite covers isolated database models, payment provider contracts, webhook signatures and
idempotency, subscription lifecycle, period-aware reminders, promo redemption and rollback,
support contacts, XLSX formula protection, admin scopes, legal acceptance/versioning, payment
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
- switch between active/archive/all tariff tabs and inspect metrics;
- create and redeem a one-use promo, then verify both join-request links;
- filter users/subscriptions/payments and open every generated XLSX;
- add one username and one Telegram ID to support, test and reorder them;
- set reminders to `1d,12h` and verify no immediate long-threshold message;
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
