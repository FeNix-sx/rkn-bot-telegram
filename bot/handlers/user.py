from __future__ import annotations
import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from aiogram import Router, F, Bot
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command

from core.config import Settings
from core.xui_api import XUIAPI, XUIAPIError
from db.repositories.users_repo import UsersRepository
from bot.admin_stats_nav import delete_admin_stats_nav_message_if_any
from bot.user_message_tidy import try_delete_user_message
from bot.user_personal_nav import delete_message_pairs, remember_personal_nav_messages, take_personal_nav_batch
from bot.handlers.stats import show_personal_stats
from bot.keyboards import reply_menu
from bot.trial_visibility import should_show_trial_button, user_reply_menu

LOGGER = logging.getLogger(__name__)
router = Router()

PENDING_TRIALS: dict[int, dict] = {}
PENDING_NEW_USERS: dict[int, dict] = {}


async def dismiss_admin_pending_notices(bot: Bot, tg_id: int) -> None:
    """Удалить у всех админов карточки ожидающих /start и триала для tg_id (например при удалении юзера)."""
    pairs: list[tuple[int, int]] = []
    p1 = PENDING_NEW_USERS.pop(tg_id, None)
    if p1:
        pairs.extend(p1.get("admin_notices") or [])
    p2 = PENDING_TRIALS.pop(tg_id, None)
    if p2:
        pairs.extend(p2.get("admin_notices") or [])
    await delete_message_pairs(bot, pairs)

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
        url = f"vless://{uuid_val}@127.0.0.1:443?type=tcp&encryption=none&security=none#mock_{tg_id}"
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
    url = await xui_api.build_vless_share_uri(
        email=email,
        public_host=settings.xui_vless_host,
    )
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
async def start(msg: Message, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI, bot: Bot):
    tg = msg.from_user
    if not tg: return
    row = await users_repo.get_user(tg.id)
    is_adm = tg.id in settings.admin_ids or await users_repo.is_admin(tg.id)

    if tg.id in settings.admin_ids:
        created = await users_repo.create_user_if_not_exists(tg.id, tg.username, tg.first_name, tg.last_name)
        text = "Привет! Профиль создан." if created else "Бот активирован"
        row2 = await users_repo.get_user(tg.id)
        await msg.answer(text, reply_markup=await user_reply_menu(tg.id, settings, users_repo, xui_api, row=row2))
        return

    if row:
        await msg.answer(
            "Бот активирован",
            reply_markup=await user_reply_menu(tg.id, settings, users_repo, xui_api, row=row),
        )
        return

    if tg.id in PENDING_NEW_USERS:
        await msg.answer(
            "⏳ Запрос уже отправлен. Дождись подтверждения администратора.",
            reply_markup=reply_menu(False, show_trial=False),
        )
        return

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
    admin_notices: list[tuple[int, int]] = []
    for aid in admins:
        try:
            m = await bot.send_message(aid, admin_txt, reply_markup=kb)
            admin_notices.append((m.chat.id, m.message_id))
        except Exception:
            pass
    PENDING_NEW_USERS[tg.id] = {
        "username": tg.username,
        "first_name": tg.first_name,
        "last_name": tg.last_name,
        "admin_notices": admin_notices,
    }
    await msg.answer(
        "⏳ Запрос отправлен администратору. Дождись подтверждения доступа.",
        reply_markup=reply_menu(False, show_trial=False),
    )

@router.message(Command("status"))
@router.message(F.text == "📊 Мой статус")
async def status(msg: Message, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI, bot: Bot):
    tg = msg.from_user
    if not tg:
        return
    await delete_admin_stats_nav_message_if_any(bot, tg.id)
    await try_delete_user_message(bot, msg)
    await show_personal_stats(msg, tg.id, settings, users_repo, xui_api, bot)

