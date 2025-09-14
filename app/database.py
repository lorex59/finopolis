"""
Вспомогательный модуль для хранения данных.

Ранее все данные хранились в Python‑структурах и JSON‑файлах.
Теперь реализация использует SQLite для постоянства между запусками и
одновременной работы Telegram‑бота и мини‑приложения. Тем не менее
часть старых глобальных словарей оставлена для обратной совместимости:
некоторые функции по‑прежнему читают из них, но при сохранении данные
сначала записываются в базу данных, затем обновляют in‑memory
структуры.

Таблицы базы данных:

* accounts: хранит профили пользователей. Поля:
    - id             INTEGER PRIMARY KEY AUTOINCREMENT
    - phone_number   TEXT
    - full_name      TEXT
    - telegram_login TEXT (используется для хранения Telegram‑идентификатора пользователя или username)
    - bank           TEXT (дополнительный атрибут)
    - telegram_id    TEXT UNIQUE (строковый идентификатор пользователя в Telegram)

* positions: хранит позиции из чеков. Поля:
    - id       INTEGER PRIMARY KEY AUTOINCREMENT
    - group_id TEXT (идентификатор чата/группы)
    - name     TEXT
    - quantity REAL
    - price    REAL

* selected_positions: хранит выбор пользователей. Поля:
    - id          INTEGER PRIMARY KEY AUTOINCREMENT
    - group_id    TEXT
    - user_tg_id  TEXT (Telegram‑идентификатор пользователя)
    - position_id INTEGER (ссылка на positions.id, может быть NULL)
    - quantity    REAL
    - price       REAL

По умолчанию база данных создаётся в файле `database.db` рядом с этим модулем.
Функция init_db() вызывается при импорте, чтобы гарантировать наличие
необходимых таблиц.
"""

