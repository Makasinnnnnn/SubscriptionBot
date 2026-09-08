# Production operations

Этот документ включает обязательные операции после первого deployment: ежедневные backup,
health monitoring и безопасный SSH. Команды рассчитаны на `/opt/propgate` и Ubuntu 24.04.

## Telegram-only юридические документы

Пользователь открывает документы внутри бота через вкладку «Документы» и команды `/terms`,
`/privacy`, `/refund_policy`. Тексты читаются из `legal/`, поэтому отдельные HTTP-страницы не
нужны.

В `.env.production` разрешено оставить URL пустыми:

```env
TERMS_URL=
PRIVACY_URL=
REFUND_POLICY_URL=
```

После изменения перезапустить bot и app. Перед запуском проверить тексты в `legal/` и повышать
`LEGAL_VERSION`, когда условия меняются существенно: бот потребует новое согласие перед
следующей оплатой.

## Ежедневные backup и локальный watchdog

После доставки актуального кода на сервер:

```bash
cd /opt/propgate
chmod +x scripts/*.sh scripts/healthcheck.py
./scripts/install_server_maintenance.sh
```

Установщик:

- создаёт проверяемый PostgreSQL dump ежедневно в 03:15 UTC;
- хранит последние `BACKUP_RETENTION_COUNT` dump;
- проверяет `/health` и `/health/db` каждые пять минут;
- отправляет одно уведомление при падении и одно при восстановлении, если настроены
  `OPS_BOT_TOKEN` и `OPS_CHAT_IDS`;
- выполняет пропущенный backup после следующего запуска сервера.

Проверка:

```bash
systemctl list-timers 'propgate-*' --no-pager
systemctl status propgate-backup.timer propgate-healthcheck.timer --no-pager
journalctl -u propgate-backup.service -n 50 --no-pager
journalctl -u propgate-healthcheck.service -n 50 --no-pager
ls -lh /opt/propgate/backups
```

## Offsite backup

Локальный dump не защищает от удаления VPS. Для отправки на второй SSH-хост:

1. Создать отдельный SSH-ключ на VPS без пароля, разрешённый только для каталога backup на
   принимающем сервере.
2. Один раз подключиться к принимающему серверу, проверить fingerprint и добавить его в
   `known_hosts`.
3. Записать целевой каталог в `/etc/propgate/maintenance.env`:

```env
BACKUP_OFFSITE_TARGET=backup-user@backup-host:/srv/backups/propgate/
```

4. Запустить и проверить:

```bash
systemctl start propgate-backup.service
journalctl -u propgate-backup.service -n 50 --no-pager
```

Если второго хоста пока нет, `BACKUP_OFFSITE_TARGET` остаётся пустым, а backup хранится только
на VPS. Его необходимо регулярно скачивать вручную или позже подключить отдельное хранилище.

## Внешний uptime monitoring

Встроенный watchdog не сообщит о полном отключении VPS. Во внешнем сервисе мониторинга создать
HTTPS monitor:

```text
URL: https://propgate.ru/health
Method: GET
Expected status: 200
Interval: 5 minutes
```

Дополнительно создать второй monitor для `https://propgate.ru/health/db`. Уведомления направить
в Telegram и email. После создания вручную остановить `app` на одну минуту, убедиться в alert,
затем запустить и дождаться recovery.

## SSH hardening без потери доступа

### 1. Создать ключ на ноутбуке

PowerShell:

```powershell
ssh-keygen -t ed25519 -a 100 -f "$env:USERPROFILE\.ssh\propgate_ed25519"
scp "$env:USERPROFILE\.ssh\propgate_ed25519.pub" root@192.0.2.10:/root/propgate_ed25519.pub
```

Приватный файл `propgate_ed25519` никому не отправлять.

### 2. Подготовить пользователя на сервере

```bash
cd /opt/propgate
chmod +x scripts/harden_ssh.sh
./scripts/harden_ssh.sh prepare propgate /root/propgate_ed25519.pub
```

Не закрывать текущую root-сессию. В новом PowerShell проверить:

```powershell
ssh -i "$env:USERPROFILE\.ssh\propgate_ed25519" propgate@192.0.2.10
sudo -v
docker ps
```

### 3. Только после успешной проверки отключить пароли

В старой root-сессии:

```bash
cd /opt/propgate
CONFIRM_SSH_KEY_LOGIN=yes ./scripts/harden_ssh.sh finalize propgate
```

Открыть ещё одно новое окно и повторить вход по ключу. Скрипт отключает парольную авторизацию,
оставляет root только по SSH-ключу, отключает X11 forwarding и ограничивает попытки входа.
Текущую root-сессию закрывать только после повторной успешной проверки.

## Проверка после перезагрузки

```bash
reboot
```

После возврата сервера:

```bash
cd /opt/propgate
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml ps
systemctl status caddy propgate-backup.timer propgate-healthcheck.timer --no-pager
curl -fsS https://propgate.ru/health
curl -fsS https://propgate.ru/health/db
```
