from __future__ import annotations
import logging
import aiosqlite
import json
from datetime import datetime, timezone
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
from bot.admin_stats_nav import (
    delete_admin_stats_nav_message_if_any,
    remember_admin_stats_nav_message,
    remember_admin_stats_reply_anchor,
)
from bot.callback_edit import edit_callback_nav
from bot.user_message_tidy import try_delete_user_message
from bot.user_personal_nav import delete_message_pairs, remember_personal_nav_messages, take_personal_nav_batch
from bot.trial_visibility import user_reply_menu
from core.xui_api import XUIAPI
from core.config import Settings
from db.repositories.users_repo import UsersRepository

LOGGER = logging.getLogger(__name__)
router = Router()


def _role_label(tg_id: int, settings: Settings, row: dict | None) -> str:
    """Админ из .env (admin_ids) или флаг is_admin в БД."""
    if tg_id in settings.admin_ids:
        return "Администратор"
    if row and row.get("is_admin"):
        return "Администратор"
    return "Пользователь"


def _gb(val: int | float) -> str:
    return f"{val / 1024**3:.2f} ГБ"

async def _get_client_info(xui_api: XUIAPI, email: str) -> dict:
    """Получает limitIp, enable и expiryTime из настроек 3X-UI."""
    try:
        inbounds = await xui_api.get_inbounds()
        for ib in inbounds:
            s_raw = ib.get("settings")
            if isinstance(s_raw, str):
                for c in json.loads(s_raw).get("clients", []):
                    if c.get("email") == email:
                        return {
                            "limit_ip": int(c.get("limitIp") or 1),
                            "enable": bool(c.get("enable", True)),
                            "expiry_ms": int(c.get("expiryTime") or 0),
                        }
    except Exception:
        LOGGER.exception("xui.client_info.error")
    return {"limit_ip": 1, "enable": True, "expiry_ms": 0}

async def _admin_display(users_repo: UsersRepository, admin_tg: int | None) -> str | None:
    if admin_tg is None:
        return None
    row = await users_repo.get_user(admin_tg)
    if row:
        un = (row.get("username") or "").strip().lstrip("@")
        if un:
            return f"@{un}"
    return f"ID:{admin_tg}"


def _format_stat_block(
    name: str | None,
    up: int,
    down: int,
    info: dict,
    show_name: bool = True,
    role_label: str | None = None,
    trial_approver: str | None = None,
    vpn_issuer: str | None = None,
) -> str:
    total = up + down
    lines = []
    if show_name and name:
        lines.append(f"👤 `{name}`")
    if role_label:
        lines.append(f"🔑 Полномочия: `{role_label}`")
    if trial_approver is not None or vpn_issuer is not None:
        lines.append(f"✅ Одобрил: `{trial_approver or '—'}`")
        lines.append(f"🔗 VPN: `{vpn_issuer or '—'}`")
    lines.append(f"📊 трафик: ↑{_gb(up)} ↓{_gb(down)} | {_gb(total)} общий")
    lines.append(f"🔗 подключений: `{info['limit_ip']}`")
    lines.append(f"📡 статус: {'✅ вкл' if info['enable'] else '❌ откл'}")

    # Форматирование даты окончания
    exp_ms = info["expiry_ms"]
    if exp_ms > 0 and exp_ms < 9999999999999:
        exp_dt = datetime.fromtimestamp(exp_ms / 1000, tz=timezone.utc)
        lines.append(f"📅 подписка до: `{exp_dt.strftime('%d.%m.%Y')}`")
    else:
        lines.append(f"📅 подписка до: `бессрочно`")

    return "\n".join(lines)


def _stats_admin_root_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="stats:admin:users")],
            [InlineKeyboardButton(text="📥 Клиенты в панели", callback_data="stats:admin:panel")],
            [InlineKeyboardButton(text="🔙 К управлению", callback_data="admin:main")],
        ]
    )


def _stats_admin_sub_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 К управлению", callback_data="admin:main")],
        ]
    )


async def _is_admin_user(tg_id: int, settings: Settings, users_repo: UsersRepository) -> bool:
    return tg_id in settings.admin_ids or await users_repo.is_admin(tg_id)


