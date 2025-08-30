import io
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

from services.llm_api import extract_items_from_image
from database import (
    add_positions, get_positions, set_positions, init_assignments, set_assignment,
    get_assignments, start_text_session, append_text_message, end_text_session,
    get_all_users, save_debts, save_selected_positions, get_selected_positions, get_user, log_payment
)
from keyboards import positions_keyboard
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from config import settings

from utils import parse_position
from services.payments import mass_pay
from services.llm_api import calculate_debts_from_messages

router = Router(name="receipts")

class EditStates(StatesGroup):
    editing = State()
    adding = State()

@router.message(Command("split"))
async def cmd_split(msg: Message):
    group_id = str(msg.chat.id)
    positions = get_positions(group_id)
    if not positions:
        await msg.answer("❗️Нет позиций для распределения. Сначала отправьте фото чека или добавьте позиции вручную.")
        return

    webapp_url = f"{settings.backend_url}/webapp/receipt?group_id={msg.chat.id}"
    try:
        if msg.chat.type == "private":
            kb = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="🧾 Разделить чек", web_app=WebAppInfo(url=webapp_url))]],
                resize_keyboard=True, one_time_keyboard=True,
                input_field_placeholder="Откройте мини‑приложение"
            )
            await msg.answer("Нажмите кнопку ниже, чтобы открыть мини‑приложение для распределения покупок.", reply_markup=kb)
        else:
            if settings.bot_username:
                payload = f"group_{msg.chat.id}"
                deep_link = f"https://t.me/{settings.bot_username}?startapp={payload}"
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧾 Разделить чек", url=deep_link)]])
                await msg.answer("Нажмите кнопку ниже, чтобы открыть мини‑приложение для распределения покупок.", reply_markup=kb)
            else:
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧾 Разделить чек", url=webapp_url)]])
                await msg.answer("Нажмите кнопку ниже, чтобы открыть мини‑приложение для распределения покупок.", reply_markup=kb)
    except Exception as e:
        await msg.answer(f"Ошибка при отправке кнопки WebApp: {e}")

@router.message(Command("show_position"))
async def cmd_show_position(msg: Message):
    group_id = str(msg.chat.id)
    selections = get_selected_positions(group_id)
    if not selections:
        await msg.answer("❗️Позиции ещё не распределены. Пользователи должны выбрать свои покупки через мини‑приложение.")
        return
    lines = ["<b>Распределение позиций:</b>"]
    for user_id, pos_list in selections.items():
        user_info = get_user(user_id) or {}
        name = user_info.get('full_name') or user_info.get('phone') or str(user_id)
        if pos_list:
            items_str = ", ".join([f"{p.get('name')} ({p.get('quantity')} × {p.get('price')}₽)" for p in pos_list])
        else:
            items_str = "—"
        lines.append(f"{name} ({user_id}): {items_str}")
    await msg.answer("\n".join(lines), parse_mode="HTML")

@router.message(F.photo)
async def handle_photo(msg: Message):
    user = get_user(msg.from_user.id)
    if user is None:
        await msg.answer("❗️Вы ещё не зарегистрированы.\nПожалуйста, напишите /start в личку боту и завершите регистрацию.")
        return
    try:
        telegram_photo = msg.photo[-1]
        file = await msg.bot.get_file(telegram_photo.file_id)
        file_bytes = await msg.bot.download_file(file.file_path)
        image_bin = io.BytesIO(file_bytes.read())
    except Exception as e:
        await msg.answer(f"Ошибка загрузки изображения: {e}")
        return
    try:
        items, _ = await extract_items_from_image(image_bin)
    except Exception:
        items = None
    if not items or not isinstance(items, list):
        await msg.answer(str(items) if items else "Это не чек")
        return

    positions_to_add = [{"name": it.name, "quantity": it.quantity, "price": it.price} for it in items]
    group_id = str(msg.chat.id)
    add_positions(group_id, positions_to_add)
    init_assignments(group_id)

    positions_text = "\n".join(f"{item['name']} — {item['quantity']} x {item['price']}₽" for item in positions_to_add)
    await msg.answer("✅ Позиции добавлены:\n" + positions_text)

    webapp_url = f"{settings.backend_url}/webapp/receipt?group_id={msg.chat.id}"
    try:
        if msg.chat.type == "private":
            kb = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="🧾 Разделить чек", web_app=WebAppInfo(url=webapp_url))]],
                resize_keyboard=True, one_time_keyboard=True,
                input_field_placeholder="Откройте мини‑приложение"
            )
            await msg.answer("Нажмите кнопку ниже, чтобы открыть мини‑приложение для распределения покупок.", reply_markup=kb)
        else:
            if settings.bot_username:
                payload = f"group_{msg.chat.id}"
                deep_link = f"https://t.me/{settings.bot_username}?startapp={payload}"
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧾 Разделить чек", url=deep_link)]])
                await msg.answer("Нажмите кнопку ниже, чтобы открыть мини‑приложение для распределения покупок.", reply_markup=kb)
            else:
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧾 Разделить чек", url=webapp_url)]])
                await msg.answer("Нажмите кнопку ниже, чтобы открыть мини‑приложение для распределения покупок.", reply_markup=kb)
    except Exception as e:
        await msg.answer(f"Ошибка при отправке кнопки WebApp: {e}")

