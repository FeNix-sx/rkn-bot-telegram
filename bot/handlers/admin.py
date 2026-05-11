from __future__ import annotations
import calendar
import json
import logging
import os
import re
import aiosqlite
from datetime import datetime, time, timezone, timedelta
from uuid import uuid4
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, User
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from bot.keyboards import reply_menu, admin_menu, back_admin
from bot.trial_visibility import user_reply_menu
from core.xui_api import XUIAPI, XUIAPIError
from core.config import Settings
from db.repositories.users_repo import UsersRepository
from db.database import utc_now_iso
from bot.handlers.user import PENDING_TRIALS, PENDING_NEW_USERS, _issue_trial_now
from bot.handlers.stats import _admin_display, _get_client_info, _gb

LOGGER = logging.getLogger(__name__)
router = Router()


class AdminRenew(StatesGroup):
    waiting_manual_date = State()


class XuiAddClient(StatesGroup):
    waiting_email = State()


XUI_VLESS_FLOW = "xtls-rprx-vision"


def _xui_add_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="xui:add:cancel")]]
    )


def _issuer_comment(user: User | None) -> str:
    if not user:
        return ""
    un = (user.username or "").strip().lstrip("@")
    return f"@{un}" if un else f"id:{user.id}"


def _suggested_email_variant_a(row: dict | None) -> str | None:
    if not row:
        return None
    un = (row.get("username") or "").strip().lstrip("@")
    return f"@{un}" if un else None


def _trial_window_3d() -> tuple[datetime, datetime, int]:
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=3)
    return start, end, int(end.timestamp() * 1000)


async def _xui_create_client_and_url(
    xui_api: XUIAPI,
    *,
    inbound_tag: str,
    inbound_numeric_id: int | None,
    email: str,
    uuid_val: str,
    expiry_ms: int,
    comment: str,
    tg_id_panel: str,
    public_host: str | None,
) -> str:
    if os.getenv("MOCK_XUI"):
        return f"vless://{uuid_val}@127.0.0.1:443?type=tcp&encryption=none&security=none#mock_{email}"
    inbound_id = await xui_api.resolve_target_inbound_id(numeric_id=inbound_numeric_id, tag=inbound_tag)
    await xui_api.add_client(
        inbound_id,
        email,
        uuid_val,
        limit_ip=1,
        total_gb=0,
        expiry_ms=expiry_ms,
        flow=XUI_VLESS_FLOW,
        tg_id=tg_id_panel,
        comment=comment,
    )
    return await xui_api.build_vless_share_uri(
        email=email,
        public_host=public_host,
    )


def _expiry_dt_from_panel_ms(expiry_ms: int) -> datetime | None:
    if not expiry_ms or expiry_ms > 9999999999999:
        return None
    return datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc)


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


async def _apply_subscription_end_by_email(
    email: str,
    new_end: datetime,
    xui_api: XUIAPI,
) -> str:
    new_end = new_end.astimezone(timezone.utc)
    end_iso = new_end.isoformat(timespec="seconds")
    if os.getenv("MOCK_XUI"):
        return f"✅ MOCK: срок в панели до: {end_iso} UTC"
    xui_note = ""
    try:
        iid = await xui_api.find_inbound_id_for_email(email)
        ms = int(new_end.timestamp() * 1000)
        await xui_api.set_client_expiry(iid, email, ms)
    except Exception as e:
        LOGGER.warning("renew.unbound.xui.warn: %s", e)
        xui_note = f"\n⚠️ 3X-UI: {e}"
    return f"✅ Срок в панели до: {end_iso} UTC{xui_note}"


def _unbound_page_email(data: str, prefix_segments: int) -> tuple[int, str] | None:
    """Разбор callback вида prefix…:page:email (email без «:» внутри)."""
    parts = data.split(":", prefix_segments + 1)
    if len(parts) < prefix_segments + 2:
        return None
    try:
        return int(parts[prefix_segments]), parts[prefix_segments + 1]
    except ValueError:
        return None


def _traffic_up_down_for_email(inbounds: list, email: str) -> tuple[int, int]:
    for ib in inbounds:
        for c in ib.get("clientStats") or []:
            if c.get("email") == email:
                return int(c.get("up", 0) or 0), int(c.get("down", 0) or 0)
    return 0, 0


def _back_to_unbound_panel_kb(email: str, list_page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔙 К карточке клиента",
                    callback_data=f"unbound:panel:menu:{list_page}:{email}",
                )
            ]
        ]
    )


async def _is_admin(tg_id: int, settings: Settings, users_repo: UsersRepository) -> bool:
    return tg_id in settings.admin_ids or await users_repo.is_admin(tg_id)

def _btn_user_label(username: str | None, tg_id: int, max_len: int = 22) -> str:
    raw = (username or "").strip() or f"ID:{tg_id}"
    return raw if len(raw) <= max_len else raw[: max_len - 1] + "…"


def _email_enable_map_from_inbounds(inbounds: list) -> dict[str, bool]:
    """email -> enable из settings клиентов (без N+1 к панели)."""
    m: dict[str, bool] = {}
    for ib in inbounds:
        s_raw = ib.get("settings")
        if not isinstance(s_raw, str):
            continue
        try:
            for c in json.loads(s_raw).get("clients", []):
                em = str(c.get("email") or "").strip()
                if em:
                    m[em] = bool(c.get("enable", True))
        except json.JSONDecodeError:
            continue
    return m


def _bound_list_client_label(username: str | None, xui_email: str, tg_id: int, max_len: int = 28) -> str:
    u = (username or "").strip().lstrip("@")
    if u:
        label = f"@{u}"
    else:
        e = (xui_email or "").strip()
        label = e if e else f"id:{tg_id}"
    return label if len(label) <= max_len else label[: max_len - 1] + "…"


def _bound_list_button_text(client_label: str, enabled: bool | None, admin_short: str) -> str:
    if enabled is True:
        on = "✅вкл"
    elif enabled is False:
        on = "❌выкл"
    else:
        on = "❔?"
    adm = (admin_short or "—").strip() or "—"
    s = f"👤{client_label}|{on}|{adm}"
    return s if len(s) <= 64 else s[:63] + "…"


def _actor_label(user: User | None) -> str:
    if not user:
        return "—"
    un = (user.username or "").strip()
    return f"@{un}" if un else str(user.id)


async def _send_vpn_link_to_admin(
    cb: CallbackQuery,
    *,
    email: str,
    client_label: str,
    xui_api: XUIAPI,
    settings: Settings,
) -> None:
    if not cb.message:
        return
    if os.getenv("MOCK_XUI"):
        vless = (
            "vless://00000000-0000-0000-0000-000000000001"
            "@127.0.0.1:443?type=tcp&encryption=none&security=none#mock"
        )
    else:
        vless = await xui_api.build_vless_share_uri(
            email=email,
            public_host=settings.xui_vless_host,
        )
    await cb.message.answer(f"Ссылка для клиента {client_label}")
    await cb.message.answer(vless)


