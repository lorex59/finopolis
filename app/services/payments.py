"""
Заглушка платёжного шлюза/смартконтракта.
"""

import asyncio
from typing import Mapping

async def mass_pay(user_to_amount: Mapping[int, float]) -> str:
    """
    Имитация массового перевода.
    Возвращает ID транзакции.
    """
    await asyncio.sleep(2)        # имитируем время обработки
    return "TX-DEMO-123456"
