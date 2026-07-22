# TODO

## Manual and external verification

- Real Telegram Stars payment and refund.
- Real invite links and Telegram member removal.
- Real Crypto Pay invoice, webhook and access delivery with a minimal payment.
- YooKassa only if that provider is enabled later.
- DNS and HTTPS for `propgate.ru` after choosing a VPS.
- Repeat backup/restore verification after deployment on the production server.

## Перед production

- Настроить постоянный HTTPS deployment для `propgate.ru`.
- Выполнить минимальный реальный платёж Crypto Pay и проверить полный цикл.
- Подключать ЮKassa только после отдельного продуктового решения.
- Принять решение по Telegram Stars и внешним платежам с учётом правил Telegram для цифровых услуг.
- Опубликовать условия использования, политику возвратов и контакт поддержки.
- Настроить резервные копии PostgreSQL и проверку восстановления.
- Подключить внешний error monitoring и uptime monitoring.
- Сменить все локальные/тестовые пароли и webhook secret на длинные случайные значения.
- Отключить `ENABLE_DEV_PAYMENTS`.

## После MVP

- Экспорт платежей и подписок в CSV/XLSX.
- Метрики MRR, churn, conversion funnel и provider success rate.
- Очередь повторных Telegram-операций при временных ошибках Bot API.
- Полноценные web-формы управления с CSRF-защитой и 2FA вместо обзорной web-панели.
- Поддержка нескольких владельцев проекта и отдельных организаций.
