import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import BACKEND_DIR, settings

def database_path() -> Path:
    path = Path(settings.database_path)
    if not path.is_absolute():
        path = BACKEND_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

@contextmanager
def connect():
    conn = sqlite3.connect(database_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
            filename TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            result_json TEXT
        )
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(audits)")}
        for name, field_type in {"storage_id": "TEXT", "sha256": "TEXT", "size_bytes": "INTEGER"}.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE audits ADD COLUMN {name} {field_type}")
