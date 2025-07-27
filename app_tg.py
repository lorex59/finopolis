# bot.py
import os
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from fastapi import FastAPI
from starlette.requests import Request
from aiogram.utils.web_app import safe_parse_webapp_init_data

API_TOKEN = os.getenv("BOT_TOKEN")
BACKEND_URL = os.getenv("BACKEND_URL")  # например, https://your-backend.com/api/receipts

logging.basicConfig(level=logging.INFO)
bot = Bot(API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Шаги FSM
class Profile(StatesGroup):
    fio = State()
    phone = State()
    bank = State()

banks = ["Сбербанк", "Тинькофф", "ВТБ"]

@dp.message(CommandStart())
async def cmd_start(msg: types.Message, state: FSMContext):
    await msg.answer("Привет! Давай зарегистрируем тебя.\nКак тебя зовут?")
    await state.set_state(Profile.fio)

@dp.message(Profile.fio)
async def proc_fio(msg: types.Message, state: FSMContext):
    await state.update_data(fio=msg.text)
    await msg.answer("Укажи номер телефона")
    await state.set_state(Profile.phone)

@dp.message(Profile.phone)
async def proc_phone(msg: types.Message, state: FSMContext):
    await state.update_data(phone=msg.text)
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for b in banks:
        kb.add(b)
    await msg.answer("Выбери банк", reply_markup=kb)
    await state.set_state(Profile.bank)

@dp.message(Profile.bank)
async def proc_bank(msg: types.Message, state: FSMContext):
    await state.update_data(bank=msg.text)
    data = await state.get_data()
    # Сохранение в БД здесь (запрос к backend)
    await msg.answer(f"Спасибо! {data['fio']}, {data['phone']}, {data['bank']}")
    await state.clear()

@dp.message(content_types=types.ContentType.PHOTO)
async def handle_photo(msg: types.Message):
    file = await msg.photo[-1].get_file()
    photo_bytes = await file.download()
    # Отправка на backend
    import httpx
    r = httpx.post(BACKEND_URL, files={"file": photo_bytes})
    await msg.answer("Чек отправлен на обработку")

# Web App
@dp.message()
async def webapp_data_handler(msg: types.Message):
    if msg.web_app_data:
        data = msg.web_app_data.data  # строка JSON
        # обработка данных: сохранить позиции
        await msg.answer(f"Вы выбрали: {data}")

# Запуск бота в Long Polling
def start_bot():
    dp.run_polling(bot)

# Backend на FastAPI для Web App
app = FastAPI()

@app.post("/webapp/auth")
async def webapp_auth(request: Request):
    form = await request.form()
    data = form["_auth"]
    user = safe_parse_webapp_init_data(data)  # валидация и получение user_data
    return {"user": user.to_python()}

if __name__ == "__main__":
    start_bot()
