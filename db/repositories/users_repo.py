from __future__ import annotations
import aiosqlite
from typing import Any
from db.database import get_connection, utc_now_iso

class UsersRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def migrate_schema(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            for col, typ in [
                ("paid_until", "TEXT"),
                ("plan_devices", "INTEGER NOT NULL DEFAULT 1"),
                ("is_admin", "INTEGER NOT NULL DEFAULT 0"),
                ("approved_by_tg_id", "INTEGER"),
                ("vpn_issued_by_tg_id", "INTEGER"),
            ]:
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

    async def set_subscription_until(self, tg_id: int, end_iso: str) -> None:
        """Единая дата окончания: paid_until и trial_end (для /status и панели)."""
        now = utc_now_iso()
        async with get_connection(self.db_path) as c:
            await c.execute(
                "UPDATE users SET paid_until=?, trial_end=?, updated_at=? WHERE tg_id=?",
                (end_iso, end_iso, now, tg_id),
            )
            await c.commit()

    async def set_trial_approved_by(self, tg_id: int, admin_tg_id: int | None) -> None:
        now = utc_now_iso()
        async with get_connection(self.db_path) as c:
            await c.execute(
                "UPDATE users SET approved_by_tg_id=?, updated_at=? WHERE tg_id=?",
                (admin_tg_id, now, tg_id),
            )
            await c.commit()

    async def set_trial(
        self,
        tg_id: int,
        trial_start: str,
        trial_end: str,
        xui_email: str,
        xui_uuid: str,
        subscription_url: str,
        *,
        vpn_issued_by_tg_id: int | None = None,
    ) -> None:
        now = utc_now_iso()
        async with get_connection(self.db_path) as c:
            if vpn_issued_by_tg_id is not None:
                await c.execute(
                    "UPDATE users SET has_trial_used=1, status='trial_active', trial_start=?, trial_end=?, xui_email=?, xui_uuid=?, subscription_url=?, warned_48h=0, warned_24h=0, updated_at=?, vpn_issued_by_tg_id=? WHERE tg_id=?",
                    (trial_start, trial_end, xui_email, xui_uuid, subscription_url, now, vpn_issued_by_tg_id, tg_id),
                )
            else:
                await c.execute(
                    "UPDATE users SET has_trial_used=1, status='trial_active', trial_start=?, trial_end=?, xui_email=?, xui_uuid=?, subscription_url=?, warned_48h=0, warned_24h=0, updated_at=? WHERE tg_id=?",
                    (trial_start, trial_end, xui_email, xui_uuid, subscription_url, now, tg_id),
                )
            await c.commit()

    async def fill_steward_nulls(self, user_tg_id: int, admin_tg_id: int) -> None:
        """Только пустые approved_by / vpn_issued → admin_tg_id; не перезаписывает уже заданные."""
        now = utc_now_iso()
        async with get_connection(self.db_path) as c:
            await c.execute(
                """UPDATE users SET
                   approved_by_tg_id = COALESCE(approved_by_tg_id, ?),
                   vpn_issued_by_tg_id = COALESCE(vpn_issued_by_tg_id, ?),
                   updated_at = ?
                   WHERE tg_id = ?""",
                (admin_tg_id, admin_tg_id, now, user_tg_id),
            )
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