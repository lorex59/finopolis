"""
Natural language interaction handler.

This module defines a router that handles free‑form text messages. It
implements a very lightweight natural language understanding layer
based on keyword heuristics. Messages are classified into a small
number of intents via services.llm_api.classify_message(). Depending
on the intent and the current session state the appropriate action is
executed: greet the user, show current positions, start a
calculation, collect messages for a text‑based calculation or provide
help.

To integrate this router import it in bot.py and include via
dp.include_router(nlu_router).
"""

from aiogram import Router, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

from services.llm_api import (
    classify_message,
    classify_intent_llm,
    extract_items_from_text,
    extract_payment_from_text,
)
# Используем единый модуль базы данных из пакета ``app`` для работы с
# таблицами. Это предотвращает возникновение нескольких экземпляров
# ``database.py`` в разных местах проекта и гарантирует, что и бот, и
# мини‑приложение используют одну и ту же БД.
from app.database import (
    get_positions,
    start_text_session,
    append_text_message,
    end_text_session,
    TEXT_SESSIONS,
    get_user,
    add_positions,
    add_payment,
    get_all_users
)

# --- helper ---
def _resolve_username_to_user_id(username: str) -> int | None:
    """
    Возвращает telegram_id пользователя по его username (с @ или без).
    Сравнение без учёта регистра. Берём из колонки accounts.telegram_login.
    """
    if not username:
        return None
    uname = username.strip()
    if not uname:
        return None
    # нормализуем к виду "@name"
    if not uname.startswith("@"):
        uname = "@" + uname
    uname_cf = uname.casefold()

    for uid, data in get_all_users():
        tl = (data or {}).get("telegram_login")
        if not tl:
            continue
        tl_norm = tl if tl.startswith("@") else "@" + tl
        if tl_norm.casefold() == uname_cf:
            return int(uid)
    return None

from handlers.receipts import finalize_receipt

nlu_router = Router(name="nlu")


