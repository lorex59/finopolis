
import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
import os

# Инициализируем логирование для отладки (Aiogram рекомендует настроить логгер)
logging.basicConfig(level=logging.INFO)

# Инициализация бота и диспетчера
BOT_TOKEN = os.getenv("TELEGRAM_API") # В продакшене лучше использовать переменную окружения
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

auth_data = {}      # auth_data[user_id] = {"name": ..., "phone": ..., "bank": ...}
receipt_data = {}   # receipt_data[chat_id] = {"positions": {...}, "payer_id": ..., "assignments": {...}, "awaiting_assign": False}
