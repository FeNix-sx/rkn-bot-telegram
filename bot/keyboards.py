from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup

def reply_menu(is_admin: bool = False, *, show_trial: bool = True) -> ReplyKeyboardMarkup:
    # Базовая раскладка 2x2 (без учёта кнопки триала):
    # [📊 Мой статус]   [🔗 Моя ссылка]
    # [📈 Статистика]   [⚙️ Управление]  (только для админов)
    kb: list[list[KeyboardButton]] = []
    if show_trial:
        kb.append([KeyboardButton(text="🚀 Получить триал")])

    kb.append(
        [
            KeyboardButton(text="📊 Мой статус"),
            KeyboardButton(text="🔗 Моя ссылка"),
        ]
    )

    if is_admin:
        kb.append(
            [
                KeyboardButton(text="📈 Статистика"),
                KeyboardButton(text="⚙️ Управление"),
            ]
        )
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, is_persistent=True)

def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:bound")],
        [InlineKeyboardButton(text="📥 Нет аккаунта ТГ", callback_data="admin:unbound")],
        [InlineKeyboardButton(text="🔌 Нет клиента VPN", callback_data="admin:unknown")],
        [InlineKeyboardButton(text="➕ Добавить клиент", callback_data="xui:add:panel")],
        [InlineKeyboardButton(text="📊 К статистике", callback_data="stats:admin:root")],
    ])

def back_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 К управлению", callback_data="admin:main")]])
