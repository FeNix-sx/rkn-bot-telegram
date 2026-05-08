from __future__ import annotations
import asyncio, logging, sys
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from core.config import ConfigError, load_settings
from core.xui_api import XUIAPI
from db import init_db
from db.repositories.users_repo import UsersRepository
from bot.router import setup_router

LOGGER = logging.getLogger(__name__)

async def run(s):
    await init_db(s.db_path)
    LOGGER.info("DB: %s", s.db_path)
    r = UsersRepository(s.db_path)
    await r.migrate_schema()
    xui = XUIAPI(s.xui_api_url, s.xui_username, s.xui_password)
    try:
        await xui.login()
        LOGGER.info("3X-UI connected")
    except Exception as e:
        LOGGER.warning("3X-UI offline: %s", e)
    bot = Bot(token=s.bot_token, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()
    dp["settings"], dp["users_repo"], dp["xui_api"], dp["bot"] = s, r, xui, bot
    dp.include_router(setup_router())
    try:
        LOGGER.info("Bot started")
        await dp.start_polling(bot)
    finally:
        await xui.close()
        await bot.session.close()

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if sys.version_info < (3,12): raise SystemExit("Python 3.12+ required")
    try: s = load_settings()
    except ConfigError as e:
        LOGGER.error("Config: %s", e)
        raise SystemExit(2)
    asyncio.run(run(s))

if __name__ == "__main__":
    main()