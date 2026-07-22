# PropGate Subscription Bot

Production deployment, backup, update and rollback: [DEPLOYMENT.md](DEPLOYMENT.md).
Repeatable local acceptance: [LOCAL_ACCEPTANCE.md](LOCAL_ACCEPTANCE.md).

Telegram-бот для продажи и ручной выдачи доступа к закрытым каналам. Проект использует FastAPI, aiogram 3, PostgreSQL, SQLAlchemy 2, Alembic, APScheduler и Docker Compose.

## Что уже работает

- Меню пользователя, тарифы и список активных подписок.
- Dev-payment для локального тестирования без реальных денег.
- Crypto Pay API: создание invoice, проверка статуса, HMAC webhook, replay-защита.
- ЮKassa: создание платежа, redirect, проверка статуса через API и полный refund.
- Telegram Stars: нативный invoice, `pre_checkout_query`, `successful_payment`, refund.
- Две одноразовые Telegram invite-ссылки: основной канал и канал с материалами.
- Продление существующей подписки вместо создания конфликтующего доступа.
- Ручная выдача, продление и отзыв подписки.
- Удаление пользователя из канала при отзыве или истечении подписки.
- Напоминания до окончания подписки и уведомление после удаления.
- Telegram-админка, администраторы из БД, роли и доступ по отдельным каналам.
- Web-админка с HTTP Basic Auth.
- Audit log, журнал уведомлений, webhook events и request ID в логах.
- Отдельный технический бот для сгруппированных уведомлений о запусках и ошибках.
- Health endpoints и автоматические тесты.

## Первый запуск через Docker

Docker Desktop должен быть запущен. Из корня проекта выполни:

```powershell
scripts\run_stack.cmd
```

Compose дождётся PostgreSQL, применит Alembic-миграции и запустит API, бота и scheduler. Проверка:

```powershell
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/health/db
```

Ожидаемый ответ: `status = ok`.

После изменения кода перезапусти стек через `Ctrl+C`, затем снова:

```powershell
scripts\run_stack.cmd
```

## Запуск без Docker

Нужны работающая PostgreSQL и заполненный `.env`.

```powershell
.\.venv\Scripts\Activate.ps1
scripts\migrate.cmd
```

Запусти три отдельных терминала:

```powershell
scripts\run_api.cmd
```

```powershell
scripts\run_bot.cmd
```

```powershell
scripts\run_scheduler.cmd
```

## Настройка `.env`

Создай `.env` на основе `.env.example`. Файл `.env` игнорируется Git и Docker build context. Не отправляй его содержимое в чат и не коммить.

Минимум для локального MVP:

```env
BOT_TOKEN=
ADMIN_IDS=[123456789]
DATABASE_URL=postgresql+asyncpg://subscription:subscription@127.0.0.1:5432/subscription
WEBHOOK_SECRET=replace-with-long-random-value
ENABLE_DEV_PAYMENTS=true
```

В Docker Compose `DATABASE_URL` автоматически заменяется на адрес контейнера `postgres`.

Для web-админки:

```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace-with-a-long-password
```

Web-панель: `http://localhost:8000/admin/web`.

## Канал и тарифы

Добавь бота в закрытый канал администратором. Ему нужны права приглашать и блокировать пользователей. Узнать свой ID можно командой `/id`.

```text
/admin_add_channel -1001234567890 | PropGate Deals | Канал со сделками
/admin_add_plan 1 | 30 дней | 30 | 2990 | RUB
```

Для Telegram Stars создай отдельный тариф с валютой `XTR` и целым количеством Stars:

```text
/admin_add_plan 1 | 30 дней Stars | 30 | 500 | XTR
```

Проверка прав бота:

```text
/admin_check_channel 1
```

## Telegram-админка

Главное меню:

```text
/admin
```

Каналы и тарифы:

