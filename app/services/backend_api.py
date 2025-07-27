"""
Отправка фото чека во внешний бэкенд.
"""
import aiohttp
from typing import BinaryIO

from config import settings

async def parse_receipt(image: BinaryIO) -> dict[str, float]:
    """
    Отправляет файл, ожидает JSON {item: price}.
    """
    async with aiohttp.ClientSession() as session:
        data = aiohttp.FormData()
        data.add_field("file", image, filename="receipt.jpg", content_type="image/jpeg")
        headers = {"X-API-KEY": "demo-key"}
        async with session.post(settings.backend_url, data=data, headers=headers, timeout=30) as resp:
            resp.raise_for_status()
            return await resp.json()
