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

from services.llm_api import classify_message
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
    Entry point for natural language interaction. This handler runs
    after all command and callback handlers. It uses the current
    session state to either collect text for a calculation or to
    interpret the message as a new command. If an unknown command is
    detected a brief help message is returned.
    """
    chat_id = str(msg.chat.id)
    text = msg.text or ""
    session = TEXT_SESSIONS.get(chat_id)
    # If we are currently collecting messages for a text calculation, handle accordingly
    if session and session.get("collecting"):
        lowered = text.lower()
        # Finish the text session if the user says it's done
        if any(word in lowered for word in ["закончен", "закончена", "завершил", "готово", "конец"]):
            messages = end_text_session(chat_id)
            # Messages collected: trigger finalization
            await msg.answer("✅ Текстовый сбор сообщений завершён. Начинаю расчёт...")
            # We reuse the same /finalize handler to compute debts
            # by artificially calling finalize_receipt
            await finalize_receipt(msg)
        else:
            append_text_message(chat_id, text)
            await msg.answer("Сообщение учтено. Когда закончите, напишите 'расчёт закончен'.")
        return

    # Otherwise classify the message
    intent = classify_message(text)
    # Greeting intent
    if intent == "greet":
        user = get_user(msg.from_user.id)
        if user is None:
            await msg.answer(
                "Привет! Вы не зарегистрированы. Хотите пройти регистрацию? Напишите /start, чтобы начать.",
                reply_markup=ReplyKeyboardRemove(),
            )
        else:
            await msg.answer(
                f"Привет, {user.get('full_name', 'друг')}! Я — Разделятор. Могу помочь вам разделить покупки.\nДобавьте меня в группу, чтобы делить чеки с друзьями."
            )
        return
    # List positions
    if intent == "list_positions":
        positions = get_positions()
        if not positions:
            await msg.answer("Нет позиций! Сначала добавьте чек.")
            return
        text_lines = [f"{idx+1}. {i['name']} — {i['quantity']} x {i['price']}₽" for idx, i in enumerate(positions)]
        await msg.answer("\n".join(text_lines))
        return
    # Calculate debts
    if intent == "calculate":
        # Ask the user whether they want to use the mini app or text input
        kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.add(KeyboardButton(text="Мини-приложение"))
        kb.add(KeyboardButton(text="Текстовый ввод"))
        await msg.answer(
            "Как будем считать расходы? Выберите:\n- \U0001F4D1 Мини-приложение\n- \U0001F4D1 Текстовый ввод",
            reply_markup=kb,
        )
        # Next message from this user will determine the mode
        # We'll use the TEXT_SESSIONS dict to flag the next step
        # When user picks text mode we start collecting
        # We store the choice as part of the session
        TEXT_SESSIONS[chat_id] = {"collecting": False, "messages": [], "await_choice": True}
        return
    # Help
    if intent == "help":
        await msg.answer(
            "Я могу помочь распределять расходы по чеку. Отправьте фото чека, \n"
            "нажмите кнопку мини‑приложения для выбора позиций или воспользуйтесь текстовым вводом.\n"
            "Доступные команды:\n"
            "/start — регистрация\n"
            "/show — показать позиции\n"
            "/finalize — завершить расчёт и разделить расходы."
        )
        return
    # Unknown intent: maybe this is a reply to a choice prompt
    session_flags = TEXT_SESSIONS.get(chat_id, {})
    if session_flags.get("await_choice"):
        # Determine if user chose mini or text
        lowered = text.lower()
        if "мини" in lowered:
            # instruct user to press the webapp button
            await msg.answer(
                "Пожалуйста, нажмите кнопку \U0001F4DD ‘Разделить чек’ в предыдущем сообщении, чтобы выбрать позиции в мини‑приложении.",
                reply_markup=ReplyKeyboardRemove(),
            )
        elif "текст" in lowered:
            # begin text session
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
        # remove await_choice flag
        session_flags["await_choice"] = False
        return
    # Fallback response
    await msg.answer(
        "Извините, я не понимаю. Напишите /help, чтобы узнать, что я умею."
    )