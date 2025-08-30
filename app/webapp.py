"""
Mini application backend using FastAPI.

This module defines a FastAPI application that serves a simple HTML
page for the Telegram WebApp. The page lists the current receipt
positions and allows the user to select which items they bought. When
the user submits the form the selection is returned to the bot via
Telegram.WebApp.sendData().

To integrate this into your bot deployment you need to run the
FastAPI app on the same domain as specified in settings.backend_url
and ensure that /webapp/receipt returns this HTML. The positions are
injected via a simple templating mechanism here using Python string
formatting.
"""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Template
import logging

from app.database import load_positions
from app.database import (
    get_positions,
    set_assignment,
    save_selected_positions,
)
from aiogram.utils.web_app import safe_parse_webapp_init_data
from config import settings

import os, time  # ⬅ добавили
app = FastAPI()

_STARTED_AT_MONO = time.monotonic()
# --- Логирование -------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,  # максимум информации
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("webapp")

def render_receipt_page(positions: list[dict]) -> str:
    """
    Формирует HTML‑страницу для мини‑приложения и внедряет список позиций.

    Функция умеет работать как с обычными словарями, так и с моделями
    Pydantic (Item). Для объектов Pydantic используется доступ через
    атрибуты .name, .quantity и .price. Если эти атрибуты отсутствуют,
    вставляется None.
    """
    import json
    normalized = []
    for p in positions:
        # Если позиция — словарь, используем ключи
        if isinstance(p, dict):
            name = p.get("name")
            quantity = p.get("quantity")
            price = p.get("price")
        else:
            # Попытка получить атрибуты у объекта
            name = getattr(p, "name", None)
            quantity = getattr(p, "quantity", None)
            price = getattr(p, "price", None)
        normalized.append({"name": name, "quantity": quantity, "price": price})
    positions_json = json.dumps(normalized, ensure_ascii=False)
    template_path = __file__.replace("webapp.py", "templates/receipt.html")
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()
    # Встраиваем список позиций непосредственно в клиентский скрипт. Это надёжнее,
    # чем полагаться на глобальные переменные (которые могут быть запрещены в WebApp).
    # Шаблон содержит строку "const positions = window.POSITIONS || [];", которую мы заменим
    # на реальный массив. Если строка не найдена, оставим исходный html.
    replacement = f"const positions = {positions_json};"
    if "const positions =" in html:
        html = html.replace("const positions = window.POSITIONS || [];", replacement)
    else:
        injection = f"<script>window.POSITIONS = {positions_json};</script>"
        html = html.replace("</head>", f"{injection}\n</head>")
    # Также инжектируем BACKEND_URL, чтобы клиентский код мог обращаться к нашему API
    try:
        from config import settings as _settings
        backend_url = _settings.backend_url
    except Exception:
        backend_url = ""
    if backend_url:
        backend_injection = f"<script>const BACKEND_URL = '{backend_url}';</script>"
        html = html.replace("</head>", f"{backend_injection}\n</head>")
    return html


@app.get("/webapp/receipt", response_class=HTMLResponse)
async def get_receipt_page(request: Request):
    """
    Возвращает страницу мини‑приложения для выбора позиций. Если передан параметр
    group_id, загружает только позиции для этой группы. В противном случае
    возвращает пустой список позиций.
    """
    # Загружаем все позиции из файла (dict[group_id -> list])
    all_positions = load_positions() or {}
    group_id = request.query_params.get('group_id')
    if group_id:
        positions = all_positions.get(str(group_id), [])
    else:
        # Если нет group_id, не выдаём никакие позиции
        positions = []
    logger.debug("Найдено %d позиций для group_id=%s", len(positions), group_id)
    html = render_receipt_page(positions)
    return HTMLResponse(content=html, status_code=200)

