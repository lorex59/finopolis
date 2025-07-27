"""
Middleware: проверяем, зарегистрирован ли пользователь.
"""
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from typing import Callable, Dict, Any, Awaitable

from database import get_user


class AuthRequiredMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        # Разрешить личные чаты без регистрации
        if event.chat and event.chat.type == "private":
            return await handler(event, data)

        # Разрешить /start или другие команды без регистрации
        if getattr(event, "text", "").startswith("/start"):
            return await handler(event, data)

        # Проверка регистрации
        user = get_user(event.from_user.id)
        if user is None:
            await event.answer("Сначала пройдите регистрацию в личном чате с ботом.")
            return

        return await handler(event, data)
