from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup

def reply_menu(is_admin: bool = False, *, show_trial: bool = True) -> ReplyKeyboardMarkup:
    top = [KeyboardButton(text="📊 Мой статус")]
    if show_trial:
        top.insert(0, KeyboardButton(text="🚀 Получить триал"))
    kb = [
        top,
        [KeyboardButton(text="🔗 Моя ссылка")],
    ]
    if is_admin:
        kb[1].append(KeyboardButton(text="📈 Статистика"))
        kb.append([KeyboardButton(text="⚙️ Управление")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, is_persistent=True)

def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:bound")],
        [InlineKeyboardButton(text="📥 Нет аккаунта ТГ", callback_data="admin:unbound")],
        [InlineKeyboardButton(text="🔌 Нет клиента VPN", callback_data="admin:unknown")],
        [InlineKeyboardButton(text="➕ Добавить клиент", callback_data="xui:add:panel")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:panel_back")],
    ])

def back_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 К управлению", callback_data="admin:main")]])
