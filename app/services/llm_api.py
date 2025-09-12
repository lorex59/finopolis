"""
Объединённый LLM-интерфейс:
- распознавание чеков по изображению
- расчёт "кто сколько кому должен"
"""
import aiohttp
from typing import BinaryIO
import re

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


# --- NLU functions ---

# Дополнительная модель для текстовых классификаций.
# Мы создаём отдельный экземпляр ChatOpenAI для обработки текстовых запросов.
# При инициализации мы используем тот же API‑ключ и базовый URL, что и для
# визуально‑текстовой модели выше. Если переменная окружения OPENROUTER_API_KEY
# отсутствует, вызов LLM будет завершаться ошибкой. В таком случае можно
# переопределить окружение или настроить fallback в вызывающем коде.
try:
    from langchain_openai import ChatOpenAI as _ChatOpenAI
    _text_llm = _ChatOpenAI(
        model="qwen/qwen2.5-vl-72b-instruct:free",
        api_key=os.environ.get("OPENROUTER_API_KEY"),
        base_url="https://openrouter.ai/api/v1",
        temperature=0,
    )
except Exception:
    _text_llm = None  # LLM недоступен; fallback будет выполнен ниже


async def classify_intent_llm(text: str) -> str:
    """
    Классифицирует входящее сообщение при помощи модели Qwen.

    Модель должна вернуть одну из следующих меток:

      - greet: приветствие
      - list_positions: запрос списка позиций
      - calculate: запрос на расчёт долга
      - delete_position: запрос на удаление позиции
      - edit_position: запрос на изменение позиции
      - finalize: запрос на завершение расчёта
      - help: запрос на помощь
      - unknown: иное

    Если модель недоступна, используется простая эвристическая классификация.
    """
    # Если LLM не инициализирован, используем эвристику как раньше.
    if _text_llm is None:
        return classify_message_heuristic(text)
    # Системное сообщение описывает задачу классификации. Мы просим модель
    # ответить только одним словом без точек и лишних символов. Это упрощает
    # последующую обработку ответа.
    system_prompt = (
        "Ты помощник по классификации. Категоризируй пользовательский запрос "
        "на одну из категорий: greet, list_positions, calculate, delete_position, "
        "edit_position, finalize, help, unknown. Ответь только названием категории "
        "без других слов.\n"
    )
    # Формируем список сообщений для модели: системное и пользовательское
    from langchain_core.messages import SystemMessage, HumanMessage
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=text),
    ]
    try:
        # Асинхронный вызов модели
        response = await _text_llm.ainvoke(messages)
        content = (response.content or "").strip().lower()
        # Иногда модель может вернуть текст вроде "гreet" или со знаками
        # пунктуации. Приведём к стандартному виду и проверим, входит ли
        # результат в допустимый набор. Если нет — помечаем как unknown.
        valid = {
            "greet",
            "list_positions",
            "calculate",
            "delete_position",
            "edit_position",
            "finalize",
            "help",
            "unknown",
        }
        # Удаляем все символы, кроме латинских букв, цифр и подчёркивания
        import re
        cleaned = re.sub(r"[^a-zA-Z0-9_]+", "", content)
        return cleaned if cleaned in valid else "unknown"
    except Exception:
        # В случае любой ошибки (тайм‑аут, отсутствие API‑ключа и т.п.)
        # используем эвристическую классификацию
        return classify_message_heuristic(text)


def classify_message_heuristic(text: str) -> str:
    """
    Очень простая эвристическая классификация сообщений. Используется как
    запасной вариант, когда LLM недоступен. Возвращаемая метка совпадает
    с той, что ожидает handle_nlu_message.
    """
    lowered = text.lower()
    # приветствие
    if any(word in lowered for word in ["привет", "здравств", "ку", "hello", "hi"]):
        return "greet"
    # запрос списка позиций
    if any(word in lowered for word in ["список", "позиции", "товары", "что", "добавлено", "покажи"]):
        return "list_positions"
    # запрос на расчёт
    if any(word in lowered for word in ["расчет", "расчёт", "кто", "сколько", "должен", "рассчитать", "поделить"]):
        return "calculate"
    # запрос удаления позиции
    if any(word in lowered for word in ["удали", "удалить", "сотри", "remove", "delete"]):
        return "delete_position"
    # запрос редактирования позиции
    if any(word in lowered for word in ["измени", "изменить", "edit", "редакт"]):
        return "edit_position"
    # запрос завершения расчёта
    if any(word in lowered for word in ["финал", "заверш", "законч", "подтверж", "итог"]):
        return "finalize"
    # помощь
    if "помощ" in lowered or "help" in lowered or "умеешь" in lowered:
        return "help"
    return "unknown"


