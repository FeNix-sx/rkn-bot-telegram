from __future__ import annotations
import calendar
import json
import logging
import os
import re
import aiosqlite
from datetime import datetime, time, timezone
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from bot.keyboards import reply_menu, admin_menu, back_admin
from core.xui_api import XUIAPI
from core.config import Settings
from db.repositories.users_repo import UsersRepository
from db.database import utc_now_iso
from bot.handlers.user import PENDING_TRIALS, _issue_trial_now

LOGGER = logging.getLogger(__name__)
router = Router()


class AdminRenew(StatesGroup):
    waiting_manual_date = State()


def _sub_end_datetime(row: dict) -> datetime | None:
    raw = row.get("paid_until") or row.get("trial_end")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _add_one_calendar_month(dt: datetime) -> datetime:
    y, m, d = dt.year, dt.month + 1, dt.day
    if m > 12:
        y, m = y + 1, m - 12
    last = calendar.monthrange(y, m)[1]
    d = min(d, last)
    return dt.replace(year=y, month=m, day=d)


def _eod_plus_one_month_from_today_utc() -> datetime:
    today = datetime.now(timezone.utc).date()
    y, m, d = today.year, today.month + 1, today.day
    if m > 12:
        y, m = y + 1, m - 12
    last = calendar.monthrange(y, m)[1]
    d = min(d, last)
    return datetime(y, m, d, 23, 59, 59, tzinfo=timezone.utc)


def _parse_ddmmyyyy(s: str) -> datetime | None:
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if mo < 1 or mo > 12 or d < 1 or d > calendar.monthrange(y, mo)[1]:
        return None
    return datetime(y, mo, d, 23, 59, 59, tzinfo=timezone.utc)


async def _apply_subscription_end(
    tg_id: int,
    new_end: datetime,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
) -> str:
    new_end = new_end.astimezone(timezone.utc)
    end_iso = new_end.isoformat(timespec="seconds")
    row = await users_repo.get_user(tg_id)
    if not row:
        raise ValueError("Пользователь не найден в БД.")
    email = (row.get("xui_email") or "").strip()
    if not email:
        raise ValueError("Нет привязанного xui_email.")
    await users_repo.set_subscription_until(tg_id, end_iso)
    xui_note = ""
    if not os.getenv("MOCK_XUI"):
        try:
            iid = await xui_api.find_inbound_id_for_email(email)
            ms = int(new_end.timestamp() * 1000)
            await xui_api.set_client_expiry(iid, email, ms)
        except Exception as e:
            LOGGER.warning("renew.xui.warn: %s", e)
            xui_note = f"\n⚠️ БД обновлена; 3X-UI: {e}"
    return f"✅ Срок подписки до: {end_iso} UTC{xui_note}"


async def _is_admin(tg_id: int, settings: Settings, users_repo: UsersRepository) -> bool:
    return tg_id in settings.admin_ids or await users_repo.is_admin(tg_id)

def _btn_user_label(username: str | None, tg_id: int, max_len: int = 22) -> str:
    raw = (username or "").strip() or f"ID:{tg_id}"
    return raw if len(raw) <= max_len else raw[: max_len - 1] + "…"

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


