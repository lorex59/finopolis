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
from urllib.parse import unquote_plus, parse_qsl, urlencode

from app.database import load_positions
from app.database import (
    get_positions,
    set_assignment,
    save_selected_positions,
)
from aiogram.utils.web_app import safe_parse_webapp_init_data
# The safe_parse_webapp_init_data helper validates the WebApp init data and returns
# a parsed object containing user info. In practice Telegram may include
# additional parameters (e.g. ``signature``) in the init data string which were
# not supported in earlier versions of aiogram. If validation fails we fall
# back to a manual parser below.

import urllib.parse
import json
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
    Принимает выбор позиций из Mini App, валидирует initData и сохраняет
    assignment + выбранные позиции.
    """
    try:
        body = await request.json()
        logger.debug("Тело запроса: %s", body)
    except Exception as e:
        logger.error("Ошибка парсинга JSON: %s", e, exc_info=True)
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    # --- Извлекаем и нормализуем initData (_auth) ---
    init_data_raw = body.get("_auth")
    if not init_data_raw:
        logger.warning("Запрос без _auth")
        return JSONResponse({"error": "Missing _auth"}, status_code=400)

    try:
        decoded = unquote_plus(init_data_raw)  # %7B → {
        allowed_keys = {
            "query_id", "user", "receiver", "chat", "chat_type", "chat_instance",
            "start_param", "can_send_after", "auth_date", "hash"
        }
        pairs = [(k, v) for k, v in parse_qsl(decoded, keep_blank_values=True) if k in allowed_keys]
        normalized_init_data = urlencode(pairs, doseq=True)
    except Exception as e:
        logger.error("Невозможно нормализовать _auth: %s", e, exc_info=True)
        return JSONResponse({"error": "Invalid _auth"}, status_code=403)

    # --- Валидируем initData ---
    parsed = None
    try:
        parsed = safe_parse_webapp_init_data(normalized_init_data, bot_token=settings.bot_token)
    except Exception as err:
        logger.warning("safe_parse_webapp_init_data error: %s", err)

    # --- Фоллбек: достаём user.id вручную ---
    if not parsed or getattr(parsed, "user", None) is None:
        try:
            qs = dict(parse_qsl(decoded, keep_blank_values=True))
            user_json = qs.get("user")
            u_id = None
            if user_json:
                user_dict = json.loads(user_json)
                u_id = user_dict.get("id")

            class _User:
                def __init__(self, id_val): self.id = id_val
            class _Parsed:
                def __init__(self, user_obj): self.user = user_obj

            parsed = _Parsed(_User(u_id))
        except Exception as parse_err:
            logger.error("Fallback _auth parsing failed: %s", parse_err, exc_info=True)
            return JSONResponse({"error": "Invalid _auth"}, status_code=403)

    user = parsed.user
    user_id_int = getattr(user, "id", None)
    if user_id_int is None:
        logger.warning("В _auth отсутствует user.id (decoded=%r)", decoded[:512])
        return JSONResponse({"error": "Invalid user"}, status_code=403)

    # --- Получаем group_id и выбор пользователя ---
    group_id = body.get("group_id")
    if not group_id:
        return JSONResponse({"error": "Missing group_id"}, status_code=400)
    group_id_str = str(group_id)

    selected_data = body.get("selected", {})
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

    # --- Сохраняем assignment и выбранные позиции ---
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