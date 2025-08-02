import io
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command

from services.llm_api import extract_items_from_image
from database import add_positions, get_positions, set_positions
from keyboards import positions_keyboard
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from utils import parse_position

from database import get_user

router = Router(name="receipts")

class EditStates(StatesGroup):
    editing = State()
    adding = State()

# @router.message(F.photo)
# async def handle_photo(msg: Message):
#     file = await msg.bot.get_file(msg.photo[-1].file_id)
#     file_bytes = await msg.bot.download_file(file.file_path)
#     image_bin = io.BytesIO(file_bytes.read())
#     items = await extract_items_from_image(image_bin)

#     add_positions(items)
#     await msg.answer(
#         f"✅ Позиции добавлены:\n" +
#         "\n".join([f"{i['name']} — {i['quantity']} x {i['price']}₽" for i in items])
#     )
@router.message(F.photo)
async def handle_photo(msg: Message):
    user = get_user(msg.from_user.id)
    if user is None:
        await msg.answer(
            "❗️Вы ещё не зарегистрированы.\n"
            "Пожалуйста, напишите /start в личку боту и завершите регистрацию."
        )
        return

    photo = msg.photo[-1]
    width = photo.width
    height = photo.height
    file_size = photo.file_size

    info = (
        f"Разрешение изображения: {width}x{height} px\n"
        f"Размер файла: {file_size} байт"
    )

    fake_llm_items = [
        {"name": "Молоко", "quantity": 2, "price": 89.99},
        {"name": "Хлеб", "quantity": 1, "price": 39.90},
        {"name": "Яблоки", "quantity": 1.5, "price": 120.00},
    ]

    add_positions(fake_llm_items)

    text = (
        info + "\n\n" + "✅ Позиции добавлены:\n" +
        "\n".join(
            f"{item['name']} — {item['quantity']} x {item['price']}₽"
            for item in fake_llm_items
        )
    )
    await msg.answer(text)



@router.message(Command("show"))
async def show_positions(msg: Message):
    positions = get_positions()
    if not positions:
        await msg.answer("Нет позиций! Сначала добавьте чеки.")
        return
    text = "\n".join([
        f"{idx+1}. {i['name']} — {i['quantity']} x {i['price']}₽"
        for idx, i in enumerate(positions)
    ])
    kb = positions_keyboard(positions)
    await msg.answer(f"<b>Все позиции:</b>\n{text}", parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("del_"))
async def delete_position(call: CallbackQuery):
    idx = int(call.data.replace("del_", ""))
    positions = get_positions()
    if idx < 0 or idx >= len(positions):
        await call.answer("Ошибка удаления")
        return
    positions.pop(idx)
    set_positions(positions)
    await call.answer("Позиция удалена")
    # Обновить сообщение:
    text = "\n".join([
        f"{ix+1}. {i['name']} — {i['quantity']} x {i['price']}₽"
        for ix, i in enumerate(positions)
    ])
    kb = positions_keyboard(positions)
    await call.message.edit_text(f"<b>Все позиции:</b>\n{text}", parse_mode="HTML", reply_markup=kb)

# Аналогично реализуй edit_ и add_ коллбэки (edit — запросить новое значение, add — диалог ввода)
# Редактирование позиции — шаг 1 (запросить ввод)
@router.callback_query(F.data.startswith("edit_"))
async def edit_position(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.replace("edit_", ""))
    positions = get_positions()
    if 0 <= idx < len(positions):
        await state.update_data(edit_idx=idx)
        await state.set_state(EditStates.editing)
        await call.message.answer(
            f"Введите новую позицию для «{positions[idx]['name']}» в формате:\nназвание, количество, цена\n\nПример:\nМолоко, 3, 75"
        )
        await call.answer()
    else:
        await call.answer("Ошибка редактирования", show_alert=True)


# Редактирование позиции — шаг 2 (сохраняем ввод)
@router.message(EditStates.editing)
async def save_edited_position(msg: Message, state: FSMContext):
    data = await state.get_data()
    idx = data.get("edit_idx")
    try:
        position = parse_position(msg.text)
        positions = get_positions()
        positions[idx] = position
        set_positions(positions)
        await msg.answer("Позиция обновлена!")
        kb = positions_keyboard(positions)
        text = "\n".join(
            f"{ix+1}. {i['name']} — {i['quantity']} x {i['price']}₽"
            for ix, i in enumerate(positions)
        )
        await msg.answer(f"<b>Все позиции:</b>\n{text}", parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        await msg.answer(f"Ошибка: {e}\nПример: Салат Оливье 1 250")
    await state.clear()



# Добавление позиции — шаг 1 (запросить ввод)
@router.callback_query(F.data == "add_new")
async def add_new_position(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditStates.adding)
    await call.message.answer("Введите новую позицию в формате:\nназвание, количество, цена")
    await call.answer()


# Добавление позиции — шаг 2 (сохраняем ввод)
@router.message(EditStates.adding)
async def save_new_position(msg: Message, state: FSMContext):
    try:
        position = parse_position(msg.text)
        positions = get_positions()
        positions.append(position)
        set_positions(positions)
        await msg.answer("Позиция добавлена!")
        kb = positions_keyboard(positions)
        text = "\n".join(
            f"{ix+1}. {i['name']} — {i['quantity']} x {i['price']}₽"
            for ix, i in enumerate(positions)
        )
        await msg.answer(f"<b>Все позиции:</b>\n{text}", parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        await msg.answer(f"Ошибка: {e}\nПример: Борщ 2 350")
    await state.clear()
