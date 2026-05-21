import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import (
    BOT_TOKEN, OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_BASE_URL,
)
from db import init_db
from handlers import router, set_agent
from llm.agent import LLMAgent
from llm.client import OpenRouterClient
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

    llm_client = OpenRouterClient(
        api_key=OPENROUTER_API_KEY,
        model=OPENROUTER_MODEL,
        base_url=OPENROUTER_BASE_URL,
    )
    agent = LLMAgent(bot=bot, client=llm_client)
    set_agent(agent)

    try:
        await dp.start_polling(bot)
    finally:
        await llm_client.close()


if __name__ == "__main__":
    asyncio.run(main())
