from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

from alembic import context
from app.core.config import get_settings
from app.db.models import User

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings = get_settings()


def _sync_database_url(database_url: str) -> str:
    url = make_url(database_url)
    if url.get_driver_name() == "asyncpg":
        url = url.set(drivername="postgresql+psycopg")
    elif url.get_driver_name() == "aiosqlite":
        url = url.set(drivername="sqlite")
    return url.render_as_string(hide_password=False)


config.set_main_option(
    "sqlalchemy.url", _sync_database_url(settings.database_url).replace("%", "%%")
)
target_metadata = User.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        if context.get_context().dialect.name == "postgresql":
            context.execute("SET LOCAL TIME ZONE 'UTC'")
        context.run_migrations()


def run_migrations_online() -> None:
    url = make_url(config.get_main_option("sqlalchemy.url"))
    connect_args = {"options": "-c timezone=UTC"} if url.get_backend_name() == "postgresql" else {}
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
