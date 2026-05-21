import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN
from db import init_db
from handlers import router
from scheduler import init_scheduler, restore_watches

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await init_db()
    init_scheduler(bot)
    await restore_watches()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
