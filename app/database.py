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

# Храним все позиции как список словарей (или по chat_id)
POSITIONS = []

def add_positions(new_positions):
    global POSITIONS
    POSITIONS.extend(new_positions)

def get_positions():
    return POSITIONS

def set_positions(positions):
    global POSITIONS
    POSITIONS = positions