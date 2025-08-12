"""
Простейшая in‑memory "БД".
В продакшене заменить на Postgres / Redis.
"""
from collections import defaultdict
from typing import Any

USERS: dict[int, dict[str, Any]] = {688410426: {'full_name': 'danil', 'phone': '+79644324111', 'bank': 'Tinkoff'}}                 # user_id → профиль
RECEIPTS: dict[str, dict[str, Any]] = defaultdict(dict)  # receipt_id → данные чека
DEBTS: dict[str, dict[int, float]] = defaultdict(dict)    # receipt_id → user_id → сумма

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

def get_positions() -> list:
    """Return a copy of the current positions list."""
    return list(POSITIONS)

def set_positions(positions: list) -> None:
    """Replace the current positions list entirely."""
    global POSITIONS
    POSITIONS = positions

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