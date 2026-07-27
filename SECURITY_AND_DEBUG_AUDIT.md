# PropGate: functional and security audit

Audit date: 2026-07-27.

## Implemented in this audit

- Added scoped, paginated Telegram reports for users, subscriptions and
  payments, including safe XLSX export with a 5000-row limit and formula
  injection protection.
- Added tariff tabs and cards with status, payment statistics, revenue by
  currency and provider availability.
- Added atomic one-use promo redemption, UTC validity windows, limits,
  administration screens and an Alembic migration.
- Unified `/support` and `Помощь`; added typed runtime contacts. Numeric IDs
  are never rendered as `@number`.
- Reworked expiration reminders around the current subscription period and
  minute-based thresholds. One scheduler run sends at most one reminder per
  subscription.
- Added savepoints around paid access provisioning. A Telegram failure no
  longer commits a half-created subscription; the paid status remains
  recoverable.
- Added admin retry for paid payments that have no subscription.
- Added compensation for a failed materials invite by revoking the newly
  created primary invite.
- Restricted operations-bot screens to explicit user IDs. A configured group
  chat ID no longer grants every group member access.
- Prevented purchase of the service/materials plan inside both Telegram
  handlers and `PaymentService`.
- Removed a duplicate operations-message formatter and kept only compact,
  HTML-escaped important notifications.

## Security checks

- Crypto Pay webhooks require the secret path, HMAC over the original body,
  a supported event, a bounded `request_date`, invoice identity, amount and
  currency.
- YooKassa webhooks require the secret path, optional trusted-source
  allowlisting and an authoritative API lookup with Basic Auth.
- Webhook bodies are JSON-only, size-limited, rate-limited and idempotent.
- Web admin uses constant-time HTTP Basic comparison, failed-auth throttling
  and production-only host allowlisting.
- Production API docs are disabled. Security headers and request-ID
  sanitization are enabled.
- Admin mutations check role/channel scope and write `AuditLog`.
- Logs redact configured secrets, Telegram bot-token patterns and webhook
  path secrets. Provider payloads stored by integrations are allowlisted.
- `.env`, dumps, backups, logs and private-key formats are excluded from Git
  and Docker context. No Telegram-token or private-key patterns were found in
  tracked files or Git history during this audit.
- Production containers run as a non-root user, read-only, with dropped
  capabilities and no published PostgreSQL/Redis ports.

## Automated verification

- `ruff check app tests main.py`
- `pytest -q` (`100 passed`)
- `compileall -q app main.py tests`
- `pip check`
- `alembic heads` and revision-chain inspection
- Docker Compose configuration validation
- `git diff --check`

Docker runtime integration could not be executed during the audit when
Docker Desktop was not running; it remains part of deployment acceptance.

## Required external checks

- Apply migration `0008_promos_and_reminders` to a backed-up PostgreSQL
  database and inspect the migrations container exit code.
- Complete one real Crypto Pay payment/webhook and one Telegram Stars
  payment/refund where regionally available.
- Verify join requests, repeated invite delivery and removal in both real
  channels.
- Test the operations bot with its production recipient IDs.
- Verify offsite backup upload and a restore in a disposable database.
- Keep YooKassa disabled until credentials and the business/legal setup are
  ready.

## Residual design risks

- Telegram and payment providers are external systems; transient failures are
  surfaced and recoverable, but there is no durable general-purpose job queue
  yet.
- Rate limits are process-local. The current production Compose runs one API
  process; a future multi-replica deployment should move limits to Redis.
- HTTP Basic protects the read-only web admin. If write forms are added,
  require CSRF protection and preferably stronger identity/2FA.
- Telegram-only legal documents work without public HTTP pages. Public
  checkout outside Telegram may still require published legal URLs.
