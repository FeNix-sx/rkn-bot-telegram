"""Удаление сообщения пользователя (reply-кнопка / команда), чтобы не засорять чат."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

LOGGER = logging.getLogger(__name__)


async def try_delete_user_message(bot: Bot, msg: Message) -> None:
    try:
        await bot.delete_message(msg.chat.id, msg.message_id)
    except TelegramBadRequest as e:
        LOGGER.debug("user_message.delete: %s", e)
    except Exception:
        LOGGER.debug("user_message.delete_failed", exc_info=True)
