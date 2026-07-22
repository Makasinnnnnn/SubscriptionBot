# TODO

## Manual and external verification

- Real Telegram Stars payment and refund.
- Real invite links and Telegram member removal.
- Real Crypto Pay invoice, webhook and access delivery with a minimal payment.
- YooKassa only if that provider is enabled later.
- Verify the installed backup timer and offsite destination on the production server.

## Перед production

- Выполнить минимальный реальный платёж Crypto Pay и проверить полный цикл.
- Подключать ЮKassa только после отдельного продуктового решения.
- Принять решение по Telegram Stars и внешним платежам с учётом правил Telegram для цифровых услуг.
- Проверить Telegram-only условия, политику возвратов и контакт поддержки.
- Указать offsite-назначение для автоматических резервных копий.
- Создать внешний uptime monitor для `/health` и `/health/db`.
- Сменить все локальные/тестовые пароли и webhook secret на длинные случайные значения.
- Отключить `ENABLE_DEV_PAYMENTS`.

## После MVP

- Экспорт платежей и подписок в CSV/XLSX.
- Метрики MRR, churn, conversion funnel и provider success rate.
- Очередь повторных Telegram-операций при временных ошибках Bot API.
- Полноценные web-формы управления с CSRF-защитой и 2FA вместо обзорной web-панели.
- Поддержка нескольких владельцев проекта и отдельных организаций.
