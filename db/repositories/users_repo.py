from __future__ import annotations
import aiosqlite
from typing import Any
from db.database import get_connection, utc_now_iso

class UsersRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def migrate_schema(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            for col, typ in [("paid_until","TEXT"), ("plan_devices","INTEGER NOT NULL DEFAULT 1"), ("is_admin","INTEGER NOT NULL DEFAULT 0")]:
                try:
                    await db.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
                    await db.commit()
                except aiosqlite.OperationalError: pass

    async def get_user(self, tg_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def create_user_if_not_exists(self, tg_id: int, username: str|None, first_name: str|None, last_name: str|None) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR IGNORE INTO users (tg_id, username, first_name, last_name, status) VALUES (?,?,?,?,'new')",
                (tg_id, username or "", first_name or "", last_name or ""))
            await db.commit()
            async with db.execute("SELECT changes()") as cur:
                return (await cur.fetchone())[0] > 0

    async def set_trial(self, tg_id: int, trial_start: str, trial_end: str, xui_email: str, xui_uuid: str, subscription_url: str) -> None:
        now = utc_now_iso()
        async with get_connection(self.db_path) as c:
            await c.execute("UPDATE users SET has_trial_used=1, status='trial_active', trial_start=?, trial_end=?, xui_email=?, xui_uuid=?, subscription_url=?, warned_48h=0, warned_24h=0, updated_at=? WHERE tg_id=?",
                (trial_start, trial_end, xui_email, xui_uuid, subscription_url, now, tg_id))
            await c.commit()

    async def set_admin(self, tg_id: int, is_admin: bool) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR IGNORE INTO users (tg_id) VALUES (?)", (tg_id,))
            await db.execute("UPDATE users SET is_admin=? WHERE tg_id=?", (int(is_admin), tg_id))
            await db.commit()

    async def is_admin(self, tg_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT is_admin FROM users WHERE tg_id=?", (tg_id,)) as cur:
                row = await cur.fetchone()
                return bool(row and row[0])

    async def get_all_dynamic_admins(self) -> list[int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT tg_id FROM users WHERE is_admin=1") as cur:
                return [r[0] async for r in cur]