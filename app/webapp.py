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

from app.database import get_positions
import uvicorn

app = FastAPI()

def render_receipt_page(positions: list[dict]) -> str:
    """Render the receipt selection page with embedded positions."""
    # We embed the positions as a JSON array into the page using a
    # <script> tag. This avoids the need for a full templating engine.
    import json
    positions_json = json.dumps([
        {"name": p.get("name"), "quantity": p.get("quantity"), "price": p.get("price")}
        for p in positions
    ])
    with open(
        __file__.replace("webapp.py", "templates/receipt.html"), "r", encoding="utf-8"
    ) as f:
        html = f.read()
    # Inject the positions into a global JavaScript variable
    injection = f"<script>window.POSITIONS = {positions_json};</script>"
    return html.replace("</head>", f"{injection}\n</head>")


@app.get("/webapp/receipt", response_class=HTMLResponse)
async def get_receipt_page(request: Request):
    positions = get_positions()
    html = render_receipt_page(positions)
    return HTMLResponse(content=html, status_code=200)


if __name__ == "__main__":
    uvicorn.run(
        "app.webapp:app",
        host="127.0.0.1",
        port=8432,
        reload=True,
        ssl_certfile=r"cert\localhost+2.pem",
        ssl_keyfile=r"cert\localhost+2-key.pem"
    )