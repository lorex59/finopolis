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

from app.database import load_positions

import os, time  # ⬅ добавили
app = FastAPI()

_STARTED_AT_MONO = time.monotonic()


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


@app.get("/webapp/receipt", response_class=HTMLResponse)
async def get_receipt_page(request: Request):
    """
    Возвращает страницу мини‑приложения для выбора позиций. Если передан параметр
    group_id, загружает только позиции для этой группы. В противном случае
    возвращает пустой список позиций.
    """
    # Загружаем все позиции из файла (dict[group_id -> list])
    all_positions = load_positions() or {}
    # Определяем идентификатор группы (чата), для которого нужно загрузить позиции.
    # В первую очередь берём параметр group_id, переданный в URL напрямую.
    group_id = request.query_params.get('group_id')
    if not group_id:
        # Если group_id не указан, пробуем извлечь его из tgWebAppStartParam,
        # который Telegram передаёт при открытии мини‑приложения через deep‑link.
        start_param = request.query_params.get('tgWebAppStartParam')
        if start_param and isinstance(start_param, str) and start_param.startswith('group_'):
            group_id = start_param[len('group_'):]
    if group_id:
        positions = all_positions.get(str(group_id), [])
    else:
        # Если нет group_id, не выдаём никакие позиции
        positions = []
    html = render_receipt_page(positions)
    return HTMLResponse(content=html, status_code=200)

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