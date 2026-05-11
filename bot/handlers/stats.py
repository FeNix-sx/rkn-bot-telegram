from __future__ import annotations
import logging
import aiosqlite
import json
from datetime import datetime, timezone
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
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
            [InlineKeyboardButton(text="🔙 К статистике", callback_data="stats:admin:root")],
            [InlineKeyboardButton(text="🔙 К управлению", callback_data="admin:main")],
        ]
    )


async def _is_admin_user(tg_id: int, settings: Settings, users_repo: UsersRepository) -> bool:
    return tg_id in settings.admin_ids or await users_repo.is_admin(tg_id)


async def _render_admin_stats_menu(msg: Message) -> None:
    await msg.answer(
        "📊 **Статистика**\n\n"
        "• **Пользователи** — есть в боте и указан клиент 3X-UI (статус, ссылка, одобрение и т.д.).\n"
        "• **Клиенты в панели** — только в 3X-UI, к Telegram в боте не привязаны (трафик и параметры клиента в панели).",
        parse_mode="Markdown",
        reply_markup=_stats_admin_root_kb(),
    )


_STAT_PREVIEW_LIMIT = 5


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
async def stats_command(msg: Message, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    tg = msg.from_user
    if not tg: return
    is_adm = tg.id in settings.admin_ids or await users_repo.is_admin(tg.id)
    if is_adm:
        await _render_admin_stats_menu(msg)
    else:
        await show_personal_stats(msg, tg.id, settings, users_repo, xui_api)


@router.message(F.text == "📈 Статистика")
async def stats_reply_button(msg: Message, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    tg = msg.from_user
    if not tg: return
    is_adm = tg.id in settings.admin_ids or await users_repo.is_admin(tg.id)
    if not is_adm:
        await msg.answer(
            "🚫 Нет доступа.",
            reply_markup=await user_reply_menu(tg.id, settings, users_repo, xui_api),
        )
        return
    await _render_admin_stats_menu(msg)


@router.callback_query(F.data.startswith("stats:admin:"))
async def stats_admin_branch(
    cb: CallbackQuery,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
):
    if not cb.from_user or not await _is_admin_user(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":")
    if len(parts) < 3:
        return await cb.answer("Ошибка данных", show_alert=True)
    mode = parts[2]
    await cb.answer()
    if mode == "root":
        return await cb.message.answer(
            "📊 **Статистика**\n\n"
            "• **Пользователи** — есть в боте и указан клиент 3X-UI.\n"
            "• **Клиенты в панели** — только в 3X-UI, без привязки к Telegram в боте.",
            parse_mode="Markdown",
            reply_markup=_stats_admin_root_kb(),
        )
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
                return await cb.message.answer("📭 Нет пользователей с клиентом в панели.", reply_markup=_stats_admin_sub_kb())
            inbounds = await xui_api.get_inbounds()
            blocks: list[str] = []
            for u in users[:_STAT_PREVIEW_LIMIT]:
                email = u["xui_email"]
                st = next((c for ib in inbounds for c in (ib.get("clientStats") or []) if c.get("email") == email), None)
                up = st.get("up", 0) if st else 0
                down = st.get("down", 0) if st else 0
                info = await _get_client_info(xui_api, email)
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
            total = len(users)
            head = f"📊 **Пользователи** (показано {min(_STAT_PREVIEW_LIMIT, total)} из {total}):\n\n"
            tail = ""
            if total > _STAT_PREVIEW_LIMIT:
                tail = f"\n\n_…и ещё {total - _STAT_PREVIEW_LIMIT}. Полный список — «⚙️ Управление» → «👥 Пользователи»._"
            text = head + "\n\n──────────────\n\n".join(blocks) + tail
            return await cb.message.answer(text, parse_mode="Markdown", reply_markup=_stats_admin_sub_kb())
        except Exception as e:
            LOGGER.exception("stats.admin.users.error")
            return await cb.message.answer(f"❌ {type(e).__name__}", reply_markup=_stats_admin_sub_kb())
    if mode == "panel":
        try:
            emails = await _collect_unbound_panel_emails(settings, users_repo, xui_api)
            if not emails:
                return await cb.message.answer(
                    "📭 Нет клиентов в панели без привязки к пользователю бота.",
                    reply_markup=_stats_admin_sub_kb(),
                )
            inbounds = await xui_api.get_inbounds()
            blocks: list[str] = []
            for email in emails[:_STAT_PREVIEW_LIMIT]:
                st = next((c for ib in inbounds for c in (ib.get("clientStats") or []) if c.get("email") == email), None)
                up = st.get("up", 0) if st else 0
                down = st.get("down", 0) if st else 0
                info = await _get_client_info(xui_api, email)
                blocks.append(_format_stat_block(email, up, down, info, show_name=True))
            total = len(emails)
            head = f"📊 **Клиенты в панели** (показано {min(_STAT_PREVIEW_LIMIT, total)} из {total}):\n\n"
            tail = ""
            if total > _STAT_PREVIEW_LIMIT:
                tail = f"\n\n_…и ещё {total - _STAT_PREVIEW_LIMIT}. Полный список — «⚙️ Управление» → «📥 Нет аккаунта ТГ»._"
            text = head + "\n\n──────────────\n\n".join(blocks) + tail
            return await cb.message.answer(text, parse_mode="Markdown", reply_markup=_stats_admin_sub_kb())
        except Exception as e:
            LOGGER.exception("stats.admin.panel.error")
            return await cb.message.answer(f"❌ {type(e).__name__}", reply_markup=_stats_admin_sub_kb())
    return await cb.answer("Неизвестно", show_alert=True)


async def show_personal_stats(msg: Message, tg_id: int, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    try:
        row = await users_repo.get_user(tg_id)
        role = _role_label(tg_id, settings, row)
        if not row or not row.get("xui_email"):
            return await msg.answer(
                f"🔍 Профиль не привязан к панели.\n🔑 Полномочия: `{role}`",
                parse_mode="Markdown",
                reply_markup=await user_reply_menu(tg_id, settings, users_repo, xui_api, row=row),
            )

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
        await msg.answer(
            text,
            parse_mode="Markdown",
            reply_markup=await user_reply_menu(tg_id, settings, users_repo, xui_api, row=row),
        )
    except Exception as e:
        LOGGER.exception("stats.personal.error")
        row_e = await users_repo.get_user(tg_id)
        await msg.answer(
            f"❌ Ошибка: {type(e).__name__}",
            reply_markup=await user_reply_menu(tg_id, settings, users_repo, xui_api, row=row_e),
        )

