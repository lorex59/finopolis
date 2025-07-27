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

import io
import json
import base64
import asyncio
from openai import OpenAI
from config import settings

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=settings.openrouter_api_key,
)

async def extract_items_from_image(image_bin: io.BytesIO) -> list[dict]:
    """
    Асинхронно отправляет изображение чека в OpenRouter через OpenAI SDK.
    Возвращает список позиций вида:
      [{'name': str, 'quantity': float, 'price': float}, ...]
    """
    # Подготовка base64-изображения
    image_bin.seek(0)
    b64_img = base64.b64encode(image_bin.read()).decode()

    prompt = (
        "Распознай этот чек и верни только JSON список:\n"
        "[{\"name\": ..., \"quantity\": ..., \"price\": ...}, ...]\n"
        "Ничего лишнего."
    )

    def sync_request():
        response = client.chat.completions.create(
            model="openai/gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                },
                {
                    "role": "user",
                    "content": {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64_img}"
                        }
                    },
                }
            ]
        )
        return response.choices[0].message.content

    # Выполняем синхронный запрос в отдельном потоке
    content = await asyncio.to_thread(sync_request)  # рекомендуется вместо run_in_executor :contentReference[oaicite:2]{index=2}

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return eval(content)




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
