from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup

def reply_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="🚀 Получить триал"), KeyboardButton(text="📊 Мой статус")],
        [KeyboardButton(text="🔗 Моя ссылка")],
    ]
    if is_admin:
        kb[1].append(KeyboardButton(text="📈 Статистика"))
        kb.append([KeyboardButton(text="⚙️ Управление")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, is_persistent=True)

def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Привязанные", callback_data="admin:bound")],
        [InlineKeyboardButton(text="🔗 Непривязанные", callback_data="admin:unbound")],
        [InlineKeyboardButton(text="❓ Не в базе", callback_data="admin:unknown")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:panel_back")],
    ])

def back_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 К управлению", callback_data="admin:main")]])