from collections import defaultdict
from typing import Any
import os
import json
import sqlite3
import logging
logging.basicConfig(
    level=logging.DEBUG,  # максимум информации
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("webapp")

# ---------------------------------------------------------------------------
# Настройка SQLite
# ---------------------------------------------------------------------------
# Путь к файлу базы данных. Для корректной работы и избегания дублирования
# базы данных между корнем проекта и подпакетом ``app`` мы всегда
# используем один и тот же файл в корне репозитория. Эта переменная
# рассчитывается относительно расположения текущего файла (app/database.py)
# и поднимается на один уровень вверх. Таким образом и бот, и мини‑приложение
# работают с одним файлом ``database.db``, расположенным в корне проекта.
DB_PATH: str = os.path.join(os.path.dirname(os.path.dirname(__file__)), "database.db")

def get_db_connection() -> sqlite3.Connection:
    """Создаёт и возвращает новое соединение с базой данных.

    Мы отключаем проверку потока (check_same_thread=False), так как
    соединения могут использоваться в асинхронном коде, где разные
    корутины работают в разных потоках. Каждое обращение получает
    собственное соединение, поэтому закрывайте его после использования.
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    """Создаёт необходимые таблицы, если они не существуют."""
    # Перед созданием таблиц удаляем существующий файл базы данных. Это
    # гарантирует, что приложение всегда начинает работу с чистой базой.
    # Если необходимо сохранять данные между перезапусками, закомментируйте
    # строку ниже.
    try:
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
            logger.debug(f"Удалён существующий файл базы данных: {DB_PATH}")
    except Exception:
        pass
    conn = get_db_connection()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT,
            full_name TEXT,
            telegram_login TEXT,
            bank TEXT,
            telegram_id TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT,
            name TEXT,
            quantity REAL,
            price REAL
        );
        CREATE TABLE IF NOT EXISTS selected_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT,
            user_tg_id TEXT,
            position_id INTEGER,
            quantity REAL,
            price REAL,
            FOREIGN KEY (position_id) REFERENCES positions(id)
        );

        -- Новая таблица для хранения внесённых платежей. Сохраняем, кто
        -- внёс платёж (tg_user_id), для какой группы (group_id), сумму
        -- платежа (amount) и опциональное описание или список позиций,
        -- за которые был внесён платёж (positions). Формат поля
        -- positions: JSON‑строка или произвольный текст.
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_user_id TEXT,
            group_id TEXT,
            amount REAL,
            positions TEXT
        );

        -- Таблица для сохранения результатов расчётов долей.
        -- Каждая запись описывает, сколько пользователь должен
        -- заплатить по итогам расчёта для конкретного чека (receipt).
        -- Поля:
        --   id          — первичный ключ
        --   receipt_id  — идентификатор расчёта (чаще всего совпадает с chat.id группы)
        --   user_tg_id  — идентификатор пользователя, который должен заплатить
        --   amount      — сумма долга пользователя (в рублях)
        --   created_at  — время создания записи (UTC)
        CREATE TABLE IF NOT EXISTS debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id TEXT,
            user_tg_id TEXT,
            amount REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()
    conn.close()

    # После создания таблиц очищаем таблицы positions и selected_positions.
    # Это позволяет избежать ситуаций, когда в базе данных остаются
    # устаревшие данные от предыдущих запусков (например, при тестировании).
    # Если вам требуется сохранение данных между перезапусками, удалите
    # строки ниже.
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM positions")
        cur.execute("DELETE FROM selected_positions")
        conn.commit()
    finally:
        conn.close()

# Инициализируем базу данных при импорте.
init_db()

# ---------------------------------------------------------------------------
# В этой версии модуля мы исключили все глобальные структуры хранения
# данных. Все сведения о пользователях, позициях, выборе пользователей и
# распределениях хранятся исключительно в базе данных. Локальные словари
# и списки используются только для временных операций, связанных с
# определёнными сессиями (например, для текстового ввода или хранения
# текущих назначений в рамках одного расчёта). Для таких целей
# предусмотрены объекты ASSIGNMENTS и TEXT_SESSIONS ниже.

def log_payment(receipt_id: str, transaction_id: str, debt_mapping: dict[int, float]) -> None:
    """Регистрирует факт платежа.

    В текущей реализации сведения о платежах не сохраняются в оперативной памяти.
    При необходимости ведения истории платежей реализуйте запись в отдельную
    таблицу базы данных.
    """
    pass

def get_payment_log() -> list[dict[str, object]]:
    """Возвращает пустой список платёжных записей.

    История платежей не сохраняется в оперативной памяти. Реализуйте этот
    интерфейс при необходимости.
    """
    return []

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

# Файл для хранения агрегированных выбранных позиций (без привязки к пользователям).
# Формат: {"group_id": [ {name, quantity, price}, ... ], ...}
SELECTED_POSITIONS_FILE: str = os.path.join(os.path.dirname(__file__), 'selected_position.json')

# Файл для хранения детального распределения выбранных позиций по пользователям.
# Формат: {"group_id": {"user_id": [ {name, quantity, price}, ... ], ...}, ...}
DETAILED_SELECTED_POSITIONS_FILE: str = os.path.join(os.path.dirname(__file__), 'selected_positions.json')

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


# --- Детальное хранение выбранных позиций по пользователям ---
def _persist_detailed_positions() -> None:
    """
    Сохраняет детальное распределение выбранных позиций в файл.
    Формат файла: {"group_id": {"user_id": [ {name, quantity, price}, ... ], ...}, ...}.

    Используется для восстановления распределений после перезапуска, в т.ч.
    когда несколько пользователей параллельно выбирают позиции в разных группах.
    """
    try:
        to_save: dict[str, dict[str, list[dict]]] = {}
        for g, users_map in SELECTED_POSITIONS.items():
            user_map_json: dict[str, list[dict]] = {}
            for u_id, pos_list in users_map.items():
                # Приводим ключ пользователя к строке для корректной сериализации
                user_map_json[str(u_id)] = list(pos_list)
            to_save[str(g)] = user_map_json
        with open(DETAILED_SELECTED_POSITIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(to_save, f, ensure_ascii=False)
    except Exception as e:
        print(f"Ошибка при сохранении детальных выбранных позиций: {e}")

def _load_detailed_positions() -> None:
    """
    Загружает детальное распределение выбранных позиций из файла и
    восстанавливает как SELECTED_POSITIONS, так и GROUP_SELECTIONS.

    Если файл отсутствует, функция ничего не делает. В случае ошибки
    выводится сообщение, а существующие данные не изменяются.
    """
    try:
        if os.path.exists(DETAILED_SELECTED_POSITIONS_FILE):
            with open(DETAILED_SELECTED_POSITIONS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Очищаем существующие структуры
                SELECTED_POSITIONS.clear()
                GROUP_SELECTIONS.clear()
                for g, users_map in data.items():
                    g_str = str(g)
                    for u_str, pos_list in users_map.items():
                        try:
                            u_id = int(u_str)
                        except Exception:
                            # Если не удалось преобразовать к int, пропускаем
                            continue
                        SELECTED_POSITIONS[g_str][u_id] = list(pos_list)
                    # Пересчитаем агрегированное представление
                    aggregated: list[dict] = []
                    for pos_list in SELECTED_POSITIONS[g_str].values():
                        aggregated.extend(list(pos_list))
                    GROUP_SELECTIONS[g_str] = aggregated
    except Exception as e:
        print(f"Ошибка при загрузке детальных выбранных позиций: {e}")


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
    """Сохраняет выбранные пользователем позиции для указанной группы.

    Данные записываются в таблицу `selected_positions`. Перед вставкой
    удаляется предыдущий выбор пользователя для данной группы. После
    обновления база данных синхронизируется с in‑memory структурами
    SELECTED_POSITIONS и GROUP_SELECTIONS.
    """
    # Сохраняем в базу данных
    conn = get_db_connection()
    cur = conn.cursor()
    # Прежний выбор этого пользователя в группе больше не удаляется.
    # Ранее реализация полностью очищала выбор пользователя перед
    # сохранением новых позиций. Это приводило к тому, что при
    # повторном выборе позиции предыдущее значение затиралось. В рамках
    # исправления поведения мы не удаляем существующие записи, чтобы
    # новые выбранные позиции добавлялись к уже сохранённым. Оставляем
    # код удаления закомментированным для наглядности.
    # cur.execute(
    #     "DELETE FROM selected_positions WHERE group_id = ? AND user_tg_id = ?",
    #     (str(group_id), str(user_id)),
    # )
    if positions:
        for pos in positions:
            name = pos.get('name')
            quantity = pos.get('quantity')
            price = pos.get('price')
            # Находим id исходной позиции, если она существует
            cur.execute(
                "SELECT id FROM positions WHERE group_id = ? AND name = ? AND price = ? ORDER BY id LIMIT 1",
                (str(group_id), name, price),
            )
            row = cur.fetchone()
            position_id = row['id'] if row else None
            cur.execute(
                "INSERT INTO selected_positions (group_id, user_tg_id, position_id, quantity, price) VALUES (?, ?, ?, ?, ?)",
                (str(group_id), str(user_id), position_id, quantity, price),
            )
    conn.commit()
    conn.close()
    # Не обновляем in‑memory SELECTED_POSITIONS или GROUP_SELECTIONS и не
    # сохраняем данные в JSON‑файлы. Все данные о выборе хранятся
    # исключительно в таблице selected_positions базы данных.

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
    """Возвращает распределённые позиции для указанной группы.

    Данные извлекаются из таблицы `selected_positions` и объединяются по
    пользователям. Для удобства также синхронизируются in‑memory
    SELECTED_POSITIONS и GROUP_SELECTIONS.

    Args:
        group_id: строковый идентификатор группы.

    Returns:
        dict[int, list[dict]]: отображение user_id → список выбранных позиций.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT sp.user_tg_id, sp.quantity, sp.price, p.name
        FROM selected_positions sp
        LEFT JOIN positions p ON sp.position_id = p.id
        WHERE sp.group_id = ?
        ORDER BY sp.id
        """,
        (str(group_id),),
    )
    rows = cur.fetchall()
    conn.close()
    result: dict[int, list[dict]] = {}
    for row in rows:
        try:
            uid = int(row['user_tg_id']) if row['user_tg_id'] is not None else None
        except Exception:
            uid = None
        if uid is None:
            continue
        name = row['name'] if row['name'] is not None else ''
        item = {'name': name, 'quantity': row['quantity'], 'price': row['price']}
        result.setdefault(uid, []).append(item)
    # Не обновляем in‑memory SELECTED_POSITIONS или GROUP_SELECTIONS.
    return result

def get_group_selected_positions(group_id: str) -> list[dict]:
    """
    Возвращает совокупные выбранные позиции для группы. Предварительно
    загружает данные из файла, чтобы учесть выбор, сделанный в других
    процессах.
    """
    """Возвращает совокупные выбранные позиции для группы.

    Данные извлекаются из таблицы `selected_positions` без учёта
    разбивки по пользователям. Для совместимости также обновляет
    in‑memory GROUP_SELECTIONS.

    Args:
        group_id: строковый идентификатор группы.

    Returns:
        list[dict]: список выбранных позиций (каждый элемент —
        словарь с ключами name, quantity, price).
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT sp.quantity, sp.price, p.name
        FROM selected_positions sp
        LEFT JOIN positions p ON sp.position_id = p.id
        WHERE sp.group_id = ?
        ORDER BY sp.id
        """,
        (str(group_id),),
    )
    rows = cur.fetchall()
    conn.close()
    result: list[dict] = []
    for row in rows:
        name = row['name'] if row['name'] is not None else ''
        result.append({'name': name, 'quantity': row['quantity'], 'price': row['price']})
    # Не обновляем in‑memory GROUP_SELECTIONS.
    return result

# Путь к файлу, в котором будем хранить список позиций. Это нужно для обмена
# данными между ботом и мини‑приложением, которые могут работать в разных
# процессах и не имеют общей памяти.
POSITIONS_FILE: str = os.path.join(os.path.dirname(__file__), 'positions.json')

def persist_positions(positions: dict[str, list]) -> None:
    """Сохраняет все позиции в базу данных.

    Этот метод удаляет существующие записи из таблицы `positions` и
    вставляет новые строки согласно переданному словарю. Также
    синхронизирует in‑memory POSITIONS для обратной совместимости.

    Args:
        positions: словарь group_id → список позиций.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    # Удаляем все записи
    cur.execute("DELETE FROM positions")
    # Вставляем новые
    for group_id, pos_list in positions.items():
        for pos in pos_list:
            cur.execute(
                "INSERT INTO positions (group_id, name, quantity, price) VALUES (?, ?, ?, ?)",
                (str(group_id), pos.get('name'), pos.get('quantity'), pos.get('price')),
            )
    conn.commit()
    conn.close()
    # Не обновляем in‑memory POSITIONS. Данные берутся строго из базы данных.

def load_positions() -> dict[str, list]:
    """Загружает словарь позиций из базы данных.

    Выбирает все записи из таблицы `positions`, группируя их по group_id.
    Также синхронизирует in‑memory POSITIONS. Если записей нет,
    возвращает пустой словарь.

    Returns:
        dict[str, list]: словарь, где ключ — group_id, значение — список позиций.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT group_id, name, quantity, price FROM positions ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    result: dict[str, list] = {}
    for row in rows:
        g_id = str(row['group_id'])
        item = {'name': row['name'], 'quantity': row['quantity'], 'price': row['price']}
        result.setdefault(g_id, []).append(item)
    return result

def save_user(user_id: int, data: dict[str, Any]) -> None:
    """Сохраняет данные пользователя в базу данных и обновляет in‑memory словарь.

    Аргументы:
        user_id: Telegram‑идентификатор пользователя (целое число).
        data: словарь со следующими ключами:
            - full_name
            - phone или phone_number
            - bank (опционально)
            - telegram_login (опционально)

    В таблице accounts поле telegram_id используется для хранения строкового
    представления user_id, поэтому для обновления записи ищем по нему.
    Если запись существует, обновляем её; иначе вставляем новую.
    После обновления синхронизируем глобальный словарь USERS.
    """
    # Извлекаем данные из словаря
    full_name = data.get('full_name') or data.get('fio')
    phone = data.get('phone') or data.get('phone_number')
    bank = data.get('bank')
    telegram_login = data.get('telegram_login')
    telegram_id = str(user_id)

    # Запись в базу данных
    conn = get_db_connection()
    cur = conn.cursor()
    # Проверяем, существует ли пользователь по telegram_id
    cur.execute("SELECT id FROM accounts WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    if row:
        # Обновляем существующую запись
        cur.execute(
            "UPDATE accounts SET phone_number = ?, full_name = ?, telegram_login = ?, bank = ? WHERE telegram_id = ?",
            (phone, full_name, telegram_login, bank, telegram_id),
        )
    else:
        # Вставляем новую запись
        cur.execute(
            "INSERT INTO accounts (phone_number, full_name, telegram_login, bank, telegram_id) VALUES (?, ?, ?, ?, ?)",
            (phone, full_name, telegram_login, bank, telegram_id),
        )
    conn.commit()
    conn.close()

    # В этой версии данные пользователя хранятся только в базе данных. Никакие
    # in‑memory словари не обновляются.


def get_all_users():
    """Возвращает список (user_id, данные) для всех пользователей.

    Пользователи извлекаются из базы данных. Если in‑memory словарь USERS
    содержит дополнительные элементы, они будут объединены с данными из базы.
    Возвращается итератор, совместимый с предыдущей версией.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT telegram_id, phone_number, full_name, bank, telegram_login FROM accounts")
    rows = cur.fetchall()
    conn.close()
    result: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        try:
            uid = int(row['telegram_id']) if row['telegram_id'] is not None else None
        except Exception:
            uid = None
        if uid is None:
            continue
        user_dict: dict[str, Any] = {
            'full_name': row['full_name'],
            'phone': row['phone_number'],
            'bank': row['bank'],
        }
        # telegram_login необязателен
        if row['telegram_login']:
            user_dict['telegram_login'] = row['telegram_login']
        result.append((uid, user_dict))
    return result

def get_user(user_id: int) -> dict[str, Any] | None:
    """Возвращает профиль пользователя по его Telegram‑идентификатору.

    Сначала пытается найти пользователя в базе данных (поле telegram_id).
    Если не найден, возвращает данные из in‑memory словаря USERS, если они есть.
    """
    telegram_id = str(user_id)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT phone_number, full_name, bank, telegram_login FROM accounts WHERE telegram_id = ?",
        (telegram_id,),
    )
    row = cur.fetchone()
    conn.close()
    if row:
        user_dict: dict[str, Any] = {
            'full_name': row['full_name'],
            'phone': row['phone_number'],
            'bank': row['bank'],
        }
        if row['telegram_login']:
            user_dict['telegram_login'] = row['telegram_login']
        return user_dict
    # Если не найдено в базе, возвращаем None. Никакого fallback к in‑memory нет.
    return None

