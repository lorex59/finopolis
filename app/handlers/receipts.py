import io
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command

from services.llm_api import extract_items_from_image
from database import add_positions, get_positions, set_positions
from keyboards import positions_keyboard
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from config import settings

from utils import parse_position

from database import get_user
from database import get_all_users, save_debts
from services.payments import mass_pay

router = Router(name="receipts")

class EditStates(StatesGroup):
    editing = State()
    adding = State()


@router.message(F.photo)
async def handle_photo(msg: Message):
    """
    Обработчик фотографий чеков. Работает только для зарегистрированных пользователей.

    1. Получает изображение из сообщения.
    2. Передаёт его в LLM через сервис `extract_items_from_image`.
    3. Если LLM возвращает список позиций, добавляет их в базу и отображает пользователю.
       Если LLM возвращает строку (например, «Это не чек») или происходит ошибка,
       уведомляет пользователя об этом.
    """
    user = get_user(msg.from_user.id)
    # Проверяем, что пользователь зарегистрирован. В групповых чатах бот
    # использует middleware, но для надёжности проверяем здесь ещё раз.
    if user is None:
        await msg.answer(
            "❗️Вы ещё не зарегистрированы.\n"
            "Пожалуйста, напишите /start в личку боту и завершите регистрацию."
        )
        return

    # Загружаем изображение чека из Telegram
    try:
        # Получаем объект файла от Telegram
        telegram_photo = msg.photo[-1]
        file = await msg.bot.get_file(telegram_photo.file_id)
        file_bytes = await msg.bot.download_file(file.file_path)
        # Сохраняем данные в BytesIO для передачи в LLM
        image_bin = io.BytesIO(file_bytes.read())
    except Exception as e:
        await msg.answer(f"Ошибка загрузки изображения: {e}")
        return

    # Передаём изображение в LLM (OpenRouter) для распознавания чека
    try:
        items, _ = await extract_items_from_image(image_bin)
        print("items", items)
    except Exception as e:
        print(f"Ошибка при распознавании чека: {e}")
        items = None

    # Если LLM вернул не список, сообщаем о том, что это не чек
    if not items or not isinstance(items, list):
        # Если items — строка, выводим её, иначе стандартное сообщение
        text = str(items) if items else "Это не чек"
        print(f"LLM returned non-list response: {text}")
        await msg.answer(text)
        return

    # Добавляем полученные позиции в временное хранилище
    add_positions(items)

    # Формируем текст с перечислением позиций и их стоимостью
    positions_text = "\n".join(
        #f"{item['name']} — {item['quantity']} x {item['price']}₽" for item in items
        f"{item.name} — {item.quantity} x {item.price}₽" for item in items
    )
    await msg.answer(
        "✅ Позиции добавлены:\n" + positions_text
    )

    # Отправляем пользователю кнопку для распределения позиций через мини‑приложение
    # URL указывается в переменной окружения BACKEND_URL (settings.backend_url). Предполагается,
    # что именно на этом адресе развернуто WebApp, которое отображает чек и позволяет отметить
    # купленные позиции. Когда пользователь завершит выбор, веб‑приложение должно вызвать
    # Telegram.WebApp.sendData() с выбранными данными, и бот получит их через webapp_data_handler.
    try:
        webapp_url = f"{settings.backend_url}/webapp/receipt"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🧾 Разделить чек",
                        web_app=WebAppInfo(url=webapp_url)
                    )
                ]
            ]
        )
        await msg.answer(
            "Нажмите кнопку ниже, чтобы открыть мини‑приложение для распределения покупок.",
            reply_markup=kb
        )
    except Exception as e:
        # Если не удалось сформировать кнопку, ничего страшного
        print(f"Ошибка при отправке кнопки WebApp: {e}")



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


# Команда для финального согласования и массового перевода
# Пользователь (например администратор) вызывает /finalize в группе, чтобы
# распределить расходы поровну между всеми зарегистрированными участниками.
# В продакшене здесь должен быть более сложный расчёт с учётом выбранных позиций,
# однако для примера реализовано равное распределение.
@router.message(Command("finalize"))
async def finalize_receipt(msg: Message):
    positions = get_positions()
    if not positions:
        await msg.answer("Нет позиций для расчёта. Сначала отправьте чек.")
        return
    users = list(get_all_users())
    if not users:
        await msg.answer("Нет зарегистрированных пользователей для расчёта.")
        return

    # Подсчитаем общую стоимость чека
    total_cost = 0.0
    for item in positions:
        try:
            total_cost += float(item.get("price", 0)) * float(item.get("quantity", 1))
        except Exception:
            pass
    # Делим стоимость поровну между всеми пользователями
    count = len(users)
    share = total_cost / count if count else 0.0
    # Округляем до двух знаков после запятой
    mapping = {user_id: round(share, 2) for user_id, _ in users}

    # Вызываем платёжный сервис (заглушка)
    tx_id = await mass_pay(mapping)
    # Сохраняем информацию о долгах (receipt_id можно заменить на UUID или id чата)
    receipt_id = str(msg.chat.id)
    save_debts(receipt_id, mapping)
    # Очищаем текущие позиции
    set_positions([])
    # Уведомляем участников
    text_lines = ["💰 Расчёт завершён!", f"ID транзакции: {tx_id}"]
    text_lines.append("\nСуммы к оплате:")
    for user_id, amount in mapping.items():
        text_lines.append(f"<code>{user_id}</code> → {amount}₽")
    await msg.answer("\n".join(text_lines), parse_mode="HTML")