async def _broadcast_to_admins(
    bot: Bot,
    settings: Settings,
    users_repo: UsersRepository,
    text: str,
) -> None:
    recipients: set[int] = set(settings.admin_ids)
    recipients.update(await users_repo.get_all_dynamic_admins())
    for aid in recipients:
        try:
            await bot.send_message(aid, text)
        except Exception:
            LOGGER.exception("admin.broadcast.fail", extra={"aid": aid})


async def _xui_email_bound_in_db(users_repo: UsersRepository, email: str) -> bool:
    target = (email or "").strip()
    if not target:
        return False
    async with aiosqlite.connect(users_repo.db_path) as db:
        async with db.execute(
            "SELECT 1 FROM users WHERE trim(COALESCE(xui_email, '')) = ? LIMIT 1",
            (target,),
        ) as cur:
            return (await cur.fetchone()) is not None


@router.message(F.text == "⚙️ Управление")
async def admin_main(msg: Message, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    if not await _is_admin(msg.from_user.id, settings, users_repo):
        uid = msg.from_user.id if msg.from_user else 0
        return await msg.answer(
            "🚫",
            reply_markup=await user_reply_menu(uid, settings, users_repo, xui_api),
        )
    await msg.answer("⚙️ Управление:", reply_markup=admin_menu())

@router.callback_query(F.data == "admin:main")
async def admin_main_cb(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    await cb.answer()
    await cb.message.answer("⚙️ Управление:", reply_markup=admin_menu())


@router.callback_query(F.data == "admin:panel_back")
async def admin_panel_back(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    await cb.answer()
    uid = cb.from_user.id if cb.from_user else 0
    await cb.message.answer(
        "Главное меню:",
        reply_markup=await user_reply_menu(uid, settings, users_repo, xui_api),
    )

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
                """SELECT tg_id, username, xui_email, vpn_issued_by_tg_id, approved_by_tg_id
                   FROM users WHERE xui_email IS NOT NULL AND trim(xui_email) != ''
                   ORDER BY tg_id"""
            ) as cur:
                users = await cur.fetchall()
        if not users:
            return await cb.message.answer("📭 Пусто.", reply_markup=back_admin())
        inbounds = await xui_api.get_inbounds()
        enable_by_email = _email_enable_map_from_inbounds(inbounds)
        admin_short_cache: dict[int | None, str] = {}

        async def _admin_short(aid: int | None) -> str:
            if aid is None:
                return "—"
            if aid in admin_short_cache:
                return admin_short_cache[aid]
            full = await _admin_display(users_repo, aid) or "—"
            short = full if len(full) <= 18 else full[:16] + "…"
            admin_short_cache[aid] = short
            return short

        rows: list[tuple[int, str | None, str, int | None, int | None]] = []
        for u in users:
            tid = u["tg_id"]
            un = u["username"]
            email = u["xui_email"]
            vpn_i = u["vpn_issued_by_tg_id"]
            appr = u["approved_by_tg_id"]
            rows.append((tid, un, email, vpn_i, appr))
        total = len(rows)
        page_size = 7
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        current = rows[page * page_size : (page + 1) * page_size]
        kb = []
        for tid, un, email, vpn_i, appr in current:
            client_label = _bound_list_client_label(un, email, tid)
            en = enable_by_email.get(email.strip()) if email else None
            adm_id = vpn_i if vpn_i is not None else appr
            adm_s = await _admin_short(adm_id)
            txt = _bound_list_button_text(client_label, en, adm_s)
            kb.append([InlineKeyboardButton(text=txt, callback_data=f"bound:user:{tid}:{page}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:bound:page:{page - 1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:bound:page:{page + 1}"))
        if nav:
            kb.append(nav)
        kb.append([InlineKeyboardButton(text="🔙 К управлению", callback_data="admin:main")])
        await cb.message.answer(
            f"👥 Привязанные (стр. {page + 1}/{total_pages}):\n\n"
            "Пользователи с Telegram, у которых в боте указан клиент 3X-UI (полная связка ТГ ↔ VPN).",
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
    ap_t = await _admin_display(users_repo, row.get("approved_by_tg_id") if row else None)
    vp_t = await _admin_display(users_repo, row.get("vpn_issued_by_tg_id") if row else None)
    head += f"\n✅ Одобрил: {ap_t or '—'}\n🔗 VPN: {vp_t or '—'}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Продлить подписку", callback_data=f"bound:renew:start:{tg_id}:{list_page}")],
            [InlineKeyboardButton(text="📶 Количество подключений (IP limit)", callback_data=f"bound:iplimit:menu:{tg_id}:{list_page}")],
            [
                InlineKeyboardButton(text="👑 Сделать админом", callback_data=f"bound:grant_admin:ask:{tg_id}:{list_page}"),
                InlineKeyboardButton(text="🚫 Убрать админа", callback_data=f"bound:revoke_admin:ask:{tg_id}:{list_page}"),
            ],
            [
                InlineKeyboardButton(text="🔗 Ссылка на VPN", callback_data=f"bound:vpn_link:{tg_id}:{list_page}"),
            ],
            [
                InlineKeyboardButton(text="⏸ Отключить", callback_data=f"bound:xui_toggle:ask:disable:{tg_id}:{list_page}"),
                InlineKeyboardButton(text="▶️ Включить", callback_data=f"bound:xui_toggle:ask:enable:{tg_id}:{list_page}"),
            ],
            [
                InlineKeyboardButton(
                    text="👤 Я ответственный админ",
                    callback_data=f"bound:claim_steward:{tg_id}:{list_page}",
                )
            ],
            [InlineKeyboardButton(text="🗑 Удалить полностью", callback_data=f"bound:stub:delete:{tg_id}")],
            [InlineKeyboardButton(text="🔙 К списку привязанных", callback_data=f"admin:bound:page:{list_page}")],
        ]
    )
    await cb.message.answer(head + "\n\nНастройки (заглушки):", reply_markup=kb)


@router.callback_query(F.data.startswith("bound:vpn_link:"))
async def bound_vpn_link(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":")
    if len(parts) < 4:
        return await cb.answer("Ошибка данных", show_alert=True)
    tg_id = int(parts[2])
    await cb.answer()
    row = await users_repo.get_user(tg_id)
    email = (row.get("xui_email") or "").strip() if row else ""
    un = (row.get("username") or "").strip().lstrip("@") if row else ""
    client_label = f"@{un}" if un else (email or f"ID:{tg_id}")
    if not email:
        return await cb.message.answer("❌ Нет привязки к клиенту панели (xui_email).", reply_markup=back_admin())
    try:
        await _send_vpn_link_to_admin(
            cb,
            email=email,
            client_label=client_label,
            xui_api=xui_api,
            settings=settings,
        )
    except Exception as e:
        await cb.message.answer(f"❌ Не удалось получить ссылку: {e}")


