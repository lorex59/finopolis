"""
Объединённый LLM-интерфейс:
- распознавание чеков по изображению
- расчёт "кто сколько кому должен"
"""
import aiohttp
from typing import BinaryIO

from config import settings

import aiohttp
from config import settings

from openai import OpenAI
import io
import json

from config import settings

# services/llm_api.py

import os
import io
import base64
import asyncio
from typing import List
from pydantic import BaseModel, Field, RootModel

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

# 1) Модель структурированного ответа
class Item(BaseModel):
    name: str = Field(description="Название позиции")
    quantity: float = Field(description="Количество (число)")
    price: float = Field(description="Цена за единицу (число)")

class ReceiptItems(RootModel[List[Item]]):
    pass

# 2) Инициализация LLM через OpenRouter (OpenAI-совместимый API)
#    Храните ключ в переменной окружения OPENROUTER_API_KEY
llm = ChatOpenAI(
    model="qwen/qwen2.5-vl-72b-instruct:free",
    api_key=os.environ.get("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1",
    # При желании можно прокинуть служебные заголовки OpenRouter:
    default_headers={
        "HTTP-Referer": "http://localhost",   # опционально
        "X-Title": "Receipt Parser",          # опционально
    },
    temperature=0,
)


# 3) Оборачиваем LLM, чтобы он ВОЗВРАЩАЛ строго список Item
structured_llm = llm.with_structured_output(
    ReceiptItems,
    include_raw=True,
    method="json_schema",   # важно: не function_calling
)


PROMPT = (
    "Распознай этот чек и верни строго JSON массив объектов с полями "
    "`name` (строка), `quantity` (число), `price` (число). Только JSON-массив, без комментариев."
)

async def extract_items_from_image(image_bin: io.BytesIO):
    """
    Отправляет изображение чека и возвращает:
      - список Item (Pydantic-модели)
      - usage-метаданные (токены)
    """
    image_bin.seek(0)
    b64_img = base64.b64encode(image_bin.read()).decode()

    # Формируем мультимодальное сообщение:
    # текст + блок с картинкой в формате OpenAI Chat Completions
    msg = HumanMessage(
        content=[
            {"type": "text", "text": PROMPT},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"},
            },
        ]
    )

    # Асинхронный вызов
    ai_response = await structured_llm.ainvoke([msg])

    items = ai_response["parsed"].root
    usage = (ai_response["raw"].usage_metadata or {})  # input_tokens/output_tokens/total_tokens

    return items, usage




async def calculate_debts_from_messages(items: dict[str, float], messages: list[str]) -> dict[int, float]:
    """
    Отправляет позиции и сообщения в LLM, получает user_id → сумма.
    """
    prompt = (
        "Вот список покупок (позиции и цены):\n"
        f"{items}\n\n"
        "А вот сообщения пользователей:\n"
        f"{messages}\n\n"
        "Распредели позиции между участниками и рассчитай, кто сколько заплатил. "
        "Верни JSON {user_id: сумма}"
    )

    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
    json_payload = {
        "model": "openrouter/gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
    }

    async with aiohttp.ClientSession() as session:
        async with session.post("https://openrouter.ai/api/v1/chat/completions",
                                json=json_payload, headers=headers) as resp:
            resp.raise_for_status()
            result = await resp.json()
            # здесь предполагаем что в ответе content — JSON строка
            return eval(result["choices"][0]["message"]["content"])  # замените eval на safe parser
