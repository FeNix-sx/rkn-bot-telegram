from __future__ import annotations
from aiogram import Router, F, Bot
from aiogram.types import Message
from aiogram.filters import Command
from bot.keyboards import reply_menu
from core.config import Settings
from core.xui_api import XUIAPI
from db.repositories.users_repo import UsersRepository
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import os, logging

LOGGER = logging.getLogger(__name__)
router = Router()

def _fmt(dt):
    if not dt: return "n/a"
    try: return datetime.fromisoformat(dt).strftime("%Y-%m-%d %H:%M:%S UTC")
    except: return "n/a"

async def _adm(tg_id: int, data: dict) -> bool:
    s = data["settings"]
    r = data["users_repo"]
    return tg_id in s.admin_ids or await r.is_admin(tg_id)

@router.message(Command("start"))
async def start(msg: Message, settings: Settings, users_repo: UsersRepository, bot: Bot):
    tg = msg.from_user
    if not tg: return
    name = tg.username or tg.first_name or "Без имени"
    txt = f"🆔 /start:\nID: {tg.id}\nИмя: {name}"
    admins = set(settings.admin_ids)
    admins.update(await users_repo.get_all_dynamic_admins())
    for aid in admins:
        try: await bot.send_message(aid, txt)
        except: pass
    await users_repo.create_user_if_not_exists(tg.id, tg.username, tg.first_name, tg.last_name)
    row = await users_repo.get_user(tg.id)
    st = row.get("status", "new") if row else "new"
    is_adm = tg.id in settings.admin_ids or await users_repo.is_admin(tg.id)
    await msg.answer(f"{'Привет! Профиль создан.' if row else 'С возвращением!'}\nСтатус: {st}\n\n/trial - триал\n/status - статус\n/link - ссылка", reply_markup=reply_menu(is_adm))

@router.message(Command("status"), F.text == "📊 Мой статус")
async def status(msg: Message, settings: Settings, users_repo: UsersRepository):
    tg = msg.from_user
    if not tg: return
    row = await users_repo.get_user(tg.id)
    if not row: return await msg.answer("Ошибка загрузки.", reply_markup=reply_menu())
    is_adm = tg.id in settings.admin_ids or await users_repo.is_admin(tg.id)
    await msg.answer(f"Статус:\n- status: {row.get('status','new')}\n- trial_used: {'yes' if row.get('has_trial_used') else 'no'}\n- end: {_fmt(row.get('trial_end'))}\n- link: {'yes' if row.get('subscription_url') else 'no'}", reply_markup=reply_menu(is_adm))

@router.message(Command("link"), F.text == "🔗 Моя ссылка")
async def link(msg: Message, data: dict):
    tg = msg.from_user
    if not tg: return
    row = await data["users_repo"].get_user(tg.id)
    if not row: return await msg.answer("Ошибка.", reply_markup=reply_menu())
    url = (row.get("subscription_url") or "").strip()
    if not url: return await msg.answer("Нет ссылки. Запусти /trial.", reply_markup=reply_menu())
    await msg.answer(f"Твоя ссылка: {url}", reply_markup=reply_menu(await _adm(tg.id, data)))

@router.message(Command("trial"), F.text == "🚀 Получить триал")
async def trial(msg: Message, data: dict):
    tg = msg.from_user
    if not tg: return
    r, xui, s = data["users_repo"], data["xui_api"], data["settings"]
    row = await r.get_user(tg.id)
    if not row or row.get("has_trial_used"): return await msg.answer("Триал уже использован.", reply_markup=reply_menu())
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=s.trial_days)
    email = f"trial_{tg.id}"
    uuid = str(uuid4())
    if os.getenv("MOCK_XUI"):
        await r.set_trial(tg.id, start.isoformat(), end.isoformat(), email, uuid, f"mock://sub/{email}")
        return await msg.answer(f"✅ MOCK: {email}", reply_markup=reply_menu())
    try:
        iid = await xui.resolve_inbound_id("vless_reality")
        await xui.add_client(iid, email, uuid, limit_ip=1)
        sub = await xui.build_or_get_subscription_url(inbound_id=iid, email=email)
        await r.set_trial(tg.id, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), email, uuid, sub)
        await msg.answer(f"Триал активирован: {sub}", reply_markup=reply_menu())
    except Exception as e:
        await msg.answer(f"Ошибка: {e}", reply_markup=reply_menu())