@router.callback_query(F.data.startswith("bound:claim_steward:"))
async def bound_claim_steward(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    if not cb.from_user:
        return await cb.answer("Нет данных", show_alert=True)
    parts = cb.data.split(":")
    if len(parts) < 4:
        return await cb.answer("Ошибка данных", show_alert=True)
    tg_id, list_page = int(parts[2]), int(parts[3])
    me = cb.from_user.id
    row = await users_repo.get_user(tg_id)
    if not row:
        return await cb.answer("Пользователь не найден", show_alert=True)
    ap = row.get("approved_by_tg_id")
    vp = row.get("vpn_issued_by_tg_id")
    if ap is not None and vp is not None:
        return await cb.answer("Оба поля уже заполнены", show_alert=True)
    await users_repo.fill_steward_nulls(tg_id, me)
    await cb.answer("✅ Сохранено: пустые слоты → ты")
    filled = []
    if ap is None:
        filled.append("одобрение")
    if vp is None:
        filled.append("VPN")
    await cb.message.answer(
        f"👤 {tg_id}: записал тебя в: {', '.join(filled)}.\n"
        f"Открой карточку снова из списка привязанных (стр. {list_page + 1}), чтобы увидеть строки.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 К карточке", callback_data=f"bound:user:{tg_id}:{list_page}")],
            ]
        ),
    )


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
        await state.update_data(target_tg_id=tg_id, list_page=list_page, target_email=None)
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
    list_page = int(data.get("list_page", 0))
    parsed = _parse_ddmmyyyy(msg.text)
    if not parsed:
        return await msg.answer("❌ Неверный формат. Нужно ДД.ММ.ГГГГ, например 09.06.2026")
    target_email = (data.get("target_email") or "").strip()
    await state.clear()
    if target_email:
        try:
            txt = await _apply_subscription_end_by_email(target_email, parsed, xui_api)
        except Exception as e:
            LOGGER.exception("renew.manual.unbound.error")
            txt = f"❌ {e}"
        return await msg.answer(
            txt,
            reply_markup=_back_to_unbound_panel_kb(target_email, list_page),
        )
    tg_id = int(data.get("target_tg_id", 0))
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


@router.callback_query(F.data.startswith("bound:xui_toggle:"))
async def bound_xui_toggle_flow(
    cb: CallbackQuery,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":")
    if len(parts) < 6:
        return await cb.answer("Ошибка данных", show_alert=True)
    action, mode, tg_id, list_page = parts[2], parts[3], int(parts[4]), int(parts[5])
    if mode not in ("disable", "enable"):
        return await cb.answer("Ошибка данных", show_alert=True)

    verb_off = mode == "disable"
    q = (
        "Отключить клиента на панели 3X-UI? Только переключатель «вкл/выкл», без удаления и без смены подписки и лимитов."
        if verb_off
        else "Включить клиента на панели 3X-UI (только переключатель «вкл»)?"
    )
    already = "ℹ️ Клиент на панели уже отключён." if verb_off else "ℹ️ Клиент на панели уже включён."
    done = "✅ Клиент отключён на панели." if verb_off else "✅ Клиент включён на панели."

    async def _fetch_enabled(em: str) -> bool | None:
        if os.getenv("MOCK_XUI"):
            return None
        try:
            return await xui_api.get_client_enabled(em)
        except Exception:
            LOGGER.exception("xui_toggle.read_enable")
            return None

    if action == "ask":
        await cb.answer()
        row = await users_repo.get_user(tg_id)
        if not row:
            return await cb.message.answer("❌ Пользователь не найден в БД.", reply_markup=back_admin())
        email = (row.get("xui_email") or "").strip()
        if not email:
            return await cb.message.answer("❌ Нет привязки к панели (xui_email).", reply_markup=_back_to_bound_user_kb(tg_id, list_page))
        cur = await _fetch_enabled(email)
        if cur is not None:
            if verb_off and not cur:
                return await cb.message.answer(already, reply_markup=_back_to_bound_user_kb(tg_id, list_page))
            if not verb_off and cur:
                return await cb.message.answer(already, reply_markup=_back_to_bound_user_kb(tg_id, list_page))
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Да", callback_data=f"bound:xui_toggle:yes:{mode}:{tg_id}:{list_page}"),
                    InlineKeyboardButton(text="❌ Нет", callback_data=f"bound:xui_toggle:no:{mode}:{tg_id}:{list_page}"),
                ],
                [InlineKeyboardButton(text="🔙 К карточке пользователя", callback_data=f"bound:user:{tg_id}:{list_page}")],
            ]
        )
        return await cb.message.answer(q, reply_markup=kb)

    if action == "yes":
        await cb.answer()
        row = await users_repo.get_user(tg_id)
        if not row:
            return await cb.message.answer("❌ Пользователь не найден.", reply_markup=back_admin())
        email = (row.get("xui_email") or "").strip()
        if not email:
            return await cb.message.answer("❌ Нет xui_email.", reply_markup=_back_to_bound_user_kb(tg_id, list_page))
        if os.getenv("MOCK_XUI"):
            return await cb.message.answer(
                f"✅ MOCK: клиент «{email}» {'отключён' if verb_off else 'включён'}.",
                reply_markup=_back_to_bound_user_kb(tg_id, list_page),
            )
        try:
            iid = await xui_api.find_inbound_id_for_email(email)
            if verb_off:
                await xui_api.disable_client(iid, email)
            else:
                await xui_api.enable_client(iid, email)
        except Exception as e:
            LOGGER.exception("xui_toggle.apply")
            return await cb.message.answer(f"❌ {e}", reply_markup=_back_to_bound_user_kb(tg_id, list_page))
        return await cb.message.answer(done, reply_markup=_back_to_bound_user_kb(tg_id, list_page))

    if action == "no":
        await cb.answer("Отменено")
        return await cb.message.answer("Действие отменено.", reply_markup=_back_to_bound_user_kb(tg_id, list_page))

    return await cb.answer("Неизвестное действие", show_alert=True)


