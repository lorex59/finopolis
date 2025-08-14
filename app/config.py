from dataclasses import dataclass
import os

from dotenv import load_dotenv
load_dotenv()

@dataclass(frozen=True)
class Settings:
    bot_token: str
    backend_url: str
    openrouter_api_key: str
    admin_id: int
    allowed_banks: tuple[str, ...]
    bot_username: str  # имя бота (например, myawesome_bot) для формирования deep‑links

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            bot_token=os.getenv("BOT_TOKEN", ""),
            #backend_url=os.getenv("BACKEND_URL", "https://127.0.0.1:8000"),
            backend_url=os.getenv("BACKEND_URL", "https://127.0.0.1:8432"),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
            admin_id=int(os.getenv("ADMIN_ID", "0")),
            allowed_banks=tuple(os.getenv("ALLOWED_BANKS", "Tinkoff,Sber,Alfa").split(",")),
            bot_username=os.getenv("BOT_USERNAME", ""),
        )

settings = Settings.from_env()
