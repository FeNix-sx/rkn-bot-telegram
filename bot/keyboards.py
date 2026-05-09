from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup

def reply_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="🚀 Получить триал"), KeyboardButton(text="📊 Мой статус")],
        [KeyboardButton(text="🔗 Моя ссылка"), KeyboardButton(text="📈 Статистика")],
    ]
    if is_admin:
        kb.append([KeyboardButton(text="⚙️ Управление")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, is_persistent=True)

def stats_menu(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🔍 Моя статистика", callback_data="stats:my")]]
    if is_admin:
        rows.append([InlineKeyboardButton(text="📊 Общая", callback_data="stats:all")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Привязанные", callback_data="admin:bound")],
        [InlineKeyboardButton(text="🔗 Непривязанные", callback_data="admin:unbound")],
        [InlineKeyboardButton(text="❓ Не в базе", callback_data="admin:unknown")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="stats:back")],
    ])

def back_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 К управлению", callback_data="admin:main")]])

def back_stats(is_admin: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 К статистике", callback_data="stats:back")]])