_STUB_LABELS = {
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


@router.callback_query(F.data == "xui:add:panel")
async def xui_add_panel_start(cb: CallbackQuery, state: FSMContext, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    await state.set_state(XuiAddClient.waiting_email)
    await state.update_data(kind="panel")
    await cb.answer()
    await cb.message.answer(
        "Вариант Б: только 3X-UI (в боте строки не будет → потом «Нет аккаунта ТГ 📥»).\n\n"
        "Отправь логин для поля Email в панели (префикс «@» не обязателен).",
        reply_markup=_xui_add_cancel_kb(),
    )


@router.callback_query(F.data.startswith("xui:add:db:"))
async def xui_add_db_start(
    cb: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
    bot: Bot,
):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":")
    if len(parts) != 4 or not parts[3].isdigit():
        return await cb.answer("Ошибка данных", show_alert=True)
    tg_id = int(parts[3])
    await cb.answer()
    row = await users_repo.get_user(tg_id)
    if not row:
        return await cb.message.answer("❌ Пользователь не найден в БД.", reply_markup=back_admin())
    suggested = _suggested_email_variant_a(row)
    issuer = cb.from_user
    comment = _issuer_comment(issuer)

    if not suggested:
        await state.set_state(XuiAddClient.waiting_email)
        await state.update_data(kind="db", target_tg_id=tg_id)
        return await cb.message.answer(
            "Нет @username в Telegram. Введи email для панели с ведущим @.",
            reply_markup=_xui_add_cancel_kb(),
        )

    if not os.getenv("MOCK_XUI"):
        try:
            if await xui_api.inbound_has_client_email(suggested):
                await state.set_state(XuiAddClient.waiting_email)
                await state.update_data(kind="db", target_tg_id=tg_id)
                return await cb.message.answer(
                    f"Email `{suggested}` уже занят в панели. Пришли другой (с ведущим @).",
                    reply_markup=_xui_add_cancel_kb(),
                )
        except XUIAPIError as e:
            LOGGER.exception("xui.add.email_check")
            return await cb.message.answer(f"❌ Панель: {e}", reply_markup=back_admin())

    start, end, expiry_ms = _trial_window_3d()
    uuid_val = str(uuid4())
    try:
        url = await _xui_create_client_and_url(
            xui_api,
            inbound_tag=settings.xui_inbound_tag,
            inbound_numeric_id=settings.xui_inbound_id,
            email=suggested,
            uuid_val=uuid_val,
            expiry_ms=expiry_ms,
            comment=comment,
            tg_id_panel=str(tg_id),
            public_host=settings.xui_vless_host,
        )
    except Exception as e:
        LOGGER.exception("xui.add.create")
        return await cb.message.answer(f"❌ {e}", reply_markup=back_admin())

    await users_repo.set_trial(
        tg_id,
        start.isoformat(timespec="seconds"),
        end.isoformat(timespec="seconds"),
        suggested,
        uuid_val,
        url,
        vpn_issued_by_tg_id=issuer.id if issuer else None,
    )
    try:
        row_u = await users_repo.get_user(tg_id)
        await bot.send_message(
            tg_id,
            f"✅ Подключение оформлено админом.\n🔗 {url}",
            reply_markup=await user_reply_menu(tg_id, settings, users_repo, xui_api, row=row_u),
        )
    except Exception:
        LOGGER.exception("xui.add.notify_user")
    await cb.message.answer(
        f"✅ Клиент в 3X-UI и БД: `{suggested}` → tg_id `{tg_id}`\n🔗 {url}",
        reply_markup=back_admin(),
    )


@router.callback_query(F.data == "xui:add:cancel")
async def xui_add_cancel(cb: CallbackQuery, state: FSMContext, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    await state.clear()
    await cb.answer("Отменено")
    await cb.message.answer("Создание клиента отменено.", reply_markup=back_admin())


@router.message(StateFilter(XuiAddClient.waiting_email), F.text)
async def xui_add_waiting_email(
    msg: Message,
    state: FSMContext,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
    bot: Bot,
):
    if not msg.from_user or not await _is_admin(msg.from_user.id, settings, users_repo):
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw.lower() in ("/cancel", "отмена", "cancel"):
        await state.clear()
        return await msg.answer("Отменено.", reply_markup=back_admin())

    data = await state.get_data()
    kind = data.get("kind")

    if kind == "panel":
        email = raw
        if not email:
            return await msg.answer("Пусто. Отправь логин/email.")
        if not os.getenv("MOCK_XUI"):
            try:
                if await xui_api.inbound_has_client_email(email):
                    return await msg.answer("Такой email уже в панели. Пришли другой.")
            except XUIAPIError as e:
                return await msg.answer(f"❌ {e}")
        _, _, expiry_ms = _trial_window_3d()
        uuid_val = str(uuid4())
        comment = _issuer_comment(msg.from_user)
        try:
            url = await _xui_create_client_and_url(
                xui_api,
                inbound_tag=settings.xui_inbound_tag,
                inbound_numeric_id=settings.xui_inbound_id,
                email=email,
                uuid_val=uuid_val,
                expiry_ms=expiry_ms,
                comment=comment,
                tg_id_panel="",
                public_host=settings.xui_vless_host,
            )
        except Exception as e:
            LOGGER.exception("xui.add.panel.create")
            return await msg.answer(f"❌ {e}")
        await state.clear()
        return await msg.answer(
            f"✅ Клиент в панели: `{email}`\n🔗 {url}\n\nДальше: «Нет аккаунта ТГ 📥» → привязка к TG.",
            reply_markup=back_admin(),
        )

    if kind == "db":
        tg_id = int(data.get("target_tg_id") or 0)
        if not tg_id:
            await state.clear()
            return await msg.answer("❌ Сессия сбита. Начни снова.", reply_markup=back_admin())
        email = raw
        if not email.startswith("@"):
            return await msg.answer("Нужен email с ведущим @.")
        if not os.getenv("MOCK_XUI"):
            try:
                if await xui_api.inbound_has_client_email(email):
                    return await msg.answer("Этот email тоже занят. Пришли другой (с @).")
            except XUIAPIError as e:
                return await msg.answer(f"❌ {e}")
        start, end, expiry_ms = _trial_window_3d()
        uuid_val = str(uuid4())
        comment = _issuer_comment(msg.from_user)
        try:
            url = await _xui_create_client_and_url(
                xui_api,
                inbound_tag=settings.xui_inbound_tag,
                inbound_numeric_id=settings.xui_inbound_id,
                email=email,
                uuid_val=uuid_val,
                expiry_ms=expiry_ms,
                comment=comment,
                tg_id_panel=str(tg_id),
                public_host=settings.xui_vless_host,
            )
        except Exception as e:
            LOGGER.exception("xui.add.db.create")
            return await msg.answer(f"❌ {e}")
        await users_repo.set_trial(
            tg_id,
            start.isoformat(timespec="seconds"),
            end.isoformat(timespec="seconds"),
            email,
            uuid_val,
            url,
            vpn_issued_by_tg_id=msg.from_user.id,
        )
        try:
            row_u = await users_repo.get_user(tg_id)
            await bot.send_message(
                tg_id,
                f"✅ Подключение оформлено админом.\n🔗 {url}",
                reply_markup=await user_reply_menu(tg_id, settings, users_repo, xui_api, row=row_u),
            )
        except Exception:
            LOGGER.exception("xui.add.notify_user")
        await state.clear()
        return await msg.answer(
            f"✅ Клиент в 3X-UI и БД: `{email}` → `{tg_id}`\n🔗 {url}",
            reply_markup=back_admin(),
        )

    await state.clear()
    return await msg.answer("❌ Неизвестный режим.", reply_markup=back_admin())

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
        panel_rows: list[dict] = []
        seen_panel: set[str] = set()
        for ib in inbounds:
            for c in ib.get("clientStats") or []:
                email = c.get("email")
                if email and email not in bound_emails and email not in seen_panel:
                    panel_rows.append({"email": email, "up": int(c.get("up", 0) or 0)})
                    seen_panel.add(email)
            s_raw = ib.get("settings")
            if isinstance(s_raw, str):
                for c in json.loads(s_raw).get("clients", []):
                    email = c.get("email")
                    if email and email not in bound_emails and email not in seen_panel:
                        panel_rows.append({"email": email, "up": 0})
                        seen_panel.add(email)
        if not panel_rows:
            return await cb.message.answer(
                "✅ Нет клиентов 3X-UI без привязки к Telegram в боте.\n\n"
                "Пользователей только из бота без VPN смотри в «Нет клиента VPN 🔌».",
                reply_markup=back_admin(),
            )
        total = len(panel_rows)
        page_size = 7
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        current = panel_rows[page * page_size : (page + 1) * page_size]
        kb = [
            [
                InlineKeyboardButton(
                    text=f"📥 {row['email']} | ↑{row['up'] // 1024**2}МБ",
                    callback_data=f"unbound:panel:menu:{page}:{row['email']}",
                )
            ]
            for row in current
        ]
        nav = []
        if page > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:unbound:page:{page-1}"))
        if page < total_pages - 1: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:unbound:page:{page+1}"))
        if nav: kb.append(nav)
        kb.append([InlineKeyboardButton(text="🔙 К управлению", callback_data="admin:main")])
        await cb.message.answer(
            f"Нет аккаунта ТГ 📥 (стр. {page + 1}/{total_pages}):\n\n"
            "Клиенты в 3X-UI, у которых в боте нет привязанного Telegram.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        )
    except Exception as e:
        LOGGER.exception("admin.unbound.error")
        await cb.message.answer(f"❌ {type(e).__name__}", reply_markup=back_admin())


@router.callback_query(F.data.startswith("unbound:panel:menu:"))
async def unbound_panel_client_menu(
    cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI
):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    parsed = _unbound_page_email(cb.data, 3)
    if not parsed:
        return await cb.answer("Ошибка данных", show_alert=True)
    list_page, email = parsed
    await cb.answer()
    try:
        inbounds = await xui_api.get_inbounds()
    except Exception as e:
        LOGGER.exception("unbound.panel.inbounds")
        return await cb.message.answer(f"❌ {e}", reply_markup=back_admin())
    up, down = _traffic_up_down_for_email(inbounds, email)
    info = await _get_client_info(xui_api, email)
    exp_ms = int(info.get("expiry_ms") or 0)
    if exp_ms > 0 and exp_ms < 9999999999999:
        exp_dt = datetime.fromtimestamp(exp_ms / 1000, tz=timezone.utc)
        sub_line = f"📅 подписка до: `{exp_dt.strftime('%d.%m.%Y')}`"
    else:
        sub_line = "📅 подписка до: `бессрочно`"
    head = (
        f"📥 Клиент панели (без аккаунта ТГ в боте)\n"
        f"👤 `{email}`\n"
        f"✅ Одобрил: —\n"
        f"🔗 VPN: —\n"
        f"📊 трафик: ↑{_gb(up)} ↓{_gb(down)} | {_gb(up + down)} общий\n"
        f"🔗 подключений: `{info['limit_ip']}`\n"
        f"📡 статус: {'✅ вкл' if info['enable'] else '❌ откл'}\n"
        f"{sub_line}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Продлить подписку", callback_data=f"unbound:renew:start:{list_page}:{email}")],
            [
                InlineKeyboardButton(
                    text="📶 Количество подключений (IP limit)",
                    callback_data=f"unbound:iplimit:menu:{list_page}:{email}",
                )
            ],
            [
                InlineKeyboardButton(text="👑 Сделать админом", callback_data=f"unbound:stub:grant:{list_page}:{email}"),
                InlineKeyboardButton(text="🚫 Убрать админа", callback_data=f"unbound:stub:revoke:{list_page}:{email}"),
            ],
            [InlineKeyboardButton(text="🔗 Ссылка на VPN", callback_data=f"unbound:vpn_link:{list_page}:{email}")],
            [
                InlineKeyboardButton(
                    text="⏸ Отключить", callback_data=f"unbound:xui_toggle:ask:disable:{list_page}:{email}"
                ),
                InlineKeyboardButton(
                    text="▶️ Включить", callback_data=f"unbound:xui_toggle:ask:enable:{list_page}:{email}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👤 Я ответственный админ",
                    callback_data=f"unbound:stub:steward:{list_page}:{email}",
                )
            ],
            [InlineKeyboardButton(text="🗑 Удалить полностью", callback_data=f"unbound:del:ask:{list_page}:{email}")],
            [InlineKeyboardButton(text="🔗 Привязать к Telegram", callback_data=f"bind:client:{list_page}:{email}")],
            [InlineKeyboardButton(text="🔙 К списку", callback_data=f"admin:unbound:page:{list_page}")],
        ]
    )
    await cb.message.answer(head + "\n\nНастройки (заглушки):", reply_markup=kb)