@router.message(F.web_app_data)
async def handle_web_app_data(msg: Message):
    import json
    try:
        raw_data = msg.web_app_data.data
        data = json.loads(raw_data)
        selected_data = data.get("selected", {})
        indices: list[int] = []
        if isinstance(selected_data, dict):
            for idx_str, qty in selected_data.items():
                try:
                    idx = int(idx_str); q = int(float(qty))
                except Exception:
                    continue
                indices.extend([idx] * max(q, 0))
        elif isinstance(selected_data, list):
            for i in selected_data:
                try:
                    indices.append(int(i))
                except Exception:
                    pass
        else:
            indices = []
    except Exception as e:
        await msg.answer(f"Ошибка обработки данных из мини‑приложения: {e}")
        return

    group_id = str(data.get("group_id") or msg.chat.id)
    set_assignment(group_id, msg.from_user.id, indices)
    try:
        all_positions = get_positions(group_id) or []
        selected_positions: list[dict] = []
        if isinstance(selected_data, dict):
            for idx_str, qty in selected_data.items():
                try:
                    idx = int(idx_str); q = int(float(qty))
                except Exception:
                    continue
                if 0 <= idx < len(all_positions) and q > 0:
                    orig = all_positions[idx]
                    selected_positions.append({"name": orig.get("name"), "quantity": q, "price": orig.get("price")})
        else:
            for idx in indices:
                if 0 <= idx < len(all_positions):
                    orig = all_positions[idx]
                    selected_positions.append({"name": orig.get("name"), "quantity": 1, "price": orig.get("price")})
        save_selected_positions(group_id, msg.from_user.id, selected_positions)
    except Exception as e:
        await msg.answer(f"Ошибка при сохранении распределённых позиций: {e}")
        return

    await msg.answer("✅ Ваш выбор сохранён! Когда все участники отметят свои позиции, используйте /finalize для расчёта.")

@router.message(Command("show"))
async def show_positions(msg: Message):
    group_id = str(msg.chat.id)
    positions = get_positions(group_id)
    if not positions:
        await msg.answer("Нет позиций! Сначала добавьте чеки.")
        return
    text = "\n".join([f"{idx+1}. {i['name']} — {i['quantity']} x {i['price']}₽" for idx, i in enumerate(positions)])
    kb = positions_keyboard(positions)
    await msg.answer(f"<b>Все позиции:</b>\n{text}", parse_mode="HTML", reply_markup=kb)

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
    text = "\n".join([f"{ix+1}. {p['name']} — {p['quantity']} x {p['price']}₽" for ix, p in enumerate(positions)])
    kb = positions_keyboard(positions)
    await call.message.edit_text(f"<b>Все позиции:</b>\n{text}", parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("edit_"))
