"""Minimal payments repository placeholders for day-2 flow."""

from __future__ import annotations

from typing import Any

from db.database import get_connection, utc_now_iso


class PaymentsRepository:
    """Minimal payment persistence methods used as day-2 stubs."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def create_payment_stub(
        self,
        user_tg_id: int,
        amount: float | None = None,
        currency: str | None = None,
        status: str = "new",
        proof_file_id: str | None = None,
    ) -> int:
        now = utc_now_iso()
        async with get_connection(self.db_path) as connection:
            cursor = await connection.execute(
                """
                INSERT INTO payments (
                    user_tg_id, amount, currency, status, proof_file_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_tg_id, amount, currency, status, proof_file_id, now, now),
            )
            await connection.commit()
            return int(cursor.lastrowid)

    async def get_payment(self, payment_id: int) -> dict[str, Any] | None:
        async with get_connection(self.db_path) as connection:
            cursor = await connection.execute(
                "SELECT * FROM payments WHERE id = ?",
                (payment_id,),
            )
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def set_status(self, payment_id: int, status: str) -> None:
        async with get_connection(self.db_path) as connection:
            await connection.execute(
                "UPDATE payments SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc_now_iso(), payment_id),
            )
            await connection.commit()
