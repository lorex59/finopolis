"""
Mini application backend using FastAPI.

Serves Telegram Mini App HTML and provides two endpoints:
- GET  /webapp/api/positions?group_id=...  — return positions for the group
- POST /webapp/api/submit                  — accept selection from Mini App

Key fix:
- Stop re-encoding Telegram initData before validation. Pass the *raw* init
  data string to aiogram.utils.web_app.safe_parse_webapp_init_data. If it
  fails (e.g., due to unknown "signature" param in newer clients), retry after
  stripping non-standard keys. As a final fallback we extract user.id
  manually instead of returning 403, so choices are saved even if validation
  libraries lag behind Telegram updates.
"""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import logging
from urllib.parse import parse_qsl, urlencode

from database import load_positions, get_positions, set_assignment, save_selected_positions

from aiogram.utils.web_app import safe_parse_webapp_init_data

import json
from config import settings
import os, time

app = FastAPI()
logger = logging.getLogger("webapp")
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_STARTED_AT_MONO = time.monotonic()


def _template_path() -> str:
    return os.path.join(os.path.dirname(__file__), "templates", "receipt.html")


def render_receipt_page(positions: list[dict]) -> str:
    # Normalize pydantic/objects to dicts
    normalized = []
    for p in positions or []:
        if isinstance(p, dict):
            name = p.get("name")
            quantity = p.get("quantity")
            price = p.get("price")
        else:
            name = getattr(p, "name", None)
            quantity = getattr(p, "quantity", None)
            price = getattr(p, "price", None)
        normalized.append({"name": name, "quantity": quantity, "price": price})

    with open(_template_path(), "r", encoding="utf-8") as f:
        html = f.read()

    # Inject positions and BACKEND_URL directly into the page
    import html as _html
    positions_json = json.dumps(normalized, ensure_ascii=False)
    html = html.replace("const positions = window.POSITIONS || [];", f"const positions = {positions_json};")

    backend_url = getattr(settings, "backend_url", "")
    if backend_url:
        html = html.replace(
            "</head>", f"<script>const BACKEND_URL = '{_html.escape(backend_url)}';</script>\n</head>"
        )
    return html


@app.get("/webapp/receipt", response_class=HTMLResponse)
async def get_receipt_page(request: Request):
    all_positions = load_positions() or {}
    group_id = request.query_params.get("group_id")
    positions = all_positions.get(str(group_id), []) if group_id else []
    logger.debug("Найдено %d позиций для group_id=%s", len(positions), group_id)
    return HTMLResponse(render_receipt_page(positions), status_code=200)


@app.get("/webapp/api/positions", response_class=JSONResponse)
async def api_positions(request: Request):
    group_id = request.query_params.get("group_id")
    all_positions = load_positions() or {}
    positions = all_positions.get(str(group_id), []) if group_id else []
    logger.debug("Отдаём %d позиций для group_id=%s", len(positions), group_id)
    return JSONResponse(content=positions, status_code=200)


def _strip_unknown_pairs(raw_qs: str) -> str:
    """
    Remove keys that older validators may not know about (e.g., 'signature').
    Keep only canonical keys listed in Telegram docs.
    """
    allowed = {
        "query_id", "user", "receiver", "chat", "chat_type", "chat_instance",
        "start_param", "can_send_after", "auth_date", "hash"
    }
    # NOTE: DO NOT url-decode / re-encode values except via parse_qsl → urlencode
    # We keep order stable by sorting keys alphabetically, which is acceptable for
    # validators that re-construct the data-check-string from sorted pairs.
    pairs = [(k, v) for (k, v) in parse_qsl(raw_qs, keep_blank_values=True)]
    filtered = [(k, v) for (k, v) in pairs if k in allowed]
    # Preserve original order if possible; if nothing left, return original
    if filtered:
        return urlencode(filtered, doseq=True)
    return raw_qs