def save_receipt(receipt_id: str, items: dict[str, float]) -> None:
    """Сохраняет информацию о чеке.

    Данная функция оставлена для совместимости, но не выполняет никакой
    операции, поскольку все позиции чека сохраняются в таблице `positions`.
    """
    pass

def save_debts(receipt_id: str, mapping: dict[int, float]) -> None:
    """Сохраняет расчётные долги для указанного расчёта (чека).

    Каждая пара ``user_id → amount`` из переданного ``mapping`` вставляется
    в таблицу ``debts``. Существующие записи для данного ``receipt_id``
    предварительно удаляются, чтобы избежать дублирования. При
    необходимости более тонкого контроля (например, хранения истории
    расчётов) можно вместо удаления добавлять новые записи с новой
    меткой времени.

    Args:
        receipt_id: идентификатор расчёта/чека (обычно идентификатор
            группового чата).
        mapping: отображение user_id → сумма долга. Значения суммы
            округляются до двух знаков после запятой.
    """
    # Открываем соединение с базой данных
    conn = get_db_connection()
    cur = conn.cursor()
    # Удаляем существующие записи для данного чека. Это обеспечивает,
    # что при повторном расчёте данные будут перезаписаны.
    cur.execute(
        "DELETE FROM debts WHERE receipt_id = ?",
        (str(receipt_id),),
    )
    # Вставляем новые записи
    for uid, amount in (mapping or {}).items():
        try:
            uid_str = str(uid)
            amt = round(float(amount), 2)
        except Exception:
            continue
        cur.execute(
            "INSERT INTO debts (receipt_id, user_tg_id, amount) VALUES (?, ?, ?)",
            (str(receipt_id), uid_str, amt),
        )
    conn.commit()
    conn.close()

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
    # Сохраняем позиции в базу данных и обновляем in‑memory словарь.
    # Получаем соединение для выполнения транзакции.
    conn = get_db_connection()
    cur = conn.cursor()
    for pos in new_positions:
        cur.execute(
            "INSERT INTO positions (group_id, name, quantity, price) VALUES (?, ?, ?, ?)",
            (str(group_id), pos.get('name'), pos.get('quantity'), pos.get('price')),
        )
    conn.commit()
    conn.close()
    # Не обновляем in‑memory список POSITIONS. Данные берутся строго из базы.

