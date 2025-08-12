"""
Точка входа.
"""
import asyncio
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import settings
from handlers import auth as auth_handlers
from handlers import receipts as receipt_handlers
from middlewares.auth_required import AuthRequiredMiddleware


async def main() -> None:
    print("Bot_token:", settings.bot_token)
    
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    dp = Dispatcher()
    
    dp.include_router(auth_handlers.router)
    dp.include_router(receipt_handlers.router)

    # Подключаем обработчик естественного языка. Он должен идти последним,
    # чтобы перехватывать только те сообщения, которые не были обработаны
    # предыдущими роутерами (командами и коллбэками).
    from handlers.nlu import nlu_router
    dp.include_router(nlu_router)

    # Подмешиваем middleware только к группам, где id < 0
    dp.message.middleware(AuthRequiredMiddleware())
    print("Bot started.")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped.")
