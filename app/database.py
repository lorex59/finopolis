"""
Простейшая in‑memory "БД".
В продакшене заменить на Postgres / Redis.
"""
from collections import defaultdict
from typing import Any
import os
import json

USERS: dict[int, dict[str, Any]] = {688410426: {'full_name': 'danil', 'phone': '+79644324111', 'bank': 'Tinkoff'}}                 # user_id → профиль
RECEIPTS: dict[str, dict[str, Any]] = defaultdict(dict)  # receipt_id → данные чека
DEBTS: dict[str, dict[int, float]] = defaultdict(dict)    # receipt_id → user_id → сумма

# Журнал выполненных платежей.
# Каждый элемент содержит идентификатор чека, идентификатор транзакции
# и отображение user_id → сумма, которое было отправлено в платёжную систему.
PAYMENT_LOG: list[dict[str, object]] = []

def log_payment(receipt_id: str, transaction_id: str, debt_mapping: dict[int, float]) -> None:
    """
    Сохраняет информацию о выполненном переводе в журнал.

    Аргументы:
        receipt_id: идентификатор чата/чека
        transaction_id: уникальный идентификатор транзакции, возвращаемый платёжным шлюзом
        debt_mapping: словарь user_id → сумма, которую пользователь должен
    """
    PAYMENT_LOG.append({
        "receipt_id": receipt_id,
        "transaction_id": transaction_id,
        "debt_mapping": debt_mapping.copy(),
    })

def get_payment_log() -> list[dict[str, object]]:
    """Возвращает копию журнала платежей."""
    return list(PAYMENT_LOG)

# Путь к файлу, в котором будем хранить список позиций. Это нужно для обмена
# данными между ботом и мини‑приложением, которые могут работать в разных
# процессах и не имеют общей памяти.
POSITIONS_FILE: str = os.path.join(os.path.dirname(__file__), 'positions.json')

def persist_positions(positions: list) -> None:
    """
    Сохраняет переданный список позиций в JSON‑файл. Мини‑приложение
    WebApp сможет затем загрузить этот файл и получить актуальный список.
    """
    try:
        with open(POSITIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(positions, f, ensure_ascii=False)
    except Exception as e:
        # выводим сообщение об ошибке, но не прерываем выполнение бота
        print(f"Ошибка при сохранении позиций: {e}")

def load_positions() -> list:
    """
    Загружает список позиций из JSON‑файла. Если файл отсутствует или не
    получается его прочитать, возвращает пустой список.
    """
    try:
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"Ошибка при загрузке позиций: {e}")
    return []

def save_user(user_id: int, data: dict[str, Any]) -> None:
    print(f"SAVE_USER called for {user_id} with {data}")

    USERS[user_id] = data


def get_all_users():
    return USERS.items()

def get_user(user_id: int) -> dict[str, Any] | None:
    return USERS.get(user_id)

def save_receipt(receipt_id: str, items: dict[str, float]) -> None:
    RECEIPTS[receipt_id] = items

def save_debts(receipt_id: str, mapping: dict[int, float]) -> None:
    DEBTS[receipt_id] = mapping

"""
In‑memory storage for users, receipts, positions and assignments.

For demo purposes we keep all data in Python dictionaries and lists. In
production these should be replaced with a proper database such as
PostgreSQL or Redis.

USERS: stores user profiles keyed by Telegram user id.
RECEIPTS: stores information about receipts keyed by a receipt id (for
example, a chat id).
DEBTS: stores the final debt mapping per receipt.
POSITIONS: a list of all currently parsed positions across receipts. In a
multi‑chat scenario you might want to scope positions per chat id.
ASSIGNMENTS: mapping of receipt id → user id → list of indices of
positions the user claims to have purchased. It is initialised when a
new receipt is processed and updated when users submit their selection
via the WebApp.
TEXT_SESSIONS: tracking ongoing natural language based calculations.

Helper functions are provided to manipulate these structures. See
handlers/receipts.py for usage.
"""

# Храним все позиции как список словарей (или по chat_id)
POSITIONS: list[dict] = []

# Назначения позиций пользователям: receipt_id -> user_id -> list of indices
from collections import defaultdict
ASSIGNMENTS: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))

# Сессии текстового расчёта: receipt_id -> {collecting: bool, messages: list[str]}
TEXT_SESSIONS: dict[str, dict[str, object]] = defaultdict(lambda: {"collecting": False, "messages": []})

def add_positions(new_positions: list) -> None:
    """Append new positions to the global list."""
    global POSITIONS
    POSITIONS.extend(new_positions)
    # также сохраняем новый список позиций в файл, чтобы его мог прочитать WebApp
    persist_positions(POSITIONS)

def get_positions() -> list:
    """Return a copy of the current positions list."""
    return list(POSITIONS)

def set_positions(positions: list) -> None:
    """Replace the current positions list entirely."""
    global POSITIONS
    POSITIONS = positions
    # при замене списка позиций обновляем файл с позициями
    persist_positions(POSITIONS)

def init_assignments(receipt_id: str) -> None:
    """Initialise (or reset) the assignments mapping for a given receipt."""
    ASSIGNMENTS[receipt_id] = defaultdict(list)

def set_assignment(receipt_id: str, user_id: int, indices: list[int]) -> None:
    """Store a user's selected position indices for a given receipt."""
    # ensure receipt exists
    if receipt_id not in ASSIGNMENTS:
        ASSIGNMENTS[receipt_id] = defaultdict(list)
    ASSIGNMENTS[receipt_id][user_id] = indices

def get_assignments(receipt_id: str) -> dict[int, list[int]]:
    """Get assignments mapping for a receipt."""
    return ASSIGNMENTS.get(receipt_id, {})

def start_text_session(receipt_id: str) -> None:
    """Begin collecting natural language messages for a receipt."""
    TEXT_SESSIONS[receipt_id] = {"collecting": True, "messages": []}

def append_text_message(receipt_id: str, message: str) -> None:
    """Append a user message to the current text session for a receipt."""
    session = TEXT_SESSIONS.setdefault(receipt_id, {"collecting": False, "messages": []})
    session["messages"].append(message)

def end_text_session(receipt_id: str) -> list[str]:
    """End a text collection session and return the collected messages."""
    session = TEXT_SESSIONS.get(receipt_id, {"collecting": False, "messages": []})
    session["collecting"] = False
    return session.get("messages", [])