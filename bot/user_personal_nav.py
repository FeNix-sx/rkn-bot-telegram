"""Пачка message_id ответов «Мой статус» / «Моя ссылка»: удаляем только после нового ответа с клавиатурой."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

LOGGER = logging.getLogger(__name__)

# tg_id -> [(chat_id, message_id), ...]
_personal: dict[int, list[tuple[int, int]]] = {}


def take_personal_nav_batch(tg_id: int) -> list[tuple[int, int]]:
    """Снять сохранённую пачку (для удаления после отправки нового сообщения)."""
    return _personal.pop(tg_id, []) or []


def remember_personal_nav_messages(tg_id: int, messages: list[Message]) -> None:
    if not messages:
        return
    _personal[tg_id] = [(m.chat.id, m.message_id) for m in messages]


async def delete_message_pairs(bot: Bot, pairs: list[tuple[int, int]]) -> None:
    for chat_id, mid in pairs:
        try:
            await bot.delete_message(chat_id, mid)
        except TelegramBadRequest as e:
            LOGGER.debug("personal_nav.delete: %s", e)
        except Exception:
            LOGGER.debug("personal_nav.delete_failed", exc_info=True)
