from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def positions_keyboard(positions):
    buttons = []
    for idx, item in enumerate(positions):
        buttons.append([
            InlineKeyboardButton(text=f"✏️ {item['name']}", callback_data=f"edit_{idx}"),
            InlineKeyboardButton(text="❌", callback_data=f"del_{idx}")
        ])
    buttons.append([InlineKeyboardButton(text="➕ Добавить позицию", callback_data="add_new")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
