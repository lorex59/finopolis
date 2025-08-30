"""
Сценарий регистрации: ФИО → телефон → банк.
"""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

from config import settings
from app.database import save_user

from app.database import get_all_users, get_user


router = Router(name="auth")


class AuthStates(StatesGroup):
    full_name = State()
    phone = State()
    bank = State()

@router.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await msg.answer("Привет! Давай зарегистрируемся.\n\nВведи своё *ФИО*:",
                     parse_mode="Markdown")
    await state.set_state(AuthStates.full_name)

@router.message(AuthStates.full_name, F.text.len() > 3)
async def process_name(msg: Message, state: FSMContext):
    await state.update_data(full_name=msg.text.strip())
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить телефон", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await msg.answer("Нажми кнопку ниже, чтобы отправить номер телефона.", reply_markup=kb)
    await state.set_state(AuthStates.phone)


@router.message(AuthStates.phone, F.contact)
async def process_phone(msg: Message, state: FSMContext):
    await state.update_data(phone=msg.contact.phone_number)
    bank_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=bank)] for bank in settings.allowed_banks],
        resize_keyboard=True, one_time_keyboard=True
    )
    await msg.answer("Выбери предпочитаемый банк для переводов:",
                     reply_markup=bank_kb)
    await state.set_state(AuthStates.bank)


@router.message(AuthStates.bank, F.text.in_(settings.allowed_banks))
async def process_bank(msg: Message, state: FSMContext):
    data = await state.get_data()        # получаем все данные из FSM
    user_id = msg.from_user.id

    # Проверка: если уже есть — не сохраняем дубликат
    if get_user(user_id) is None:
        # Данные из FSM + последний выбор банка
        data.update({"bank": msg.text})
        save_user(user_id, data)

    await msg.answer("✅ Регистрация завершена!", reply_markup=ReplyKeyboardRemove())
    await state.clear()



@router.message(Command("show_users"))
async def cmd_show_users(msg: Message):
    users = list(get_all_users())
    if not users:
        await msg.answer("Нет зарегистрированных пользователей.")
        return
    text = "<b>Зарегистрированные пользователи:</b>\n"
    print(users)
    for user_id, user in users:
        text += (
            f"\nID: <code>{user_id}</code>\n"
            f"👤 {user.get('full_name','?')}\n"
            f"📱 {user.get('phone','?')}\n"
            f"🏦 {user.get('bank','?')}\n"
            "---"
        )
    await msg.answer(text, parse_mode="HTML")