@router.callback_query(F.data.startswith("unbound:vpn_link:"))
async def unbound_vpn_link(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    parsed = _unbound_page_email(cb.data, 2)
    if not parsed:
        return await cb.answer("Ошибка данных", show_alert=True)
    list_page, email = parsed
    await cb.answer()
    try:
        await _send_vpn_link_to_admin(
            cb,
            email=email,
            client_label=email,
            xui_api=xui_api,
            settings=settings,
        )
    except Exception as e:
        await cb.message.answer(f"❌ Не удалось получить ссылку: {e}")


@router.callback_query(F.data.startswith("unbound:iplimit:"))
async def unbound_iplimit_flow(
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
        parsed = _unbound_page_email(cb.data, 3)
        if not parsed:
            return await cb.answer("Ошибка данных", show_alert=True)
        list_page, email = parsed
        await cb.answer()
        if await _xui_email_bound_in_db(users_repo, email):
            return await cb.message.answer(
                f"❌ `{email}` уже привязан в БД — открой карточку из «Привязанные».",
                reply_markup=back_admin(),
            )
        nums = [
            InlineKeyboardButton(text=str(n), callback_data=f"unbound:iplimit:set:{list_page}:{email}:{n}")
            for n in (1, 2, 3, 4, 5)
        ]
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                nums,
                [
                    InlineKeyboardButton(
                        text="🔙 К карточке клиента",
                        callback_data=f"unbound:panel:menu:{list_page}:{email}",
                    )
                ],
            ]
        )
        return await cb.message.answer("Лимит одновременных подключений (IP limit), значение 1–5:", reply_markup=kb)
    if kind == "set":
        if len(parts) < 6:
            return await cb.answer("Ошибка данных", show_alert=True)
        try:
            list_page, email, lim_s = int(parts[3]), parts[4], parts[5]
        except ValueError:
            return await cb.answer("Ошибка данных", show_alert=True)
        lim = int(lim_s)
        if lim not in (1, 2, 3, 4, 5):
            return await cb.answer("Только 1–5", show_alert=True)
        await cb.answer()
        if await _xui_email_bound_in_db(users_repo, email):
            return await cb.message.answer(
                f"❌ `{email}` уже привязан в БД.",
                reply_markup=back_admin(),
            )
        try:
            if os.getenv("MOCK_XUI"):
                txt = f"✅ MOCK: limitIp={lim} для {email}"
            else:
                iid = await xui_api.find_inbound_id_for_email(email)
                await xui_api.set_client_limit_ip(iid, email, lim)
                txt = f"✅ Лимит IP для {email}: {lim}"
        except Exception as e:
            LOGGER.exception("unbound.iplimit.set.error")
            txt = f"❌ {e}"
        return await cb.message.answer(txt, reply_markup=_back_to_unbound_panel_kb(email, list_page))
    return await cb.answer("Неизвестное действие", show_alert=True)


