from aiogram import Router
from bot.handlers import user, admin, stats

def setup_router() -> Router:
    r = Router()
    r.include_router(user.router)
    r.include_router(admin.router)
    r.include_router(stats.router)
    return r