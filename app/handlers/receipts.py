import io
import sqlite3
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, WebAppInfo

from services.llm_api import extract_items_from_image
# Используем общий модуль базы данных из пакета ``app``. Это исключает
# дублирование кода и разделение данных между двумя разными файлами
# database.py в корне проекта и в подпакете ``app``. Все функции
# взаимодействия с базой данных импортируем из ``app.database``.
from app.database import (
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
    save_selected_positions,
    get_selected_positions,
)
from keyboards import positions_keyboard
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from config import settings

from utils import parse_position

from app.database import get_user
from app.database import get_all_users, save_debts, log_payment

# Импортируем новые функции для платежей и расчёта баланса
from app.database import (
    add_payment,
    get_payments,
    calculate_group_balance,
    get_unassigned_positions,
)
from services.payments import mass_pay
from services.llm_api import calculate_debts_from_messages

router = Router(name="receipts")

class EditStates(StatesGroup):
    editing = State()
    adding = State()

# Позволяет пользователю вызвать мини‑приложение для выбора покупок в любой момент.
# Команда /split отправляет кнопку «Разделить чек» в группу, если
# для текущего чата уже существуют позиции. Это позволяет нескольким
# участникам группы выбрать свои покупки независимо от того, кто
# загрузил чек. Если позиций нет, бот уведомит об этом.
@router.message(Command("split"))
async def cmd_split(msg: Message):
    group_id = str(msg.chat.id)
    # Проверяем, есть ли позиции для текущей группы. Если нет, предупредим пользователя.
    positions = get_positions(group_id)
    if not positions:
        await msg.answer(
            "❗️Нет позиций для распределения. Сначала отправьте фото чека или добавьте позиции вручную."
        )
        return
    try:
        # Формируем URL для мини‑приложения, добавляя идентификатор группы.
        print(f"Type { msg.chat.type}")
        webapp_url = f"{settings.backend_url}/webapp/receipt?group_id={msg.chat.id}"
        # В приватном чате можно отправлять WebApp-кнопку на обычной клавиатуре. В группах используем deep‑link.
        if msg.chat.type == "private":
            kb = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="🧾 Разделить чек", web_app=WebAppInfo(url=webapp_url))]],
                resize_keyboard=True,
                one_time_keyboard=True,
                input_field_placeholder="Откройте мини‑приложение"
            )
            await msg.answer(
                "Нажмите кнопку ниже, чтобы открыть мини‑приложение для распределения покупок.",
                reply_markup=kb
            )
        else:
            # Формируем deep‑link, который открывает мини‑приложение из профиля бота
            if settings.bot_username:
                payload = f"group_{msg.chat.id}"
                deep_link = f"https://t.me/{settings.bot_username}?startapp={payload}"
                print(deep_link)
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🧾 Разделить чек", url=deep_link)]]
                )
                await msg.answer(
                    "Нажмите кнопку ниже, чтобы открыть мини‑приложение для распределения покупок.",
                    reply_markup=kb
                )
            else:
                # Без имени бота отправляем прямую ссылку на страницу WebApp
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🧾 Разделить чек", url=webapp_url)]]
                )
                await msg.answer(
                    "Нажмите кнопку ниже, чтобы открыть мини‑приложение для распределения покупок.",
                    reply_markup=kb
                )
    except Exception as e:
        print(f"Ошибка при отправке кнопки WebApp: {e}")
        await msg.answer("Произошла ошибка при формировании ссылки на мини‑приложение.")

# --- Новые команды ---
# show_position: показать распределённые позиции