```text
/admin_channels
/admin_add_channel chat_id | Название | описание
/admin_disable_channel channel_id
/admin_enable_channel channel_id
/admin_plans
/admin_add_plan channel_id | Название | дни | цена | RUB
/admin_disable_plan plan_id
/admin_enable_plan plan_id
```

Пользователи и подписки:

```text
/admin_user telegram_id
/admin_subscriptions 20
/admin_grant telegram_id | channel_id | days
/admin_extend subscription_id | days
/admin_revoke subscription_id
/admin_revoke_user telegram_id | channel_id
/admin_resend_invite subscription_id
```

Платежи и диагностика:

```text
/admin_payments 20
/admin_refresh_payment payment_id
/admin_refund payment_id
/admin_debug_payment payment_id
/admin_debug_user telegram_id
/admin_debug_webhooks 20
/admin_stats
/admin_audit 20
/admin_notifications 20
```

`/admin_refund` сначала показывает кнопку подтверждения. После успешного полного возврата связанная активная подписка отзывается.

## Администраторы и доступ к каналам

ID из `ADMIN_IDS` считаются superadmin. Только они могут менять администраторов и глобальные настройки.

```text
/admin_admins
/admin_add_admin telegram_id admin
/admin_add_admin telegram_id support
/admin_remove_admin telegram_id
```

Обычный `admin` может управлять подписками и тарифами. `support` имеет просмотр без изменяющих команд.

По умолчанию новый admin видит и управляет всеми каналами. Чтобы ограничить его выбранными каналами:

```text
/admin_set_admin_scope telegram_id | restricted
/admin_grant_admin_channel telegram_id | channel_id
/admin_revoke_admin_channel telegram_id | channel_id
/admin_admin_access telegram_id
```

Вернуть полный доступ:

```text
/admin_set_admin_scope telegram_id | all
```

## Настройки уведомлений

Настройки хранятся в БД и применяются scheduler без пересборки контейнера:

```text
/admin_settings
/admin_set_notification_days 7,3,1
/admin_set_invite_ttl 60
/admin_set_scheduler_interval 10
```

- `notification_days` — за сколько дней напоминать об окончании.
- `invite_ttl` — срок одноразовой ссылки в минутах, от 5 до 10080.
- `scheduler_interval` — частота обработки подписок, от 1 до 1440 минут.

## Dev-payment

Оставь `ENABLE_DEV_PAYMENTS=true`, выбери тариф и нажми `Тестовая оплата` → `Подтвердить тестовую оплату`. Бот создаст реальную одноразовую ссылку в канал, но деньги не списываются.

В production обязательно установи:

```env
ENABLE_DEV_PAYMENTS=false
```

## CryptoBot / Crypto Pay

```env
CRYPTOBOT_TOKEN=
CRYPTOBOT_API_URL=https://pay.crypt.bot/api
ENABLE_CRYPTOBOT=true
WEBHOOK_SECRET=replace-with-long-random-value
```

Для testnet:

```env
CRYPTOBOT_API_URL=https://testnet-pay.crypt.bot/api
```

Webhook URL:

```text
https://your-domain.example/webhooks/cryptobot/<WEBHOOK_SECRET>
```

Бот проверяет `crypto-pay-api-signature` по исходному body, тип `invoice_paid`, invoice ID, сумму, валюту, провайдера и `request_date`. Повторное событие не продлевает подписку второй раз.

## ЮKassa

```env
YOOKASSA_SHOP_ID=
YOOKASSA_SECRET_KEY=
YOOKASSA_API_URL=https://api.yookassa.ru/v3
ENABLE_YOOKASSA=true
WEBHOOK_SECRET=replace-with-long-random-value
```

Webhook URL:

```text
https://your-domain.example/webhooks/yookassa/<WEBHOOK_SECRET>
```

Подлинность события подтверждается загрузкой платежа из API ЮKassa. Дополнительно можно включить allowlist официальных сетей:

```env
YOOKASSA_ALLOWED_IPS=["185.71.76.0/27","185.71.77.0/27","77.75.153.0/25","77.75.156.11/32","77.75.156.35/32","77.75.154.128/25","2a02:5180::/32"]
```