async def edit_position(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.replace("edit_", ""))
    group_id = str(call.message.chat.id)
    positions = get_positions(group_id)
    if 0 <= idx < len(positions):
        await state.update_data(edit_idx=idx)
        await state.set_state(EditStates.editing)
        await call.message.answer(f"Введите новую позицию для «{positions[idx]['name']}» в формате:\nназвание, количество, цена\n\nПример:\nМолоко, 3, 75")
        await call.answer()
    else:
        await call.answer("Ошибка редактирования", show_alert=True)

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
        text = "\n".join(f"{ix+1}. {i['name']} — {i['quantity']} x {i['price']}₽" for ix, i in enumerate(positions))
        await msg.answer(f"<b>Все позиции:</b>\n{text}", parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        await msg.answer(f"Ошибка: {e}\nПример: Салат Оливье 1 250")
    await state.clear()

@router.callback_query(F.data == "add_new")
async def add_new_position(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditStates.adding)
    await call.message.answer("Введите новую позицию в формате:\nназвание, количество, цена")
    await call.answer()

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
        text = "\n".join(f"{ix+1}. {i['name']} — {i['quantity']} x {i['price']}₽" for ix, i in enumerate(positions))
        await msg.answer(f"<b>Все позиции:</b>\n{text}", parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        await msg.answer(f"Ошибка: {e}\nПример: Борщ 2 350")
    await state.clear()

@router.message(Command("finalize"))
async def finalize_receipt(msg: Message):
    group_id = str(msg.chat.id)
    positions = get_positions(group_id)
    if not positions:
        await msg.answer("Нет позиций для расчёта. Сначала отправьте чек.")
        return
    users = list(get_all_users())
    if not users:
        await msg.answer("Нет зарегистрированных пользователей для расчёта.")
        return

    from database import TEXT_SESSIONS
    session = TEXT_SESSIONS.get(group_id)

    if session and not session.get("collecting") and session.get("messages"):
        items_for_llm = {}
        for item in positions:
            try:
                total_price = float(item.get("price", 0)) * float(item.get("quantity", 1))
                items_for_llm[item.get("name")] = total_price
            except Exception:
                pass
        messages = session["messages"]
        try:
            debt_mapping = await calculate_debts_from_messages(items_for_llm, messages)
        except Exception as e:
            await msg.answer(f"Ошибка при расчёте через LLM: {e}\nПробуем поровну разделить.")
            debt_mapping = None
        if isinstance(debt_mapping, dict):
            mapping = {}
            for k, v in debt_mapping.items():
                try:
                    mapping[int(k)] = round(float(v), 2)
                except Exception:
                    pass
            if mapping:
                tx_id = await mass_pay(mapping)
                save_debts(group_id, mapping)
                log_payment(group_id, tx_id, mapping)
                set_positions(group_id, [])
                session["messages"] = []
                for user_id, amount in mapping.items():
                    try:
                        user_info = get_user(user_id) or {}
                        name = user_info.get('full_name') or user_info.get('phone') or str(user_id)
                        await msg.bot.send_message(user_id, f"{name}, вы должны {amount}₽. Спасибо за участие!")
                    except Exception:
                        pass
                text_lines = ["💰 Расчёт завершён!", f"ID транзакции: {tx_id}", "\nСуммы к оплате:"]
                for user_id, amount in mapping.items():
                    user_info = get_user(user_id) or {}
                    name = user_info.get('full_name') or user_info.get('phone') or str(user_id)
                    text_lines.append(f"{name} ({user_id}) → {amount}₽")
                await msg.answer("\n".join(text_lines), parse_mode="HTML")
                return

    assignments = get_assignments(group_id)
    if assignments:
        cost_per_position = []
        for item in positions:
            try:
                cost_per_position.append(float(item.get("price", 0)))
            except Exception:
                cost_per_position.append(0.0)

        mapping = {user_id: 0.0 for user_id, _ in users}
        for user_id, indices in assignments.items():
            total = 0.0
            for idx in indices:
                if 0 <= idx < len(cost_per_position):
                    total += cost_per_position[idx]
            mapping[user_id] = round(total, 2)

        payer_id = msg.from_user.id
        debt_mapping = {uid: amt for uid, amt in mapping.items() if uid != payer_id}

        tx_id = await mass_pay(debt_mapping)
        save_debts(group_id, debt_mapping)
        log_payment(group_id, tx_id, debt_mapping)
        set_positions(group_id, [])

        for uid, amount in debt_mapping.items():
            try:
                user_info = get_user(uid) or {}
                name = user_info.get('full_name') or user_info.get('phone') or str(uid)
                payer_info = get_user(payer_id) or {}
                payer_name = payer_info.get('full_name') or payer_info.get('phone') or str(payer_id)
                await msg.bot.send_message(uid, f"{name}, вы должны {amount}₽ пользователю {payer_name}.")
            except Exception:
                pass

        text_lines = ["💰 Расчёт завершён!", f"ID транзакции: {tx_id}", "\nСуммы к оплате:"]
        for uid, amount in debt_mapping.items():
            user_info = get_user(uid) or {}
            name = user_info.get('full_name') or user_info.get('phone') or str(uid)
            text_lines.append(f"{name} ({uid}) → {amount}₽")
        await msg.answer("\n".join(text_lines), parse_mode="HTML")
        return

    total_cost = 0.0
    for item in positions:
        try:
            total_cost += float(item.get("price", 0)) * float(item.get("quantity", 1))
        except Exception:
            pass
    count = len(users)
    share = total_cost / count if count else 0.0
    mapping = {user_id: round(share, 2) for user_id, _ in users}

    tx_id = await mass_pay(mapping)
    save_debts(group_id, mapping)
    log_payment(group_id, tx_id, mapping)
    set_positions(group_id, [])

    for uid, amount in mapping.items():
        try:
            user_info = get_user(uid) or {}
            name = user_info.get('full_name') or user_info.get('phone') or str(uid)
            await msg.bot.send_message(uid, f"{name}, вы должны {amount}₽ (поровну разделено).")
        except Exception:
            pass

    text_lines = ["💰 Расчёт завершён!", f"ID транзакции: {tx_id}", "\nСуммы к оплате:"]
    for user_id, amount in mapping.items():
        user_info = get_user(user_id) or {}
        name = user_info.get('full_name') or user_info.get('phone') or str(user_id)
        text_lines.append(f"{name} ({user_id}) → {amount}₽")
    await msg.answer("\n".join(text_lines), parse_mode="HTML")