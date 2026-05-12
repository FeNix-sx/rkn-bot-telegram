from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


async def edit_callback_nav(
    cb: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    *,
    parse_mode: str | None = None,
) -> Message | None:
    """Одно сообщение: правка текста и inline-клавиатуры (навигация без спама новыми сообщениями).

    Возвращает сообщение, в котором сейчас отображается навигация (для трекинга админ-статистики).
    """
    msg = cb.message
    if msg is None or not isinstance(msg, Message):
        return None
    kwargs: dict = {}
    if parse_mode:
        kwargs["parse_mode"] = parse_mode
    try:
        await msg.edit_text(text, reply_markup=reply_markup, **kwargs)
        return msg
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "message is not modified" in err:
            return msg
        return await msg.answer(text, reply_markup=reply_markup, **kwargs)