def get_positions(group_id: str | None = None) -> list:
    """
    Возвращает копию списка позиций для конкретной группы. Если group_id не указан,
    возвращает объединённый список всех позиций из всех групп.

    Args:
        group_id: идентификатор группы. Если None, возвращается объединённый список.

    Returns:
        list: копия списка позиций.
    """
    # Подключаемся к базе данных и создаём курсор
    conn = get_db_connection()
    cur = conn.cursor()
    if group_id is None:
        cur.execute("SELECT group_id, name, quantity, price FROM positions ORDER BY id")
        rows = cur.fetchall()
        # Закрываем соединение сразу после выборки
        conn.close()
        combined: list[dict] = []
        for row in rows:
            item = {'name': row['name'], 'quantity': row['quantity'], 'price': row['price']}
            combined.append(item)
        return combined
    else:
        cur.execute(
            "SELECT name, quantity, price FROM positions WHERE group_id = ? ORDER BY id",
            (str(group_id),),
        )
        rows = cur.fetchall()
        # Закрываем соединение
        conn.close()
        result = [
            {'name': row['name'], 'quantity': row['quantity'], 'price': row['price']}
            for row in rows
        ]
        return result

def set_positions(group_id: str, positions: list) -> None:
    """
    Заменяет список позиций для указанной группы полностью. Если передать пустой
    список, очищает список позиций группы. После обновления данные сохраняются в файл.

    Args:
        group_id: идентификатор группы.
        positions: новый список позиций.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM positions WHERE group_id = ?", (str(group_id),))
    if positions:
        for pos in positions:
            cur.execute(
                "INSERT INTO positions (group_id, name, quantity, price) VALUES (?, ?, ?, ?)",
                (str(group_id), pos.get('name'), pos.get('quantity'), pos.get('price')),
            )
    conn.commit()
    conn.close()
    # Не обновляем in‑memory POSITIONS. Данные берутся из базы данных.

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

# ---------------------------------------------------------------------------
# Новые функции для учёта платежей и расчёта баланса
# ---------------------------------------------------------------------------

def add_payment(group_id: str, tg_user_id: int, amount: float, positions: list[dict] | str | None = None) -> None:
    """
    Добавляет запись о платеже в таблицу payments.

    Args:
        group_id: Идентификатор группы (чата).
        tg_user_id: Идентификатор пользователя Telegram, внесшего платёж.
        amount: Сумма платежа (в рублях). Может быть положительной или нулевой.
        positions: Список позиций (словарей) или строка с описанием, за которые был внесён платёж.
                   Если передан список, он будет сериализован в JSON. Если передана строка,
                   она будет сохранена как есть. Если None, поле positions будет NULL.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    # Преобразуем positions в строку, если необходимо
    pos_value: str | None
    if positions is None:
        pos_value = None
    elif isinstance(positions, str):
        pos_value = positions
    else:
        try:
            pos_value = json.dumps(positions, ensure_ascii=False)
        except Exception:
            # В случае ошибки сериализации сохраняем строковое представление
            pos_value = str(positions)
    cur.execute(
        "INSERT INTO payments (tg_user_id, group_id, amount, positions) VALUES (?, ?, ?, ?)",
        (str(tg_user_id), str(group_id), float(amount), pos_value),
    )
    conn.commit()
    conn.close()


