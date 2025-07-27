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

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            bot_token=os.getenv("BOT_TOKEN", ""),
            backend_url=os.getenv("BACKEND_URL", "https://example.com/api/parse"),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
            admin_id=int(os.getenv("ADMIN_ID", "0")),
            allowed_banks=tuple(os.getenv("ALLOWED_BANKS", "Tinkoff,Sber,Alfa").split(",")),
        )

settings = Settings.from_env()
