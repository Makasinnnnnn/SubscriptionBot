import importlib.util
import io
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_sqlite_promo_migrations_preserve_old_subscription_and_reminder(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    versions = tmp_path / "versions"
    versions.mkdir()
    for migration in (repo / "alembic/versions").glob("*.py"):
        if int(migration.name[:4]) <= 9:
            shutil.copy2(migration, versions / migration.name)
    config = (
        (repo / "alembic.ini")
        .read_text()
        .replace(
            "script_location = alembic",
            f"script_location = {(repo / 'alembic').as_posix()}\n"
            f"version_locations = {versions.as_posix()}",
        )
    )
    config_path = tmp_path / "alembic.ini"
    config_path.write_text(config)
    database = tmp_path / "test.db"
    env = dict(
        os.environ,
        APP_ENV="development",
        DATABASE_URL=f"sqlite:///{database.as_posix()}",
        PYTHONPATH=str(repo),
        PYTHONDONTWRITEBYTECODE="1",
    )

    def migrate(command, revision):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(config_path), command, revision],
            env=env,
            cwd=repo,
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("upgrade", "0007_join_request_invites")
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            INSERT INTO users (id,telegram_id) VALUES (1,12345);
            INSERT INTO channels (id,telegram_chat_id,title,is_active)
            VALUES (1,-10012345,'test',1);
            INSERT INTO plans (id,channel_id,title,duration_days,price,currency)
            VALUES (1,1,'test',30,100,'RUB');
            INSERT INTO payments
            (id,user_id,plan_id,provider,status,amount,currency,idempotency_key,metadata_json)
            VALUES (1,1,1,'dev','paid',100,'RUB','test','{}');
            INSERT INTO subscriptions
            (id,user_id,channel_id,payment_id,status,starts_at,expires_at)
            VALUES (1,1,1,1,'active','2026-10-01 00:00:00','2026-10-31 00:00:00');
            INSERT INTO notification_logs (id,user_id,subscription_id,type,sent_at)
            VALUES (1,1,1,'expires_3d','2026-10-28 00:00:00');
            """
        )
    migrate("upgrade", "0009_discount_promos")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT period_started_at FROM subscriptions WHERE id=1"
        ).fetchone() == ("2026-10-01 00:00:00",)
        assert connection.execute(
            "SELECT expires_at_snapshot,threshold_minutes FROM notification_logs WHERE id=1"
        ).fetchone() == ("2026-10-31 00:00:00", 4320)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    migrate("downgrade", "0007_join_request_invites")
    migrate("upgrade", "0009_discount_promos")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM subscriptions").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM notification_logs").fetchone() == (1,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_full_sqlite_migrations_match_current_models(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    database = tmp_path / "head.db"
    env = dict(
        os.environ,
        APP_ENV="development",
        DATABASE_URL=f"sqlite:///{database.as_posix()}",
        PYTHONPATH=str(repo),
        PYTHONDONTWRITEBYTECODE="1",
    )
    for command in [
        ("upgrade", "head"),
        ("check",),
        ("downgrade", "0011_access_operations"),
        ("upgrade", "head"),
        ("check",),
    ]:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *command],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_schema_index_migration_can_render_postgresql_offline_sql():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).resolve().parents[1] / "alembic/versions/0012_schema_indexes.py"
    spec = importlib.util.spec_from_file_location("schema_indexes", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    output = io.StringIO()
    context = MigrationContext.configure(
        url="postgresql://", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        migration.upgrade()
    sql = output.getvalue()
    assert 'ALTER TABLE "users" DROP CONSTRAINT IF EXISTS "users_telegram_id_key"' in sql
    assert 'ALTER TABLE "promo_codes" DROP CONSTRAINT IF EXISTS "uq_promo_codes_code"' in sql
    assert "CREATE UNIQUE INDEX ix_users_telegram_id" in sql
    assert "CREATE INDEX ix_payments_user_id" in sql