@router.message(Command("show_position"))
async def cmd_show_position(msg: Message):
    """
    Отображает распределённые позиции для текущей группы. Использует
    SELECTED_POSITIONS из database.py, где ключом является название
    группы (chat.title). Если нет распределений, выводит соответствующее
    сообщение.
    """
    # Используем идентификатор группы. Для приватных чатов используем chat.id
    group_id = str(msg.chat.id)
    selections = get_selected_positions(group_id)
    if not selections:
        await msg.answer("❗️Позиции ещё не распределены. Пользователи должны выбрать свои покупки через мини‑приложение.")
        return
    lines: list[str] = ["<b>Распределение позиций:</b>"]
    # Проходим по каждому пользователю и его выбору
    for user_id, pos_list in selections.items():
        # Определяем имя пользователя (ФИО, телефон или ID)
        user_info = get_user(user_id) or {}
        name = user_info.get('full_name') or user_info.get('phone') or str(user_id)
        if pos_list:
            # Формируем строку с перечислением позиций
            items_str = ", ".join([
                f"{p.get('name')} ({p.get('quantity')} × {p.get('price')}₽)"
                for p in pos_list
            ])
        else:
            items_str = "—"
        lines.append(f"{name} ({user_id}): {items_str}")
    await msg.answer("\n".join(lines), parse_mode="HTML")


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
    except Exception as e:
        items = None

    # Если LLM вернул не список, сообщаем о том, что это не чек
    if not items or not isinstance(items, list):
        # Если items — строка, выводим её, иначе стандартное сообщение
        text = str(items) if items else "Это не чек"
        print(f"LLM returned non-list response: {text}")
        await msg.answer(text)
        return
    # Сохраняем позиции с исходным количеством и ценой. Количество
    # понадобится при расчёте, если пользователь выберет меньше, чем
    # указанное количество (частичный выбор реализуется в мини‑приложении).
    positions_to_add = [
        {"name": it.name, "quantity": it.quantity, "price": it.price}
        for it in items
    ]
    # Определяем идентификатор группы (чата) для привязки позиций
    group_id = str(msg.chat.id)
    add_positions(group_id, positions_to_add)

    # Инициализируем назначение позиций для данного чата
    chat_receipt_id = str(msg.chat.id)
    init_assignments(chat_receipt_id)

   
    # Формируем текст с перечислением позиций и их стоимостью
    positions_text = "\n".join(
        f"{item['name']} — {item['quantity']} x {item['price']}₽" for item in positions_to_add
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
        # Передаём ID группы в URL, чтобы мини‑приложение могло загрузить позиции
        webapp_url = f"{settings.backend_url}/webapp/receipt?group_id={msg.chat.id}"
        # В приватных чатах Telegram позволяет использовать клавиши WebApp на reply‑клавиатуре.
        if msg.chat.type == "private":
            kb = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="🧾 Разделить чек", web_app=WebAppInfo(url=webapp_url))]],
                resize_keyboard=True,
                one_time_keyboard=True,
                input_field_placeholder="Откройте мини‑приложение"
            )
            await msg.answer(
                "Нажмите кнопку ниже, чтобы открыть мини‑приложение для распределения покупок.",
                reply_markup=kb
            )
        else:
            # В группах reply‑кнопки с WebApp не поддерживаются, поэтому используем deep‑link.
            # Если задано имя бота, формируем ссылку startapp; иначе открываем страницу WebApp напрямую.
            if settings.bot_username:
                payload = f"group_{msg.chat.id}"
                deep_link = f"https://t.me/{settings.bot_username}?startapp={payload}"
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🧾 Разделить чек", url=deep_link)]]
                )
                await msg.answer(
                    "Нажмите кнопку ниже, чтобы открыть мини‑приложение для распределения покупок.",
                    reply_markup=kb
                )
            else:
                # Если username не указан, просто отправляем ссылку на веб‑страницу
                link = webapp_url
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🧾 Разделить чек", url=link)]]
                )
                await msg.answer(
                    "Нажмите кнопку ниже, чтобы открыть мини‑приложение для распределения покупок.",
                    reply_markup=kb
                )
    except Exception as e:
        # Если не удалось сформировать кнопку, просто выводим сообщение об ошибке
        print(f"Ошибка при отправке кнопки WebApp: {e}")


