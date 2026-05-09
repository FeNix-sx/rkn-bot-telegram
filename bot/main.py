from __future__ import annotations
import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from core.config import ConfigError, load_settings
from core.xui_api import XUIAPI
from db import init_db
from db.repositories.users_repo import UsersRepository
from bot.router import setup_router

LOGGER = logging.getLogger(__name__)

async def run(settings) -> None:
    await init_db(settings.db_path)
    LOGGER.info("DB initialized: %s", settings.db_path)

    users_repo = UsersRepository(settings.db_path)
    await users_repo.migrate_schema()

    xui_api = XUIAPI(settings.xui_api_url, settings.xui_username, settings.xui_password)
    try:
        await xui_api.login()
        LOGGER.info("3X-UI connected")
    except Exception as e:
        LOGGER.warning("3X-UI offline: %s", e)

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher(storage=MemoryStorage())

    # Регистрация зависимостей для DI
    dp["settings"] = settings
    dp["users_repo"] = users_repo
    dp["xui_api"] = xui_api
    dp["bot"] = bot

    dp.include_router(setup_router())

    try:
        LOGGER.info("Bot started")
        await dp.start_polling(bot)
    finally:
        await xui_api.close()
        await bot.session.close()

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if sys.version_info < (3, 12):
        raise SystemExit("Python 3.12+ required")
    try:
        settings = load_settings()
    except ConfigError as e:
        LOGGER.error("Config failed: %s", e)
        raise SystemExit(2)
    asyncio.run(run(settings))

if __name__ == "__main__":
    main()