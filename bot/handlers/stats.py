from __future__ import annotations
import logging
import aiosqlite
import json
from datetime import datetime, timezone
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from bot.keyboards import back_admin
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

@router.message(Command("stats"))
async def stats_command(msg: Message, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    tg = msg.from_user
    if not tg: return
    is_adm = tg.id in settings.admin_ids or await users_repo.is_admin(tg.id)
    if is_adm:
        await _render_admin_stats(msg, settings, users_repo, xui_api)
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
    await _render_admin_stats(msg, settings, users_repo, xui_api)

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

async def _render_admin_stats(msg: Message, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    try:
        async with aiosqlite.connect(settings.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT tg_id, username, xui_email, is_admin, approved_by_tg_id, vpn_issued_by_tg_id
                FROM users WHERE xui_email IS NOT NULL AND xui_email != '' ORDER BY tg_id
            """) as cur:
                users = await cur.fetchall()

        if not users:
            return await msg.answer("📭 Пусто.", reply_markup=back_admin())

        inbounds = await xui_api.get_inbounds()
        blocks = []
        for u in users:
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

        text = "📊 Привязанные:\n\n" + "\n\n──────────────\n\n".join(blocks[:5])
        await msg.answer(text, parse_mode="Markdown", reply_markup=back_admin())
    except Exception as e:
        LOGGER.exception("stats.admin.error")
        await msg.answer(f"❌ {type(e).__name__}", reply_markup=back_admin())