@nlu_router.message()
async def handle_nlu_message(msg: Message):
    """
    Точка входа для обработки свободного текста. Этот обработчик
    выполняется после всех команд и коллбэков. Сначала он проверяет,
    собираем ли мы сообщения для текстового расчёта. Если да, то
    добавляет сообщение в коллекцию или завершает сбор. В противном
    случае классифицирует запрос с помощью LLM и выполняет
    соответствующее действие.
    """
    chat_id = str(msg.chat.id)
    text = msg.text or ""
    session = TEXT_SESSIONS.get(chat_id)

    # --- Сбор сообщений для текстового сценария ---
    if session and session.get("collecting"):
        lowered = text.lower()
        if any(word in lowered for word in ["закончен", "закончена", "завершил", "готово", "конец"]):
            messages = end_text_session(chat_id)
            await msg.answer("✅ Текстовый сбор сообщений завершён. Начинаю расчёт...")
            await finalize_receipt(msg)
        else:
            append_text_message(chat_id, text)
            await msg.answer("Сообщение учтено. Когда закончите, напишите 'расчёт закончен'.")
        return

    # --- Классификация запроса ---
    intent = await classify_intent_llm(text)

    # --- Реакции на намерения ---
    if intent == "greet":
        user = get_user(msg.from_user.id)
        if user is None:
            await msg.answer(
                "Привет! Вы не зарегистрированы. Хотите пройти регистрацию? Напишите /start, чтобы начать.",
                reply_markup=ReplyKeyboardRemove(),
            )
        else:
            await msg.answer(
                f"Привет, {user.get('full_name', 'друг')}! Я — Разделятор. Могу помочь вам разделить покупки.\n"
                "Чтобы воспользоваться мной, необходимо зарегистрироваться, для этого напишите /start в личных сообщениях. Затем добавьте меня в группу и сделайте адмитистратором.\n"
                "После этого отправьте фото чека или воспользуйтесь текстовым вводом. Напишите /help, чтобы узнать, что я умею.",
            )
        return

    if intent == "add_position":
        try:
            items = await extract_items_from_text(text)
        except Exception:
            await msg.answer(
                "Не удалось распознать позиции. Попробуйте ещё раз или воспользуйтесь мини-приложением."
            )
            return
        if not items:
            await msg.answer(
                "Не удалось распознать позиции в сообщении. Попробуйте указать название и цену, например: 'такси за 300'."
            )
            return
        new_positions = []
        for item in items:
            try:
                new_positions.append({
                    "name": item.name,
                    "quantity": item.quantity,
                    "price": item.price,
                })
            except Exception:
                new_positions.append(item)
        group_id = str(msg.chat.id)
        add_positions(group_id, new_positions)
        lines = []
        for item in new_positions:
            name = item.get("name")
            quantity = item.get("quantity")
            price = item.get("price")
            price_str = f"{price:.0f}" if price == int(price) else f"{price:.2f}"
            qty_str = f"{quantity:.0f}" if quantity == int(quantity) else f"{quantity:.2f}"
            lines.append(f"{name} — {qty_str} x {price_str}₽")
        await msg.answer("Добавлены позиции:\n" + "\n".join(lines))
        return

    if intent == "list_positions":
        positions = get_positions()
        if not positions:
            await msg.answer("Нет позиций! Сначала добавьте чек.")
            return
        text_lines = [
            f"{idx+1}. {i['name']} — {i['quantity']} x {i['price']}₽" for idx, i in enumerate(positions)
        ]
        await msg.answer("\n".join(text_lines))
        return

    if intent == "calculate":
        kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.add(KeyboardButton(text="Мини-приложение"))
        kb.add(KeyboardButton(text="Текстовый ввод"))
        await msg.answer(
            "Как будем считать расходы? Выберите:\n- \U0001F4D1 Мини-приложение\n- \U0001F4D1 Текстовый ввод",
            reply_markup=kb,
        )
        TEXT_SESSIONS[chat_id] = {"collecting": False, "messages": [], "await_choice": True}
        return

    if intent == "delete_position":
        await msg.answer(
            "Чтобы удалить позицию, отправьте /show и нажмите соответствующую кнопку ✖️ рядом с нужной позицией."
        )
        return

    if intent == "edit_position":
        await msg.answer(
            "Чтобы изменить позицию или добавить новую, отправьте /show и воспользуйтесь кнопками '✏️' для редактирования и '➕' для добавления."
        )
        return

    if intent == "finalize":
        await finalize_receipt(msg)
        return

    if intent == "pay":
        try:
            payments = await extract_payment_from_text(text)
        except Exception:
            payments = []

        if payments:
            group_id = str(msg.chat.id)

            # Получим карту username -> telegram_id из базы
            # get_all_users() -> List[(telegram_id:int, data:dict{..., telegram_login:str})]
            login_to_id: dict[str, int] = {}
            try:
                for uid, data in get_all_users():
                    login = (data or {}).get("telegram_login")
                    if login:
                        login_to_id[login.lstrip("@").lower()] = int(uid)
            except Exception:
                # в худшем случае просто останется пустым
                pass

            lines_msgs: list[str] = []
            for p in payments:
                amt = float(p.get("amount") or 0)
                desc = p.get("description") or ""
                target_login = p.get("user_login")  # без '@', в нижнем регистре

                # Определяем пользователя-плательщика: либо из user_login, либо автор сообщения
                if target_login:
                    payer_id = login_to_id.get(target_login.lower())
                    if payer_id is None:
                        lines_msgs.append(f"⚠️ Не нашёл пользователя @{target_login} в базе — платёж {amt:.0f}₽ не сохранён.")
                        continue
                else:
                    payer_id = msg.from_user.id

                try:
                    add_payment(group_id, payer_id, amt, desc)
                    prefix = f"@{target_login}" if target_login else "Вы"
                    lines_msgs.append(f"✅ {prefix}: платёж {amt:.0f}₽ зарегистрирован.")
                except Exception as e:
                    lines_msgs.append(f"Ошибка при сохранении платежа на {amt:.0f}₽: {e}")

            await msg.answer("\n".join(lines_msgs))
        else:
            await msg.answer("Не удалось распознать сумму платежа. Укажите, сколько и кто заплатил. Пример: «Я заплатил 3000, @user — 4000».")
        return

    if intent == "help":
        await msg.answer(
            "Я могу помочь распределять расходы по чеку. Отправьте фото чека,\n"
            "нажмите кнопку мини-приложения для выбора позиций или воспользуйтесь текстовым вводом.\n"
            "Доступные команды:\n"
            "/start — регистрация\n"
            "/show — показать позиции\n"
            "/show_position — показать выбранные позиции\n"
            "/split - вызов Mini App для выбора позиций\n"
            "/finalize — завершить расчёт и разделить расходы."
        )
        return

    # --- Обработка выбора режима расчёта ---
    session_flags = TEXT_SESSIONS.get(chat_id, {})
    if session_flags.get("await_choice"):
        lowered = text.lower()
        if "мини" in lowered:
            await msg.answer(
                "Пожалуйста, нажмите команду /split или кнопку \U0001F4DD «Разделить чек» в сообщении бота, чтобы выбрать позиции в мини-приложении.",
                reply_markup=ReplyKeyboardRemove(),
            )
        elif "текст" in lowered:
            start_text_session(chat_id)
            await msg.answer(
                "Отлично! Теперь по одному сообщению перечисляйте, кто и что ел.\n"
                "Например: ‘@user1 ел пиццу и салат’, ‘@user2 — только суп’.\n"
                "Когда закончите, отправьте ‘расчёт закончен’.",
                reply_markup=ReplyKeyboardRemove(),
            )
        else:
            await msg.answer(
                "Не понял выбор. Пожалуйста, выберите ‘Мини-приложение’ или ‘Текстовый ввод’."
            )
        session_flags["await_choice"] = False
        return

    # --- Попытка распознать платёж в свободном тексте (fallback) ---
    payment_keywords = ["заплат", "оплат", "перевел", "перевёл", "потрат", "платеж", "платёж"]
    currency_markers = ["руб", "р", "₽"]
    lowered_text = text.lower()
    should_try_payment = any(k in lowered_text for k in payment_keywords) or any(c in lowered_text for c in currency_markers)

    if should_try_payment:
        try:
            payments = await extract_payment_from_text(text)
        except Exception:
            payments = []

        if payments:
            group_id = str(msg.chat.id)
            confirmations: list[str] = []

            for p in payments:
                amt = float(p.get("amount") or 0)
                desc = p.get("description")
                username = p.get("username")
                is_self = bool(p.get("is_self"))

                target_user_id: int | None = None
                target_label: str = ""

                if is_self or not username:
                    target_user_id = msg.from_user.id
                    me_info = get_user(target_user_id) or {}
                    target_label = me_info.get("full_name") or me_info.get("phone") or "Вы"
                else:
                    target_user_id = _resolve_username_to_user_id(username)
                    if target_user_id is None:
                        confirmations.append(
                            f"⚠️ Не нашёл пользователя {username} среди зарегистрированных. Пропустил платёж {amt}₽."
                        )
                        continue
                    user_info = get_user(target_user_id) or {}
                    target_label = user_info.get("full_name") or username

                try:
                    add_payment(group_id, target_user_id, amt, desc)
                    confirmations.append(f"✅ Зарегистрирован платёж {amt}₽ от {target_label}.")
                except Exception as e:
                    confirmations.append(f"Ошибка при сохранении платежа ({target_label}): {e}")

            await msg.answer("\n".join(confirmations))
            return

    await msg.answer(
        "Извините, я не понимаю. Напишите /help, чтобы узнать, что я умею."
    )