"""Repository for users table operations."""

from __future__ import annotations

from typing import Any

from db.database import get_connection, utc_now_iso


class UsersRepository:
    """Thin async repository for user records."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def get_user(self, tg_id: int) -> dict[str, Any] | None:
        async with get_connection(self.db_path) as connection:
            cursor = await connection.execute(
                "SELECT * FROM users WHERE tg_id = ?",
                (tg_id,),
            )
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def create_user_if_not_exists(
        self,
        tg_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> bool:
        """Create user if absent. Returns True only when inserted."""
        now = utc_now_iso()
        async with get_connection(self.db_path) as connection:
            cursor = await connection.execute(
                """
                INSERT OR IGNORE INTO users (
                    tg_id, username, first_name, last_name, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (tg_id, username, first_name, last_name, now, now),
            )
            await connection.commit()
        return cursor.rowcount > 0

    async def set_trial(
        self,
        tg_id: int,
        trial_start: str,
        trial_end: str,
        xui_email: str,
        xui_uuid: str,
        subscription_url: str,
    ) -> None:
        now = utc_now_iso()
        async with get_connection(self.db_path) as connection:
            await connection.execute(
                """
                UPDATE users
                SET
                    has_trial_used = 1,
                    status = 'trial_active',
                    trial_start = ?,
                    trial_end = ?,
                    xui_email = ?,
                    xui_uuid = ?,
                    subscription_url = ?,
                    warned_48h = 0,
                    warned_24h = 0,
                    updated_at = ?
                WHERE tg_id = ?
                """,
                (
                    trial_start,
                    trial_end,
                    xui_email,
                    xui_uuid,
                    subscription_url,
                    now,
                    tg_id,
                ),
            )
            await connection.commit()

    async def set_status(self, tg_id: int, status: str) -> None:
        async with get_connection(self.db_path) as connection:
            await connection.execute(
                "UPDATE users SET status = ?, updated_at = ? WHERE tg_id = ?",
                (status, utc_now_iso(), tg_id),
            )
            await connection.commit()

    async def set_subscription_url(self, tg_id: int, subscription_url: str) -> None:
        async with get_connection(self.db_path) as connection:
            await connection.execute(
                """
                UPDATE users
                SET subscription_url = ?, updated_at = ?
                WHERE tg_id = ?
                """,
                (subscription_url, utc_now_iso(), tg_id),
            )
            await connection.commit()
