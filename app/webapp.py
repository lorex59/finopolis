import os
import time
import logging
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Template

from app.database import load_positions
from app.database import (
    get_positions,
    set_assignment,
    save_selected_positions,
)
from aiogram.utils.web_app import safe_parse_webapp_init_data
from config import settings

# --- Логирование -------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,  # максимум информации
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("webapp")

app = FastAPI()
_STARTED_AT_MONO = time.monotonic()


def render_receipt_page(positions: list[dict]) -> str:
    logger.debug("Формируем HTML для %d позиций", len(positions))
    import json
    normalized = []
    for p in positions:
        if isinstance(p, dict):
            name = p.get("name")
            quantity = p.get("quantity")
            price = p.get("price")
        else:
            name = getattr(p, "name", None)
            quantity = getattr(p, "quantity", None)
            price = getattr(p, "price", None)
        normalized.append({"name": name, "quantity": quantity, "price": price})
    logger.debug("Нормализованные позиции: %s", normalized)

    positions_json = json.dumps(normalized, ensure_ascii=False)
    template_path = __file__.replace("webapp.py", "templates/receipt.html")
    logger.debug("Путь к шаблону: %s", template_path)

    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()

    replacement = f"const positions = {positions_json};"
    if "const positions =" in html:
        html = html.replace("const positions = window.POSITIONS || [];", replacement)
    else:
        injection = f"<script>window.POSITIONS = {positions_json};</script>"
        html = html.replace("</head>", f"{injection}\n</head>")

    try:
        from config import settings as _settings
        backend_url = _settings.backend_url
    except Exception as e:
        logger.warning("Не удалось получить backend_url: %s", e)
        backend_url = ""

    if backend_url:
        logger.debug("Инжектируем BACKEND_URL: %s", backend_url)
        backend_injection = f"<script>const BACKEND_URL = '{backend_url}';</script>"
        html = html.replace("</head>", f"{backend_injection}\n</head>")

    return html


@app.get("/webapp/receipt", response_class=HTMLResponse)
async def get_receipt_page(request: Request):
    logger.info("GET /webapp/receipt params=%s", request.query_params)
    all_positions = load_positions() or {}
    group_id = request.query_params.get('group_id')
    positions = all_positions.get(str(group_id), []) if group_id else []
    logger.debug("Найдено %d позиций для group_id=%s", len(positions), group_id)
    html = render_receipt_page(positions)
    return HTMLResponse(content=html, status_code=200)


@app.get("/webapp/api/positions", response_class=JSONResponse)
async def api_positions(request: Request):
    logger.info("GET /webapp/api/positions params=%s", request.query_params)
    group_id = request.query_params.get('group_id')
    all_positions = load_positions() or {}
    positions = all_positions.get(str(group_id), []) if group_id else []
    logger.debug("Отдаём %d позиций для group_id=%s", len(positions), group_id)
    return JSONResponse(content=positions, status_code=200)


@app.post("/webapp/api/submit", response_class=JSONResponse)
async def submit_selection(request: Request):
    logger.info("POST /webapp/api/submit")
    try:
        body = await request.json()
        logger.debug("Тело запроса: %s", body)
    except Exception as e:
        logger.error("Ошибка парсинга JSON: %s", e, exc_info=True)
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    init_data = body.get("_auth")
    if not init_data:
        logger.warning("Запрос без _auth")
        return JSONResponse({"error": "Missing _auth"}, status_code=400)

    try:
        parsed = safe_parse_webapp_init_data(init_data=init_data, token=settings.bot_token)
        user = parsed.user
    except Exception as e:
        logger.error("Ошибка проверки _auth: %s", e, exc_info=True)
        return JSONResponse({"error": "Invalid _auth"}, status_code=403)

    if user is None:
        logger.warning("Не удалось извлечь пользователя из _auth")
        return JSONResponse({"error": "Invalid user"}, status_code=403)

    group_id = body.get("group_id")
    selected_data = body.get("selected", {})
    logger.debug("group_id=%s user_id=%s selected=%s", group_id, user.id, selected_data)

    if not group_id:
        return JSONResponse({"error": "Missing group_id"}, status_code=400)

    group_id_str = str(group_id)
    user_id_int = user.id
    indices: list[int] = []

    if isinstance(selected_data, dict):
        for idx_str, qty in selected_data.items():
            try:
                idx = int(idx_str)
                q = int(float(qty))
                indices.extend([idx] * max(q, 0))
            except Exception as e:
                logger.warning("Пропуск некорректного selected_data: %s", e)
    elif isinstance(selected_data, list):
        for i in selected_data:
            try:
                indices.append(int(i))
            except Exception as e:
                logger.warning("Пропуск некорректного индекса: %s", e)

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


def _check_positions_store() -> dict:
    logger.debug("Проверка хранилища позиций")
    try:
        data = load_positions() or {}
        ok = isinstance(data, dict)
        return {
            "status": "ok" if ok else "error",
            "groups": len(data) if ok else 0,
            "type": type(data).__name__,
        }
    except Exception as e:
        logger.error("Ошибка проверки хранилища: %s", e, exc_info=True)
        return {"status": "error", "error": str(e)}


def _check_template() -> dict:
    logger.debug("Проверка шаблона receipt.html")
    try:
        template_path = __file__.replace("webapp.py", "templates/receipt.html")
        exists = os.path.isfile(template_path)
        return {
            "status": "ok" if exists else "missing",
            "path": template_path,
        }
    except Exception as e:
        logger.error("Ошибка проверки шаблона: %s", e, exc_info=True)
        return {"status": "error", "error": str(e)}


@app.get("/health", response_class=JSONResponse)
async def health():
    logger.info("GET /health")
    details = {
        "template_receipt_html": _check_template(),
        "positions_store": _check_positions_store(),
    }
    overall = "ok"
    for d in details.values():
        if d.get("status") in {"error", "missing"}:
            overall = "degraded"
            break

    uptime = round(time.monotonic() - _STARTED_AT_MONO, 2)
    logger.debug("Uptime=%.2f, details=%s", uptime, details)

    return JSONResponse(
        {"status": overall, "uptime_seconds": uptime, "details": details},
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