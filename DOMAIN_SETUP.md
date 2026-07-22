# Подключение propgate.ru

План рассчитан на production-сервер `212.192.0.127`, где PropGate уже работает в Docker,
а API доступен только локально по адресу `127.0.0.1:8000`.

## 1. Настроить DNS

В панели регистратора открыть управление DNS-зоной `propgate.ru` и создать записи:

| Тип | Имя | Значение | TTL |
| --- | --- | --- | --- |
| A | `@` | `212.192.0.127` | 300 или Auto |
| A | `www` | `212.192.0.127` | 300 или Auto |

Удалить конфликтующие A/AAAA-записи для `@` и `www`. Отключить парковочную страницу и
перенаправление регистратора. Обновление DNS может занять до 24 часов.

Проверка с сервера:

```bash
apt-get update && apt-get install -y dnsutils
dig +short A propgate.ru
dig +short A www.propgate.ru
```

Обе команды должны вернуть `212.192.0.127`. Не выпускать HTTPS, пока основной домен не
указывает на новый сервер.

## 2. Настроить firewall

Сначала разрешить SSH, затем включить firewall:

```bash
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status verbose
```

Порты PostgreSQL `5432`, Redis `6379` и FastAPI `8000` наружу не открывать. Compose публикует
API только на `127.0.0.1:8000`.

## 3. Установить Caddy

```bash
apt-get update
apt-get install -y caddy
cat >/etc/caddy/Caddyfile <<'EOF'
propgate.ru, www.propgate.ru {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
EOF

caddy validate --config /etc/caddy/Caddyfile
systemctl enable --now caddy
systemctl reload caddy
systemctl status caddy --no-pager
```

Caddy автоматически получает и обновляет TLS-сертификаты. Диагностика:

```bash
journalctl -u caddy -n 100 --no-pager
```

## 4. Проверить HTTPS

```bash
curl -fsS https://propgate.ru/health
curl -fsS https://propgate.ru/health/db
curl -I https://propgate.ru
```

Первые две команды должны вернуть статус `ok`, а сертификат должен быть действительным.

## 5. Проверить production-конфигурацию

Не публиковать содержимое `.env.production` и webhook secret.

```bash
cd /opt/propgate
grep -E '^(APP_ENV|APP_URL|ALLOWED_HOSTS)=' .env.production
```

Ожидаемые параметры:

```env
APP_ENV=production
APP_URL=https://propgate.ru
ALLOWED_HOSTS=["propgate.ru","www.propgate.ru","localhost","127.0.0.1"]
```

После изменения конфигурации перезапустить API:

```bash
cd /opt/propgate
APP_ENV_FILE=.env.production docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml up -d app
```

## 6. Подключить постоянный Crypto Pay webhook

Посмотреть secret только в своей SSH-сессии:

```bash
cd /opt/propgate
grep '^WEBHOOK_SECRET=' .env.production
```

В настройках приложения Crypto Pay включить webhook и указать:

```text
https://propgate.ru/webhooks/cryptobot/<WEBHOOK_SECRET>
```

Вместо `<WEBHOOK_SECRET>` подставить значение из серверного файла. Не отправлять полный URL в
чат и не сохранять в Git. Crypto Pay token и webhook должны относиться к одной среде.

После сохранения провести минимальный реальный платёж и проверить:

```bash
cd /opt/propgate
APP_ENV_FILE=.env.production docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml logs --since=10m app bot
```

Ожидаемый результат: webhook принят, платёж получил статус `paid`, подписка создана, бот выдал
ссылки на оба настроенных канала.

## 7. Финальная проверка

- `/start`, `/support` и меню тарифов отвечают;
- Stars создают счёт внутри Telegram;
- Crypto Pay создаёт invoice и подтверждает его через постоянный webhook;
- после оплаты выдаются обе ссылки;
- `/health` и `/health/db` доступны по HTTPS;
- контейнеры не перезапускаются;
- Cloudflare Quick Tunnel больше не используется;
- порты `5432`, `6379` и `8000` недоступны извне.

## Откат

Если появились проблемы, временно выключить webhook в Crypto Pay и остановить Caddy:

```bash
systemctl stop caddy
```

Telegram polling и Stars продолжат работать. После исправления снова запустить Caddy командой
`systemctl start caddy` и повторить проверки.