@router.callback_query(F.data.startswith("unbound:renew:"))
async def unbound_renew_flow(
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
                    [InlineKeyboardButton(text="🔙 К списку", callback_data=f"admin:unbound:page:{list_page}")]
                ]
            ),
        )
    parsed = _unbound_page_email(cb.data, 3)
    if not parsed:
        return await cb.answer("Ошибка данных", show_alert=True)
    list_page, email = parsed
    if await _xui_email_bound_in_db(users_repo, email):
        await cb.answer("Уже в БД", show_alert=True)
        return await cb.message.answer(f"❌ `{email}` привязан — карточка в «Привязанные».", reply_markup=back_admin())
    if kind == "start":
        await state.set_state(AdminRenew.waiting_manual_date)
        await state.update_data(target_email=email, list_page=list_page, target_tg_id=None)
        await cb.answer()
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📆 На 1 месяц", callback_data=f"unbound:renew:1m:{list_page}:{email}")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"unbound:renew:cancel:{list_page}")],
            ]
        )
        return await cb.message.answer(
            f"Продление подписки в панели для `{email}` (строка в БД бота не меняется).\n\n"
            "Отправь дату окончания: ДД.ММ.ГГГГ (до 23:59:59 UTC этого дня), "
            "или нажми «На 1 месяц».",
            reply_markup=kb,
        )
    if kind == "1m":
        await state.clear()
        await cb.answer()
        now = datetime.now(timezone.utc)
        info = await _get_client_info(xui_api, email)
        end_dt = _expiry_dt_from_panel_ms(int(info.get("expiry_ms") or 0))
        if end_dt and end_dt > now:
            new_end = _add_one_calendar_month(end_dt)
        else:
            new_end = _eod_plus_one_month_from_today_utc()
        try:
            txt = await _apply_subscription_end_by_email(email, new_end, xui_api)
        except Exception as e:
            LOGGER.exception("unbound.renew.1m.error")
            txt = f"❌ {e}"
        return await cb.message.answer(txt, reply_markup=_back_to_unbound_panel_kb(email, list_page))
    return await cb.answer("Неизвестное действие", show_alert=True)


@router.callback_query(F.data.startswith("unbound:stub:"))
async def unbound_stub_no_tg(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":", 4)
    if len(parts) < 5:
        return await cb.answer("Ошибка данных", show_alert=True)
    kind, list_page_s, email = parts[2], parts[3], parts[4]
    try:
        list_page = int(list_page_s)
    except ValueError:
        return await cb.answer("Ошибка данных", show_alert=True)
    await cb.answer()
    if kind == "grant":
        msg = "ℹ️ Права администратора в боте выдаются пользователю Telegram. Сначала привяжи этого клиента к TG."
    elif kind == "revoke":
        msg = "ℹ️ Снять админа можно только у пользователя в боте. Сначала привяжи клиента к TG."
    else:
        msg = "ℹ️ «Ответственный админ» пишется в БД по пользователю Telegram. Сначала привяжи клиента к TG."
    await cb.message.answer(msg, reply_markup=_back_to_unbound_panel_kb(email, list_page))


@router.callback_query(F.data.startswith("unbound:xui_toggle:"))
async def unbound_xui_toggle_flow(
    cb: CallbackQuery,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":", 5)
    if len(parts) < 6:
        return await cb.answer("Ошибка данных", show_alert=True)
    action, mode, list_page_s, email = parts[2], parts[3], parts[4], parts[5]
    if mode not in ("disable", "enable"):
        return await cb.answer("Ошибка данных", show_alert=True)
    try:
        list_page = int(list_page_s)
    except ValueError:
        return await cb.answer("Ошибка данных", show_alert=True)

    verb_off = mode == "disable"
    q = (
        "Отключить клиента на панели 3X-UI? Только переключатель «вкл/выкл», без удаления и без смены подписки и лимитов."
        if verb_off
        else "Включить клиента на панели 3X-UI (только переключатель «вкл»)?"
    )
    already = "ℹ️ Клиент на панели уже отключён." if verb_off else "ℹ️ Клиент на панели уже включён."
    done = "✅ Клиент отключён на панели." if verb_off else "✅ Клиент включён на панели."

    async def _fetch_enabled(em: str) -> bool | None:
        if os.getenv("MOCK_XUI"):
            return None
        try:
            return await xui_api.get_client_enabled(em)
        except Exception:
            LOGGER.exception("unbound.xui_toggle.read_enable")
            return None

    if action == "ask":
        await cb.answer()
        if await _xui_email_bound_in_db(users_repo, email):
            return await cb.message.answer(
                f"❌ `{email}` уже в БД — управляй из «Привязанные».",
                reply_markup=back_admin(),
            )
        cur = await _fetch_enabled(email)
        if cur is not None:
            if verb_off and not cur:
                return await cb.message.answer(already, reply_markup=_back_to_unbound_panel_kb(email, list_page))
            if not verb_off and cur:
                return await cb.message.answer(already, reply_markup=_back_to_unbound_panel_kb(email, list_page))
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Да", callback_data=f"unbound:xui_toggle:yes:{mode}:{list_page}:{email}"
                    ),
                    InlineKeyboardButton(
                        text="❌ Нет", callback_data=f"unbound:xui_toggle:no:{mode}:{list_page}:{email}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🔙 К карточке клиента",
                        callback_data=f"unbound:panel:menu:{list_page}:{email}",
                    )
                ],
            ]
        )
        return await cb.message.answer(q, reply_markup=kb)

    if action == "yes":
        await cb.answer()
        if await _xui_email_bound_in_db(users_repo, email):
            return await cb.message.answer("❌ Уже привязан в БД.", reply_markup=back_admin())
        if os.getenv("MOCK_XUI"):
            return await cb.message.answer(
                f"✅ MOCK: клиент «{email}» {'отключён' if verb_off else 'включён'}.",
                reply_markup=_back_to_unbound_panel_kb(email, list_page),
            )
        try:
            iid = await xui_api.find_inbound_id_for_email(email)
            if verb_off:
                await xui_api.disable_client(iid, email)
            else:
                await xui_api.enable_client(iid, email)
        except Exception as e:
            LOGGER.exception("unbound.xui_toggle.apply")
            return await cb.message.answer(f"❌ {e}", reply_markup=_back_to_unbound_panel_kb(email, list_page))
        return await cb.message.answer(done, reply_markup=_back_to_unbound_panel_kb(email, list_page))

    if action == "no":
        await cb.answer("Отменено")
        return await cb.message.answer("Действие отменено.", reply_markup=_back_to_unbound_panel_kb(email, list_page))

    return await cb.answer("Неизвестное действие", show_alert=True)