Если запрос идёт через reverse proxy, укажи адреса доверенных proxy в `TRUSTED_PROXY_IPS`. Не доверяй `X-Forwarded-For` от любого клиента.

## Telegram Stars

Для Stars внешний provider token не нужен. Тариф обязан использовать валюту `XTR`. Бот проверяет пользователя, payload, сумму и валюту в `pre_checkout_query`, а доступ выдаёт только после `successful_payment`. Для возврата сохраняется `telegram_payment_charge_id`.

Важно: Telegram требует использовать Stars для цифровых товаров и услуг, продаваемых внутри Telegram-приложений. Перед production нужно принять продуктовое решение, как сочетать Stars с внешней оплатой на отдельном сайте и соблюдать правила Telegram и применимое законодательство.

Команды `/terms`, `/support` и `/paysupport` используют:

```env
TERMS_URL=https://propgate.ru/terms
SUPPORT_CONTACT=@your_support
```

## Юридические тексты бота

Тексты, которые бот показывает во вкладке «Документы» и по юридическим командам,
хранятся внутри репозитория в каталоге `legal/`:

- `legal/terms.md` — условия использования (`/terms`);
- `legal/privacy.md` — правила обработки персональных данных (`/privacy`);
- `legal/refunds.md` — правила оплаты и возврата (`/refund_policy`).

После изменения этих файлов пересобери контейнер бота:

```powershell
scripts\docker_compose.cmd up -d --build bot
```

Поля `TERMS_URL`, `PRIVACY_URL` и `REFUND_POLICY_URL` в `.env` предназначены для
внешних публичных страниц. Встроенный раздел Telegram-бота читает тексты именно
из файлов каталога `legal/`.

## Tunnel для webhook

Сначала запусти стек, затем во втором терминале:

```powershell
scripts\run_tunnel.cmd
```

Cloudflare выведет временный адрес вида `https://...trycloudflare.com`. Проверка:

```powershell
Invoke-RestMethod https://YOUR-TUNNEL.trycloudflare.com/health
```

Quick Tunnel меняет URL после перезапуска. Для production используй `propgate.ru`, HTTPS и постоянную конфигурацию reverse proxy/tunnel.

## Диагностика

```powershell
scripts\logs_app.cmd
scripts\logs_bot.cmd
scripts\logs_scheduler.cmd
scripts\psql.cmd
scripts\test.cmd
```

Seed для локальной разработки:

```env
DEV_CHANNEL_ID=-1001234567890
DEV_ADMIN_ID=123456789
```

```powershell
scripts\seed_dev.cmd
```

Seed идемпотентно создаёт администратора, dev-канал и тестовый тариф, если их ещё нет.

## Типовые ошибки

- `docker-credential-desktop not found`: используй `scripts\docker_compose.cmd`, он добавляет Docker Desktop CLI в `PATH`.
- `.pytest_cache: Access is denied`: каталог исключён через `.dockerignore`; пересобери образ.
- `bot is not admin`: выдай боту права приглашать и блокировать участников, затем `/admin_check_channel channel_id`.
- `invite link failed`: проверь chat ID, статус канала и права бота.
- webhook `403`: использован URL без `WEBHOOK_SECRET`, неверный secret, HMAC или IP allowlist.
- CryptoBot signature invalid: проверь, что token соответствует testnet/mainnet и webhook URL настроен в том же приложении.
- `/health/db` возвращает `503`: PostgreSQL недоступна или неверен `DATABASE_URL`.

## Проверка перед production

```powershell
scripts\test.cmd
.\.venv\Scripts\python.exe -m compileall app main.py tests
scripts\docker_compose.cmd config
```

Также нужны: постоянный сервер, HTTPS для `propgate.ru`, резервные копии PostgreSQL, мониторинг ошибок, сильные секреты, опубликованные условия/политика возвратов и реальные тестовые платежи каждого включённого провайдера.