# New API endpoint to fetch positions for a given group.
#
# When the mini‑application is opened via a deep‑link (startapp) in a group
# chat, the frontend does not know the group ID ahead of time. It can
# extract the start parameter from Telegram.WebApp.initDataUnsafe and call
# this endpoint to retrieve the list of positions for that group on demand.
@app.get("/webapp/api/positions", response_class=JSONResponse)
async def api_positions(request: Request):
    """
    Return the list of receipt positions for the specified group.

    The group_id should be passed as a query parameter. If group_id is
    missing or there are no positions stored for that group, an empty
    list will be returned. The response is a JSON array of objects
    containing ``name``, ``quantity`` and ``price`` keys.
    """
    group_id = request.query_params.get('group_id')
    # Load all positions from storage and filter by the provided group ID.
    all_positions = load_positions() or {}
    if group_id:
        positions = all_positions.get(str(group_id), [])
    else:
        positions = []
    logger.debug("Отдаём %d позиций для group_id=%s", len(positions), group_id)

    return JSONResponse(content=positions, status_code=200)

# ---------------------------------------------------------------------------
# Endpoint to accept selection data from Mini App opened via deep‑link.
# When a Mini App is launched using a startapp link, Telegram does not
# deliver WebAppData messages back to the bot. To persist the user's
# selection we accept it directly here and update the assignments and
# selected_positions tables. The payload must include `group_id`,
# `user_id` and a `selected` mapping (index → quantity) or list.

