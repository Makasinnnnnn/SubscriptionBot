import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_separate_migration_preserves_subscription_tables(tmp_path):
    database = tmp_path / "shared.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, marker TEXT)")
        conn.execute("INSERT INTO subscriptions VALUES (1, 'untouched')")
    env = dict(os.environ, JOURNAL_DATABASE_URL=f"sqlite+aiosqlite:///{database}")
    root = Path(__file__).resolve().parents[1]
    for _ in range(2):
        completed = subprocess.run([sys.executable, "-m", "app.journal.migrate"], check=False, cwd=root, env=env,
                                   capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, completed.stderr
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT marker FROM subscriptions").fetchone() == ("untouched",)
        assert conn.execute("SELECT version_num FROM journal_alembic_version").fetchone() == ("j0001",)
        assert conn.execute("SELECT COUNT(*) FROM journal_mutex").fetchone() == (1,)
