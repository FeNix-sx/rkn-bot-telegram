"""Когда показывать кнопку «Получить триал» и как собрать reply-меню."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from aiogram.types import ReplyKeyboardMarkup

from bot.keyboards import reply_menu
from core.config import Settings
from core.xui_api import XUIAPI
from db.repositories.users_repo import UsersRepository

# Как в bot/handlers/stats.py: бессрочно в 3X-UI
XUI_UNLIMITED_MS = 9999999999999


def _is_sub_active_db(row: dict) -> bool:
    now = datetime.now(timezone.utc)
    end_raw = row.get("paid_until") or row.get("trial_end")
    if not end_raw:
        return False
    try:
        end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        return end_dt.astimezone(timezone.utc) > now
    except ValueError:
        return False


def has_xui_binding(row: dict | None) -> bool:
    if not row:
        return False
    return bool((row.get("xui_email") or "").strip() or (row.get("xui_uuid") or "").strip())


async def panel_subscription_active(xui_api: XUIAPI, email: str) -> bool | None:
    """
    True — клиент найден в панели и подписка активна или бессрочна.
    False — найден и срок истёк.
    None — клиент не найден или ошибка API.
    """
    try:
        inbounds = await xui_api.get_inbounds()
        for ib in inbounds:
            s_raw = ib.get("settings")
            if not isinstance(s_raw, str):
                continue
            for c in json.loads(s_raw).get("clients", []):
                if c.get("email") != email:
                    continue
                exp_ms = int(c.get("expiryTime") or 0)
                if exp_ms <= 0 or exp_ms >= XUI_UNLIMITED_MS:
                    return True
                end = datetime.fromtimestamp(exp_ms / 1000, tz=timezone.utc)
                return end > datetime.now(timezone.utc)
        return None
    except Exception:
        return None


async def should_show_trial_button(row: dict | None, xui_api: XUIAPI) -> bool:
    if not row:
        return False
    if int(row.get("has_trial_used") or 0):
        return False
    if _is_sub_active_db(row):
        return False
    if not has_xui_binding(row):
        return True
    email = (row.get("xui_email") or "").strip()
    if not email:
        return True
    pa = await panel_subscription_active(xui_api, email)
    if pa is True:
        return False
    return True


async def user_reply_menu(
    tg_id: int,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
    *,
    row: dict | None = None,
) -> ReplyKeyboardMarkup:
    if row is None:
        row = await users_repo.get_user(tg_id)
    is_adm = tg_id in settings.admin_ids or await users_repo.is_admin(tg_id)
    show_trial = await should_show_trial_button(row, xui_api)
    return reply_menu(is_adm, show_trial=show_trial)
