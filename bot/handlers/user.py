from __future__ import annotations
import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from aiogram import Router, F, Bot
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command

from core.config import Settings
from core.xui_api import XUIAPI
from db.repositories.users_repo import UsersRepository
from bot.keyboards import reply_menu
from bot.handlers.stats import show_personal_stats

LOGGER = logging.getLogger(__name__)
router = Router()

PENDING_TRIALS: dict[int, dict] = {}
PENDING_NEW_USERS: dict[int, dict] = {}

def _fmt(dt):
    if not dt: return "n/a"
    try: return datetime.fromisoformat(dt).strftime("%Y-%m-%d %H:%M:%S UTC")
    except: return "n/a"

def _is_sub_active(row: dict | None) -> tuple[bool, str | None]:
    if not row: return False, None
    now = datetime.now(timezone.utc)
    end_raw = row.get("paid_until") or row.get("trial_end")
    if not end_raw: return False, None
    try:
        end_dt = datetime.fromisoformat(end_raw).replace(tzinfo=timezone.utc)
        if end_dt > now: return True, end_raw
    except: pass
    return False, None

async def _issue_trial_now(
    tg_id: int,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
    *,
    vpn_issued_by_tg_id: int | None = None,
) -> str:
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=settings.trial_days)
    email = f"trial_{tg_id}"
    uuid_val = str(uuid4())
    if os.getenv("MOCK_XUI"):
        url = f"mock://sub/{email}"
        await users_repo.set_trial(
            tg_id,
            start.isoformat(timespec="seconds"),
            end.isoformat(timespec="seconds"),
            email,
            uuid_val,
            url,
            vpn_issued_by_tg_id=vpn_issued_by_tg_id,
        )
        return url
    inbound_id = await xui_api.resolve_target_inbound_id(
        numeric_id=settings.xui_inbound_id,
        tag=settings.xui_inbound_tag,
    )
    await xui_api.add_client(inbound_id, email, uuid_val, limit_ip=1)
    url = await xui_api.build_or_get_subscription_url(inbound_id=inbound_id, email=email)
    await users_repo.set_trial(
        tg_id,
        start.isoformat(timespec="seconds"),
        end.isoformat(timespec="seconds"),
        email,
        uuid_val,
        url,
        vpn_issued_by_tg_id=vpn_issued_by_tg_id,
    )
    return url

@router.message(Command("start"))
async def start(msg: Message, settings: Settings, users_repo: UsersRepository, bot: Bot):
    tg = msg.from_user
    if not tg: return
    row = await users_repo.get_user(tg.id)
    is_adm = tg.id in settings.admin_ids or await users_repo.is_admin(tg.id)

    if tg.id in settings.admin_ids:
        created = await users_repo.create_user_if_not_exists(tg.id, tg.username, tg.first_name, tg.last_name)
        text = "Привет! Профиль создан." if created else "Бот активирован"
        await msg.answer(text, reply_markup=reply_menu(True))
        return

    if row:
        await msg.answer("Бот активирован", reply_markup=reply_menu(is_adm))
        return

    if tg.id in PENDING_NEW_USERS:
        await msg.answer(
            "⏳ Запрос уже отправлен. Дождись подтверждения администратора.",
            reply_markup=reply_menu(False),
        )
        return

    PENDING_NEW_USERS[tg.id] = {
        "username": tg.username,
        "first_name": tg.first_name,
        "last_name": tg.last_name,
    }
    display_name = tg.username or tg.first_name or "Без имени"
    admin_txt = (
        "Новый пользователь активировал бота.\n\n"
        f"🆔 /start:\nИмя: {display_name}\nID: {tg.id}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=f"start:approve:{tg.id}"),
                InlineKeyboardButton(text="❌ Нет", callback_data=f"start:deny:{tg.id}"),
            ],
        ]
    )
    admins = set(settings.admin_ids)
    admins.update(await users_repo.get_all_dynamic_admins())
    for aid in admins:
        try:
            await bot.send_message(aid, admin_txt, reply_markup=kb)
        except Exception:
            pass
    await msg.answer(
        "⏳ Запрос отправлен администратору. Дождись подтверждения доступа.",
        reply_markup=reply_menu(False),
    )

@router.message(Command("status"))
@router.message(F.text == "📊 Мой статус")
async def status(msg: Message, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    tg = msg.from_user
    if not tg: return
    await show_personal_stats(msg, tg.id, settings, users_repo, xui_api)

@router.message(Command("link"))
@router.message(F.text == "🔗 Моя ссылка")
async def link(msg: Message, settings: Settings, users_repo: UsersRepository):
    tg = msg.from_user
    if not tg: return
    row = await users_repo.get_user(tg.id)
    if not row:
        if tg.id in PENDING_NEW_USERS:
            return await msg.answer(
                "⏳ Сначала дождись подтверждения регистрации (/start).",
                reply_markup=reply_menu(False),
            )
        return await msg.answer(
            "Сначала /start и подтверждение администратора.",
            reply_markup=reply_menu(False),
        )
    url = (row.get("subscription_url") or "").strip()
    if not url: return await msg.answer("Нет ссылки. Запусти /trial.", reply_markup=reply_menu())
    is_adm = tg.id in settings.admin_ids or await users_repo.is_admin(tg.id)
    await msg.answer(f"Твоя ссылка: {url}", reply_markup=reply_menu(is_adm))

@router.message(Command("trial"))
@router.message(F.text == "🚀 Получить триал")
async def cmd_trial(msg: Message, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI, bot: Bot):
    tg = msg.from_user
    if not tg: return
    row = await users_repo.get_user(tg.id)
    if not row:
        if tg.id in PENDING_NEW_USERS:
            return await msg.answer(
                "⏳ Сначала дождись подтверждения регистрации (/start).",
                reply_markup=reply_menu(False),
            )
        return await msg.answer(
            "Сначала нажми /start и дождись подтверждения администратора.",
            reply_markup=reply_menu(False),
        )
    active, end_date = _is_sub_active(row)
    if active:
        await msg.answer(f"🚫 Подписка уже активирована.\n📅 Истекает: `{_fmt(end_date)}`", parse_mode="Markdown", reply_markup=reply_menu())
        return
    PENDING_TRIALS[tg.id] = {"username": tg.username, "first_name": tg.first_name}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Разрешить", callback_data=f"trial:approve:{tg.id}")],
        [InlineKeyboardButton(text="❌ Запретить", callback_data=f"trial:deny:{tg.id}")]
    ])
    admins = set(settings.admin_ids)
    admins.update(await users_repo.get_all_dynamic_admins())
    name = tg.username or tg.first_name or f"ID:{tg.id}"
    text = f"🆔 Запрос на триал:\n👤 {name}\n⏳ Ожидает решения..."
    for aid in admins:
        try: await bot.send_message(aid, text, reply_markup=kb)
        except: pass
    await msg.answer("⏳ Запрос отправлен администраторам. Ожидайте решения.")