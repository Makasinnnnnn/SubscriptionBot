# Telegram Admin Guide

The primary admin bot now contains the operational controls needed for daily work.

## Channels

Open `/admin` and choose **Channels**. The menu supports:

- adding a channel by Telegram `chat_id`;
- verifying that the bot is an administrator with invite and member-management rights;
- editing title and description;
- archiving and reactivating a channel;
- selecting a separate materials channel;
- clearing the materials-channel override.

Only active channels can receive new tariffs. Archiving a channel does not delete its
plans or subscriptions, so existing access can be revoked or audited later.

## Plans and access

Use **Plans** to create, edit, enable, or archive tariffs. Use **Access** to find a
user by Telegram ID or username, grant manual access, and revoke an active subscription.
Every write operation is checked against the actor's channel scope and recorded in the
audit log.

## Payments

CryptoBot webhooks require the configured secret path, a valid Crypto Pay signature,
fresh request timestamp, matching provider payment ID, amount, currency, and invoice
payload. Duplicate webhook events are idempotent. Telegram Stars validates the invoice
payload, payer, amount, currency, plan state, and Telegram charge ID before granting access.

Real provider payments and refunds must be tested only after production credentials and
provider dashboards are configured. The local `dev` provider remains the safe test path.

## Deployment note

After updating the server bundle, rebuild the bot and app containers. No database
migration is required for channel management because the runtime materials-channel
selection uses the existing `app_settings` table.