def get_payments(group_id: str) -> dict[int, float]:
    """
    Возвращает суммы внесённых платежей для указанной группы.

    Args:
        group_id: Идентификатор группы (чата).

    Returns:
        dict[int, float]: отображение user_id → суммарная сумма платежей.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT tg_user_id, SUM(amount) AS total_amount FROM payments WHERE group_id = ? GROUP BY tg_user_id",
        (str(group_id),),
    )
    rows = cur.fetchall()
    conn.close()
    result: dict[int, float] = {}
    for row in rows:
        uid_raw = row["tg_user_id"]
        total = row["total_amount"] or 0.0
        try:
            uid = int(uid_raw) if uid_raw is not None else None
        except Exception:
            uid = None
        if uid is None:
            continue
        result[uid] = float(total)
    return result


def get_unassigned_positions(group_id: str) -> list[dict]:
    """
    Проверяет, все ли позиции из чека распределены участниками.

    Для каждой исходной позиции из таблицы positions сравнивается исходное
    количество (quantity) с суммарным количеством, выбранным участниками
    (selected_positions). Если выбранное количество меньше исходного,
    позиция считается неразделённой. Возвращает список таких позиций
    с указанием оставшегося количества.

    Args:
        group_id: Идентификатор группы.

    Returns:
        list[dict]: список словарей {"name": str, "quantity": float, "price": float}
        для неразделённых позиций. Если все позиции распределены, список пуст.
    """
    # Загружаем исходные позиции из базы
    original_positions = get_positions(group_id) or []
    # Загружаем выбранные позиции пользователей
    selections = get_selected_positions(group_id) or {}
    # Суммируем выбранные количества для каждой позиции по ключу (name, price)
    selected_qty: dict[tuple[str, float], float] = {}
    for pos_list in selections.values():
        for pos in pos_list:
            key = (pos.get("name"), float(pos.get("price", 0)))
            qty = float(pos.get("quantity", 0))
            selected_qty[key] = selected_qty.get(key, 0.0) + qty
    unassigned: list[dict] = []
    for orig in original_positions:
        key = (orig.get("name"), float(orig.get("price", 0)))
        orig_qty = float(orig.get("quantity", 0))
        # Сколько уже выбрали
        chosen = selected_qty.get(key, 0.0)
        remaining = orig_qty - chosen
        # Считаем, что погрешности менее 0.01 можно игнорировать
        if remaining > 0.01:
            # Добавляем позицию с оставшимся количеством и той же ценой
            unassigned.append({
                "name": orig.get("name"),
                "quantity": round(remaining, 2),
                "price": float(orig.get("price", 0)),
            })
    return unassigned


def calculate_group_balance(group_id: str) -> list[tuple[int, int, float]]:
    """
    Рассчитывает оптимальные переводы между участниками, чтобы покрыть
    расходы, исходя из выбранных позиций и внесённых платежей.

    Алгоритм:
      1. Подсчитать стоимость выбранных позиций для каждого пользователя.
      2. Подсчитать, сколько каждый пользователь заплатил (payments).
      3. Для каждого участника вычислить баланс: balance = paid - cost.
         Положительный баланс означает, что пользователь должен получить
         деньги; отрицательный — что он должен заплатить.
      4. Построить список минимального количества переводов между
         должниками и кредиторами. Каждый элемент списка имеет вид
         (debtor_id, creditor_id, amount), что означает, что должник
         debtor_id должен перевести amount рублей кредитору creditor_id.

    Args:
        group_id: Идентификатор группы.

    Returns:
        list[tuple[int, int, float]]: список переводов. Может быть пустым,
        если нет долгов или все балансы нулевые.
    """
    # 1. Считаем стоимость выбранных позиций для каждого пользователя
    selections = get_selected_positions(group_id) or {}
    cost_map: dict[int, float] = {}
    for uid, pos_list in selections.items():
        total = 0.0
        for pos in pos_list:
            try:
                qty = float(pos.get("quantity", 0))
                price = float(pos.get("price", 0))
                total += qty * price
            except Exception:
                pass
        cost_map[uid] = round(total, 2)
    # 2. Считаем внесённые платежи
    payments_map = get_payments(group_id) or {}
    # 3. Список всех участников (те, кто что‑то выбрал или оплатил)
    all_users: set[int] = set(cost_map.keys()) | set(payments_map.keys())
    # 3. Вычисляем баланс для каждого участника
    balances: dict[int, float] = {}
    for uid in all_users:
        paid = payments_map.get(uid, 0.0)
        cost = cost_map.get(uid, 0.0)
        balances[uid] = round(paid - cost, 2)
    # 4. Формируем списки должников и кредиторов
    debtors: list[list] = []  # [user_id, amount_owed]
    creditors: list[list] = []  # [user_id, amount_to_receive]
    for uid, bal in balances.items():
        if bal < -0.01:  # должен
            debtors.append([uid, round(-bal, 2)])
        elif bal > 0.01:  # кредитор
            creditors.append([uid, round(bal, 2)])
    # Сортируем по сумме, чтобы оптимизировать количество переводов
    debtors.sort(key=lambda x: x[1], reverse=True)
    creditors.sort(key=lambda x: x[1], reverse=True)
    transfers: list[tuple[int, int, float]] = []
    i = 0
    j = 0
    # Используем два указателя для прохода по спискам
    while i < len(debtors) and j < len(creditors):
        d_uid, d_amount = debtors[i]
        c_uid, c_amount = creditors[j]
        # Определяем сумму перевода
        transfer_amount = round(min(d_amount, c_amount), 2)
        if transfer_amount <= 0:
            # Если что‑то пошло не так, просто выходим
            break
        transfers.append((d_uid, c_uid, transfer_amount))
        # Обновляем остатки
        debtors[i][1] = round(d_amount - transfer_amount, 2)
        creditors[j][1] = round(c_amount - transfer_amount, 2)
        # Если должник погасил долг, переходим к следующему должнику
        if debtors[i][1] <= 0.01:
            i += 1
        # Если кредитор получил всё, переходим к следующему кредитору
        if creditors[j][1] <= 0.01:
            j += 1
    return transfers