def classify_message(text: str) -> str:
    """
    Сохранили старую подпись функции для обратной совместимости. Она
    вызывает эвристическую классификацию. Для использования LLM нужно
    вызывать classify_intent_llm из асинхронного контекста. Если в
    каких‑то местах код ещё обращается к classify_message, он будет
    работать по старому механизму.
    """
    return classify_message_heuristic(text)


# --- Text extraction for adding positions ------------------------------------

TEXT_POSITIONS_PROMPT = """
You are a parser. Extract shopping positions from the user's Russian text and return ONLY a JSON array.

Each element must be an object with keys:
- "name": string — short item title (2–6 words), Russian preferred.
- "quantity": number — default 1 if not stated. Accept decimals (e.g., 0.5).
- "price": number — unit price in RUB. If user gives total sum for multiple units, infer unit price = total / quantity.

Rules:
- Ignore chatter and greetings.
- Russian input like "такси за 300" → [{"name":"такси","quantity":1,"price":300}]
- "ещё за дом за 10к" → [{"name":"дом","quantity":1,"price":10000}]
- Support slang like "к", "косарей" (= *1000).
- If price currency not mentioned, assume RUB.
- If nothing to extract, return [] (empty array).
Output: STRICT JSON array, no comments.
"""

async def extract_items_from_text(text: str) -> list[dict]:
    """
    Parse free-form text into positions (name, quantity, price). Falls back to a
    lightweight regex if the LLM is unavailable.
    """
    try:
        headers = {
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        json_payload = {
            "model": settings.openrouter_model or "anthropic/claude-3.5-sonnet",
            "messages": [
                {"role":"system","content": TEXT_POSITIONS_PROMPT},
                {"role":"user","content": text},
            ],
            "temperature": 0.0,
            "response_format": {"type":"json_object","schema": {"type":"array"}}
        }
        async with aiohttp.ClientSession() as session:
            async with session.post("https://openrouter.ai/api/v1/chat/completions",
                                    json=json_payload, headers=headers) as resp:
                resp.raise_for_status()
                result = await resp.json()
                raw = result["choices"][0]["message"]["content"]
                try:
                    data = json.loads(raw)
                    if isinstance(data, list):
                        norm = []
                        for it in data:
                            name = str(it.get("name","")).strip()
                            if not name:
                                continue
                            qty = float(it.get("quantity", 1) or 1)
                            price = float(it.get("price", 0) or 0)
                            norm.append({"name": name, "quantity": qty, "price": price})
                        return norm
                except Exception:
                    pass
    except Exception:
        # fall back
        pass
    # Heuristic fallback: find fragments like "<word(s)> за <number>"
    items = []
    text_l = text.lower().replace(" ", " ")
    # normalize "10к" -> 10000
    def parse_amount(tok: str) -> float:
        tok = tok.replace(" ", "")
        m = re.match(r"(\d+(?:[.,]\d+)?)(k|к|тыс|тысяч|к?осар[ьяе]|т|тр)?", tok)
        if not m:
            return 0.0
        num = float(m.group(1).replace(",", "."))
        mul = m.group(2)
        if mul:
            return num * 1000.0
        return num
    for m in re.finditer(r"([а-яa-z0-9\s\-]+?)\s+за\s+(\d+[.,]?\d*\s*[кk]?)", text_l):
        name = m.group(1).strip(" ,.-")
        amt = parse_amount(m.group(2))
        if name and amt>0:
            items.append({"name": name, "quantity": 1.0, "price": float(amt)})
    return items

# Strengthen intent classifier prompt
INTENT_PROMPT = """
Classify the user's Russian message into one of intents:
- "add_position" — they ask to add items/positions to the current receipt (e.g., 'добавь такси за 300', 'ещё кофе за 120', 'пиво 2 по 150').
- "show_positions" — they want to show/display positions (/show_position, 'покажи позиции').
- "calculate" — they want to calculate/settle debts ('посчитай', 'кто кому должен', 'итог').
- "help" — help/about commands.
- "unknown" — none of the above.

Return ONLY the label string.
"""

async def classify_intent_llm(text: str) -> str:
    try:
        headers = {
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        json_payload = {
            "model": settings.openrouter_model or "anthropic/claude-3.5-sonnet",
            "messages": [
                {"role":"system","content": INTENT_PROMPT},
                {"role":"user","content": text},
            ],
            "temperature": 0.0,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post("https://openrouter.ai/api/v1/chat/completions",
                                    json=json_payload, headers=headers) as resp:
                resp.raise_for_status()
                result = await resp.json()
                content = result["choices"][0]["message"]["content"].strip().lower()
                content = re.sub(r"[^a-z_]+", "", content)
                return content if content in {"add_position","show_positions","calculate","help","unknown"} else "unknown"
    except Exception:
        return classify_message_heuristic(text)
