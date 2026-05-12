"""Трекинг сообщения админской статистики (inline-навигация) для удаления при уходе в reply-меню."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

LOGGER = logging.getLogger(__name__)

# tg_id -> (chat_id, message_id) — основное сообщение со статистикой / inline-навигация
_nav: dict[int, tuple[int, int]] = {}
# Отдельное сообщение только с reply-клавиатурой (Telegram не даёт inline+reply в одном сообщении)
_reply_anchor: dict[int, tuple[int, int]] = {}


def remember_admin_stats_nav_message(tg_id: int, chat_id: int, message_id: int) -> None:
    _nav[tg_id] = (chat_id, message_id)


def remember_admin_stats_reply_anchor(tg_id: int, chat_id: int, message_id: int) -> None:
    _reply_anchor[tg_id] = (chat_id, message_id)


def forget_admin_stats_nav_message(tg_id: int) -> None:
    _nav.pop(tg_id, None)


async def _try_delete_pair(bot: Bot, pair: tuple[int, int] | None) -> None:
    if not pair:
        return
    chat_id, message_id = pair
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest as e:
        LOGGER.debug("admin_stats_nav.delete: %s", e)
    except Exception:
        LOGGER.debug("admin_stats_nav.delete_failed", exc_info=True)


async def delete_admin_stats_nav_message_if_any(bot: Bot, tg_id: int) -> None:
    """Удалить сообщение со статистикой и якорь reply-клавиатуры, если отслеживаются."""
    main = _nav.pop(tg_id, None)
    anchor = _reply_anchor.pop(tg_id, None)
    await _try_delete_pair(bot, main)
    await _try_delete_pair(bot, anchor)


async def pop_delete_admin_reply_anchor_if_any(bot: Bot, tg_id: int) -> None:
    """Снять и удалить только якорь reply-клавиатуры (например перед сменой экрана по callback)."""
    pair = _reply_anchor.pop(tg_id, None)
    await _try_delete_pair(bot, pair)