@router.callback_query(F.data.startswith("unbound:del:ask:"))
async def unbound_delete_ask(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    parsed = _unbound_page_email(cb.data, 3)
    if not parsed:
        return await cb.answer("Ошибка данных", show_alert=True)
    list_page, email = parsed
    await cb.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"unbound:del:yes:{email}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"unbound:panel:menu:{list_page}:{email}"),
            ],
            [InlineKeyboardButton(text="🔙 К списку", callback_data=f"admin:unbound:page:{list_page}")],
        ]
    )
    await cb.message.answer(
        f"⚠️ Удалить клиента `{email}` из 3X-UI безвозвратно?\n"
        "В БД бота он не привязан — строка пользователя не затрагивается.",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("unbound:del:yes:"))
async def unbound_delete_confirm(
    cb: CallbackQuery,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
    bot: Bot,
):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":", 3)
    if len(parts) < 4:
        return await cb.answer("Ошибка данных", show_alert=True)
    email = parts[3]
    if await _xui_email_bound_in_db(users_repo, email):
        await cb.answer("Уже привязан в БД", show_alert=True)
        return await cb.message.answer(
            f"❌ Клиент `{email}` привязан к пользователю бота — удаление отменено.",
            reply_markup=back_admin(),
        )
    await cb.answer("Удаляю…")
    actor = _actor_label(cb.from_user)
    try:
        if os.getenv("MOCK_XUI"):
            pass
        else:
            iid = await xui_api.find_inbound_id_for_email(email)
            await xui_api.delete_client_by_email(iid, email)
    except Exception as e:
        LOGGER.exception("unbound.del.client")
        return await cb.message.answer(f"❌ {e}", reply_markup=back_admin())
    note = (
        f"🗑 Клиент VPN `{email}` удалён из 3X-UI.\n"
        f"Админ: {actor} ({cb.from_user.id if cb.from_user else '—'})"
    )
    await _broadcast_to_admins(bot, settings, users_repo, note)
    await cb.message.answer("✅ Клиент удалён. Уведомление отправлено всем админам.", reply_markup=back_admin())


