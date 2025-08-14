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

from services.llm_api import classify_message, classify_intent_llm
from database import (
    get_positions,
    start_text_session,
    append_text_message,
    end_text_session,
    TEXT_SESSIONS,
)
from database import get_user

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
        # Пользователь завершает ввод
        if any(word in lowered for word in ["закончен", "закончена", "завершил", "готово", "конец"]):
            messages = end_text_session(chat_id)
            await msg.answer("✅ Текстовый сбор сообщений завершён. Начинаю расчёт...")
            # Вызываем /finalize как будто это команда
            await finalize_receipt(msg)
        else:
            append_text_message(chat_id, text)
            await msg.answer("Сообщение учтено. Когда закончите, напишите 'расчёт закончен'.")
        return

    # --- Классификация запроса ---
    # Используем LLM для определения намерения. Этот вызов может
    # завершиться эвристикой, если LLM недоступен.
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
                "Добавьте меня в группу, чтобы делить чеки с друзьями."
            )
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
        # Предложить пользователю выбрать способ расчёта: мини‑приложение или текст
        kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.add(KeyboardButton(text="Мини-приложение"))
        kb.add(KeyboardButton(text="Текстовый ввод"))
        await msg.answer(
            "Как будем считать расходы? Выберите:\n- \U0001F4D1 Мини-приложение\n- \U0001F4D1 Текстовый ввод",
            reply_markup=kb,
        )
        # Помечаем, что ждём выбора пользователя
        TEXT_SESSIONS[chat_id] = {"collecting": False, "messages": [], "await_choice": True}
        return

    if intent == "delete_position":
        # Пока что не реализуем автоматическое удаление через естественный язык.
        # Просим пользователя воспользоваться командой /show и кнопками для удаления.
        await msg.answer(
            "Чтобы удалить позицию, отправьте /show и нажмите соответствующую кнопку ✖️ рядом с нужной позицией."
        )
        return

    if intent == "edit_position":
        # Аналогично, редактирование через НЛУ не поддержано. Предлагаем использовать кнопки.
        await msg.answer(
            "Чтобы изменить позицию или добавить новую, отправьте /show и воспользуйтесь кнопками '✏️' для редактирования и '➕' для добавления."
        )
        return

    if intent == "finalize":
        # Пользователь просит завершить расчёт
        await finalize_receipt(msg)
        return

    if intent == "help":
        await msg.answer(
            "Я могу помочь распределять расходы по чеку. Отправьте фото чека,\n"
            "нажмите кнопку мини‑приложения для выбора позиций или воспользуйтесь текстовым вводом.\n"
            "Доступные команды:\n"
            "/start — регистрация\n"
            "/show — показать позиции\n"
            "/finalize — завершить расчёт и разделить расходы."
        )
        return

    # --- Обработка выбора режима расчёта ---
    session_flags = TEXT_SESSIONS.get(chat_id, {})
    if session_flags.get("await_choice"):
        lowered = text.lower()
        if "мини" in lowered:
            # Если пользователь выбрал мини‑приложение, сообщаем как его открыть. Теперь можно
            # воспользоваться командой /split, чтобы получить кнопку, даже если чек был загружен ранее.
            await msg.answer(
                "Пожалуйста, нажмите команду /split или кнопку \U0001F4DD «Разделить чек» в сообщении бота, чтобы выбрать позиции в мини‑приложении.",
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

    # --- Неизвестный запрос ---
    await msg.answer(
        "Извините, я не понимаю. Напишите /help, чтобы узнать, что я умею."
    )