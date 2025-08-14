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

# --- Распределённые позиции ---
# В рамках распределения позиций мы храним два уровня данных:
#   1) SELECTED_POSITIONS:   словарь group_id → user_id → список позиций. Используется для
#      расчётов и отображения распределения. Каждая позиция представлена словарём
#      {"name": str, "quantity": float, "price": float}.
#   2) GROUP_SELECTIONS:     словарь group_id → список всех выбранных позиций (суммарно от
#      всех участников). Это агрегированное представление используется в мини‑приложении
#      (FastAPI) для отображения уже распределённых позиций при параллельной работе бота и
#      фронтенда. Поскольку бот и веб‑приложение работают в отдельных процессах и не
#      разделяют память, GROUP_SELECTIONS периодически сохраняется в JSON‑файл. При
#      необходимости можно восстановить данные из файла, чтобы видеть выбор, сделанный в
#      другой сессии.

from collections import defaultdict

# Позиции пользователей в рамках конкретной группы (для расчёта и отображения).
# Каждый элемент: SELECTED_POSITIONS[group_id][user_id] = list[dict(name, quantity, price)]
SELECTED_POSITIONS: dict[str, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))

# Совокупные выбранные позиции по группам: group_id → список всех выбранных позиций.
GROUP_SELECTIONS: dict[str, list[dict]] = defaultdict(list)

# Файл для хранения выбранных позиций. По требованию пользователя имя файла singular.
# Данные в файле хранятся в виде словаря group_id → список позиций. Сохраняется
# агрегированный список позиций для каждой группы (без привязки к пользователям).
SELECTED_POSITIONS_FILE: str = os.path.join(os.path.dirname(__file__), 'selected_position.json')

def _persist_selected_positions() -> None:
    """
    Сохраняет агрегированное представление GROUP_SELECTIONS в файл. Формат файла:
        {
            "group_id": [ {"name": ..., "quantity": ..., "price": ...}, ... ],
            ...
        }
    """
    try:
        data_to_save: dict[str, list[dict]] = {str(g): list(pos_list) for g, pos_list in GROUP_SELECTIONS.items()}
        with open(SELECTED_POSITIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data_to_save, f, ensure_ascii=False)
    except Exception as e:
        print(f"Ошибка при сохранении выбранных позиций: {e}")