@router.callback_query(F.data == "admin:panel_back")
async def admin_panel_back(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    await cb.answer()
    await cb.message.answer("Главное меню:", reply_markup=reply_menu(True))

@router.callback_query((F.data == "admin:bound") | F.data.startswith("admin:bound:page:"))
async def admin_bound(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":")
    page = int(parts[3]) if len(parts) > 3 and parts[2] == "page" else 0
    await cb.answer()
    try:
        async with aiosqlite.connect(settings.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT tg_id, username, xui_email FROM users WHERE xui_email IS NOT NULL AND xui_email != '' ORDER BY tg_id"
            ) as cur:
                users = await cur.fetchall()
        if not users:
            return await cb.message.answer("📭 Пусто.", reply_markup=back_admin())
        inbounds = await xui_api.get_inbounds()
        rows: list[tuple[int, str | None, str, int]] = []
        for u in users:
            email = u["xui_email"]
            st = next((c for ib in inbounds for c in (ib.get("clientStats") or []) if c.get("email") == email), None)
            up = int(st.get("up", 0) if st else 0)
            rows.append((u["tg_id"], u["username"], email, up))
        total = len(rows)
        page_size = 7
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        current = rows[page * page_size : (page + 1) * page_size]
        kb = [
            [
                InlineKeyboardButton(
                    text=f"👤 {_btn_user_label(un, tid)} | ↑{up // 1024**2}МБ",
                    callback_data=f"bound:user:{tid}:{page}",
                )
            ]
            for tid, un, _email, up in current
        ]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:bound:page:{page - 1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:bound:page:{page + 1}"))
        if nav:
            kb.append(nav)
        kb.append([InlineKeyboardButton(text="🔙 К управлению", callback_data="admin:main")])
        await cb.message.answer(
            f"👥 Привязанные (стр. {page + 1}/{total_pages}):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        )
    except Exception as e:
        LOGGER.exception("admin.bound.error")
        await cb.message.answer(f"❌ {type(e).__name__}", reply_markup=back_admin())

@router.callback_query(F.data.startswith("bound:user:"))
async def bound_user_menu(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    seg = cb.data.split(":")
    if len(seg) < 4:
        return await cb.answer("Ошибка данных", show_alert=True)
    tg_id, list_page = int(seg[2]), int(seg[3])
    await cb.answer()
    row = await users_repo.get_user(tg_id)
    label = _btn_user_label(row.get("username") if row else None, tg_id, max_len=40) if row else str(tg_id)
    email = (row.get("xui_email") or "") if row else ""
    head = f"👤 {label} ({tg_id})"
    if email:
        head += f"\n📧 {email}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Продлить подписку", callback_data=f"bound:renew:start:{tg_id}:{list_page}")],
            [InlineKeyboardButton(text="📶 Количество подключений (IP limit)", callback_data=f"bound:iplimit:menu:{tg_id}:{list_page}")],
            [
                InlineKeyboardButton(text="👑 Сделать админом", callback_data=f"bound:grant_admin:ask:{tg_id}:{list_page}"),
                InlineKeyboardButton(text="🚫 Убрать админа", callback_data=f"bound:revoke_admin:ask:{tg_id}:{list_page}"),
            ],
            [
                InlineKeyboardButton(text="⏸ Отключить", callback_data=f"bound:stub:disable:{tg_id}"),
                InlineKeyboardButton(text="▶️ Включить", callback_data=f"bound:stub:enable:{tg_id}"),
            ],
            [InlineKeyboardButton(text="🗑 Удалить полностью", callback_data=f"bound:stub:delete:{tg_id}")],
            [InlineKeyboardButton(text="🔙 К списку привязанных", callback_data=f"admin:bound:page:{list_page}")],
        ]
    )
    await cb.message.answer(head + "\n\nНастройки (заглушки):", reply_markup=kb)


@router.callback_query(F.data.startswith("bound:iplimit:"))
async def bound_iplimit_flow(
    cb: CallbackQuery,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":")
    if len(parts) < 5:
        return await cb.answer("Ошибка данных", show_alert=True)
    kind = parts[2]
    if kind == "menu":
        tg_id, list_page = int(parts[3]), int(parts[4])
        await cb.answer()
        row = await users_repo.get_user(tg_id)
        if not row or not (row.get("xui_email") or "").strip():
            return await cb.message.answer("❌ Нет привязки к панели.", reply_markup=back_admin())
        nums = [
            InlineKeyboardButton(text=str(n), callback_data=f"bound:iplimit:set:{tg_id}:{list_page}:{n}")
            for n in (1, 2, 3, 4, 5)
        ]
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                nums,
                [InlineKeyboardButton(text="🔙 К карточке пользователя", callback_data=f"bound:user:{tg_id}:{list_page}")],
            ]
        )
        return await cb.message.answer("Лимит одновременных подключений (IP limit), значение 1–5:", reply_markup=kb)
    if kind == "set":
        if len(parts) < 6:
            return await cb.answer("Ошибка данных", show_alert=True)
        tg_id, list_page, lim = int(parts[3]), int(parts[4]), int(parts[5])
        if lim not in (1, 2, 3, 4, 5):
            return await cb.answer("Только 1–5", show_alert=True)
        await cb.answer()
        row = await users_repo.get_user(tg_id)
        if not row:
            return await cb.message.answer("❌ Пользователь не найден.", reply_markup=back_admin())
        email = (row.get("xui_email") or "").strip()
        if not email:
            return await cb.message.answer("❌ Нет xui_email.", reply_markup=back_admin())
        try:
            if os.getenv("MOCK_XUI"):
                txt = f"✅ MOCK: limitIp={lim} для {email}"
            else:
                iid = await xui_api.find_inbound_id_for_email(email)
                await xui_api.set_client_limit_ip(iid, email, lim)
                txt = f"✅ Лимит IP для {email}: {lim}"
        except Exception as e:
            LOGGER.exception("iplimit.set.error")
            txt = f"❌ {e}"
        return await cb.message.answer(
            txt,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 К карточке пользователя", callback_data=f"bound:user:{tg_id}:{list_page}")]
                ]
            ),
        )
    return await cb.answer("Неизвестное действие", show_alert=True)


