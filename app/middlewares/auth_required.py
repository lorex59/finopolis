"""
Middleware: проверяем, зарегистрирован ли пользователь.
"""
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery

from database import get_user

class AuthRequiredMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        # Только для сообщений (можно аналогично для CallbackQuery)
        if isinstance(event, Message):
            user_id = event.from_user.id
            # Разрешаем всё в личных чатах
            if event.chat.type == "private":
                return await handler(event, data)
            # Проверяем регистрацию для группы
            user = get_user(user_id)
            if user is None:
                # В личку отправляем только если нет регистрации!
                try:
                    await event.bot.send_message(
                        chat_id=user_id,
                        text=(
                            "👋 Вы пытаетесь воспользоваться ботом в группе, "
                            "но ещё не зарегистрированы.\n"
                            "Напишите /start, "
                            "чтобы пройти регистрацию."
                        )
                    )
                except Exception:
                    pass  # возможно, пользователь не писал боту в личку (Telegram API restriction)
                # В группе отвечаем коротко, чтобы не спамить
                await event.reply("ℹ️ Для использования бота зарегистрируйтесь в личных сообщениях. Инструкция отправлена вам в личку.", reply=False)
                return  # Больше ничего не делаем
            # Если зарегистрирован — продолжаем
            return await handler(event, data)

        # Для CallbackQuery и других событий, если нужно — аналогичная логика
        return await handler(event, data)
