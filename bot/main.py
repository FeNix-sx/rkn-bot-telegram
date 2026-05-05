"""Telegram bot entrypoint with day-1 user handlers."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_SUBMITTED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, User

from core import ConfigError, Settings, load_settings
from core import XUIAPI, XUIAPIError
from db import init_db
from db.repositories.users_repo import UsersRepository

LOGGER = logging.getLogger(__name__)
MIN_PYTHON = (3, 12)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_utc(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_dt(raw_value: str | None) -> str:
    dt = _parse_iso_utc(raw_value)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else "n/a"


def _status_text(user_row: dict[str, object] | None) -> str:
    if not user_row:
        return "new"
    return str(user_row.get("status") or "new")


def _build_menu_text() -> str:
    return (
        "Команды:\n"
        "/trial - получить триал\n"
        "/status - показать статус\n"
        "/link - повторно выдать ссылку"
    )


def _reply_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚀 Получить триал"), KeyboardButton(text="📊 Мой статус")],
            [KeyboardButton(text=" Моя ссылка")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    running_jobs_started_at: dict[tuple[str, str], float] = {}
    stale_timing_ttl_seconds = 60 * 60 * 6
    cleanup_interval_seconds = 60
    last_cleanup_at_monotonic = 0.0

    def _event_key(job_id: str, scheduled_run_time: datetime) -> tuple[str, str]:
        return (job_id, scheduled_run_time.isoformat(timespec="microseconds"))

    def _cleanup_stale_timings(now_monotonic: float) -> None:
        stale_keys = [
            key for key, started_at in running_jobs_started_at.items()
            if now_monotonic - started_at > stale_timing_ttl_seconds
        ]
        for stale_key in stale_keys:
            running_jobs_started_at.pop(stale_key, None)
        if stale_keys:
            LOGGER.warning("scheduler.timings.cleaned", extra={"removed_count": len(stale_keys), "ttl_seconds": stale_timing_ttl_seconds})

    def _scheduler_listener(event: object) -> None:
        nonlocal last_cleanup_at_monotonic
        event_code = getattr(event, "code", None)
        now_monotonic = time.perf_counter()
        if now_monotonic - last_cleanup_at_monotonic >= cleanup_interval_seconds:
            _cleanup_stale_timings(now_monotonic)
            last_cleanup_at_monotonic = now_monotonic

        if event_code == EVENT_JOB_SUBMITTED:
            job_id = getattr(event, "job_id", None)
            scheduled_run_times = getattr(event, "scheduled_run_times", None)
            if job_id and scheduled_run_times:
                for t in scheduled_run_times:
                    running_jobs_started_at[_event_key(job_id, t)] = now_monotonic
            return

        if event_code not in (EVENT_JOB_EXECUTED, EVENT_JOB_ERROR):
            return
        job_id = getattr(event, "job_id", None)
        scheduled_run_time = getattr(event, "scheduled_run_time", None)
        if not job_id or scheduled_run_time is None:
            return

        started_at = running_jobs_started_at.pop(_event_key(job_id, scheduled_run_time), None)
        duration_ms = int((now_monotonic - started_at) * 1000) if started_at else None
        extra = {"job_id": job_id, "scheduled_run_time": scheduled_run_time.isoformat(timespec="seconds"), "duration_ms": duration_ms}

        if event_code == EVENT_JOB_EXECUTED:
            LOGGER.info("scheduler.job.executed", extra=extra)
        else:
            LOGGER.error("scheduler.job.failed", extra={**extra, "exception": repr(getattr(event, "exception", None)), "traceback": getattr(event, "traceback", None)})

    async def check_trials_job() -> None:
        LOGGER.info("scheduler.check_trials.tick", extra={"job_id": "check_trials", "tick_at": _utc_now().isoformat(timespec="seconds")})

    scheduler.add_job(check_trials_job, trigger="interval", minutes=15, id="check_trials", replace_existing=True, coalesce=True, max_instances=1)
    scheduler.add_listener(_scheduler_listener, EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    return scheduler


def _attach_handlers(
    dispatcher: Dispatcher,
    settings: Settings,
    users_repo: UsersRepository,
    xui_api: XUIAPI,
) -> None:
    async def _ensure_user_exists(tg_user: User) -> bool:
        return await users_repo.create_user_if_not_exists(
            tg_id=tg_user.id, username=tg_user.username, first_name=tg_user.first_name, last_name=tg_user.last_name
        )

    async def _send_status(message: Message, tg_user: User) -> None:
        await _ensure_user_exists(tg_user)
        user_row = await users_repo.get_user(tg_user.id)
        if user_row is None:
            await message.answer("Не удалось загрузить статус.", reply_markup=_reply_menu())
            return

        status_text = _status_text(user_row)
        trial_end = _format_dt(user_row.get("trial_end"))
        has_trial_used = "yes" if int(user_row.get("has_trial_used") or 0) == 1 else "no"
        has_link = "yes" if str(user_row.get("subscription_url") or "").strip() else "no"
        LOGGER.info("cmd.status", extra={"tg_id": tg_user.id, "status": status_text})
        await message.answer(
            "Статус профиля:\n"
            f"- status: {status_text}\n"
            f"- has_trial_used: {has_trial_used}\n"
            f"- trial_end: {trial_end}\n"
            f"- has_subscription_url: {has_link}",
            reply_markup=_reply_menu(),
        )

    async def _send_link(message: Message, tg_user: User) -> None:
        await _ensure_user_exists(tg_user)
        user_row = await users_repo.get_user(tg_user.id)
        if user_row is None:
            await message.answer("Не удалось загрузить пользователя.", reply_markup=_reply_menu())
            return

        subscription_url = str(user_row.get("subscription_url") or "").strip()
        if not subscription_url:
            await message.answer("Ссылка отсутствует. Запусти /trial для первичной выдачи.", reply_markup=_reply_menu())
            LOGGER.info("cmd.link.missing", extra={"tg_id": tg_user.id})
            return

        await message.answer(f"Твоя подписка: {subscription_url}", reply_markup=_reply_menu())
        LOGGER.info("cmd.link.ok", extra={"tg_id": tg_user.id})

    async def _issue_trial(message: Message, tg_user: User) -> None:
        await _ensure_user_exists(tg_user)
        user_row = await users_repo.get_user(tg_user.id)
        if user_row is None:
            await message.answer("Не удалось загрузить пользователя. Попробуйте позже.", reply_markup=_reply_menu())
            return

        if int(user_row.get("has_trial_used") or 0) == 1:
            await message.answer("Триал уже использован ранее. Используй /status или /link, если он еще активен.", reply_markup=_reply_menu())
            LOGGER.info("cmd.trial.denied.used", extra={"tg_id": tg_user.id})
            return

        trial_start = _utc_now()
        trial_end = trial_start + timedelta(days=settings.trial_days)
        xui_email = f"trial_{tg_user.id}"
        xui_uuid = str(uuid4())

        # --- MOCK: Эмуляция ответа 3X-UI без сетевых запросов ---
        if os.getenv("MOCK_XUI"):
            mock_url = f"mock://sub/{xui_email}"
            await users_repo.set_trial(
                tg_id=tg_user.id,
                trial_start=trial_start.isoformat(),
                trial_end=trial_end.isoformat(),
                xui_email=xui_email,
                xui_uuid=xui_uuid,
                subscription_url=mock_url,
            )
            await message.answer(
                f"✅ MOCK: Триал активирован.\nДействует до: {_format_dt(trial_end.isoformat())}\nСсылка: {mock_url}",
                reply_markup=_reply_menu(),
            )
            LOGGER.info("cmd.trial.mock", extra={"tg_id": tg_user.id, "url": mock_url})
            return

        try:
            inbound_id = await xui_api.resolve_inbound_id("vless_reality")
            await xui_api.add_client(inbound_id=inbound_id, email=xui_email, uuid=xui_uuid, limit_ip=1)
            subscription_url = await xui_api.build_or_get_subscription_url(inbound_id=inbound_id, email=xui_email)
            await users_repo.set_trial(
                tg_id=tg_user.id, trial_start=trial_start.isoformat(timespec="seconds"), trial_end=trial_end.isoformat(timespec="seconds"),
                xui_email=xui_email, xui_uuid=xui_uuid, subscription_url=subscription_url,
            )
            LOGGER.info("cmd.trial.ok", extra={"tg_id": tg_user.id, "inbound_id": inbound_id, "trial_end": trial_end.isoformat(timespec="seconds")})
            await message.answer(
                f"Триал активирован.\nДействует до: {_format_dt(trial_end.isoformat(timespec='seconds'))}\nСсылка: {subscription_url}",
                reply_markup=_reply_menu(),
            )
        except XUIAPIError as exc:
            LOGGER.exception("cmd.trial.xui_error", extra={"tg_id": tg_user.id})
            await message.answer(f"Не удалось выдать триал из-за ошибки API: {exc}", reply_markup=_reply_menu())
        except Exception:
            LOGGER.exception("cmd.trial.unexpected_error", extra={"tg_id": tg_user.id})
            await message.answer("Внутренняя ошибка при выдаче триала. Попробуй позже.", reply_markup=_reply_menu())

    @dispatcher.message(Command("start"))
    async def start_handler(message: Message) -> None:
        tg_user = message.from_user
        if tg_user is None:
            await message.answer("Не удалось определить пользователя.", reply_markup=_reply_menu())
            return

        created = await _ensure_user_exists(tg_user)
        user_row = await users_repo.get_user(tg_user.id)
        LOGGER.info("cmd.start", extra={"tg_id": tg_user.id, "result": "created" if created else "existing"})
        greeting = "Привет! Профиль создан." if created else "С возвращением!"
        await message.answer(f"{greeting}\nСтатус: {_status_text(user_row)}\n\n{_build_menu_text()}", reply_markup=_reply_menu())

    @dispatcher.message(Command("trial"))
    async def trial_handler(message: Message) -> None:
        tg_user = message.from_user
        if tg_user is None:
            await message.answer("Не удалось определить пользователя.", reply_markup=_reply_menu())
            return
        await _issue_trial(message, tg_user)

    @dispatcher.message(Command("status"))
    async def status_handler(message: Message) -> None:
        tg_user = message.from_user
        if tg_user is None:
            await message.answer("Не удалось определить пользователя.", reply_markup=_reply_menu())
            return
        await _send_status(message, tg_user)

    @dispatcher.message(Command("link"))
    async def link_handler(message: Message) -> None:
        tg_user = message.from_user
        if tg_user is None:
            await message.answer("Не удалось определить пользователя.", reply_markup=_reply_menu())
            return
        await _send_link(message, tg_user)

    @dispatcher.message(F.text == "🚀 Получить триал")
    async def trial_button_handler(message: Message) -> None:
        tg_user = message.from_user
        if tg_user is None:
            await message.answer("Не удалось определить пользователя.", reply_markup=_reply_menu())
            return
        await _issue_trial(message, tg_user)

    @dispatcher.message(F.text == "📊 Мой статус")
    async def status_button_handler(message: Message) -> None:
        tg_user = message.from_user
        if tg_user is None:
            await message.answer("Не удалось определить пользователя.", reply_markup=_reply_menu())
            return
        await _send_status(message, tg_user)

    @dispatcher.message(F.text == "🔗 Моя ссылка")
    async def link_button_handler(message: Message) -> None:
        tg_user = message.from_user
        if tg_user is None:
            await message.answer("Не удалось определить пользователя.", reply_markup=_reply_menu())
            return
        await _send_link(message, tg_user)

    @dispatcher.message(F.text)
    async def fallback_handler(message: Message) -> None:
        await message.answer(_build_menu_text(), reply_markup=_reply_menu())


async def run(settings: Settings) -> None:
    await init_db(settings.db_path)
    LOGGER.info("DB initialized", extra={"db_path": settings.db_path})

    users_repo = UsersRepository(settings.db_path)
    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher()
    xui_api = XUIAPI(settings.xui_api_url, settings.xui_username, settings.xui_password)
    scheduler = _build_scheduler()

    _attach_handlers(dispatcher, settings, users_repo, xui_api)

    # Не блокируем старт, если 3X-UI недоступен (локальный тест UI)
    try:
        await xui_api.login()
        LOGGER.info("3X-UI connected")
    except Exception as e:
        LOGGER.warning("3X-UI offline/skipped (local mode): %s", e)

    try:
        scheduler.start()
        check_trials_job = scheduler.get_job("check_trials")
        LOGGER.info("Scheduler started", extra={"next_run_time": check_trials_job.next_run_time if check_trials_job else None})
        LOGGER.info("Bot startup complete", extra={"trial_days": settings.trial_days})
        await dispatcher.start_polling(bot)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await xui_api.close()
        await bot.session.close()


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def validate_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(f"Python {'.'.join(map(str, MIN_PYTHON))}+ is required.")


def main() -> None:
    configure_logging()
    validate_python_version()
    try:
        settings = load_settings()
    except ConfigError as exc:
        LOGGER.error("Configuration validation failed: %s", exc)
        raise SystemExit(2) from exc
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()