def _remember_stats_nav(cb: CallbackQuery, nav_msg: Message | None) -> None:
    if cb.from_user and nav_msg:
        remember_admin_stats_nav_message(cb.from_user.id, nav_msg.chat.id, nav_msg.message_id)


async def _render_admin_stats_menu(
    msg: Message,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
) -> None:
    tg = msg.from_user
    if not tg:
        return
    row = await users_repo.get_user(tg.id)
    anchor = await msg.answer(
        "\u2060",
        reply_markup=await user_reply_menu(tg.id, settings, users_repo, xui_api, row=row),
    )
    remember_admin_stats_reply_anchor(tg.id, anchor.chat.id, anchor.message_id)
    sent = await msg.answer(
        "📊 **Статистика**\n\n"
        "• **Пользователи** — есть в боте и указан клиент 3X-UI (статус, ссылка, одобрение и т.д.).\n"
        "• **Клиенты в панели** — только в 3X-UI, к Telegram в боте не привязаны (трафик и параметры клиента в панели).",
        parse_mode="Markdown",
        reply_markup=_stats_admin_root_kb(),
    )
    remember_admin_stats_nav_message(tg.id, sent.chat.id, sent.message_id)


_STAT_PREVIEW_LIMIT = 5

def _collect_client_info_map(inbounds: list) -> dict[str, dict]:
    """Собирает email -> {limit_ip, enable, expiry_ms} одним проходом по inbounds."""
    out: dict[str, dict] = {}
    for ib in inbounds:
        s_raw = ib.get("settings")
        if not isinstance(s_raw, str):
            continue
        try:
            for c in json.loads(s_raw).get("clients", []):
                email = str(c.get("email") or "").strip()
                if not email or email in out:
                    continue
                out[email] = {
                    "limit_ip": int(c.get("limitIp") or 1),
                    "enable": bool(c.get("enable", True)),
                    "expiry_ms": int(c.get("expiryTime") or 0),
                }
        except json.JSONDecodeError:
            continue
    return out


def _collect_traffic_map(inbounds: list) -> dict[str, tuple[int, int]]:
    """Собирает email -> (up, down) по clientStats."""
    out: dict[str, tuple[int, int]] = {}
    for ib in inbounds:
        for c in ib.get("clientStats") or []:
            email = str(c.get("email") or "").strip()
            if not email:
                continue
            out[email] = (int(c.get("up", 0) or 0), int(c.get("down", 0) or 0))
    return out