@router.callback_query(F.data.startswith("bound:renew:"))
async def bound_renew_flow(
    cb: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":")
    if len(parts) < 4:
        return await cb.answer("Ошибка данных", show_alert=True)
    kind = parts[2]
    if kind == "cancel":
        list_page = int(parts[3]) if len(parts) > 3 else 0
        await state.clear()
        await cb.answer("Отменено")
        return await cb.message.answer(
            "Продление отменено.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 К списку привязанных", callback_data=f"admin:bound:page:{list_page}")]
                ]
            ),
        )
    if len(parts) < 5:
        return await cb.answer("Ошибка данных", show_alert=True)
    tg_id, list_page = int(parts[3]), int(parts[4])
    if kind == "start":
        await state.set_state(AdminRenew.waiting_manual_date)
        await state.update_data(target_tg_id=tg_id, list_page=list_page)
        await cb.answer()
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📆 На 1 месяц", callback_data=f"bound:renew:1m:{tg_id}:{list_page}")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"bound:renew:cancel:{list_page}")],
            ]
        )
        return await cb.message.answer(
            f"Продление подписки для {tg_id}.\n\n"
            "Отправь дату окончания: ДД.ММ.ГГГГ (до 23:59:59 UTC этого дня), "
            "или нажми «На 1 месяц».",
            reply_markup=kb,
        )
    if kind == "1m":
        await state.clear()
        await cb.answer()
        row = await users_repo.get_user(tg_id)
        if not row:
            return await cb.message.answer("❌ Пользователь не найден.", reply_markup=back_admin())
        now = datetime.now(timezone.utc)
        end_dt = _sub_end_datetime(row)
        if end_dt and end_dt > now:
            new_end = _add_one_calendar_month(end_dt)
        else:
            new_end = _eod_plus_one_month_from_today_utc()
        try:
            txt = await _apply_subscription_end(tg_id, new_end, users_repo, xui_api)
        except Exception as e:
            LOGGER.exception("renew.1m.error")
            txt = f"❌ {e}"
        return await cb.message.answer(
            txt,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 К списку привязанных", callback_data=f"admin:bound:page:{list_page}")]
                ]
            ),
        )
    return await cb.answer("Неизвестное действие", show_alert=True)


@router.message(StateFilter(AdminRenew.waiting_manual_date), F.text)
async def bound_renew_manual_date(
    msg: Message,
    state: FSMContext,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
):
    if not msg.from_user or not await _is_admin(msg.from_user.id, settings, users_repo):
        await state.clear()
        return
    data = await state.get_data()
    tg_id = int(data.get("target_tg_id", 0))
    list_page = int(data.get("list_page", 0))
    parsed = _parse_ddmmyyyy(msg.text)
    if not parsed:
        return await msg.answer("❌ Неверный формат. Нужно ДД.ММ.ГГГГ, например 09.06.2026")
    await state.clear()
    try:
        txt = await _apply_subscription_end(tg_id, parsed, users_repo, xui_api)
    except Exception as e:
        LOGGER.exception("renew.manual.error")
        txt = f"❌ {e}"
    await msg.answer(
        txt,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 К списку привязанных", callback_data=f"admin:bound:page:{list_page}")]
            ]
        ),
    )


def _back_to_bound_user_kb(tg_id: int, list_page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 К карточке пользователя", callback_data=f"bound:user:{tg_id}:{list_page}")]
        ]
    )


@router.callback_query(F.data.startswith("bound:grant_admin:"))
async def bound_grant_admin_flow(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":")
    if len(parts) < 5:
        return await cb.answer("Ошибка данных", show_alert=True)
    action, tg_id, list_page = parts[2], int(parts[3]), int(parts[4])

    async def _already_admin(tid: int) -> bool:
        return tid in settings.admin_ids or await users_repo.is_admin(tid)

    if action == "ask":
        await cb.answer()
        row = await users_repo.get_user(tg_id)
        if not row:
            return await cb.message.answer("❌ Пользователь не найден в БД.", reply_markup=back_admin())
        if await _already_admin(tg_id):
            return await cb.message.answer(
                "ℹ️ У этого пользователя уже есть права администратора.",
                reply_markup=_back_to_bound_user_kb(tg_id, list_page),
            )
        label = _btn_user_label(row.get("username"), tg_id, max_len=36)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Да", callback_data=f"bound:grant_admin:yes:{tg_id}:{list_page}"),
                    InlineKeyboardButton(text="❌ Нет", callback_data=f"bound:grant_admin:no:{tg_id}:{list_page}"),
                ],
                [InlineKeyboardButton(text="🔙 К карточке пользователя", callback_data=f"bound:user:{tg_id}:{list_page}")],
            ]
        )
        return await cb.message.answer(
            f"Выдать пользователю «{label}» (id: {tg_id}) права администратора в боте?",
            reply_markup=kb,
        )

    if action == "yes":
        await cb.answer()
        row = await users_repo.get_user(tg_id)
        if not row:
            return await cb.message.answer("❌ Пользователь не найден.", reply_markup=back_admin())
        if await _already_admin(tg_id):
            return await cb.message.answer(
                "ℹ️ Уже администратор, изменения не требуются.",
                reply_markup=_back_to_bound_user_kb(tg_id, list_page),
            )
        await users_repo.set_admin(tg_id, True)
        return await cb.message.answer(
            "✅ Пользователю выданы права администратора.",
            reply_markup=_back_to_bound_user_kb(tg_id, list_page),
        )

    if action == "no":
        await cb.answer("Отменено")
        return await cb.message.answer("Действие отменено.", reply_markup=_back_to_bound_user_kb(tg_id, list_page))

    return await cb.answer("Неизвестное действие", show_alert=True)