# Обработчик данных, отправляемых из мини‑приложения. Aiogram предоставляет
# встроенный фильтр ``F.web_app_data``, который срабатывает, если объект
# ``Message`` содержит поле ``web_app_data``. Ранее использовалось
# лямбда‑выражение с ``getattr``, однако в некоторых версиях Aiogram оно
# некорректно отрабатывало и не вызывало хендлер, из‑за чего данные
# мини‑приложения не доходили до бота. Используем штатный фильтр для
# надёжной обработки.
@router.message(F.web_app_data)
async def handle_web_app_data(msg: Message):
    """
    Обработчик данных, присылаемых из WebApp. telegram.web_app_data.data содержит строку JSON,
    которую нужно распарсить. Предполагаем, что она имеет структуру
    {"selected": [0, 3, 5]} или {"selected": {index: quantity}} — индексы позиций, которые выбрал пользователь.
    Храним выбор в БД и сохраняем агрегированный список позиций по группе.
    """
    try:
        import json
        # Данные из мини‑приложения приходят в виде строки JSON. Сначала
        # распарсим их, чтобы достать структуру выбранных позиций. В случае
        # невалидного JSON выведем ошибку пользователю.
        raw_data = msg.web_app_data.data
        print(f"Received web_app_data: {raw_data}")
        data = json.loads(raw_data)
        selected_data = data.get("selected", {})
        indices: list[int] = []
        # Поддерживаем два формата передачи: список индексов (старый) и
        # словарь index → quantity (современный интерфейс). Для словаря
        # разворачиваем количество в список индексов, чтобы далее считать
        # стоимость пользователя по каждому выбранному товару.
        if isinstance(selected_data, dict):
            # В словаре qty может быть дробным значением. Сохраняем как float
            for idx_str, qty in selected_data.items():
                try:
                    idx = int(idx_str)
                    q_raw = float(qty)
                except Exception:
                    continue
                # Для совместимости со старым протоколом assignments
                # добавляем индекс int(q_raw) раз. Дробную часть не учитываем,
                # так как фактическое количество будет храниться в selected_positions.
                count = int(q_raw) if q_raw > 0 else 0
                for _ in range(count):
                    indices.append(idx)
        elif isinstance(selected_data, list):
            # Формат {selected: [0,1,2]}
            for i in selected_data:
                try:
                    indices.append(int(i))
                except Exception:
                    pass
        else:
            indices = []
        print(f"Received indices from WebApp: {indices}")
    except Exception as e:
        await msg.answer(f"Ошибка обработки данных из мини‑приложения: {e}")
        return
    # When the mini‑app is opened via a deep‑link in a group, the message
    # containing the selection is sent from the user's private chat. To
    # correctly associate the selection with the original group, we look
    # for a "group_id" field in the received data. If absent, fall back
    # to using the current chat ID (suitable for private chat usage).
    group_id = str(data.get("group_id") or msg.chat.id)
    receipt_id = group_id
    # Сохраняем выбор пользователя в in‑memory assignments для совместимости
    set_assignment(receipt_id, msg.from_user.id, indices)
    try:
        # Получаем список всех позиций в текущей группе
        all_positions = get_positions(str(group_id))
        selected_positions: list[dict] = []
        if isinstance(selected_data, dict):
            for idx_str, qty in selected_data.items():
                try:
                    idx = int(idx_str)
                    q_raw = float(qty)
                except Exception:
                    continue
                # Проверяем индекс и количество
                if 0 <= idx < len(all_positions) and q_raw > 0:
                    orig = all_positions[idx]
                    selected_positions.append({
                        "name": orig.get("name"),
                        "quantity": q_raw,
                        "price": orig.get("price"),
                    })
        else:
            for idx in indices:
                if 0 <= idx < len(all_positions):
                    orig = all_positions[idx]
                    selected_positions.append({
                        "name": orig.get("name"),
                        "quantity": 1.0,
                        "price": orig.get("price"),
                    })
        save_selected_positions(str(group_id), msg.from_user.id, selected_positions)
    except Exception as e:
        print(f"Ошибка при сохранении распределённых позиций: {e}")
    await msg.answer(
        "✅ Ваш выбор сохранён! Когда все участники отметят свои позиции, используйте /finalize для расчёта."
    )