def _stats_admin_paged_kb(branch: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Только пагинация; выход — через reply-меню (📈 Статистика / ⚙️ Управление и т.д.)."""
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️", callback_data=f"stats:admin:{branch}:page:{page - 1}"
            )
        )
    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton(
                text="➡️", callback_data=f"stats:admin:{branch}:page:{page + 1}"
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[nav] if nav else [])


async def _collect_unbound_panel_emails(settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI) -> list[str]:
    async with aiosqlite.connect(users_repo.db_path) as db:
        async with db.execute(
            "SELECT xui_email FROM users WHERE xui_email IS NOT NULL AND trim(xui_email) != ''"
        ) as cur:
            bound_emails = {row[0] async for row in cur}
    inbounds = await xui_api.get_inbounds()
    seen: set[str] = set()
    out: list[str] = []
    for ib in inbounds:
        for c in ib.get("clientStats") or []:
            email = c.get("email")
            if email and email not in bound_emails and email not in seen:
                seen.add(email)
                out.append(email)
        s_raw = ib.get("settings")
        if isinstance(s_raw, str):
            for c in json.loads(s_raw).get("clients", []):
                email = c.get("email")
                if email and email not in bound_emails and email not in seen:
                    seen.add(email)
                    out.append(email)
    out.sort()
    return out


@router.message(Command("stats"))
async def stats_command(
    msg: Message,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
    bot: Bot,
):
    tg = msg.from_user
    if not tg:
        return
    await try_delete_user_message(bot, msg)
    is_adm = tg.id in settings.admin_ids or await users_repo.is_admin(tg.id)
    if is_adm:
        old = take_personal_nav_batch(tg.id)
        try:
            await delete_admin_stats_nav_message_if_any(bot, tg.id)
            await _render_admin_stats_menu(msg, settings, users_repo, xui_api)
        finally:
            await delete_message_pairs(bot, old)
    else:
        await show_personal_stats(msg, tg.id, settings, users_repo, xui_api, bot)


@router.message(F.text == "📈 Статистика")
async def stats_reply_button(
    msg: Message,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
    bot: Bot,
):
    tg = msg.from_user
    if not tg:
        return
    await try_delete_user_message(bot, msg)
    old = take_personal_nav_batch(tg.id)
    try:
        is_adm = tg.id in settings.admin_ids or await users_repo.is_admin(tg.id)
        if not is_adm:
            await msg.answer(
                "🚫 Нет доступа.",
                reply_markup=await user_reply_menu(tg.id, settings, users_repo, xui_api),
            )
            return
        await delete_admin_stats_nav_message_if_any(bot, tg.id)
        await _render_admin_stats_menu(msg, settings, users_repo, xui_api)
    finally:
        await delete_message_pairs(bot, old)


@router.callback_query(F.data.startswith("stats:admin:"))
async def stats_admin_branch(
    cb: CallbackQuery,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
    bot: Bot,
):
    if not cb.from_user or not await _is_admin_user(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":")
    if len(parts) < 3:
        return await cb.answer("Ошибка данных", show_alert=True)
    mode = parts[2]
    page = 0
    # stats:admin:{users|panel}:page:{n}
    if len(parts) >= 5 and parts[3] == "page":
        try:
            page = int(parts[4])
        except ValueError:
            page = 0
    await cb.answer()
    old = take_personal_nav_batch(cb.from_user.id) if cb.from_user else []
    try:
        if mode == "root":
            nav_msg = await edit_callback_nav(
                cb,
                "📊 **Статистика**\n\n"
                "• **Пользователи** — есть в боте и указан клиент 3X-UI.\n"
                "• **Клиенты в панели** — только в 3X-UI, без привязки к Telegram в боте.",
                _stats_admin_root_kb(),
                parse_mode="Markdown",
            )
            _remember_stats_nav(cb, nav_msg)
            return
        if mode == "users":
            try:
                async with aiosqlite.connect(settings.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.execute("""
                        SELECT tg_id, username, xui_email, is_admin, approved_by_tg_id, vpn_issued_by_tg_id
                        FROM users WHERE xui_email IS NOT NULL AND trim(xui_email) != '' ORDER BY tg_id
                    """) as cur:
                        users = await cur.fetchall()
                if not users:
                    nav_msg = await edit_callback_nav(
                        cb, "📭 Нет пользователей с клиентом в панели.", _stats_admin_sub_kb()
                    )
                    _remember_stats_nav(cb, nav_msg)
                    return
                inbounds = await xui_api.get_inbounds()
                traffic_map = _collect_traffic_map(inbounds)
                client_info_map = _collect_client_info_map(inbounds)
                blocks: list[str] = []
                total = len(users)
                page_size = _STAT_PREVIEW_LIMIT
                total_pages = max(1, (total + page_size - 1) // page_size)
                page = max(0, min(page, total_pages - 1))

                current = users[page * page_size : (page + 1) * page_size]
                for u in current:
                    email = u["xui_email"]
                    up, down = traffic_map.get(email, (0, 0))
                    info = client_info_map.get(email) or {"limit_ip": 1, "enable": True, "expiry_ms": 0}
                    name = u["username"] or f"ID:{u['tg_id']}"
                    rid = u["tg_id"]
                    role = _role_label(rid, settings, {k: u[k] for k in u.keys()})
                    ap = await _admin_display(users_repo, u["approved_by_tg_id"])
                    vp = await _admin_display(users_repo, u["vpn_issued_by_tg_id"])
                    blocks.append(
                        _format_stat_block(
                            name, up, down, info, show_name=True, role_label=role, trial_approver=ap, vpn_issuer=vp
                        )
                    )
                head = f"📊 **Пользователи** (стр. {page + 1}/{total_pages}, всего {total}):\n\n"
                text = head + "\n\n──────────────\n\n".join(blocks)
                kb = _stats_admin_paged_kb("users", page, total_pages)
                nav_msg = await edit_callback_nav(cb, text, kb, parse_mode="Markdown")
                _remember_stats_nav(cb, nav_msg)
                return
            except Exception as e:
                LOGGER.exception("stats.admin.users.error")
                nav_msg = await edit_callback_nav(cb, f"❌ {type(e).__name__}", _stats_admin_sub_kb())
                _remember_stats_nav(cb, nav_msg)
                return
        if mode == "panel":
            try:
                emails = await _collect_unbound_panel_emails(settings, users_repo, xui_api)
                if not emails:
                    nav_msg = await edit_callback_nav(
                        cb,
                        "📭 Нет клиентов в панели без привязки к пользователю бота.",
                        _stats_admin_sub_kb(),
                    )
                    _remember_stats_nav(cb, nav_msg)
                    return
                inbounds = await xui_api.get_inbounds()
                traffic_map = _collect_traffic_map(inbounds)
                client_info_map = _collect_client_info_map(inbounds)
                blocks: list[str] = []
                total = len(emails)
                page_size = _STAT_PREVIEW_LIMIT
                total_pages = max(1, (total + page_size - 1) // page_size)
                page = max(0, min(page, total_pages - 1))

                current = emails[page * page_size : (page + 1) * page_size]
                for email in current:
                    up, down = traffic_map.get(email, (0, 0))
                    info = client_info_map.get(email) or {"limit_ip": 1, "enable": True, "expiry_ms": 0}
                    blocks.append(_format_stat_block(email, up, down, info, show_name=True))
                head = f"📊 **Клиенты в панели** (стр. {page + 1}/{total_pages}, всего {total}):\n\n"
                text = head + "\n\n──────────────\n\n".join(blocks)
                kb = _stats_admin_paged_kb("panel", page, total_pages)
                nav_msg = await edit_callback_nav(cb, text, kb, parse_mode="Markdown")
                _remember_stats_nav(cb, nav_msg)
                return
            except Exception as e:
                LOGGER.exception("stats.admin.panel.error")
                nav_msg = await edit_callback_nav(cb, f"❌ {type(e).__name__}", _stats_admin_sub_kb())
                _remember_stats_nav(cb, nav_msg)
                return
        return await cb.answer("Неизвестно", show_alert=True)
    finally:
        await delete_message_pairs(bot, old)


async def show_personal_stats(
    msg: Message,
    tg_id: int,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
    bot: Bot,
):
    old = take_personal_nav_batch(tg_id)
    try:
        row = await users_repo.get_user(tg_id)
        role = _role_label(tg_id, settings, row)
        if not row or not row.get("xui_email"):
            sent = await msg.answer(
                f"🔍 Профиль не привязан к панели.\n🔑 Полномочия: `{role}`",
                parse_mode="Markdown",
                reply_markup=await user_reply_menu(tg_id, settings, users_repo, xui_api, row=row),
            )
            remember_personal_nav_messages(tg_id, [sent])
            return

        email = row["xui_email"]
        inbounds = await xui_api.get_inbounds()
        client_stats = next((c for ib in inbounds for c in (ib.get("clientStats") or []) if c.get("email") == email), None)
        up = client_stats.get("up", 0) if client_stats else 0
        down = client_stats.get("down", 0) if client_stats else 0
        info = await _get_client_info(xui_api, email)

        ap = await _admin_display(users_repo, row.get("approved_by_tg_id"))
        vp = await _admin_display(users_repo, row.get("vpn_issued_by_tg_id"))
        text = _format_stat_block(
            None, up, down, info, show_name=False, role_label=role, trial_approver=ap, vpn_issuer=vp
        )
        sent = await msg.answer(
            text,
            parse_mode="Markdown",
            reply_markup=await user_reply_menu(tg_id, settings, users_repo, xui_api, row=row),
        )
        remember_personal_nav_messages(tg_id, [sent])
    except Exception as e:
        LOGGER.exception("stats.personal.error")
        row_e = await users_repo.get_user(tg_id)
        sent = await msg.answer(
            f"❌ Ошибка: {type(e).__name__}",
            reply_markup=await user_reply_menu(tg_id, settings, users_repo, xui_api, row=row_e),
        )
        remember_personal_nav_messages(tg_id, [sent])
    finally:
        await delete_message_pairs(bot, old)
