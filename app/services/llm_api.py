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

# Prompt template for extracting positions from a free‑form Russian text.
TEXT_POSITIONS_PROMPT = (
    "Ты — помощник, который извлекает список покупок из текста, написанного на русском. "
    "Тебе нужно вернуть строго JSON‑массив объектов с полями name (строка), quantity (число) и price (число). "
    "Если количество не указано — считать его равным 1. "
    "Не добавляй никаких пояснений, только JSON‑массив. "
    "Пример: пользователь пишет: «Добавь в позиции такси за 300 рублей и дом за 10к». "
    "Ты возвращаешь: [{\"name\": \"такси\", \"quantity\": 1, \"price\": 300}, {\"name\": \"дом\", \"quantity\": 1, \"price\": 10000}]. "
    "Текст: {text}"
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
      - add_position: добавление новой позиции по тексту (например, "добавь в позиции такси за 300")
      - finalize: запрос на завершение расчёта
      - help: запрос на помощь
      - unknown: иное

    Если модель недоступна, используется простая эвристическая классификация.
    """
    # Если LLM не инициализирован, используем эвристику как раньше.
    if _text_llm is None:
        # Если LLM недоступен, используем эвристику, чтобы не терять важные намерения
        return classify_message_heuristic(text)
    # Системное сообщение описывает задачу классификации. Мы просим модель
    # ответить только одним словом без точек и лишних символов. Это упрощает
    # последующую обработку ответа.
    system_prompt = (
        "Ты помощник по классификации. Категоризируй пользовательский запрос "
        "на одну из категорий: greet, list_positions, calculate, delete_position, "
        "edit_position, add_position, finalize, help, unknown. Ответь только названием категории "
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
            "add_position",
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
    # добавление позиции: проверяем до списка, чтобы избежать перехвата слова "позиции"
    if any(word in lowered for word in ["добав", "добавь", "добавить", "прибав", "прибавь"]):
        # фразы вроде "добавь", "хочу добавить", "добавь позицию" указывают на необходимость
        # добавить новые позиции по тексту. Возвращаем add_position.
        return "add_position"
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

# ---------------------------------------------------------------------------
# Новый функционал: разбор позиций из текстовых сообщений через LLM
# ---------------------------------------------------------------------------
async def extract_items_from_text(text: str) -> list[Item]:
    """
    Извлекает список позиций из свободного текста с помощью LLM.

    Принимает текстовое сообщение от пользователя, которое содержит названия
    товаров, их количество и цену. Возвращает список объектов Item. Для разбора
    используется json_schema, поэтому модель вернёт строго список Item в
    соответствии со схемой. Если количество в тексте не указано, считается 1.

    Args:
        text: строка с описанием покупок

    Returns:
        list[Item]: список распознанных позиций

    Raises:
        любое исключение, возникающее при вызове structured_llm
    """
    # Подставляем пользовательский текст в шаблон промпта
    prompt = TEXT_POSITIONS_PROMPT.format(text=text)
    from langchain_core.messages import HumanMessage
    msg = HumanMessage(content=prompt)
    # Асинхронный вызов модели
    ai_response = await structured_llm.ainvoke([msg])
    # parsed.root содержит список Item
    items: list[Item] = ai_response["parsed"].root
    return items

# ---------------------------------------------------------------------------
# Новый функционал: разбор позиций из текстовых сообщений
# ---------------------------------------------------------------------------
def _extract_items_from_text_regex(text: str) -> list[dict]:
    """
    Простая эвристика для извлечения позиций из естественного языка.

    Принимает строку с описанием покупок (например,
    "добавь в позиции такси за 300 рублей и пирожок за 2к") и возвращает
    список словарей с ключами ``name``, ``quantity`` и ``price``.

    - ``name`` — название позиции (строка)
    - ``quantity`` — количество (float), по умолчанию 1
    - ``price`` — цена (float) за единицу

    Функция использует регулярные выражения для поиска цен и простые
    эвристики для отделения названия от цены. Она поддерживает числа
    формата 1, 1.5, 1,5, а также суффикс «к»/«k» для тысяч (например, 5к = 5000).

    Если цена не найдена, позиция игнорируется.
    """
    import re

    # приведение строки к нижнему регистру для стабильности поиска
    lowered = text.lower()
    # убираем триггерные слова, которые не относятся к названию
    lowered = re.sub(
        r"\b(добав(ь|ить)?|хочу|в\s*позици(?:и|ю|ях)?|позицию|позиции|позиций)\b",
        " ",
        lowered,
    )
    # заменяем распространённые разделители на единый символ '|'
    # Важно: более длинные фразы ("и ещё", "и еще") заменяем раньше, чем короткое "и",
    # чтобы не порезать слово «ещё» при замене.
    for sep in [",", ";", " и ещё ", " и еще ", " и "]:
        lowered = lowered.replace(sep, "|")
    parts = []
    for p in lowered.split("|"):
        part = p.strip()
        if not part:
            continue
        # убираем оставшееся слово "ещё" или "еще" в начале фрагмента, если оно осталось
        if part.startswith("ещё") or part.startswith("еще"):
            # удаляем только само слово, оставляя последующий текст
            part = part.split(" ", 1)[1].strip() if " " in part else ""
        if part:
            parts.append(part)
    items: list[dict] = []
    for part in parts:
        # Находим все числовые токены в части. Каждый токен состоит из числа с возможной
        # десятичной точкой/запятой и опциональным суффиксом k/к.
        num_matches = re.findall(r"(\d+[\.,]?\d*)\s*(к|k|к)?", part)
        if not num_matches:
            continue
        # Обрабатываем список чисел: конвертируем в float, учитывая суффикс тысяч
        numbers: list[float] = []
        for num_str, suffix in num_matches:
            try:
                val = float(num_str.replace(",", "."))
            except ValueError:
                continue
            if suffix:
                val *= 1000
            numbers.append(val)
        if not numbers:
            continue
        # Если найдено более одного числа, первое трактуем как количество, второе — как цену.
        # Иначе, если одно число, количество считаем равным 1, а число — ценой.
        if len(numbers) >= 2:
            quantity_val = numbers[0]
            price_val = numbers[1]
        else:
            quantity_val = 1.0
            price_val = numbers[0]
        # Очищаем название: удаляем все числовые токены вместе с суффиксами,
        # а также служебные слова и обозначения. Работаем с копией исходного текста части.
        name_candidate = part
        # Удаляем все числа с возможными суффиксами (например "10к", "50", "2.5")
        name_candidate = re.sub(r"\d+[\.,]?\d*\s*(к|k|к)?", "", name_candidate)
        # Удаляем указания умножения и единицы (x, ×, шт и др.)
        name_candidate = re.sub(r"\b(?:x|×|шт|штуки|штук|по|за)\b", "", name_candidate)
        # Удаляем валюту, но только как самостоятельные слова
        name_candidate = re.sub(r"\b(?:руб(?:\.|лей)?|р(?:\.)?|₽)\b", "", name_candidate)
        # Удаляем символы 'x' или '×' оставшиеся после чисел (без границ слова)
        name_candidate = re.sub(r"[x×]", "", name_candidate)
        # Сжимаем пробелы
        name_candidate = re.sub(r"\s+", " ", name_candidate).strip()
        # Если после очистки название пустое, используем заглушку
        name_final = name_candidate or "позиция"
        items.append({
            "name": name_final,
            "quantity": quantity_val,
            "price": price_val
        })
    return items