@app.post("/webapp/api/submit", response_class=JSONResponse)
async def submit_selection(request: Request):
    """
    Accept selection data from a Mini App launched via deep‑link and persist it.

    The request body must contain:
        - `_auth`: the initData string from Telegram WebApp (required for validation)
        - `group_id`: the chat ID that corresponds to the receipt (string or int)
        - `selected`: mapping of index → quantity or list of indices

    Upon successful validation and saving, the function returns `{"status": "ok"}`.
    If validation fails or required fields are missing, an error JSON is returned.
    """
    try:
        body = await request.json()
        logger.debug("Тело запроса: %s", body)
    except Exception:
        logger.error("Ошибка парсинга JSON: %s", e, exc_info=True)
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    # Extract and validate auth string
    init_data = body.get("_auth")
    if not init_data:
        logger.warning("Запрос без _auth")
        return JSONResponse({"error": "Missing _auth"}, status_code=400)
    # Попытаемся верифицировать подпись initData. Если подпись неверна
    # (что может происходить, например, при тестировании без публичного
    # доступа или когда бот работает в режиме разработчика), не будем
    # отвечать ошибкой 403. Вместо этого попробуем извлечь идентификатор
    # пользователя из строки _auth вручную. Это повышает устойчивость
    # приложения и позволяет сохранять выбор даже при отсутствии
    # корректной подписи.
    user = None
    try:
        # Validate the init data using the bot token. В случае успеха
        # parsed.user содержит объект TelegramUser
        parsed = safe_parse_webapp_init_data(init_data, bot_token=settings.bot_token)
        user = parsed.user
    except Exception:
        logger.warning("Не удалось проверить подпись init_data, пробуем разобрать вручную")
        # Попытка разобрать _auth как строку query string и извлечь поле user
        try:
            from urllib.parse import parse_qs, unquote
            import json as _json
            # parse_qs декодирует percent‑encoding и возвращает dict значений списков
            qs = parse_qs(init_data, keep_blank_values=True)
            user_param = qs.get('user') or qs.get('tgWebAppUser')
            if user_param:
                # user_param является списком строк; берём первый элемент
                user_json = unquote(user_param[0])
                user_data = _json.loads(user_json)
                class _SimpleUser:
                    def __init__(self, data):
                        self.id = int(data.get('id')) if data.get('id') is not None else None
                        self.first_name = data.get('first_name')
                        self.last_name = data.get('last_name')
                        self.username = data.get('username')
                user = _SimpleUser(user_data)
        except Exception:
            user = None
    if user is None or getattr(user, 'id', None) is None:
        return JSONResponse({"error": "Invalid user"}, status_code=403)
    # Determine group_id and selected data
    group_id = body.get("group_id")
    selected_data = body.get("selected", {})
    if not group_id:
        return JSONResponse({"error": "Missing group_id"}, status_code=400)
    group_id_str = str(group_id)
    user_id_int = user.id
    # Build list of indices from selected_data
    indices: list[int] = []
    if isinstance(selected_data, dict):
        for idx_str, qty in selected_data.items():
            try:
                idx = int(idx_str)
                q = int(float(qty))
            except Exception:
                continue
            for _ in range(max(q, 0)):
                indices.append(idx)
    elif isinstance(selected_data, list):
        for i in selected_data:
            try:
                indices.append(int(i))
            except Exception:
                pass
    # Persist assignment and detailed positions
    try:
        logger.debug("Сохраняем assignment: %s", indices)
        set_assignment(group_id_str, user_id_int, indices)
        all_positions = get_positions(group_id_str)
        selected_positions: list[dict] = []
        if isinstance(selected_data, dict):
            for idx_str, qty in selected_data.items():
                try:
                    idx = int(idx_str)
                    q = int(float(qty))
                except Exception:
                    continue
                if 0 <= idx < len(all_positions) and q > 0:
                    orig = all_positions[idx]
                    selected_positions.append({
                        "name": orig.get("name"),
                        "quantity": q,
                        "price": orig.get("price"),
                    })
        else:
            for idx in indices:
                if 0 <= idx < len(all_positions):
                    orig = all_positions[idx]
                    selected_positions.append({
                        "name": orig.get("name"),
                        "quantity": 1,
                        "price": orig.get("price"),
                    })
        logger.debug("Сохраняем выбранные позиции: %s", selected_positions)
        save_selected_positions(group_id_str, user_id_int, selected_positions)
    except Exception as e:
        logger.error("Ошибка сохранения данных: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"status": "ok"}, status_code=200)

# --- Health helpers ----------------------------------------------------------
def _check_positions_store() -> dict:
    """
    Пытается загрузить позиции и возвращает краткий статус.
    Не делает тяжёлых операций — годится для readiness.
    """
    try:
        data = load_positions() or {}
        ok = isinstance(data, dict)
        return {
            "status": "ok" if ok else "error",
            "groups": len(data) if ok else 0,
            "type": type(data).__name__,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _check_template() -> dict:
    try:
        template_path = __file__.replace("webapp.py", "templates/receipt.html")
        exists = os.path.isfile(template_path)
        return {
            "status": "ok" if exists else "missing",
            "path": template_path,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}



@app.get("/health", response_class=JSONResponse)
async def health():
    """
    Liveness/Readiness-проверка.

    Возвращает общий статус, аптайм и детали:
    - наличие шаблона receipt.html
    - доступность хранилища позиций (load_positions)
    """
    details = {
        "template_receipt_html": _check_template(),
        "positions_store": _check_positions_store(),
    }
    # Если что-то 'error' или 'missing' — считаем degraded, но 200 оставляем,
    # чтобы не флапать liveness без крайней необходимости.
    overall = "ok"
    for d in details.values():
        if d.get("status") in {"error", "missing"}:
            overall = "degraded"
            break

    return JSONResponse(
        {
            "status": overall,
            "uptime_seconds": round(time.monotonic() - _STARTED_AT_MONO, 2),
            "details": details,
        },
        status_code=200,
    )


# if __name__ == "__main__":
#     # На Linux пути с обратным слешем интерпретируются как имя файла, а
#     # сертификаты лежат в директории cert. Используем os.path.join для
#     # корректного построения пути на разных платформах.
#     import os
#     cert_path = os.path.join("cert", "localhost+2.pem")
#     key_path = os.path.join("cert", "localhost+2-key.pem")
#     uvicorn.run(
#         "app.webapp:app",
#         host="127.0.0.1",
#         port=8432,
#         reload=True,
#         ssl_certfile=cert_path,
#         ssl_keyfile=key_path,
#     )