@router.message(Command("show"))
async def show_positions(msg: Message):
    # Показываем позиции только для текущей группы и отображаем, кто какие позиции выбрал.
    group_id = str(msg.chat.id)
    positions = get_positions(group_id) or []
    if not positions:
        await msg.answer("Нет позиций! Сначала добавьте чеки.")
        return
    # Загружаем выбранные позиции, чтобы показать, кто что выбрал
    from app.database import get_selected_positions as _get_selected_positions
    selections = _get_selected_positions(group_id) or {}
    # Сгруппируем выбранные позиции по ключу (name, price) → список (user_name, quantity)
    selection_map: dict[tuple[str, float], list[tuple[str, float]]] = {}
    for uid, pos_list in selections.items():
        # Получаем имя пользователя для отображения
        user_info = get_user(uid) or {}
        user_name = user_info.get('full_name') or user_info.get('phone') or str(uid)
        for pos in pos_list:
            try:
                key = (pos.get('name'), float(pos.get('price', 0)))
                qty_raw = pos.get('quantity')
                qty = float(qty_raw) if qty_raw is not None else 0.0
            except Exception:
                continue
            selection_map.setdefault(key, []).append((user_name, qty))
    # Формируем текст для вывода
    lines: list[str] = []
    for idx, item in enumerate(positions):
        name = item.get('name')
        qty = item.get('quantity')
        price = item.get('price')
        lines.append(f"{idx+1}. {name} — {qty} × {price}₽")
        key = (name, float(price) if price is not None else 0.0)
        participants = selection_map.get(key)
        if participants:
            parts = []
            for uname, q in participants:
                # Округляем количество до двух знаков
                try:
                    q_disp = round(float(q), 2)
                except Exception:
                    q_disp = q
                parts.append(f"{uname} × {q_disp}")
            lines.append("<i>Выбрали: " + "; ".join(parts) + "</i>")
    kb = positions_keyboard(positions)
    await msg.answer("<b>Все позиции:</b>\n" + "\n".join(lines), parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("del_"))
async def delete_position(call: CallbackQuery):
    idx = int(call.data.replace("del_", ""))
    group_id = str(call.message.chat.id)
    positions = get_positions(group_id)
    if idx < 0 or idx >= len(positions):
        await call.answer("Ошибка удаления")
        return
    positions.pop(idx)
    set_positions(group_id, positions)
    await call.answer("Позиция удалена")
    # Обновить сообщение:
    # Формируем текст со списком оставшихся позиций. Используем индексы
    # из enumerate для корректной нумерации и обращаемся к ключам
    # словаря, так как позиция представлена как dict.
    text = "\n".join([
        f"{ix+1}. {p['name']} — {p['quantity']} x {p['price']}₽"
        for ix, p in enumerate(positions)
    ])
    kb = positions_keyboard(positions)
    await call.message.edit_text(f"<b>Все позиции:</b>\n{text}", parse_mode="HTML", reply_markup=kb)

