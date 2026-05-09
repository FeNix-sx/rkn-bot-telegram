from __future__ import annotations
import logging
import aiosqlite
import json
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
from bot.keyboards import reply_menu, admin_menu, back_admin
from core.xui_api import XUIAPI
from core.config import Settings
from db.repositories.users_repo import UsersRepository
from db.database import utc_now_iso
from bot.handlers.user import PENDING_TRIALS, _issue_trial_now

LOGGER = logging.getLogger(__name__)
router = Router()

async def _is_admin(tg_id: int, settings: Settings, users_repo: UsersRepository) -> bool:
    return tg_id in settings.admin_ids or await users_repo.is_admin(tg_id)

@router.message(F.text == "⚙️ Управление")
async def admin_main(msg: Message, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(msg.from_user.id, settings, users_repo):
        return await msg.answer("🚫", reply_markup=reply_menu())
    await msg.answer("⚙️ Управление:", reply_markup=admin_menu())

@router.callback_query(F.data == "admin:main")
async def admin_main_cb(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    await cb.answer()
    await cb.message.answer("⚙️ Управление:", reply_markup=admin_menu())

@router.callback_query(F.data == "admin:bound")
async def admin_bound(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    await cb.answer()
    try:
        async with aiosqlite.connect(settings.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT tg_id, username, xui_email FROM users WHERE xui_email IS NOT NULL AND xui_email != '' ORDER BY tg_id") as cur:
                users = await cur.fetchall()
        if not users: return await cb.message.answer("📭 Пусто.", reply_markup=back_admin())
        inbounds = await xui_api.get_inbounds()
        blocks = []
        for u in users:
            email = u["xui_email"]
            st = next((c for ib in inbounds for c in (ib.get("clientStats") or []) if c.get("email") == email), None)
            up = st.get("up", 0) if st else 0
            down = st.get("down", 0) if st else 0
            total = up + down
            name = u["username"] or f"ID:{u['tg_id']}"
            blocks.append(f"👤 `{name}`\n📊 трафик: ↑{up/1024**3:.2f} ГБ ↓{down/1024**3:.2f} ГБ | {total/1024**3:.2f} ГБ общий")
        await cb.message.answer("📊 Привязанные:\n\n" + "\n\n──────────────\n\n".join(blocks[:5]), reply_markup=back_admin())
    except Exception as e:
        LOGGER.exception("admin.bound.error")
        await cb.message.answer(f"❌ {type(e).__name__}", reply_markup=back_admin())

@router.callback_query(F.data.startswith("admin:unbound"))
async def admin_unbound(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    if not await _is_admin(cb.from_user.id, settings, users_repo): return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":")
    page = int(parts[3]) if len(parts) > 3 and parts[2] == "page" else 0
    await cb.answer()
    try:
        async with aiosqlite.connect(users_repo.db_path) as db:
            async with db.execute("SELECT xui_email FROM users WHERE xui_email IS NOT NULL AND xui_email != ''") as cur:
                bound_emails = {row[0] async for row in cur}
        inbounds = await xui_api.get_inbounds()
        unbound, seen = [], set()
        for ib in inbounds:
            for c in ib.get("clientStats") or []:
                email = c.get("email")
                if email and email not in bound_emails and email not in seen:
                    unbound.append({"email": email, "up": c.get("up", 0)}); seen.add(email)
            s_raw = ib.get("settings")
            if isinstance(s_raw, str):
                for c in json.loads(s_raw).get("clients", []):
                    email = c.get("email")
                    if email and email not in bound_emails and email not in seen:
                        unbound.append({"email": email, "up": 0}); seen.add(email)
        if not unbound: return await cb.message.answer("✅ Все клиенты из панели привязаны.", reply_markup=back_admin())
        total = len(unbound)
        page_size = 7
        total_pages = (total + page_size - 1) // page_size
        page = max(0, min(page, total_pages - 1))
        current = unbound[page * page_size : (page + 1) * page_size]
        kb = [[InlineKeyboardButton(text=f"📥 {c['email']} | ↑{c['up']//1024**2}МБ", callback_data=f"bind:client:{c['email']}")] for c in current]
        nav = []
        if page > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:unbound:page:{page-1}"))
        if page < total_pages - 1: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:unbound:page:{page+1}"))
        if nav: kb.append(nav)
        kb.append([InlineKeyboardButton(text="🔙 К управлению", callback_data="admin:main")])
        await cb.message.answer(f"🔗 Непривязанные (Стр. {page+1}/{total_pages}):", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    except Exception as e:
        LOGGER.exception("admin.unbound.error")
        await cb.message.answer(f"❌ {type(e).__name__}", reply_markup=back_admin())

@router.callback_query(F.data.startswith("bind:client:"))
async def bind_select_client(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(cb.from_user.id, settings, users_repo): return await cb.answer("🚫", show_alert=True)
    email = cb.data.split(":", 2)[2]
    await cb.answer()
    async with aiosqlite.connect(users_repo.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT tg_id, username, first_name FROM users WHERE xui_email IS NULL ORDER BY tg_id") as cur:
            tg_users = await cur.fetchall()
    kb = [[InlineKeyboardButton(text=f"👤 {u['username'] or u['first_name'] or 'ID:' + str(u['tg_id'])} ({u['tg_id']})", callback_data=f"bind:do_tg:{u['tg_id']}:{email}")] for u in tg_users[:5]]
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin:unbound:page:0")])
    await cb.message.answer(f"🔗 Привязка `{email}` → TG:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("bind:do_tg:"))
async def bind_do_tg(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    if not await _is_admin(cb.from_user.id, settings, users_repo): return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":")
    tg_id, email = int(parts[2]), parts[3]
    await cb.answer()
    try:
        inbounds = await xui_api.get_inbounds()
        uuid = None
        for ib in inbounds:
            s = ib.get("settings")
            if isinstance(s, str):
                for c in json.loads(s).get("clients", []):
                    if c.get("email") == email: uuid = c.get("id"); break
            if uuid: break
        if not uuid: return await cb.message.answer("❌ UUID не найден.", reply_markup=back_admin())
        now = utc_now_iso()
        async with aiosqlite.connect(users_repo.db_path) as db:
            cur = await db.execute("UPDATE users SET xui_email=?, xui_uuid=?, username=COALESCE(NULLIF(?, ''), username), updated_at=? WHERE tg_id=?", (email, uuid, "", now, tg_id))
            if cur.rowcount == 0:
                await db.execute("INSERT INTO users (tg_id, username, xui_email, xui_uuid, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (tg_id, "", email, uuid, 'manual', now, now))
            await db.commit()
        await cb.message.answer(f"✅ Привязано: `{tg_id}` ↔ `{email}`", reply_markup=back_admin())
    except Exception as e:
        LOGGER.exception("bind.do_tg.error")
        await cb.message.answer(f"❌ {type(e).__name__}", reply_markup=back_admin())

@router.callback_query(F.data == "admin:unknown")
async def admin_unknown_list(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(cb.from_user.id, settings, users_repo): return await cb.answer("🚫", show_alert=True)
    await cb.answer()
    try:
        async with aiosqlite.connect(users_repo.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT tg_id, username, first_name FROM users WHERE xui_email IS NULL OR xui_email = '' ORDER BY created_at DESC") as cur:
                unknown = await cur.fetchall()
        if not unknown: return await cb.message.answer("✅ Все пользователи из бота привязаны к панели.", reply_markup=back_admin())
        kb = [[InlineKeyboardButton(text=f"👤 {u['username'] or u['first_name'] or 'ID:' + str(u['tg_id'])} ({u['tg_id']})", callback_data=f"bind:unknown:{u['tg_id']}")] for u in unknown[:10]]
        kb.append([InlineKeyboardButton(text="🔙 К управлению", callback_data="admin:main")])
        await cb.message.answer("❓ Не в базе (нажали /start, но нет в 3X-UI):\nВыберите для привязки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    except Exception as e:
        LOGGER.exception("admin.unknown.error")
        await cb.message.answer(f"❌ {type(e).__name__}", reply_markup=back_admin())

@router.callback_query(F.data.startswith("bind:unknown:"))
async def admin_unknown_select(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    if not await _is_admin(cb.from_user.id, settings, users_repo): return await cb.answer("🚫", show_alert=True)
    tg_id = int(cb.data.split(":")[2])
    await cb.answer()
    try:
        async with aiosqlite.connect(users_repo.db_path) as db:
            async with db.execute("SELECT xui_email FROM users WHERE xui_email IS NOT NULL AND xui_email != ''") as cur:
                bound_emails = {row[0] async for row in cur}
        inbounds = await xui_api.get_inbounds()
        unbound_clients, seen = [], set()
        for ib in inbounds:
            for c in ib.get("clientStats") or []:
                email = c.get("email")
                if email and email not in bound_emails and email not in seen:
                    unbound_clients.append({"email": email}); seen.add(email)
            s_raw = ib.get("settings")
            if isinstance(s_raw, str):
                for c in json.loads(s_raw).get("clients", []):
                    email = c.get("email")
                    if email and email not in bound_emails and email not in seen:
                        unbound_clients.append({"email": email}); seen.add(email)
        if not unbound_clients: return await cb.message.answer("✅ В панели нет свободных клиентов.", reply_markup=back_admin())
        kb = [[InlineKeyboardButton(text=f"📥 {c['email']}", callback_data=f"bind:do_unknown:{tg_id}:{c['email']}")] for c in unbound_clients[:10]]
        kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin:unknown")])
        await cb.message.answer(f"🔗 Привязать пользователя к клиенту 3X-UI:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    except Exception as e:
        LOGGER.exception("admin.unknown_select.error")
        await cb.message.answer(f"❌ {type(e).__name__}", reply_markup=back_admin())

@router.callback_query(F.data.startswith("bind:do_unknown:"))
async def bind_unknown_do(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    if not await _is_admin(cb.from_user.id, settings, users_repo): return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":")
    tg_id, target_email = int(parts[2]), parts[3]
    await cb.answer()
    try:
        inbounds = await xui_api.get_inbounds()
        uuid = None
        for ib in inbounds:
            s = ib.get("settings")
            if isinstance(s, str):
                for c in json.loads(s).get("clients", []):
                    if c.get("email") == target_email: uuid = c.get("id"); break
            if uuid: break
        if not uuid: return await cb.message.answer("❌ UUID не найден.", reply_markup=back_admin())
        now = utc_now_iso()
        async with aiosqlite.connect(users_repo.db_path) as db:
            cur = await db.execute("UPDATE users SET xui_email=?, xui_uuid=?, status='active', updated_at=? WHERE tg_id=?", (target_email, uuid, now, tg_id))
            if cur.rowcount == 0: return await cb.message.answer(f"❌ Юзер {tg_id} не найден.", reply_markup=back_admin())
            await db.commit()
        await cb.message.answer(f"✅ Привязано: `{tg_id}` ↔ `{target_email}`", reply_markup=back_admin())
    except Exception as e:
        LOGGER.exception("bind.unknown.error")
        await cb.message.answer(f"❌ {type(e).__name__}", reply_markup=back_admin())

# === ОДОБРЕНИЕ / ОТКЛОНЕНИЕ ТРИАЛА ===
@router.callback_query(F.data.startswith("trial:approve:"))
async def trial_approve(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI, bot: Bot):
    if not await _is_admin(cb.from_user.id, settings, users_repo): return await cb.answer("🚫", show_alert=True)
    tg_id = int(cb.data.split(":")[2])
    req = PENDING_TRIALS.pop(tg_id, None)
    if not req: return await cb.answer("⚠️ Запрос уже обработан", show_alert=True)
    await cb.answer("✅ Одобрено")
    try:
        url = await _issue_trial_now(tg_id, settings, users_repo, xui_api)
        await bot.send_message(tg_id, f"✅ Триал одобрен и активирован!\n🔗 {url}")
    except Exception as e:
        await bot.send_message(tg_id, f"❌ Ошибка активации: {e}")

@router.callback_query(F.data.startswith("trial:deny:"))
async def trial_deny(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, bot: Bot):
    if not await _is_admin(cb.from_user.id, settings, users_repo): return await cb.answer("🚫", show_alert=True)
    tg_id = int(cb.data.split(":")[2])
    if not PENDING_TRIALS.pop(tg_id, None): return await cb.answer("⚠️ Запрос уже обработан", show_alert=True)
    await cb.answer("❌ Отклонено")
    await bot.send_message(tg_id, "❌ В выдаче триала отказано. Обратитесь в поддержку.")

@router.message(Command("add_admin"))
async def add_admin(msg: Message, settings: Settings, users_repo: UsersRepository):
    if msg.from_user.id not in settings.admin_ids: return await msg.answer("🚫 Только рут", reply_markup=reply_menu(True))
    args = msg.text.split()
    if len(args) != 2 or not args[1].isdigit(): return await msg.answer("Формат: /add_admin <tg_id>", reply_markup=reply_menu(True))
    await users_repo.set_admin(int(args[1]), True)
    await msg.answer(f"✅ {args[1]} — админ", reply_markup=reply_menu(True))

@router.message(Command("remove_admin"))
async def rem_admin(msg: Message, settings: Settings, users_repo: UsersRepository):
    if msg.from_user.id not in settings.admin_ids: return await msg.answer("🚫 Только рут", reply_markup=reply_menu(True))
    args = msg.text.split()
    if len(args) != 2 or not args[1].isdigit(): return await msg.answer("Формат: /remove_admin <tg_id>", reply_markup=reply_menu(True))
    await users_repo.set_admin(int(args[1]), False)
    await msg.answer(f"❌ {args[1]} — не админ", reply_markup=reply_menu(True))

@router.message(Command("admins"))
async def list_admins(msg: Message, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(msg.from_user.id, settings, users_repo): return await msg.answer("🚫")
    dyn = await users_repo.get_all_dynamic_admins()
    await msg.answer(f"👑 Рут: {list(settings.admin_ids)}\n🔹 Динамические: {dyn or 'нет'}", reply_markup=reply_menu(True))