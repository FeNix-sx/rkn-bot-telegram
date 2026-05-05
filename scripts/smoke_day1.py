"""Deterministic day-1 smoke scenario runner."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.main import _format_dt, _status_text, _utc_now
from db.database import init_db
from db.repositories.users_repo import UsersRepository


class FakeXUI:
    """Minimal fake 3x-ui client for local deterministic smoke."""

    async def resolve_inbound_id(self, tag_or_remark: str = "vless_reality") -> int:
        if tag_or_remark != "vless_reality":
            raise RuntimeError(f"unexpected tag_or_remark={tag_or_remark!r}")
        return 101

    async def add_client(self, inbound_id: int, email: str, client_uuid: str, limit_ip: int) -> None:
        if inbound_id != 101:
            raise RuntimeError(f"unexpected inbound_id={inbound_id}")
        if not email.startswith("trial_"):
            raise RuntimeError(f"unexpected email={email}")
        if not client_uuid:
            raise RuntimeError("uuid is empty")
        if limit_ip != 1:
            raise RuntimeError(f"unexpected limit_ip={limit_ip}")

    async def build_or_get_subscription_url(
        self,
        *,
        inbound_id: int | None = None,
        email: str | None = None,
        existing_url: str | None = None,
        sub_id: str | None = None,
    ) -> str:
        del inbound_id, sub_id
        if existing_url and existing_url.strip():
            return existing_url.strip()
        if not email:
            raise RuntimeError("email is required")
        return f"https://fake-xui.local/sub/{email}"


async def run_smoke() -> dict[str, object]:
    db_fd, db_path = tempfile.mkstemp(prefix="smoke_day1_", suffix=".sqlite3")
    os.close(db_fd)
    try:
        await init_db(db_path)
        users_repo = UsersRepository(db_path)
        xui_api = FakeXUI()
        tg_id = 900000001

        created = await users_repo.create_user_if_not_exists(
            tg_id=tg_id,
            username="smoke_user",
            first_name="Smoke",
            last_name="Test",
        )
        user_after_start = await users_repo.get_user(tg_id)

        trial_result: dict[str, object] = {"granted": False, "reason": None}
        row = await users_repo.get_user(tg_id)
        if row is None:
            raise RuntimeError("user row not found after create_user_if_not_exists")

        if int(row.get("has_trial_used") or 0) == 1:
            trial_result["reason"] = "already_used"
        else:
            trial_start = _utc_now()
            trial_end = trial_start + timedelta(days=3)
            xui_email = f"trial_{tg_id}"
            xui_uuid = str(uuid4())

            inbound_id = await xui_api.resolve_inbound_id("vless_reality")
            await xui_api.add_client(
                inbound_id=inbound_id,
                email=xui_email,
                client_uuid=xui_uuid,
                limit_ip=1,
            )
            subscription_url = await xui_api.build_or_get_subscription_url(
                inbound_id=inbound_id,
                email=xui_email,
            )
            await users_repo.set_trial(
                tg_id=tg_id,
                trial_start=trial_start.isoformat(timespec="seconds"),
                trial_end=trial_end.isoformat(timespec="seconds"),
                xui_email=xui_email,
                xui_uuid=xui_uuid,
                subscription_url=subscription_url,
            )
            trial_result.update(
                {
                    "granted": True,
                    "reason": None,
                    "inbound_id": inbound_id,
                    "subscription_url": subscription_url,
                }
            )

        row_after_trial = await users_repo.get_user(tg_id)
        if row_after_trial is None:
            raise RuntimeError("user row not found after set_trial")

        second_trial_denied = int(row_after_trial.get("has_trial_used") or 0) == 1
        status_view = {
            "status": _status_text(row_after_trial),
            "has_trial_used": "yes"
            if int(row_after_trial.get("has_trial_used") or 0) == 1
            else "no",
            "trial_end": _format_dt(row_after_trial.get("trial_end")),
            "has_subscription_url": "yes"
            if str(row_after_trial.get("subscription_url") or "").strip()
            else "no",
        }
        link_url = str(row_after_trial.get("subscription_url") or "").strip()
        link_view = {
            "subscription_url": link_url,
            "present": bool(link_url),
        }
        checks = [
            {
                "id": "start_new_user",
                "ok": created is True and user_after_start is not None,
            },
            {
                "id": "trial_first_request",
                "ok": trial_result["granted"] is True,
            },
            {
                "id": "trial_second_request_denied",
                "ok": second_trial_denied is True,
            },
            {
                "id": "status_after_trial",
                "ok": status_view["status"] == "trial_active"
                and status_view["has_subscription_url"] == "yes",
            },
            {
                "id": "link_returns_url",
                "ok": link_view["present"] is True,
            },
        ]
        return {
            "checks": checks,
            "trial_result": trial_result,
            "status_view": status_view,
            "link_view": link_view,
        }
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def main() -> int:
    result = asyncio.run(run_smoke())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    failed = [item for item in result["checks"] if not item["ok"]]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
