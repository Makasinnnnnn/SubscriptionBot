# Проверка изменений

Используется Python 3.11. `uv.lock` фиксирует разрешённые версии, включая dev-зависимости. Подготовка среды и проверки:

```sh
uv sync --frozen --extra dev
uv run --frozen --extra dev ruff check app tests main.py alembic
uv run --frozen --extra dev pytest -q
uv pip check
```

Docker устанавливает production-зависимости из `requirements.lock` с проверкой hashes. При изменении зависимостей обновляйте оба файла:

```sh
uv lock
uv export --frozen --no-dev --no-emit-project --no-header --no-annotate --output-file requirements.lock
```

CI проверяет lint, тесты, миграции SQLite и конкурентные сценарии на отдельном PostgreSQL 16. Локально эти сценарии включаются через `TEST_POSTGRES_URL`, указывающий на отдельную тестовую БД. Реальные webhook и выдачу Telegram-доступа проверяйте отдельно в тестовых системах. Тесты с подменами интеграций не доказывают работоспособность production-секретов, внешней сети и прав Telegram-бота.

Для восстановления БД используйте инструкции `docs/database-recovery.md`.