@router.callback_query(F.data.startswith("bound:revoke_admin:"))
async def bound_revoke_admin_flow(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":")
    if len(parts) < 5:
        return await cb.answer("Ошибка данных", show_alert=True)
    action, tg_id, list_page = parts[2], int(parts[3]), int(parts[4])

    if action == "ask":
        await cb.answer()
        row = await users_repo.get_user(tg_id)
        if not row:
            return await cb.message.answer("❌ Пользователь не найден в БД.", reply_markup=back_admin())
        if tg_id in settings.admin_ids:
            return await cb.message.answer(
                "⚠️ Этот ID указан в ADMIN_IDS в конфиге бота. Снять такие права можно только убрав id из конфига и перезапустив бота.",
                reply_markup=_back_to_bound_user_kb(tg_id, list_page),
            )
        if not await users_repo.is_admin(tg_id):
            return await cb.message.answer(
                "ℹ️ У пользователя нет прав администратора в базе.",
                reply_markup=_back_to_bound_user_kb(tg_id, list_page),
            )
        label = _btn_user_label(row.get("username"), tg_id, max_len=36)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Да", callback_data=f"bound:revoke_admin:yes:{tg_id}:{list_page}"),
                    InlineKeyboardButton(text="❌ Нет", callback_data=f"bound:revoke_admin:no:{tg_id}:{list_page}"),
                ],
                [InlineKeyboardButton(text="🔙 К карточке пользователя", callback_data=f"bound:user:{tg_id}:{list_page}")],
            ]
        )
        return await cb.message.answer(
            f"Снять у пользователя «{label}» (id: {tg_id}) права администратора в боте (запись в БД)?",
            reply_markup=kb,
        )

    if action == "yes":
        await cb.answer()
        row = await users_repo.get_user(tg_id)
        if not row:
            return await cb.message.answer("❌ Пользователь не найден.", reply_markup=back_admin())
        if tg_id in settings.admin_ids:
            return await cb.message.answer(
                "⚠️ ID в ADMIN_IDS — снятие через бота невозможно, правь конфиг.",
                reply_markup=_back_to_bound_user_kb(tg_id, list_page),
            )
        if not await users_repo.is_admin(tg_id):
            return await cb.message.answer(
                "ℹ️ Уже без прав администратора в БД.",
                reply_markup=_back_to_bound_user_kb(tg_id, list_page),
            )
        await users_repo.set_admin(tg_id, False)
        return await cb.message.answer(
            "✅ Права администратора в базе сняты.",
            reply_markup=_back_to_bound_user_kb(tg_id, list_page),
        )

    if action == "no":
        await cb.answer("Отменено")
        return await cb.message.answer("Действие отменено.", reply_markup=_back_to_bound_user_kb(tg_id, list_page))

    return await cb.answer("Неизвестное действие", show_alert=True)


_STUB_LABELS = {
    "disable": "Отключить клиента в панели",
    "enable": "Включить клиента в панели",
    "delete": "Удалить полностью (БД + 3X-UI)",
}

@router.callback_query(F.data.startswith("bound:stub:"))
async def bound_stub_echo(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    seg = cb.data.split(":")
    if len(seg) < 4:
        await cb.answer()
        return await cb.message.answer(f"🔧 Заглушка (raw): {cb.data}")
    action, tg_s = seg[2], seg[3]
    label = _STUB_LABELS.get(action, action)
    await cb.answer()
    await cb.message.answer(f"🔧 Заглушка: {label}\ntg_id: {tg_s}\nraw: {cb.data}")

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