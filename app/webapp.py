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
import time, os
from config import settings
import hmac, hashlib, time, json, os
from app.database import load_positions, set_assignment, save_selected_positions, get_positions

import os, time  # ⬅ добавили
app = FastAPI()

_STARTED_AT_MONO = time.monotonic()

BOT_TOKEN =  settings.bot_token

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
        # В качестве резервного варианта добавим инъекцию в head
        injection = f"<script>window.POSITIONS = {positions_json};</script>"
        html = html.replace("</head>", f"{injection}\n</head>")
    return html

def _telegram_check_init_data(init_data: str) -> dict | None:
    """
    Проверка подписи init_data по правилам Telegram Mini Apps.
    https://docs.telegram-mini-apps.com/platform/init-data
    """
    try:
      data = dict(x.split('=', 1) for x in init_data.split('&'))
      hash_recv = data.pop('hash')
      check_string = '\n'.join(f"{k}={data[k]}" for k in sorted(data.keys()))
      secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
      hash_calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
      if hmac.compare_digest(hash_calc, hash_recv):
          # вернём распарсенные поля как dict (user/receiver/chat и т.д. в JSON)
          for k in list(data.keys()):
              if k in ('user','receiver','chat'):
                  data[k] = json.loads(data[k])
          return data
    except Exception:
      return None
    return None

@app.post("/webapp/api/submit", response_class=JSONResponse)
async def submit_selection(request: Request):
    """
    Fallback для групп/деплинков: принимает выбор из Mini App,
    валидирует init_data и сохраняет выбор в БД.
    """
    body = await request.json()
    selected = body.get("selected") or {}
    group_id = str(body.get("group_id") or "")
    init_data = body.get("init_data") or ""

    user_id = None
    if init_data and BOT_TOKEN:
        parsed = _telegram_check_init_data(init_data)
        if not parsed or "user" not in parsed:
            return JSONResponse({"ok": False, "error": "bad_init_data"}, status_code=400)
        user_id = int(parsed["user"]["id"])

    if not group_id or not isinstance(selected, dict):
        return JSONResponse({"ok": False, "error": "bad_payload"}, status_code=400)

    # Преобразуем словарь {idx: qty} в список индексов (как в хендлере бота)
    indices: list[int] = []
    for idx_str, qty in selected.items():
        try:
            idx = int(idx_str); q = int(float(qty))
        except Exception:
            continue
        indices.extend([idx] * max(q, 0))

    # Сохраняем назначения индексов (для /finalize)
    if user_id is not None:
        set_assignment(group_id, user_id, indices)

        # Сохраним развёрнутые позиции (для /show_position)
        all_positions = get_positions(group_id) or []
        selected_positions: list[dict] = []
        for idx_str, qty in selected.items():
            try:
                idx = int(idx_str); q = int(float(qty))
            except Exception:
                continue
            if 0 <= idx < len(all_positions) and q > 0:
                orig = all_positions[idx]
                selected_positions.append({"name": orig.get("name"), "quantity": q, "price": orig.get("price")})
        save_selected_positions(group_id, user_id, selected_positions)

    return JSONResponse({"ok": True})

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
    return JSONResponse(content=positions, status_code=200)





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


if __name__ == "__main__":
    # На Linux пути с обратным слешем интерпретируются как имя файла, а
    # сертификаты лежат в директории cert. Используем os.path.join для
    # корректного построения пути на разных платформах.
    import uvicorn
    import os
    cert_path = os.path.join("cert", "localhost+2.pem")
    key_path = os.path.join("cert", "localhost+2-key.pem")
    uvicorn.run(
        "app.webapp:app",
        host="127.0.0.1",
        port=8432,
        reload=True,
        ssl_certfile=cert_path,
        ssl_keyfile=key_path,
    )