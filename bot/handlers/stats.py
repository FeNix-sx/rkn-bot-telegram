from __future__ import annotations
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command  # ← добавлено
from bot.keyboards import reply_menu, stats_menu, back_stats
from core.xui_api import XUIAPI
from db.repositories.users_repo import UsersRepository
import aiosqlite, logging

LOGGER = logging.getLogger(__name__)
router = Router()

async def _adm(tg_id: int, data: dict) -> bool:
    s, r = data["settings"], data["users_repo"]
    return tg_id in s.admin_ids or await r.is_admin(tg_id)

def _fmt(dt):
    if not dt: return "n/a"
    try: return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except: return "n/a"

@router.message(Command("stats"), F.text == "📈 Статистика")
async def stats_menu_h(msg: Message, data: dict):
    tg = msg.from_user
    if not tg: return
    await msg.answer("📊 Статистика:", reply_markup=stats_menu(await _adm(tg.id, data)))

@router.callback_query(F.data == "stats:my")
async def my_stats(cb: CallbackQuery, data: dict):
    tg = cb.from_user
    if not tg: return await cb.answer("Ошибка", show_alert=True)
    row = await data["users_repo"].get_user(tg.id)
    if not row or not row.get("xui_email"):
        return await cb.message.answer("🔍 Не идентифицирован. Обратитесь к админу.", reply_markup=reply_menu(await _adm(tg.id, data)))
    try:
        inbounds = await data["xui_api"].get_inbounds()
        st = next((c for ib in inbounds for c in (ib.get("clientStats") or []) if c.get("email") == row["xui_email"]), None)
        if not st: return await cb.message.answer("❌ Клиент не найден в панели.", reply_markup=reply_menu(await _adm(tg.id, data)))
        up, down = st.get("up",0)/1024**3, st.get("down",0)/1024**3
        lim = row.get("traffic_limit_bytes") or 0
        lim_s = f"{lim/1024**3:.1f}" if lim else "∞"
        await cb.message.answer(f"📊 Твоя статистика:\n↑ {up:.2f} ГБ\n↓ {down:.2f} ГБ\n📦 {up+down:.2f} ГБ / {lim_s} ГБ", reply_markup=reply_menu(await _adm(tg.id, data)))
    except Exception as e:
        await cb.message.answer(f"Ошибка: {e}", reply_markup=reply_menu(await _adm(tg.id, data)))
    await cb.answer()

@router.callback_query(F.data == "stats:all")
async def all_stats(cb: CallbackQuery, data: dict):
    if not await _adm(cb.from_user.id, data): return await cb.answer("🚫", show_alert=True)
    await cb.answer()
    await _render_bound(cb.message, data)

async def _render_bound(msg: Message, data: dict):
    try:
        r, xui = data["users_repo"], data["xui_api"]
        async with aiosqlite.connect(r.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT tg_id, username, xui_email, status, trial_end, paid_until FROM users WHERE xui_email IS NOT NULL ORDER BY tg_id") as cur:
                users = await cur.fetchall()
        if not users: return await msg.answer("📭 Пусто.", reply_markup=back_stats(True))
        inbounds = await xui.get_inbounds()
        rows = []
        for u in users:
            st = next((c for ib in inbounds for c in (ib.get("clientStats") or []) if c.get("email") == u["xui_email"]), None)
            up = (st.get("up",0) if st else 0)/1024**3
            down = (st.get("down",0) if st else 0)/1024**3
            end = u["paid_until"] or u["trial_end"] or "∞"
            name = u["username"] or f"ID:{u['tg_id']}"
            rows.append(f"👤 `{name}` | `{u['xui_email']}`\n↑{up:.2f} ГБ ↓{down:.2f} ГБ | до {_fmt(end)} | `{u['status']}`")
        await msg.answer("📊 Привязанные:\n\n" + "\n\n".join(rows[:10]), reply_markup=back_stats(True))
    except Exception as e:
        await msg.answer(f"❌ {e}", reply_markup=back_stats(True))

@router.callback_query(F.data == "stats:back")
async def stats_back(cb: CallbackQuery, data: dict):
    await cb.answer()
    await cb.message.answer("📊 Статистика:", reply_markup=stats_menu(await _adm(cb.from_user.id, data)))