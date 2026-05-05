"""SQLite async initialization and connection helpers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import aiosqlite


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _schema_sql() -> str:
    schema_path = Path(__file__).with_name("schema.sql")
    return schema_path.read_text(encoding="utf-8")


async def init_db(db_path: str) -> None:
    """Create sqlite file (if missing) and apply schema idempotently."""
    target = Path(db_path)
    if target.parent != Path("."):
        target.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(target) as connection:
        await connection.execute("PRAGMA foreign_keys = ON;")
        await connection.executescript(_schema_sql())
        await connection.commit()


@asynccontextmanager
async def get_connection(db_path: str):
    """Yield configured aiosqlite connection with row-based access."""
    async with aiosqlite.connect(db_path) as connection:
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON;")
        yield connection
