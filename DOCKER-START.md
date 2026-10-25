# Запуск PropGate через Docker

## Перенос архива на Linux-сервер

Архив `propgate-server-*.tar.gz` содержит секреты из env-файлов. Передавай его
на свой сервер; не загружай в GitHub или публичные ссылки. Рабочая база данных
в архив не входит: на новом сервере создаётся пустая база. Для переноса
существующих подписок отдельно восстанови backup по `DEPLOYMENT.md`.

На сервере с установленными Docker Engine и Compose v2:

```sh
mkdir -p /opt/propgate-release
tar -xzf propgate-server-2026-10-26.tar.gz -C /opt/propgate-release
cd /opt/propgate-release/propgate
chmod 600 .env .env.production .env.journal
sh START.sh
curl -fsS http://127.0.0.1:8000/health/db
```

Перед запуском на сервере останови прежнюю копию основного бота с тем же токеном.
`START.sh` запускает основной SubscriptionBot; отдельный journal-бот не стартует.
Google OAuth-файлы журнала за пределами проекта в архив не входят.
Для публичной работы платежей настрой домен и HTTPS по `DOMAIN_SETUP.md`.

Пересобрать приватный архив на своём компьютере:

```powershell
.venv\Scripts\python.exe scripts/package_server.py ..\releases\propgate-server-2026-10-26.tar.gz
```

Нужен запущенный Docker Desktop в режиме Linux containers. Основной бот,
API, планировщик и служебный бот работают отдельно; база хранится в Docker volume.
Это запуск основного SubscriptionBot. Отдельный бот журнала @proptablebot
этими командами не запускается и не останавливается.

## На этом компьютере

Настройки находятся в `.env.production`. Секреты не нужно присылать в чат.
Встроенная методичка по умолчанию выключена: кнопка открывает https://t.me/propmethods.

1. Запусти Docker Desktop и дождись готовности.
2. Открой `START.cmd` в корне проекта.
3. Дождись сообщения `Started`. Окно можно закрыть: контейнеры работают в фоне.
4. Открой своего основного бота в Telegram и отправь `/start`.

Запуск собирает образы, проверяет настройки, поднимает PostgreSQL, сохраняет
проверенную резервную копию в `backups/`, останавливает процессы приложения,
применяет миграции и запускает службы. При ошибке миграции приложение остаётся
остановленным; повторный запуск после исправления снова выполнит эти шаги.
Не запускай параллельно вторую копию основного бота с тем же токеном.

## Команды из PowerShell в корне проекта

Только подготовить и проверить, без запуска ботов:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/production.ps1 prepare
```

Запуск / обновление:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/production.ps1 start
```

Состояние и последние сообщения:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/production.ps1 status
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/production.ps1 logs
```

Остановить, сохранив базу:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/production.ps1 stop
```

Проверка API: http://127.0.0.1:8000/health/db — ответ `{"status":"ok"}`.
Если бот не отвечает, проверь состояние служб и логи `bot`.
Если Telegram сообщает `Conflict`, останови другую копию бота с этим токеном.

Контейнеры перезапускаются автоматически, пока Docker работает. Для работы
после перезагрузки компьютера Docker Desktop должен запускаться автоматически.
Для круглосуточной работы на VPS используй `DEPLOYMENT.md`: домен и HTTPS
нужны отдельно для публичных платёжных webhook. Локальный запуск их не настраивает.

## Возврат встроенного обучения

В `.env.production` поставь `ENABLE_BUILTIN_EDUCATION=true` и повтори запуск.
При `false` кнопка ведёт в канал propmethods. Доступ к закрытому каналу
по одной этой ссылке автоматически не выдаётся.
