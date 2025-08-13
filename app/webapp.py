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
from fastapi.responses import HTMLResponse
from jinja2 import Template

from app.database import load_positions
import uvicorn

app = FastAPI()

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
    # Загружаем позиции из файла. Это обеспечивает корректную работу, даже если бот
    # и WebApp запущены в разных процессах.
    positions = load_positions()
    html = render_receipt_page(positions)
    return HTMLResponse(content=html, status_code=200)


if __name__ == "__main__":
    # На Linux пути с обратным слешем интерпретируются как имя файла, а
    # сертификаты лежат в директории cert. Используем os.path.join для
    # корректного построения пути на разных платформах.
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