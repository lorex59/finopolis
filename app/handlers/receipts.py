import io
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command

from services.llm_api import extract_items_from_image
from database import (
    add_positions,
    get_positions,
    set_positions,
    init_assignments,
    set_assignment,
    get_assignments,
    start_text_session,
    append_text_message,
    end_text_session,
    get_all_users,
    save_debts,
)
from keyboards import positions_keyboard
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from config import settings

from utils import parse_position

from database import get_user
from database import get_all_users, save_debts, log_payment
from services.payments import mass_pay
from services.llm_api import calculate_debts_from_messages

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
    print("Items extracted from image:", items)
    # Добавляем полученные позиции в временное хранилище
    items = [
        {"name": item.name, "quantity": item.quantity, "price": item.price}
        for item in items
    ]

    add_positions(items)

    # Инициализируем назначение позиций для данного чата
    chat_receipt_id = str(msg.chat.id)
    init_assignments(chat_receipt_id)

   
    # Формируем текст с перечислением позиций и их стоимостью
    positions_text = "\n".join(
        f"{item['name']} — {item['quantity']} x {item['price']}₽" for item in items
        #f"{item.name} — {item.quantity} x {item.price}₽" for item in items
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


@router.message(F.web_app_data)
async def handle_web_app_data(msg: Message):
    """
    Обработчик данных, присылаемых из WebApp. telegram.web_app_data.data содержит строку JSON,
    которую нужно распарсить. Предполагаем, что она имеет структуру
    {"selected": [0, 3, 5]} — индексы позиций, которые выбрал пользователь.
    Храним выбор в БД через set_assignment().
    """
    try:
        import json
        data = json.loads(msg.web_app_data.data)
        selected_indices = data.get("selected", [])
        # Приводим индексы к целым числам
        indices = [int(i) for i in selected_indices]
    except Exception as e:
        await msg.answer(f"Ошибка обработки данных из мини‑приложения: {e}")
        return
    receipt_id = str(msg.chat.id)
    set_assignment(receipt_id, msg.from_user.id, indices)
    await msg.answer("✅ Ваш выбор сохранён! Когда все участники отметят свои позиции, используйте /finalize для расчёта.")



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
        #f"{ix+1}. {i['name']} — {i['quantity']} x {i['price']}₽"
        f"{idx+1}. {i.name} — {i.quantity} x {i.price}₽"
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
    """
    Финальный расчёт для текущего чека.

    Есть два режима:
      - Если участники распределили позиции через мини‑приложение, то расходы
        рассчитываются на основе выбранных позиций (ASSIGNMENTS).
      - Если был активирован текстовый сценарий, то используются
        накопленные сообщения и LLM для распределения.
      - Если ни одно из условий не выполнено, делим сумму поровну как раньше.
    """
    receipt_id = str(msg.chat.id)
    positions = get_positions()
    if not positions:
        await msg.answer("Нет позиций для расчёта. Сначала отправьте чек.")
        return
    users = list(get_all_users())
    if not users:
        await msg.answer("Нет зарегистрированных пользователей для расчёта.")
        return

    # 1. Попробуем использовать текстовый сценарий, если он завершён
    from database import TEXT_SESSIONS
    session = TEXT_SESSIONS.get(receipt_id)
    if session and not session.get("collecting") and session.get("messages"):
        # items for LLM: convert positions to dict{name: price}
        items_for_llm: dict[str, float] = {}
        for item in positions:
            try:
                total_price = float(item.get("price", 0)) * float(item.get("quantity", 1))
                items_for_llm[item.get("name")] = total_price
            except Exception:
                pass
        messages = session["messages"]
        try:
            debt_mapping = await calculate_debts_from_messages(items_for_llm, messages)
        except Exception as e:
            await msg.answer(f"Ошибка при расчёте через LLM: {e}\nПробуем поровну разделить.")
            debt_mapping = None
        if isinstance(debt_mapping, dict):
            # Округлить и привести ключи к int
            mapping: dict[int, float] = {}
            for k, v in debt_mapping.items():
                try:
                    mapping[int(k)] = round(float(v), 2)
                except Exception:
                    pass
            if mapping:
                # Выполняем платёж и логируем его
                tx_id = await mass_pay(mapping)
                save_debts(receipt_id, mapping)
                log_payment(receipt_id, tx_id, mapping)
                set_positions([])
                # Очистим текстовую сессию
                session["messages"] = []
                # Отправляем каждому пользователю личное уведомление с их суммой
                for user_id, amount in mapping.items():
                    try:
                        # Покажем имя, если доступно
                        user_info = get_user(user_id) or {}
                        name = user_info.get('full_name') or user_info.get('phone') or str(user_id)
                        await msg.bot.send_message(user_id, f"{name}, вы должны {amount}₽. Спасибо за участие!")
                    except Exception:
                        pass
                # Формируем текст для группового чата
                text_lines = ["💰 Расчёт завершён!", f"ID транзакции: {tx_id}"]
                text_lines.append("\nСуммы к оплате:")
                for user_id, amount in mapping.items():
                    user_info = get_user(user_id) or {}
                    name = user_info.get('full_name') or user_info.get('phone') or str(user_id)
                    text_lines.append(f"{name} ({user_id}) → {amount}₽")
                await msg.answer("\n".join(text_lines), parse_mode="HTML")
                return

    # 2. Если у нас есть назначения из WebApp
    assignments = get_assignments(receipt_id)
    if assignments:
        # Рассчитаем стоимость каждой позиции
        cost_per_position = []
        for item in positions:
            try:
                cost_per_position.append(float(item.get("price", 0)) * float(item.get("quantity", 1)))
            except Exception:
                cost_per_position.append(0.0)
        # Считаем сумму для каждого пользователя
        mapping: dict[int, float] = {user_id: 0.0 for user_id, _ in users}
        for user_id, indices in assignments.items():
            total = 0.0
            for idx in indices:
                if 0 <= idx < len(cost_per_position):
                    total += cost_per_position[idx]
            mapping[user_id] = round(total, 2)
        # Определим плательщика (первый отправивший фото)
        payer_id = msg.from_user.id
        # Преобразуем mapping в формат "кто сколько кому должен":
        # все кроме payer должны payer
        debt_mapping: dict[int, float] = {}
        for uid, amount in mapping.items():
            if uid == payer_id:
                continue
            debt_mapping[uid] = amount
        # Выполняем перевод (заглушка)
        # Выполняем платёж и логируем его
        tx_id = await mass_pay(debt_mapping)
        save_debts(receipt_id, debt_mapping)
        log_payment(receipt_id, tx_id, debt_mapping)
        set_positions([])
        # Уведомляем каждого должника персонально
        for uid, amount in debt_mapping.items():
            try:
                user_info = get_user(uid) or {}
                name = user_info.get('full_name') or user_info.get('phone') or str(uid)
                payer_info = get_user(payer_id) or {}
                payer_name = payer_info.get('full_name') or payer_info.get('phone') or str(payer_id)
                await msg.bot.send_message(uid, f"{name}, вы должны {amount}₽ пользователю {payer_name}.")
            except Exception:
                pass
        # Подготовим текст для группового чата с именами
        text_lines = ["💰 Расчёт завершён!", f"ID транзакции: {tx_id}"]
        text_lines.append("\nСуммы к оплате:")
        for uid, amount in debt_mapping.items():
            user_info = get_user(uid) or {}
            name = user_info.get('full_name') or user_info.get('phone') or str(uid)
            text_lines.append(f"{name} ({uid}) → {amount}₽")
        await msg.answer("\n".join(text_lines), parse_mode="HTML")
        return

    # 3. По умолчанию делим сумму поровну между всеми участниками
    total_cost = 0.0
    for item in positions:
        try:
            total_cost += float(item.get("price", 0)) * float(item.get("quantity", 1))
        except Exception:
            pass
    count = len(users)
    share = total_cost / count if count else 0.0
    mapping = {user_id: round(share, 2) for user_id, _ in users}
    # Выполняем платёж и логируем его
    tx_id = await mass_pay(mapping)
    save_debts(receipt_id, mapping)
    log_payment(receipt_id, tx_id, mapping)
    set_positions([])
    # Уведомления в личку: каждый получает уведомление о сумме, которую должен
    for uid, amount in mapping.items():
        try:
            user_info = get_user(uid) or {}
            name = user_info.get('full_name') or user_info.get('phone') or str(uid)
            await msg.bot.send_message(uid, f"{name}, вы должны {amount}₽ (поровну разделено).")
        except Exception:
            pass
    # Групповое сообщение с именами
    text_lines = ["💰 Расчёт завершён!", f"ID транзакции: {tx_id}"]
    text_lines.append("\nСуммы к оплате:")
    for user_id, amount in mapping.items():
        user_info = get_user(user_id) or {}
        name = user_info.get('full_name') or user_info.get('phone') or str(user_id)
        text_lines.append(f"{name} ({user_id}) → {amount}₽")
    await msg.answer("\n".join(text_lines), parse_mode="HTML")
