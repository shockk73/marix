import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Callable

import httpx

from config import OPENROUTER_MAX_TURNS, LLM_HISTORY_SIZE
import db as db_module
from llm.client import OpenRouterClient
from llm.history import to_openai_messages
from llm.prompt import build_system_prompt
from llm.tools import TOOL_SCHEMAS, ToolContext, dispatch_tool

logger = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))


def _default_now() -> datetime:
    return datetime.now(_MSK)


class LLMAgent:
    def __init__(
        self,
        bot: Any,
        client: OpenRouterClient,
        now_provider: Callable[[], datetime] = _default_now,
    ) -> None:
        self._bot = bot
        self._client = client
        self._now = now_provider
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    async def run_turn(self, user_id: int, text: str, user_name: str | None) -> None:
        async with self._lock_for(user_id):
            await db_module.set_user_name(user_id, user_name)
            await db_module.insert_chat_message(user_id, "user", content=text)
            await self._drive_turn(user_id)

    async def _drive_turn(self, user_id: int) -> None:
        for _turn in range(OPENROUTER_MAX_TURNS):
            try:
                rows = await db_module.get_recent_chat_messages(user_id, LLM_HISTORY_SIZE)
                stored_name = await db_module.get_user_name(user_id)
                messages = [{"role": "system",
                             "content": build_system_prompt(
                                 now=self._now(), user_name=stored_name,
                             )}]
                messages.extend(to_openai_messages(rows))
                msg = await self._client.chat_completion(
                    messages=messages, tools=TOOL_SCHEMAS,
                )
            except httpx.HTTPError as e:
                logger.warning("LLM HTTP error: %s", e)
                err = "Не получилось связаться с AI, попробуй ещё раз позже."
                await db_module.insert_chat_message(user_id, "assistant", content=err)
                await self._bot.send_message(user_id, err)
                return
            except Exception as e:
                logger.exception("LLM unexpected error: %s", e)
                err = "Что-то сломалось на стороне AI. Попробуй ещё раз."
                await db_module.insert_chat_message(user_id, "assistant", content=err)
                await self._bot.send_message(user_id, err)
                return

            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")

            if not tool_calls:
                await db_module.insert_chat_message(user_id, "assistant", content=content)
                if content:
                    await self._bot.send_message(user_id, content)
                await db_module.prune_chat_messages(user_id, LLM_HISTORY_SIZE * 2)
                return

            await db_module.insert_chat_message(
                user_id, "assistant", content=content,
                tool_calls=json.dumps(tool_calls),
            )

            ctx = ToolContext(user_id=user_id)
            for tc in tool_calls:
                tc_id = tc["id"]
                fn = tc["function"]
                name = fn["name"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name == "ask_user":
                    # Placeholder — реальная обработка добавится в следующем таске.
                    result = json.dumps({"error": "ask_user not yet wired"})
                else:
                    result = await dispatch_tool(name, args, ctx)
                await db_module.insert_chat_message(
                    user_id, "tool", content=result, tool_call_id=tc_id,
                )

        msg_text = "Запутался — попробуй переформулировать."
        await db_module.insert_chat_message(user_id, "assistant", content=msg_text)
        await self._bot.send_message(user_id, msg_text)
        await db_module.prune_chat_messages(user_id, LLM_HISTORY_SIZE * 2)