@router.message(Command("link"))
@router.message(F.text == "🔗 Моя ссылка")
async def link(msg: Message, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI, bot: Bot):
    tg = msg.from_user
    if not tg:
        return
    await delete_admin_stats_nav_message_if_any(bot, tg.id)
    await try_delete_user_message(bot, msg)
    old = take_personal_nav_batch(tg.id)
    try:
        row = await users_repo.get_user(tg.id)
        if not row:
            if tg.id in PENDING_NEW_USERS:
                sent = await msg.answer(
                    "⏳ Сначала дождись подтверждения регистрации (/start).",
                    reply_markup=reply_menu(False, show_trial=False),
                )
                remember_personal_nav_messages(tg.id, [sent])
                return
            sent = await msg.answer(
                "Сначала /start и подтверждение администратора.",
                reply_markup=reply_menu(False, show_trial=False),
            )
            remember_personal_nav_messages(tg.id, [sent])
            return
        email = (row.get("xui_email") or "").strip()
        if not email:
            sent = await msg.answer(
                "Нет привязки к клиенту в панели (xui_email). Сначала триал / выдача админом.",
                reply_markup=await user_reply_menu(tg.id, settings, users_repo, xui_api, row=row),
            )
            remember_personal_nav_messages(tg.id, [sent])
            return
        kb = await user_reply_menu(tg.id, settings, users_repo, xui_api, row=row)
        if os.getenv("MOCK_XUI"):
            uid = (row.get("xui_uuid") or "00000000-0000-0000-0000-000000000001").strip()
            vless = f"vless://{uid}@127.0.0.1:443?type=tcp&encryption=none&security=none#mock"
        else:
            try:
                vless = await xui_api.build_vless_share_uri(
                    email=email,
                    public_host=settings.xui_vless_host,
                )
            except XUIAPIError as e:
                sent = await msg.answer(
                    f"Не удалось собрать vless-ссылку из панели: {e}",
                    reply_markup=kb,
                )
                remember_personal_nav_messages(tg.id, [sent])
                return
        s1 = await msg.answer("Ваша ссылка для подключения:", reply_markup=kb)
        s2 = await msg.answer(vless, reply_markup=kb)
        remember_personal_nav_messages(tg.id, [s1, s2])
    finally:
        await delete_message_pairs(bot, old)

@router.message(Command("trial"))
@router.message(F.text == "🚀 Получить триал")
async def cmd_trial(msg: Message, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI, bot: Bot):
    tg = msg.from_user
    if not tg:
        return
    await delete_admin_stats_nav_message_if_any(bot, tg.id)
    await try_delete_user_message(bot, msg)
    old = take_personal_nav_batch(tg.id)
    try:
        row = await users_repo.get_user(tg.id)
        if not row:
            if tg.id in PENDING_NEW_USERS:
                return await msg.answer(
                    "⏳ Сначала дождись подтверждения регистрации (/start).",
                    reply_markup=reply_menu(False, show_trial=False),
                )
            return await msg.answer(
                "Сначала нажми /start и дождись подтверждения администратора.",
                reply_markup=reply_menu(False, show_trial=False),
            )
        if not await should_show_trial_button(row, xui_api):
            if int(row.get("has_trial_used") or 0):
                reason = "Триал уже был использован."
            else:
                active, end_date = _is_sub_active(row)
                reason = (
                    f"Подписка уже активна до `{_fmt(end_date)}`."
                    if active
                    else "Сейчас триал недоступен (есть активная подписка в панели)."
                )
            return await msg.answer(
                f"🚫 {reason}",
                parse_mode="Markdown",
                reply_markup=await user_reply_menu(tg.id, settings, users_repo, xui_api, row=row),
            )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Разрешить", callback_data=f"trial:approve:{tg.id}")],
            [InlineKeyboardButton(text="❌ Запретить", callback_data=f"trial:deny:{tg.id}")]
        ])
        admins = set(settings.admin_ids)
        admins.update(await users_repo.get_all_dynamic_admins())
        name = tg.username or tg.first_name or f"ID:{tg.id}"
        text = f"🆔 Запрос на триал:\n👤 {name}\n⏳ Ожидает решения..."
        admin_notices: list[tuple[int, int]] = []
        for aid in admins:
            try:
                m = await bot.send_message(aid, text, reply_markup=kb)
                admin_notices.append((m.chat.id, m.message_id))
            except Exception:
                pass
        PENDING_TRIALS[tg.id] = {
            "username": tg.username,
            "first_name": tg.first_name,
            "admin_notices": admin_notices,
        }
        await msg.answer(
            "⏳ Запрос отправлен администраторам. Ожидайте решения.",
            reply_markup=await user_reply_menu(tg.id, settings, users_repo, xui_api, row=row),
        )
    finally:
        await delete_message_pairs(bot, old)