def _load_selected_positions() -> None:
    """
    Загружает агрегированные выбранные позиции из файла и обновляет
    GROUP_SELECTIONS. SELECTED_POSITIONS (детальное распределение по
    пользователям) не восстанавливается из файла, так как файл хранит
    только агрегированное представление.
    """
    try:
        if os.path.exists(SELECTED_POSITIONS_FILE):
            with open(SELECTED_POSITIONS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                GROUP_SELECTIONS.clear()
                for g_key, pos_list in data.items():
                    GROUP_SELECTIONS[str(g_key)] = list(pos_list)
    except Exception as e:
        print(f"Ошибка при загрузке выбранных позиций: {e}")

def _persist_group_selections() -> None:
    """
    Сохраняет агрегированные выбранные позиции (GROUP_SELECTIONS) в файл. По сути
    это обёртка над _persist_selected_positions для обратной совместимости.
    """
    _persist_selected_positions()

def _load_group_selections() -> None:
    """
    Загружает агрегированные выбранные позиции из файла и обновляет
    GROUP_SELECTIONS. Если файл отсутствует, GROUP_SELECTIONS остаётся пустым.
    """
    _load_selected_positions()


def save_selected_positions(group_id: str, user_id: int, positions: list[dict]) -> None:
    """
    Сохраняет выбранные пользователем позиции для указанной группы.

    Если `positions` непустой, перезаписывает выбор пользователя. Если передан
    пустой список, удаляет существующий выбор пользователя. После обновления
    пересчитывает агрегированное представление и сохраняет как детальное, так и
    агрегированное представления в файл.

    Args:
        group_id: Строковый идентификатор группы (например, chat.id).
        user_id: Идентификатор пользователя Telegram, сделавшего выбор.
        positions: Список позиций (словарей с ключами name, quantity, price).
    """
    # Обновляем детальные данные о выбранных позициях
    if positions:
        SELECTED_POSITIONS[group_id][user_id] = positions
    else:
        # Удаляем выбор пользователя
        if group_id in SELECTED_POSITIONS and user_id in SELECTED_POSITIONS[group_id]:
            del SELECTED_POSITIONS[group_id][user_id]
            # Если в группе не осталось выборов, удаляем группу
            if not SELECTED_POSITIONS[group_id]:
                del SELECTED_POSITIONS[group_id]
    # Пересчитываем агрегированное представление: объединяем выборы всех пользователей
    aggregated: list[dict] = []
    for lst in SELECTED_POSITIONS.get(group_id, {}).values():
        aggregated.extend(lst)
    GROUP_SELECTIONS[group_id] = aggregated
    # Сохраняем агрегированное представление в файл. Детальное распределение по
    # пользователям не сохраняется, поскольку файл хранит только список
    # выбранных позиций для каждой группы.
    _persist_group_selections()

def get_selected_positions(group_id: str) -> dict[int, list[dict]]:
    """
    Возвращает копию распределённых позиций для указанной группы. Структура: user_id → список позиций.

    Перед возвратом выполняет попытку загрузки детальных распределений из файла, если
    текущий словарь SELECTED_POSITIONS пустой. Это позволяет восстанавливать
    распределения после перезапуска бота.

    Args:
        group_id: Строковый идентификатор группы.

    Returns:
        dict[int, list[dict]]: Копия распределения для указанной группы. Если группа
            отсутствует, возвращается пустой словарь.
    """
    # Если память пуста — попытаться загрузить из файла
    if not SELECTED_POSITIONS:
        _load_selected_positions()
    # Извлекаем копию для указанной группы
    group_map = SELECTED_POSITIONS.get(group_id, {})
    return {uid: list(pos_list) for uid, pos_list in group_map.items()}

def get_group_selected_positions(group_id: str) -> list[dict]:
    """
    Возвращает совокупные выбранные позиции для группы. Предварительно
    загружает данные из файла, чтобы учесть выбор, сделанный в других
    процессах.
    """
    # Обновляем данные из файла, если таковые есть
    _load_group_selections()
    return list(GROUP_SELECTIONS.get(group_id, []))

# Путь к файлу, в котором будем хранить список позиций. Это нужно для обмена
# данными между ботом и мини‑приложением, которые могут работать в разных
# процессах и не имеют общей памяти.
POSITIONS_FILE: str = os.path.join(os.path.dirname(__file__), 'positions.json')

def persist_positions(positions: dict[str, list]) -> None:
    """
    Сохраняет переданный словарь позиций в JSON‑файл. Ключами
    являются идентификаторы групп, значениями — списки позиций. Мини‑приложение
    WebApp сможет затем загрузить файл и получить актуальные позиции по группе.
    """
    try:
        with open(POSITIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(positions, f, ensure_ascii=False)
    except Exception as e:
        print(f"Ошибка при сохранении позиций: {e}")

def load_positions() -> dict[str, list]:
    """
    Загружает словарь позиций из JSON‑файла. Если файл отсутствует или не
    получается его прочитать, возвращает пустой словарь.
    """
    try:
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Убедимся, что это словарь. Если список, преобразуем
                if isinstance(data, dict):
                    return {str(k): v for k, v in data.items()}
                # Если старая схема (список), помещаем в специальный ключ
                if isinstance(data, list):
                    return {"default": data}
    except Exception as e:
        print(f"Ошибка при загрузке позиций: {e}")
    return {}

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

# Позиции по группам: group_id → список позиций. Каждая позиция — словарь
# вида {"name": str, "quantity": float, "price": float}. Если группа отсутствует,
# считается, что у неё нет позиций.
POSITIONS: dict[str, list[dict]] = defaultdict(list)

# Назначения позиций пользователям: receipt_id -> user_id -> list of indices
from collections import defaultdict
ASSIGNMENTS: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))

# Сессии текстового расчёта: receipt_id -> {collecting: bool, messages: list[str]}
TEXT_SESSIONS: dict[str, dict[str, object]] = defaultdict(lambda: {"collecting": False, "messages": []})

def add_positions(group_id: str, new_positions: list) -> None:
    """
    Добавляет новые позиции для указанной группы. Если группа уже существует, позиции
    добавляются в конец списка. После обновления данные сохраняются в файл.

    Args:
        group_id: строковый идентификатор группы (например, chat.id в строковом виде).
        new_positions: список позиций (словари с полями name, quantity, price).
    """
    POSITIONS[group_id].extend(new_positions)
    persist_positions(POSITIONS)

def get_positions(group_id: str | None = None) -> list:
    """
    Возвращает копию списка позиций для конкретной группы. Если group_id не указан,
    возвращает объединённый список всех позиций из всех групп.

    Args:
        group_id: идентификатор группы. Если None, возвращается объединённый список.

    Returns:
        list: копия списка позиций.
    """
    if group_id is None:
        # объединяем все позиции
        combined: list[dict] = []
        for lst in POSITIONS.values():
            combined.extend(lst)
        return list(combined)
    return list(POSITIONS.get(group_id, []))

def set_positions(group_id: str, positions: list) -> None:
    """
    Заменяет список позиций для указанной группы полностью. Если передать пустой
    список, очищает список позиций группы. После обновления данные сохраняются в файл.

    Args:
        group_id: идентификатор группы.
        positions: новый список позиций.
    """
    POSITIONS[group_id] = positions
    # если список пуст и группа не имеет позиций, можно удалить ключ
    if not positions:
        try:
            del POSITIONS[group_id]
        except KeyError:
            pass
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