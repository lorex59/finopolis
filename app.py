"""Пример минимального бота ВКонтакте, который отвечает пользователям
личным сообщением. Подготовлен для python-vk_api >= 11.9.
"""
import os
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.utils import get_random_id

VK_TOKEN = os.getenv("vk_api")  # Токен сообщества в переменных окружения
GROUP_ID = 231492060  # ID вашего сообщества

# Инициализируем сессию и объекты API
vk_session = vk_api.VkApi(token=VK_TOKEN)
longpoll = VkBotLongPoll(vk_session, group_id=GROUP_ID)
vk = vk_session.get_api()

# Простейшие хранилища, если понадобится сохранять данные о пользователях
user_data = {}
user_state = {}

def send_private_message(user_id: int, text: str) -> None:
    """Отправляет личное сообщение пользователю от имени сообщества."""
    vk.messages.send(
        peer_id=user_id,             # Для личного сообщения достаточно ID пользователя
        random_id=get_random_id(),   # Уникальный random_id предотвращает дубли
        message=text
    )

def main() -> None:
    print("Bot started — listening for events…")
    for event in longpoll.listen():
        # Отслеживаем входящие личные сообщения пользователям
        if event.type == VkBotEventType.MESSAGE_NEW and event.from_user:
            user_id = event.message["from_id"]
            text = event.message.get("text", "").strip().lower()

            if text == "/start":
                send_private_message(user_id, "Привет! Я бот и теперь могу писать в личку 😉")
            else:
                send_private_message(user_id, f"Вы написали: {text or '...' }")

if __name__ == "__main__":
    main()