# Аналогично реализуй edit_ и add_ коллбэки (edit — запросить новое значение, add — диалог ввода)
# Редактирование позиции — шаг 1 (запросить ввод)
@router.callback_query(F.data.startswith("edit_"))
async def edit_position(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.replace("edit_", ""))
    group_id = str(call.message.chat.id)
    positions = get_positions(group_id)
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
        group_id = str(msg.chat.id)
        positions = get_positions(group_id)
        positions[idx] = position
        set_positions(group_id, positions)
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
        group_id = str(msg.chat.id)
        positions = get_positions(group_id)
        positions.append(position)
        set_positions(group_id, positions)
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
    Выполняет финальный клиринг по текущему чеку.

    В новой версии расчёт основан только на выбранных вручную позициях и
    внесённых платежах. Распределение поровну и текстовый сценарий
    (через LLM) не используются. Итогом работы является список
    оптимальных переводов между участниками, который отправляется
    каждому пользователю в личные сообщения. В группу отправляется
    сводка переводов. Также сохраняется информация о долгах в таблицу
    debts и журнал платежей.
    """
    group_id = str(msg.chat.id)
    receipt_id = group_id
    # Проверяем, что есть позиции для расчёта
    positions = get_positions(group_id) or []
    if not positions:
        await msg.answer("Нет позиций для расчёта. Сначала отправьте чек.")
        return
    # Загружаем распределённые позиции и платежи
    from app.database import get_selected_positions as _get_selected_positions, get_payments
    selections = _get_selected_positions(group_id) or {}
    payments = get_payments(group_id) or {}
    # Если нет ни выбранных позиций, ни платежей, расчёт невозможен
    if not selections and not payments:
        await msg.answer("Нет данных для расчёта. Сначала распределите позиции или укажите платежи командой /pay.")
        return
    # Вычисляем оптимальные переводы на основе клиринга
    transfers = calculate_group_balance(group_id) or []
    # Подготовим отображение долгов для сохранения: должник → сумма
    debt_mapping: dict[int, float] = {}
    for debtor_id, creditor_id, amount in transfers:
        # Суммируем долг должника по всем переводам
        debt_mapping[debtor_id] = round(debt_mapping.get(debtor_id, 0.0) + float(amount), 2)
    # Сохраняем долги и логируем платёж (используем фиктивный ID транзакции)
    save_debts(receipt_id, debt_mapping)
    # Записываем в журнал. Используем фиктивный идентификатор, так как реального
    # массового платежа здесь не выполняется.
    fake_tx_id = "manual_clear"
    log_payment(receipt_id, fake_tx_id, debt_mapping)
    # Ссылка на группу для удобства пользователей. Для супергрупп Telegram использует
    # идентификаторы вида -100.... Чтобы построить ссылку, убираем префикс -100.
    group_link = ""
    try:
        chat_id_str = str(msg.chat.id)
        if chat_id_str.startswith("-100"):
            group_link = f"https://t.me/c/{chat_id_str[4:]}"
    except Exception:
        group_link = ""
    # Строим персональные сообщения для каждого участника. Собираем входящие и исходящие
    # переводы, чтобы пользователь видел, кому он должен и кто должен ему.
    user_transfers: dict[int, dict[str, list[tuple[int, float]]]] = {}
    for debtor_id, creditor_id, amount in transfers:
        user_transfers.setdefault(debtor_id, {"out": [], "in": []})
        user_transfers.setdefault(creditor_id, {"out": [], "in": []})
        user_transfers[debtor_id]["out"].append((creditor_id, float(amount)))
        user_transfers[creditor_id]["in"].append((debtor_id, float(amount)))
    # Отправляем каждому пользователю личное сообщение
    for uid, flows in user_transfers.items():
        messages: list[str] = []
        outgoing = flows.get("out", [])
        incoming = flows.get("in", [])
        for creditor_id, amount in outgoing:
            creditor_info = get_user(creditor_id) or {}
            creditor_name = creditor_info.get('full_name') or creditor_info.get('phone') or str(creditor_id)
            messages.append(f"Вы должны {amount}₽ пользователю {creditor_name}.")
        for debtor_id, amount in incoming:
            debtor_info = get_user(debtor_id) or {}
            debtor_name = debtor_info.get('full_name') or debtor_info.get('phone') or str(debtor_id)
            messages.append(f"{debtor_name} должен вам {amount}₽.")
        if not messages:
            messages.append("Ваш баланс нулевой. Нет обязательств.")
        if group_link:
            messages.append(f"Группа: {group_link}")
        try:
            await msg.bot.send_message(uid, "\n".join(messages))
        except Exception:
            pass
    # Формируем сводку для группового чата
    summary_lines: list[str] = ["💰 Клиринг завершён!"]
    if transfers:
        summary_lines.append("\n<b>Оптимальные переводы:</b>")
        for debtor_id, creditor_id, amount in transfers:
            debtor_info = get_user(debtor_id) or {}
            creditor_info = get_user(creditor_id) or {}
            debtor_name = debtor_info.get('full_name') or debtor_info.get('phone') or str(debtor_id)
            creditor_name = creditor_info.get('full_name') or creditor_info.get('phone') or str(creditor_id)
            summary_lines.append(f"{debtor_name} → {creditor_name}: {amount}₽")
        summary_lines.append("\nПодробности отправлены каждому участнику в личные сообщения.")
    else:
        summary_lines.append("\nВсе расчёты закрыты. Нет обязательств между участниками.")
    await msg.answer("\n".join(summary_lines), parse_mode="HTML")
    # Очищаем позиции, чтобы следующий расчёт был независим
    set_positions(group_id, [])


# ---------------------------------------------------------------------------
# Дополнительные команды для учёта платежей и расчёта баланса
# ---------------------------------------------------------------------------

@router.message(Command("pay"))
async def cmd_pay(msg: Message):
    """
    Записывает факт оплаты от пользователя.

    Использование:
      /pay <сумма> [описание]

    Пример: /pay 1500 Обед и напитки

    Сумма может быть десятичным числом (разделитель точки или запятая).
    Описание является необязательным и будет сохранено как текст.
    """
    group_id = str(msg.chat.id)
    user_id = msg.from_user.id
    # Проверяем, что пользователь зарегистрирован
    if get_user(user_id) is None:
        await msg.answer(
            "❗️Вы ещё не зарегистрированы. Напишите /start в личку боту, чтобы пройти регистрацию."
        )
        return
    # Разбираем аргументы команды
    text = msg.text or ""
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        # Убираем угловые скобки, чтобы избежать ошибки парсинга HTML от Telegram
        await msg.answer(
            "Использование: /pay сумма [описание позиций]"
        )
        return
    amount_str = parts[1].replace(",", ".")
    try:
        amount = float(amount_str)
    except Exception:
        await msg.answer("Сумма должна быть числом. Пример: /pay 100.50")
        return
    description = parts[2] if len(parts) >= 3 else None
    try:
        add_payment(group_id, user_id, amount, description)
        await msg.answer(f"✅ Платёж на сумму {amount}₽ зарегистрирован.")
    except Exception as e:
        await msg.answer(f"Ошибка при сохранении платежа: {e}")


@router.message(Command("unassigned"))
async def cmd_unassigned(msg: Message):
    """
    Показывает список позиций из чека, которые ещё не были выбраны ни одним участником.

    Команда полезна для контроля распределения: если остались строки, их
    необходимо распределить, иначе баланс может быть рассчитан некорректно.
    """
    group_id = str(msg.chat.id)
    unassigned = get_unassigned_positions(group_id) or []
    if not unassigned:
        await msg.answer("Все позиции распределены.")
        return
    lines: list[str] = ["<b>Неразделённые позиции:</b>"]
    for pos in unassigned:
        try:
            name = pos.get("name")
            qty = pos.get("quantity")
            price = pos.get("price")
            lines.append(f"{name} ({qty} × {price}₽)")
        except Exception:
            pass
    await msg.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("balance"))
async def cmd_balance(msg: Message):
    """
    Рассчитывает баланс группы и предлагает оптимальные переводы между участниками.

    Баланс считается на основе выбранных позиций (selected_positions) и
    внесённых платежей (payments). Для каждого участника выводится,
    сколько он потратил по выбранным позициям, сколько внёс платежей и
    итоговый баланс. Далее перечисляются переводы: кто, кому и сколько
    должен перевести, чтобы закрыть долги.
    """
    group_id = str(msg.chat.id)
    # Загружаем данные
    selections = get_selected_positions(group_id) or {}
    payments_map = get_payments(group_id) or {}
    # Если нет данных ни о позициях, ни о платежах
    if not selections and not payments_map:
        await msg.answer("Нет данных для расчёта баланса. Сначала распределите позиции или внесите платежи.")
        return
    # Формируем cost_map: пользователь → сумма по выбранным позициям
    cost_map: dict[int, float] = {}
    for uid, pos_list in selections.items():
        total = 0.0
        for pos in pos_list:
            try:
                qty = float(pos.get("quantity", 0))
                price = float(pos.get("price", 0))
                total += qty * price
            except Exception:
                pass
        cost_map[uid] = round(total, 2)
    # Список всех участников
    users = set(cost_map.keys()) | set(payments_map.keys())
    # Строим строки с балансом по каждому
    lines: list[str] = ["<b>Баланс группы:</b>"]
    for uid in users:
        spent = cost_map.get(uid, 0.0)
        paid = payments_map.get(uid, 0.0)
        diff = round(paid - spent, 2)
        user_info = get_user(uid) or {}
        name = user_info.get('full_name') or user_info.get('phone') or str(uid)
        sign = "+" if diff >= 0 else ""
        lines.append(f"{name} ({uid}): потратил {spent}₽, оплатил {paid}₽ → баланс {sign}{diff}₽")
    # Получаем оптимальные переводы
    transfers = calculate_group_balance(group_id)
    if transfers:
        lines.append("\n<b>Оптимальные переводы:</b>")
        for debtor_id, creditor_id, amount in transfers:
            debtor_info = get_user(debtor_id) or {}
            creditor_info = get_user(creditor_id) or {}
            debtor_name = debtor_info.get('full_name') or debtor_info.get('phone') or str(debtor_id)
            creditor_name = creditor_info.get('full_name') or creditor_info.get('phone') or str(creditor_id)
            lines.append(f"{debtor_name} → {creditor_name}: {amount}₽")
    else:
        lines.append("\nВсе расчёты закрыты. Нет обязательств между участниками.")
    await msg.answer("\n".join(lines), parse_mode="HTML")

# ---------------------------------------------------------------------------
# Команды для просмотра содержимого таблиц базы данных
# ---------------------------------------------------------------------------

def _format_rows(columns: list[str], rows: list[sqlite3.Row]) -> str:
    """
    Вспомогательная функция для форматирования выборки из БД в строку.

    Args:
        columns: список названий столбцов
        rows: список строк (sqlite3.Row)
    Returns:
        Готовую строку, где каждая строка представлена в формате key: value;
        разделитель между записями — перевод строки.
    """
    if not rows:
        return "(нет записей)"
    lines: list[str] = []
    for row in rows:
        parts = []
        for col in columns:
            parts.append(f"{col}={row[col]}")
        lines.append(", ".join(parts))
    return "\n".join(lines)


@router.message(Command("accounts"))
async def cmd_show_accounts(msg: Message):
    """Показывает список записей в таблице accounts."""
    from app.database import get_db_connection
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, phone_number, full_name, telegram_login, bank, telegram_id FROM accounts")
    rows = cur.fetchall()
    conn.close()
    columns = ["id", "phone_number", "full_name", "telegram_login", "bank", "telegram_id"]
    text = _format_rows(columns, rows)
    await msg.answer(f"<b>Таблица accounts:</b>\n{text}", parse_mode="HTML")


@router.message(Command("positions_db"))
async def cmd_show_positions_db(msg: Message):
    """Показывает содержимое таблицы positions."""
    from app.database import get_db_connection
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, group_id, name, quantity, price FROM positions ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    columns = ["id", "group_id", "name", "quantity", "price"]
    text = _format_rows(columns, rows)
    await msg.answer(f"<b>Таблица positions:</b>\n{text}", parse_mode="HTML")


@router.message(Command("selected_positions_db"))
async def cmd_show_selected_positions_db(msg: Message):
    """Показывает содержимое таблицы selected_positions."""
    from app.database import get_db_connection
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, group_id, user_tg_id, position_id, quantity, price FROM selected_positions ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    columns = ["id", "group_id", "user_tg_id", "position_id", "quantity", "price"]
    text = _format_rows(columns, rows)
    await msg.answer(f"<b>Таблица selected_positions:</b>\n{text}", parse_mode="HTML")


@router.message(Command("payments_db"))
async def cmd_show_payments_db(msg: Message):
    """Показывает содержимое таблицы payments."""
    from app.database import get_db_connection
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, tg_user_id, group_id, amount, positions FROM payments ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    columns = ["id", "tg_user_id", "group_id", "amount", "positions"]
    text = _format_rows(columns, rows)
    await msg.answer(f"<b>Таблица payments:</b>\n{text}", parse_mode="HTML")


@router.message(Command("debts_db"))
async def cmd_show_debts_db(msg: Message):
    """Показывает содержимое таблицы debts."""
    from app.database import get_db_connection
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, receipt_id, user_tg_id, amount, created_at FROM debts ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    columns = ["id", "receipt_id", "user_tg_id", "amount", "created_at"]
    text = _format_rows(columns, rows)
    await msg.answer(f"<b>Таблица debts:</b>\n{text}", parse_mode="HTML")
