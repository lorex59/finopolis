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
    get_all_users,
    save_debts,
    save_selected_positions,
    get_selected_positions,
    log_payment,
    get_user
)
from keyboards import positions_keyboard
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from config import settings

from utils import parse_position


# Импортируем новые функции для платежей и расчёта баланса
from app.database import (
    add_payment,
    get_payments,
    calculate_group_balance,
    get_unassigned_positions,
    init_assignments,
    TEXT_SESSIONS,
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
    Красивый форматированный вывод распределённых позиций по пользователям.

    Формат строки:
      Название — qty × price₽ - total₽
    Если quantity == -1 (или < 0) — позицию делят поровну:
      Название — поровну - price₽
    """
    group_id = str(msg.chat.id)
    selections = get_selected_positions(group_id)
    if not selections:
        await msg.answer("❗️Позиции ещё не распределены. Откройте мини-приложение через /split и отметьте свои покупки.")
        return

    def fmt_money(x: float | int | None) -> str:
        try:
            v = float(x or 0)
            s = f"{v:.2f}".rstrip("0").rstrip(".")
            return f"{s}₽"
        except Exception:
            return f"{x}₽" if x is not None else "—"

    def fmt_qty(q: float | int | None) -> str:
        try:
            v = float(q or 0)
            if abs(v - int(v)) < 1e-9:
                return str(int(v))
            # 1 знак после запятой, если хватает точности, иначе до 2-х
            s = f"{v:.2f}".rstrip("0")
            if s.endswith("."):
                s = s[:-1]
            return s
        except Exception:
            return str(q)

    lines: list[str] = ["<b>Распределение позиций:</b>"]
    # Чтобы вывод был стабильным – сортируем пользователей по ФИО/логину
    for user_id in sorted(selections.keys(), key=lambda uid: (get_user(uid) or {}).get("full_name", str(uid))):
        u = get_user(user_id) or {}
        full_name = u.get("full_name") or f"ID {user_id}"
        login = u.get("telegram_login")
        login_part = f" (@{login})" if login and not str(login).startswith("@") else (f" ({login})" if login else "")
        lines.append(f"<b>{full_name}{login_part}:</b>")

        # список позиций данного пользователя
        pos_list = selections.get(user_id) or []
        if not pos_list:
            lines.append("—")
            continue

        for p in pos_list:
            name = p.get("name")
            qty = p.get("quantity")
            price = p.get("price")
            try:
                qv = float(qty) if qty is not None else 0.0
            except Exception:
                qv = 0.0
            # «поровну»
            if qv < 0 or qv == -1:
                lines.append(f"{name} — поровну - {fmt_money(price)}")
            else:
                total = (float(price or 0) * float(qv or 0))
                lines.append(f"{name} — {fmt_qty(qv)} × {fmt_money(price)} - {fmt_money(total)}")

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
    """
    Показывает список позиций и отображает, кто какие позиции выбрал.

    Для каждой позиции отображается её название, количество и цена. Под
    каждой позицией выводится список участников, выбравших её, с
    указанием количества. Если никто не выбрал позицию, строка
    «Выбрали…» опускается.
    """
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
        user_info = get_user(uid) or {}
        user_name = user_info.get('full_name') or user_info.get('phone') or str(uid)
        for pos in pos_list:
            try:
                key = (pos.get('name'), float(pos.get('price', 0)))
                qty_raw = pos.get('quantity')
                qty = float(qty_raw) if qty_raw is not None else 0.0
            except Exception:
                continue
            # Не отображаем количество для отметок «поровну» (отрицательные значения)
            if qty < 0:
                q_display = 'поровну'
            else:
                try:
                    q_display = round(qty, 2)
                except Exception:
                    q_display = qty
            selection_map.setdefault(key, []).append((user_name, q_display))
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
            parts: list[str] = []
            for uname, q in participants:
                parts.append(f"{uname} × {q}")
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
    debts и журнал платежей. После завершения расчёта данные чеков,
    выборов и платежей переносятся в архив и удаляются из рабочих
    таблиц.
    """
    group_id = str(msg.chat.id)
    receipt_id = group_id
    # Проверяем, что есть позиции для расчёта
    positions = get_positions(group_id) or []
    if not positions:
        await msg.answer("Нет позиций для расчёта. Сначала отправьте чек.")
        return
    # Загружаем распределённые позиции и платежи
    from app.database import get_selected_positions as _get_selected_positions, get_payments, archive_group_data, clear_group_data
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
        debt_mapping[debtor_id] = round(debt_mapping.get(debtor_id, 0.0) + float(amount), 2)
    # Сохраняем долги и логируем платёж (используем фиктивный ID транзакции)
    save_debts(receipt_id, debt_mapping)
    fake_tx_id = "manual_clear"
    log_payment(receipt_id, fake_tx_id, debt_mapping)
    # Ссылка на группу для удобства пользователей. Если у группы есть username,
    # используем https://t.me/<username>. Если это супергруппа (chat_id
    # начинается с -100), используем формат https://t.me/c/<id без -100>.
    group_link = ""
    try:
        chat_username = getattr(msg.chat, "username", None)
        if chat_username:
            group_link = f"https://t.me/{chat_username}"
        else:
            # create_chat_invite_link требует права администратора 'can_invite_users'
            invite = await msg.bot.create_chat_invite_link(
                chat_id=msg.chat.id,
                name="Разделить чек",
                creates_join_request=False
            )
            group_link = invite.invite_link or ""
    except Exception:
        # Если прав нет — просто не добавляем ссылку (лучше, чем битая t.me/c/...)
        group_link = ""
    # Строим персональные сообщения для каждого участника. Собираем входящие и исходящие
    # переводы, чтобы пользователь видел, кому он должен и кто должен ему.
    user_transfers: dict[int, dict[str, list[tuple[int, float]]]] = {}
    for debtor_id, creditor_id, amount in transfers:
        user_transfers.setdefault(debtor_id, {"out": [], "in": []})
        user_transfers.setdefault(creditor_id, {"out": [], "in": []})
        user_transfers[debtor_id]["out"].append((creditor_id, float(amount)))
        user_transfers[creditor_id]["in"].append((debtor_id, float(amount)))
    
    # Вычисляем баланс (платежи - стоимость) для каждого участника.
    # Для корректного расчёта стоимости учтём ручные выборы и отметки «поровну».
    balances_map: dict[int, float] = {}
    from app.database import get_db_connection
    try:
        conn2 = get_db_connection()
        cur2 = conn2.cursor()
        # Загружаем исходные позиции
        cur2.execute(
            "SELECT id, quantity, price FROM positions WHERE group_id = ?",
            (str(group_id),),
        )
        pos_rows2 = cur2.fetchall()
        pos_map2: dict[int, dict[str, float]] = {}
        for row in pos_rows2:
            try:
                pid = row["id"]
                pos_map2[pid] = {
                    "quantity": float(row["quantity"] or 0),
                    "price": float(row["price"] or 0),
                }
            except Exception:
                pass
        # Загружаем выбранные позиции
        cur2.execute(
            "SELECT user_tg_id, position_id, quantity, price FROM selected_positions WHERE group_id = ?",
            (str(group_id),),
        )
        sp_rows2 = cur2.fetchall()
        selections_by_pos2: dict[int | None, list[tuple[int, float, float]]] = {}
        for row in sp_rows2:
            try:
                uid_raw = row["user_tg_id"]
                uid_int2 = int(uid_raw) if uid_raw is not None else None
            except Exception:
                uid_int2 = None
            if uid_int2 is None:
                continue
            pos_id2 = row["position_id"]
            qty_raw2 = row["quantity"]
            try:
                qv2 = float(qty_raw2 or 0)
            except Exception:
                qv2 = 0.0
            pval2 = None
            try:
                pval2 = float(row["price"] or 0)
            except Exception:
                pval2 = 0.0
            selections_by_pos2.setdefault(pos_id2, []).append((uid_int2, qv2, pval2))
        cost_map2: dict[int, float] = {}
        for pos_id2, sel_list2 in selections_by_pos2.items():
            if pos_id2 is not None and pos_id2 in pos_map2:
                orig2 = pos_map2[pos_id2]
                total_qty2 = float(orig2.get("quantity", 0))
                price2 = float(orig2.get("price", 0))
                manual_sum2 = 0.0
                equal_users2: list[int] = []
                for uid_int2, qty_val2, _p2 in sel_list2:
                    if qty_val2 is None:
                        continue
                    try:
                        qvtmp = float(qty_val2)
                    except Exception:
                        qvtmp = 0.0
                    if qvtmp >= 0:
                        manual_sum2 += qvtmp
                    else:
                        equal_users2.append(uid_int2)
                # Ручной расход
                for uid_int2, qty_val2, _p2 in sel_list2:
                    if qty_val2 is None:
                        continue
                    try:
                        qvtmp = float(qty_val2)
                    except Exception:
                        qvtmp = 0.0
                    if qvtmp > 0:
                        cost_map2[uid_int2] = cost_map2.get(uid_int2, 0.0) + qvtmp * price2
                leftover2 = max(total_qty2 - manual_sum2, 0.0)
                if equal_users2:
                    share2 = leftover2 / len(equal_users2) if len(equal_users2) > 0 else 0.0
                    for uid_int2 in equal_users2:
                        cost_map2[uid_int2] = cost_map2.get(uid_int2, 0.0) + share2 * price2
            else:
                for uid_int2, qty_val2, price_val2 in sel_list2:
                    try:
                        qvtmp = float(qty_val2)
                    except Exception:
                        qvtmp = 0.0
                    try:
                        pvt2 = float(price_val2)
                    except Exception:
                        pvt2 = 0.0
                    if qvtmp > 0:
                        cost_map2[uid_int2] = cost_map2.get(uid_int2, 0.0) + qvtmp * pvt2
        # Теперь вычисляем балансы: оплачено - потрачено
        all_bal_users2 = set(cost_map2.keys()) | set(payments.keys())
        for uid in all_bal_users2:
            paid_amt = payments.get(uid, 0.0)
            spent_amt = cost_map2.get(uid, 0.0)
            balances_map[uid] = round(paid_amt - spent_amt, 2)
    except Exception:
        # Fallback: если что-то сломалось, попробуем простой расчёт
        balances_map = {}
        try:
            cost_map_simple: dict[int, float] = {}
            for _uid, _plist in selections.items():
                total_cost_simple = 0.0
                for _pos in _plist:
                    try:
                        qtmp = float(_pos.get('quantity', 0))
                        ptmp = float(_pos.get('price', 0))
                        total_cost_simple += qtmp * ptmp
                    except Exception:
                        pass
                cost_map_simple[int(_uid)] = round(total_cost_simple, 2)
            all_users_simple = set(cost_map_simple.keys()) | set(payments.keys())
            for _uid in all_users_simple:
                paid = payments.get(_uid, 0.0)
                spent = cost_map_simple.get(_uid, 0.0)
                balances_map[_uid] = round(paid - spent, 2)
        except Exception:
            balances_map = {}
    finally:
        try:
            conn2.close()
        except Exception:
            pass
    # Определяем полный список участников: те, кто выбрал позиции или внёс платежи.
    all_user_ids: set[int] = set(selections.keys()) | set(payments.keys())
    # Отправляем каждому пользователю личное сообщение. Если пользователь не участвовал
    # в переводах (у него нулевой баланс), всё равно уведомим его об отсутствии
    # обязательств.
    for uid in all_user_ids:
        flows = user_transfers.get(uid, {"out": [], "in": []})
        messages: list[str] = []
        # Исходящие переводы (должен)
        for creditor_id, amount in flows.get("out", []):
            creditor_info = get_user(creditor_id) or {}
            creditor_name = creditor_info.get('full_name') or creditor_info.get('phone') or str(creditor_id)
            messages.append(f"Вы должны {amount}₽ пользователю {creditor_name}.")
        # Входящие переводы (вам должны)
        for debtor_id, amount in flows.get("in", []):
            debtor_info = get_user(debtor_id) or {}
            debtor_name = debtor_info.get('full_name') or debtor_info.get('phone') or str(debtor_id)
            messages.append(f"{debtor_name} должен вам {amount}₽.")
        if not messages:
            bal = balances_map.get(uid, 0.0)
            if abs(bal) > 0.01:
                sign = '+' if bal > 0 else ''
                messages.append(f'Ваш баланс {sign}{bal}₽. Нет обязательств.')
            else:
                messages.append('Ваш баланс нулевой. Нет обязательств.')
        if group_link:
            messages.append(f"Группа: {group_link}")
        try:
            await msg.bot.send_message(uid, "\n".join(messages))
        except Exception:
            # Игнорируем ошибку отправки (например, если пользователь запретил ЛС)
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
        # Добавляем информацию о балансе каждого участника. Для расчёта
        # используем ту же логику стоимости, что и в calculate_group_balance.
        try:
            from app.database import get_db_connection
            conn_rep = get_db_connection()
            cur_rep = conn_rep.cursor()
            # Сформируем карту исходных позиций
            cur_rep.execute(
                "SELECT id, quantity, price FROM positions WHERE group_id = ?",
                (str(group_id),),
            )
            pos_rows_rep = cur_rep.fetchall()
            positions_map_rep: dict[int, dict[str, float]] = {}
            for row in pos_rows_rep:
                try:
                    positions_map_rep[row["id"]] = {
                        "quantity": float(row["quantity"] or 0),
                        "price": float(row["price"] or 0),
                    }
                except Exception:
                    pass
            # Выборки
            cur_rep.execute(
                "SELECT user_tg_id, position_id, quantity, price FROM selected_positions WHERE group_id = ?",
                (str(group_id),),
            )
            sel_rows_rep = cur_rep.fetchall()
            selections_by_pos_rep: dict[int | None, list[tuple[int, float, float]]] = {}
            for row in sel_rows_rep:
                try:
                    uid_raw = row["user_tg_id"]
                    uid_int_rep = int(uid_raw) if uid_raw is not None else None
                except Exception:
                    uid_int_rep = None
                if uid_int_rep is None:
                    continue
                pid_rep = row["position_id"]
                qty_rep = 0.0
                try:
                    qty_rep = float(row["quantity"] or 0)
                except Exception:
                    qty_rep = 0.0
                price_rep = 0.0
                try:
                    price_rep = float(row["price"] or 0)
                except Exception:
                    price_rep = 0.0
                selections_by_pos_rep.setdefault(pid_rep, []).append((uid_int_rep, qty_rep, price_rep))
            report_cost_map: dict[int, float] = {}
            for pid_rep, sel_list_rep in selections_by_pos_rep.items():
                if pid_rep is not None and pid_rep in positions_map_rep:
                    orig_rep = positions_map_rep[pid_rep]
                    total_qty_rep = float(orig_rep.get("quantity", 0))
                    price_val_rep = float(orig_rep.get("price", 0))
                    manual_sum_rep = 0.0
                    equal_users_rep: list[int] = []
                    for uid_int_rep, qty_val_rep, _pr in sel_list_rep:
                        if qty_val_rep is None:
                            continue
                        try:
                            q_tmp_rep = float(qty_val_rep)
                        except Exception:
                            q_tmp_rep = 0.0
                        if q_tmp_rep >= 0:
                            manual_sum_rep += q_tmp_rep
                        else:
                            equal_users_rep.append(uid_int_rep)
                    # manual
                    for uid_int_rep, qty_val_rep, _pr in sel_list_rep:
                        if qty_val_rep is None:
                            continue
                        try:
                            q_tmp_rep = float(qty_val_rep)
                        except Exception:
                            q_tmp_rep = 0.0
                        if q_tmp_rep > 0:
                            report_cost_map[uid_int_rep] = report_cost_map.get(uid_int_rep, 0.0) + q_tmp_rep * price_val_rep
                    # поровну
                    leftover_rep = max(total_qty_rep - manual_sum_rep, 0.0)
                    if equal_users_rep:
                        share_rep = leftover_rep / len(equal_users_rep) if len(equal_users_rep) > 0 else 0.0
                        for uid_int_rep in equal_users_rep:
                            report_cost_map[uid_int_rep] = report_cost_map.get(uid_int_rep, 0.0) + share_rep * price_val_rep
                else:
                    for uid_int_rep, qty_val_rep, price_val_rep2 in sel_list_rep:
                        try:
                            q_tmp_rep = float(qty_val_rep)
                        except Exception:
                            q_tmp_rep = 0.0
                        try:
                            price_val_rep3 = float(price_val_rep2)
                        except Exception:
                            price_val_rep3 = 0.0
                        if q_tmp_rep > 0:
                            report_cost_map[uid_int_rep] = report_cost_map.get(uid_int_rep, 0.0) + q_tmp_rep * price_val_rep3
            # Все участники, участвовавшие в выборе или оплате
            all_rep_users = set(report_cost_map.keys()) | set(payments.keys())
            if all_rep_users:
                summary_lines.append("\n<b>Баланс группы:</b>")
            for _u in all_rep_users:
                spent = round(report_cost_map.get(_u, 0.0), 2)
                paid = round(payments.get(_u, 0.0), 2)
                diff = round(paid - spent, 2)
                u_info = get_user(_u) or {}
                u_name = u_info.get('full_name') or u_info.get('phone') or str(_u)
                sign = '+' if diff >= 0 else ''
                summary_lines.append(f"{u_name} ({_u}): потратил {spent}₽, оплатил {paid}₽ → баланс {sign}{diff}₽")
            try:
                conn_rep.close()
            except Exception:
                pass
        except Exception:
            pass
    await msg.answer("\n".join(summary_lines), parse_mode="HTML")
    # Архивируем данные и очищаем рабочие таблицы для группы.
    # Даже если архивирование или очистка завершатся с ошибкой,
    # мы продолжим выполнение. Для надёжности выполняем операции
    # последовательно и не оборачиваем их в общий try. Если
    # возникнет исключение, оно будет выведено в логи, но не
    # прервёт оставшийся код.
    try:
        archive_group_data(group_id)
    except Exception as arch_err:
        try:
            print(f"Ошибка архивации данных для группы {group_id}: {arch_err}")
        except Exception:
            pass
    try:
        clear_group_data(group_id)
    except Exception as clear_err:
        try:
            print(f"Ошибка очистки данных для группы {group_id}: {clear_err}")
        except Exception:
            pass

    # Также сбрасываем временные структуры для данного чата: ASSIGNMENTS и TEXT_SESSIONS.
    try:
        init_assignments(group_id)
        # Удаляем любую текущую сессию текстового сбора
        TEXT_SESSIONS.pop(group_id, None)
    except Exception:
        pass


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
    # -------------------------------------------------------------------
    # Формируем cost_map: пользователь → сумма по выбранным позициям.
    # Учитываем как ручные выборы, так и отметки «поровну». Для
    # корректного расчёта нам нужно знать исходное количество и цену
    # каждой позиции, поэтому делаем прямой запрос к таблицам positions
    # и selected_positions, аналогичный логике calculate_group_balance.
    from app.database import get_db_connection
    cost_map: dict[int, float] = {}
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Загружаем исходные позиции
        cur.execute(
            "SELECT id, quantity, price FROM positions WHERE group_id = ?",
            (str(group_id),),
        )
        pos_rows = cur.fetchall()
        positions_map: dict[int, dict[str, float]] = {}
        for row in pos_rows:
            try:
                pid = row["id"]
                qval = float(row["quantity"] or 0)
                pval = float(row["price"] or 0)
                positions_map[pid] = {"quantity": qval, "price": pval}
            except Exception:
                pass
        # Загружаем выбранные позиции
        cur.execute(
            "SELECT user_tg_id, position_id, quantity, price FROM selected_positions WHERE group_id = ?",
            (str(group_id),),
        )
        sp_rows = cur.fetchall()
        selections_by_pos: dict[int | None, list[tuple[int, float, float]]] = {}
        for row in sp_rows:
            try:
                uid_raw = row["user_tg_id"]
                uid_int = int(uid_raw) if uid_raw is not None else None
            except Exception:
                uid_int = None
            if uid_int is None:
                continue
            pos_id = row["position_id"]
            qty_raw = row["quantity"]
            try:
                qty_val = float(qty_raw or 0)
            except Exception:
                qty_val = 0.0
            price_val = None
            try:
                price_val = float(row["price"] or 0)
            except Exception:
                price_val = 0.0
            selections_by_pos.setdefault(pos_id, []).append((uid_int, qty_val, price_val))
        # Расчёт стоимости по каждому выбору
        for pos_id, sel_list in selections_by_pos.items():
            if pos_id is not None and pos_id in positions_map:
                orig = positions_map[pos_id]
                total_qty = float(orig.get("quantity", 0))
                price = float(orig.get("price", 0))
                manual_sum = 0.0
                equal_users: list[int] = []
                for uid_int, qty_val, _p in sel_list:
                    if qty_val is None:
                        continue
                    try:
                        qv = float(qty_val)
                    except Exception:
                        qv = 0.0
                    if qv >= 0:
                        manual_sum += qv
                    else:
                        equal_users.append(uid_int)
                # Стоимость по ручным количествам
                for uid_int, qty_val, _p in sel_list:
                    if qty_val is None:
                        continue
                    try:
                        qv = float(qty_val)
                    except Exception:
                        qv = 0.0
                    if qv > 0:
                        cost_map[uid_int] = cost_map.get(uid_int, 0.0) + qv * price
                # Стоимость по поровну
                leftover = max(total_qty - manual_sum, 0.0)
                if equal_users:
                    share = leftover / len(equal_users) if len(equal_users) > 0 else 0.0
                    for uid_int in equal_users:
                        cost_map[uid_int] = cost_map.get(uid_int, 0.0) + share * price
            else:
                # Позиция не сопоставлена с исходной записью
                for uid_int, qty_val, price_val in sel_list:
                    try:
                        qv = float(qty_val)
                    except Exception:
                        qv = 0.0
                    try:
                        pv = float(price_val)
                    except Exception:
                        pv = 0.0
                    if qv > 0:
                        cost_map[uid_int] = cost_map.get(uid_int, 0.0) + qv * pv
    except Exception:
        # Если что‑то пошло не так, fallback: суммируем qty*price из selections
        cost_map = {}
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
    finally:
        try:
            conn.close()
        except Exception:
            pass
    # Список всех участников
    users = set(cost_map.keys()) | set(payments_map.keys())
    # Строим строки с балансом по каждому
    lines: list[str] = ["<b>Баланс группы:</b>"]
    for uid in users:
        spent_val = round(cost_map.get(uid, 0.0), 2)
        paid_val = round(payments_map.get(uid, 0.0), 2)
        diff = round(paid_val - spent_val, 2)
        user_info = get_user(uid) or {}
        name = user_info.get('full_name') or user_info.get('phone') or str(uid)
        sign = "+" if diff >= 0 else ""
        lines.append(f"{name} ({uid}): потратил {spent_val}₽, оплатил {paid_val}₽ → баланс {sign}{diff}₽")
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