@router.callback_query(F.data.startswith("bind:client:"))
async def bind_select_client(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository):
    if not await _is_admin(cb.from_user.id, settings, users_repo): return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":", 3)
    if len(parts) < 4:
        return await cb.answer("Ошибка данных", show_alert=True)
    try:
        list_page = int(parts[2])
    except ValueError:
        return await cb.answer("Ошибка данных", show_alert=True)
    email = parts[3]
    await cb.answer()
    async with aiosqlite.connect(users_repo.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT tg_id, username, first_name FROM users
               WHERE xui_email IS NULL OR trim(COALESCE(xui_email, '')) = ''
               ORDER BY COALESCE(created_at, '') DESC, tg_id DESC"""
        ) as cur:
            tg_users = await cur.fetchall()
    kb = [
        [
            InlineKeyboardButton(
                text=f"👤 {u['username'] or u['first_name'] or 'ID:' + str(u['tg_id'])} ({u['tg_id']})",
                callback_data=f"bind:do_tg:{list_page}:{u['tg_id']}:{email}",
            )
        ]
        for u in tg_users[:10]
    ]
    kb.append(
        [
            InlineKeyboardButton(
                text="🔙 Назад",
                callback_data=f"unbound:panel:menu:{list_page}:{email}",
            )
        ]
    )
    await cb.message.answer(f"🔗 Привязка `{email}` → TG:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("bind:do_tg:"))
async def bind_do_tg(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    if not await _is_admin(cb.from_user.id, settings, users_repo): return await cb.answer("🚫", show_alert=True)
    parts = cb.data.split(":", 4)
    if len(parts) < 5:
        return await cb.answer("Ошибка данных", show_alert=True)
    try:
        list_page = int(parts[2])
    except ValueError:
        return await cb.answer("Ошибка данных", show_alert=True)
    tg_id, email = int(parts[3]), parts[4]
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
            issuer = cb.from_user.id if cb.from_user else None
            cur = await db.execute(
                "UPDATE users SET xui_email=?, xui_uuid=?, username=COALESCE(NULLIF(?, ''), username), vpn_issued_by_tg_id=?, updated_at=? WHERE tg_id=?",
                (email, uuid, "", issuer, now, tg_id),
            )
            if cur.rowcount == 0:
                await db.execute(
                    "INSERT INTO users (tg_id, username, xui_email, xui_uuid, status, vpn_issued_by_tg_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (tg_id, "", email, uuid, "manual", issuer, now, now),
                )
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
            async with db.execute(
                """SELECT tg_id, username, first_name FROM users
                   WHERE xui_email IS NULL OR trim(COALESCE(xui_email, '')) = ''
                   ORDER BY COALESCE(created_at, '') DESC, tg_id DESC"""
            ) as cur:
                unknown = await cur.fetchall()
        if not unknown: return await cb.message.answer("✅ У всех пользователей бота есть клиент VPN в привязке.", reply_markup=back_admin())
        kb = [[InlineKeyboardButton(text=f"🔌 {u['username'] or u['first_name'] or 'ID:' + str(u['tg_id'])} ({u['tg_id']})", callback_data=f"bind:unknown:{u['tg_id']}")] for u in unknown[:10]]
        kb.append([InlineKeyboardButton(text="🔙 К управлению", callback_data="admin:main")])
        await cb.message.answer(
            "Нет клиента VPN 🔌:\n\n"
            "Пользователь в боте, но в 3X-UI для него ещё не оформлена связка (или xui_email пустой).\n\n"
            "Выбери пользователя:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        )
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
        if not unbound_clients:
            kb = [
                [InlineKeyboardButton(text="➕ Добавить клиент", callback_data=f"xui:add:db:{tg_id}")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:unknown")],
            ]
            return await cb.message.answer(
                "✅ В панели нет свободных клиентов для привязки.\n\n"
                "Можно создать нового клиента в 3X-UI кнопкой ниже.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            )
        kb = [[InlineKeyboardButton(text=f"📥 {c['email']}", callback_data=f"bind:do_unknown:{tg_id}:{c['email']}")] for c in unbound_clients[:10]]
        kb.append([InlineKeyboardButton(text="➕ Добавить клиент", callback_data=f"xui:add:db:{tg_id}")])
        kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin:unknown")])
        await cb.message.answer(
            f"🔗 Привязать пользователя `{tg_id}` к клиенту 3X-UI или создать нового:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        )
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
            issuer = cb.from_user.id if cb.from_user else None
            cur = await db.execute(
                "UPDATE users SET xui_email=?, xui_uuid=?, status='active', vpn_issued_by_tg_id=?, updated_at=? WHERE tg_id=?",
                (target_email, uuid, issuer, now, tg_id),
            )
            if cur.rowcount == 0: return await cb.message.answer(f"❌ Юзер {tg_id} не найден.", reply_markup=back_admin())
            await db.commit()
        await cb.message.answer(f"✅ Привязано: `{tg_id}` ↔ `{target_email}`", reply_markup=back_admin())
    except Exception as e:
        LOGGER.exception("bind.unknown.error")
        await cb.message.answer(f"❌ {type(e).__name__}", reply_markup=back_admin())

# === РЕГИСТРАЦИЯ НОВОГО ПОЛЬЗОВАТЕЛЯ (/start) ===
@router.callback_query(F.data.startswith("start:approve:"))
async def start_approve(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI, bot: Bot):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    tg_id = int(cb.data.split(":")[2])
    pending = PENDING_NEW_USERS.pop(tg_id, None)
    if not pending:
        return await cb.answer("⚠️ Запрос уже обработан", show_alert=True)
    await cb.answer("✅ Добавлен")
    await users_repo.create_user_if_not_exists(
        tg_id,
        pending.get("username"),
        pending.get("first_name"),
        pending.get("last_name"),
    )
    try:
        row_new = await users_repo.get_user(tg_id)
        await bot.send_message(
            tg_id,
            "Привет! Профиль создан.",
            reply_markup=await user_reply_menu(tg_id, settings, users_repo, xui_api, row=row_new),
        )
    except Exception:
        LOGGER.exception("start.approve.notify_user")
    await cb.message.answer(f"✅ Пользователь добавлен в БД: `{tg_id}`")


@router.callback_query(F.data.startswith("start:deny:"))
async def start_deny(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, bot: Bot):
    if not await _is_admin(cb.from_user.id, settings, users_repo):
        return await cb.answer("🚫", show_alert=True)
    tg_id = int(cb.data.split(":")[2])
    if not PENDING_NEW_USERS.pop(tg_id, None):
        return await cb.answer("⚠️ Запрос уже обработан", show_alert=True)
    await cb.answer("Отклонено")
    try:
        await bot.send_message(tg_id, "❌ В доступе отказано.", reply_markup=reply_menu(False, show_trial=False))
    except Exception:
        LOGGER.exception("start.deny.notify_user")
    await cb.message.answer(f"❌ Доступ отклонён: `{tg_id}`")


# === ОДОБРЕНИЕ / ОТКЛОНЕНИЕ ТРИАЛА ===
@router.callback_query(F.data.startswith("trial:approve:"))
async def trial_approve(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI, bot: Bot):
    if not await _is_admin(cb.from_user.id, settings, users_repo): return await cb.answer("🚫", show_alert=True)
    tg_id = int(cb.data.split(":")[2])
    req = PENDING_TRIALS.pop(tg_id, None)
    if not req: return await cb.answer("⚠️ Запрос уже обработан", show_alert=True)
    await cb.answer("✅ Одобрено")
    try:
        approver = cb.from_user.id if cb.from_user else None
        await users_repo.set_trial_approved_by(tg_id, approver)
        url = await _issue_trial_now(
            tg_id, settings, users_repo, xui_api, vpn_issued_by_tg_id=approver
        )
        row_u = await users_repo.get_user(tg_id)
        await bot.send_message(
            tg_id,
            f"✅ Триал одобрен и активирован!\n🔗 {url}",
            reply_markup=await user_reply_menu(tg_id, settings, users_repo, xui_api, row=row_u),
        )
    except Exception as e:
        row_u = await users_repo.get_user(tg_id)
        await bot.send_message(
            tg_id,
            f"❌ Ошибка активации: {e}",
            reply_markup=await user_reply_menu(tg_id, settings, users_repo, xui_api, row=row_u),
        )

@router.callback_query(F.data.startswith("trial:deny:"))
async def trial_deny(cb: CallbackQuery, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI, bot: Bot):
    if not await _is_admin(cb.from_user.id, settings, users_repo): return await cb.answer("🚫", show_alert=True)
    tg_id = int(cb.data.split(":")[2])
    if not PENDING_TRIALS.pop(tg_id, None): return await cb.answer("⚠️ Запрос уже обработан", show_alert=True)
    await cb.answer("❌ Отклонено")
    row_u = await users_repo.get_user(tg_id)
    await bot.send_message(
        tg_id,
        "❌ В выдаче триала отказано. Обратитесь в поддержку.",
        reply_markup=await user_reply_menu(tg_id, settings, users_repo, xui_api, row=row_u),
    )

@router.message(Command("add_admin"))
async def add_admin(msg: Message, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    uid = msg.from_user.id
    if msg.from_user.id not in settings.admin_ids:
        return await msg.answer("🚫 Только рут", reply_markup=await user_reply_menu(uid, settings, users_repo, xui_api))
    args = msg.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return await msg.answer(
            "Формат: /add_admin <tg_id>",
            reply_markup=await user_reply_menu(uid, settings, users_repo, xui_api),
        )
    await users_repo.set_admin(int(args[1]), True)
    await msg.answer(
        f"✅ {args[1]} — админ",
        reply_markup=await user_reply_menu(uid, settings, users_repo, xui_api),
    )

@router.message(Command("remove_admin"))
async def rem_admin(msg: Message, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    uid = msg.from_user.id
    if msg.from_user.id not in settings.admin_ids:
        return await msg.answer("🚫 Только рут", reply_markup=await user_reply_menu(uid, settings, users_repo, xui_api))
    args = msg.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return await msg.answer(
            "Формат: /remove_admin <tg_id>",
            reply_markup=await user_reply_menu(uid, settings, users_repo, xui_api),
        )
    await users_repo.set_admin(int(args[1]), False)
    await msg.answer(
        f"❌ {args[1]} — не админ",
        reply_markup=await user_reply_menu(uid, settings, users_repo, xui_api),
    )

@router.message(Command("admins"))
async def list_admins(msg: Message, settings: Settings, users_repo: UsersRepository, xui_api: XUIAPI):
    uid = msg.from_user.id if msg.from_user else 0
    if not await _is_admin(msg.from_user.id, settings, users_repo):
        return await msg.answer("🚫", reply_markup=await user_reply_menu(uid, settings, users_repo, xui_api))
    dyn = await users_repo.get_all_dynamic_admins()
    await msg.answer(
        f"👑 Рут: {list(settings.admin_ids)}\n🔹 Динамические: {dyn or 'нет'}",
        reply_markup=await user_reply_menu(uid, settings, users_repo, xui_api),
    )