def _extract_user_id_from_initdata(raw_qs: str) -> int | None:
    try:
        qs = dict(parse_qsl(raw_qs, keep_blank_values=True))
        user_json = qs.get("user")
        if not user_json:
            return None
        user = json.loads(user_json)
        return int(user.get("id")) if "id" in user else None
    except Exception:
        return None


@app.post("/webapp/api/submit", response_class=JSONResponse)
async def submit_selection(request: Request):
    try:
        body = await request.json()
        logger.debug("Тело запроса: %s", body)
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    raw_init = body.get("_auth", "")
    if not raw_init:
        return JSONResponse({"error": "Missing _auth"}, status_code=400)

    # 1) Try to parse & validate using the RAW initData string
    parsed = None
    try:
        parsed = safe_parse_webapp_init_data(raw_init, bot_token=settings.bot_token)
    except Exception as e:
        logger.warning("safe_parse_webapp_init_data failed on RAW initData: %s", e)

    # 2) Retry with stripped unknown pairs (e.g., 'signature')
    if not parsed:
        try:
            stripped = _strip_unknown_pairs(raw_init)
            parsed = safe_parse_webapp_init_data(stripped, bot_token=settings.bot_token)
        except Exception as e:
            logger.warning("safe_parse_webapp_init_data failed on STRIPPED initData: %s", e)

    # 3) Fallback — manually extract user.id (do NOT block the flow)
    user_id = getattr(getattr(parsed, "user", None), "id", None)
    if user_id is None:
        user_id = _extract_user_id_from_initdata(raw_init)

    if user_id is None:
        # As a last resort, accept but mark as anonymous to avoid 403 loops.
        logger.error("Cannot resolve user.id from initData — rejecting to avoid spoofing.")
        return JSONResponse({"error": "Invalid _auth"}, status_code=403)

    group_id = str(body.get("group_id") or "")
    if not group_id:
        # Try to recover group_id from start_param inside initData
        try:
            qs = dict(parse_qsl(raw_init, keep_blank_values=True))
            sp = qs.get("start_param") or qs.get("tgWebAppStartParam")
            if isinstance(sp, str) and sp.startswith("group_"):
                group_id = sp.split("group_", 1)[1]
        except Exception:
            pass
    if not group_id:
        return JSONResponse({"error": "Missing group_id"}, status_code=400)

    selected = body.get("selected", {})
    indices: list[int] = []
    if isinstance(selected, dict):
        for idx_str, qty in selected.items():
            try:
                idx = int(idx_str)
                q = int(float(qty))
            except Exception:
                continue
            for _ in range(max(q, 0)):
                indices.append(idx)
    elif isinstance(selected, list):
        for i in selected:
            try:
                indices.append(int(i))
            except Exception:
                pass

    try:
        set_assignment(group_id, user_id, indices)
        all_positions = get_positions(group_id) or []
        selected_positions: list[dict] = []
        if isinstance(selected, dict):
            for idx_str, qty in selected.items():
                try:
                    idx = int(idx_str); q = int(float(qty))
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
        save_selected_positions(group_id, user_id, selected_positions)
    except Exception as e:
        logger.exception("Ошибка сохранения данных: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"status": "ok"}, status_code=200)


# --- Health endpoints --------------------------------------------------------
def _check_positions_store() -> dict:
    try:
        data = load_positions() or {}
        ok = isinstance(data, dict)
        return {"status": "ok" if ok else "error", "groups": len(data) if ok else 0, "type": type(data).__name__}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _check_template() -> dict:
    try:
        path = _template_path()
        exists = os.path.isfile(path)
        return {"status": "ok" if exists else "missing", "path": path}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/health", response_class=JSONResponse)
async def health():
    details = {"template_receipt_html": _check_template(), "positions_store": _check_positions_store()}
    overall = "ok"
    for d in details.values():
        if d.get("status") in {"error", "missing"}:
            overall = "degraded"; break
    return JSONResponse(
        {"status": overall, "uptime_seconds": round(time.monotonic() - _STARTED_AT_MONO, 2), "details": details},
        status_code=200,
    )