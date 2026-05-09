from __future__ import annotations
import logging
import aiosqlite
import json
from datetime import datetime, timezone
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from bot.keyboards import reply_menu, stats_menu, back_admin
from core.xui_api import XUIAPI
from core.config import Settings
from db.repositories.users_repo import UsersRepository

LOGGER = logging.getLogger(__name__)
router = Router()

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

def _format_stat_block(name: str | None, up: int, down: int, info: dict, show_name: bool = True) -> str:
    total = up + down
    lines = []
    if show_name and name:
        lines.append(f"👤 `{name}`")
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
@router.message(F.text == "📈 Статистика")
async def stats_menu_h(msg: Message, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    tg = msg.from_user
    if not tg: return
    is_adm = tg.id in settings.admin_ids or await users_repo.is_admin(tg.id)
    if not is_adm:
        await _show_personal_stats(msg, tg.id, settings, users_repo, xui_api)
        return
    await msg.answer("📊 Статистика:", reply_markup=stats_menu(is_adm))

@router.callback_query(F.data == "stats:my")
async def my_stats(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    tg = cb.from_user
    if not tg: return await cb.answer("Ошибка", show_alert=True)
    await cb.answer()
    await _show_personal_stats(cb.message, tg.id, settings, users_repo, xui_api)

async def _show_personal_stats(msg: Message, tg_id: int, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    try:
        row = await users_repo.get_user(tg_id)
        if not row or not row.get("xui_email"):
            is_adm = tg_id in settings.admin_ids or await users_repo.is_admin(tg_id)
            return await msg.answer("🔍 Профиль не привязан к панели.", reply_markup=reply_menu(is_adm))

        email = row["xui_email"]
        inbounds = await xui_api.get_inbounds()
        client_stats = next((c for ib in inbounds for c in (ib.get("clientStats") or []) if c.get("email") == email), None)
        up = client_stats.get("up", 0) if client_stats else 0
        down = client_stats.get("down", 0) if client_stats else 0
        info = await _get_client_info(xui_api, email)

        text = _format_stat_block(None, up, down, info, show_name=False)
        is_adm = tg_id in settings.admin_ids or await users_repo.is_admin(tg_id)
        await msg.answer(text, parse_mode="Markdown", reply_markup=reply_menu(is_adm))
    except Exception as e:
        LOGGER.exception("stats.personal.error")
        is_adm = tg_id in settings.admin_ids or await users_repo.is_admin(tg_id)
        await msg.answer(f"❌ Ошибка: {type(e).__name__}", reply_markup=reply_menu(is_adm))

@router.callback_query(F.data == "stats:all")
async def all_stats(cb: CallbackQuery, settings: Settings, xui_api: XUIAPI):
    tg = cb.from_user
    if tg.id not in settings.admin_ids: return await cb.answer("🚫", show_alert=True)
    await cb.answer()
    await _render_admin_stats(cb.message, settings.db_path, xui_api)

async def _render_admin_stats(msg: Message, db_path: str, xui_api: XUIAPI):
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT tg_id, username, xui_email
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
            blocks.append(_format_stat_block(name, up, down, info, show_name=True))

        text = "📊 Привязанные:\n\n" + "\n\n──────────────\n\n".join(blocks[:5])
        await msg.answer(text, parse_mode="Markdown", reply_markup=back_admin())
    except Exception as e:
        LOGGER.exception("stats.admin.error")
        await msg.answer(f"❌ {type(e).__name__}", reply_markup=back_admin())

@router.callback_query(F.data == "stats:back")
async def stats_back(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository):
    await cb.answer()
    is_adm = cb.from_user.id in settings.admin_ids or await users_repo.is_admin(cb.from_user.id)
    await cb.message.answer("📊 Статистика:", reply_markup=stats_menu(is_adm))