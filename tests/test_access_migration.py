import sqlite3
from pathlib import Path
from types import SimpleNamespace

from alembic.config import Config

from alembic import command


def test_upgrade_seeds_known_inactive_reconciliation_jobs(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "legacy.db"
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: SimpleNamespace(database_url=f"sqlite:///{database.as_posix()}"),
    )
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(config, "0009_discount_promos")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO users (id,telegram_id) VALUES (1,500)")
        connection.execute(
            "INSERT INTO channels (id,telegram_chat_id,title,is_active) VALUES (1,-1001,'A',1)"
        )
        connection.execute(
            "INSERT INTO subscriptions (id,user_id,channel_id,status,starts_at,period_started_at,expires_at,source) VALUES (1,1,1,'expired','2026-01-01','2026-01-01','2026-02-01','manual')"
        )
        connection.execute(
            "INSERT INTO invite_links (id,subscription_id,link,is_revoked,primary_join_request_enabled,materials_channel_id,materials_link,materials_is_revoked,materials_join_request_enabled) VALUES (1,1,'https://t.me/+old',1,1,-1099,'https://t.me/+materials',1,1)"
        )
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT kind,chat_id,invite_link,completed_at FROM access_operations ORDER BY operation_key"
        ).fetchall()
        assert len(rows) == 4
        assert sum(kind == "remove_member" for kind, *_ in rows) == 2
        assert sum(kind == "revoke_link" for kind, *_ in rows) == 2
        assert all(completed is None for *_, completed in rows)
        assert {chat for _, chat, _, _ in rows} == {-1001, -1099}
