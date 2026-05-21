import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Callable

import httpx
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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
            await self._auto_cancel_pending(user_id)
            await db_module.insert_chat_message(user_id, "user", content=text)
            await self._drive_turn(user_id)

    async def continue_turn(self, user_id: int, selected_option: str) -> None:
        async with self._lock_for(user_id):
            pending = await db_module.get_pending_tool_call(user_id)
            if pending is None:
                return
            await db_module.insert_chat_message(
                user_id, "tool", content=selected_option,
                tool_call_id=pending["tool_call_id"],
            )
            await db_module.delete_pending_tool_call(user_id)
            await self._drive_turn(user_id)

    async def cancel_pending(self, user_id: int) -> None:
        async with self._lock_for(user_id):
            await self._auto_cancel_pending(user_id)

    async def _auto_cancel_pending(self, user_id: int) -> None:
        pending = await db_module.get_pending_tool_call(user_id)
        if pending is None:
            return
        try:
            await self._bot.edit_message_reply_markup(
                chat_id=user_id,
                message_id=pending["message_id"],
                reply_markup=None,
            )
        except Exception as e:
            logger.debug("Could not strip keyboard from msg %s: %s",
                         pending["message_id"], e)
        await db_module.insert_chat_message(
            user_id, "tool",
            content=json.dumps({"canceled": True,
                                "reason": "user sent new message"}),
            tool_call_id=pending["tool_call_id"],
        )
        await db_module.delete_pending_tool_call(user_id)

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
            ask_user_pending = False
            for tc in tool_calls:
                tc_id = tc["id"]
                fn = tc["function"]
                name = fn["name"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name == "ask_user":
                    await self._handle_ask_user(user_id, tc_id, args)
                    ask_user_pending = True
                    break
                else:
                    result = await dispatch_tool(name, args, ctx)
                    await db_module.insert_chat_message(
                        user_id, "tool", content=result, tool_call_id=tc_id,
                    )

            if ask_user_pending:
                return

        msg_text = "Запутался — попробуй переформулировать."
        await db_module.insert_chat_message(user_id, "assistant", content=msg_text)
        await self._bot.send_message(user_id, msg_text)
        await db_module.prune_chat_messages(user_id, LLM_HISTORY_SIZE * 2)

    async def _handle_ask_user(self, user_id: int, tool_call_id: str, args: dict) -> None:
        question = str(args.get("question", "Уточни, пожалуйста"))
        options = args.get("options") or []
        if not isinstance(options, list) or len(options) < 2:
            await db_module.insert_chat_message(
                user_id, "tool",
                content=json.dumps({"error": "ask_user needs 2..8 options"}),
                tool_call_id=tool_call_id,
            )
            return
        options = [str(o) for o in options][:8]

        rows = [[InlineKeyboardButton(text=opt[:64], callback_data=f"ai:{i}")]
                for i, opt in enumerate(options)]
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        sent = await self._bot.send_message(user_id, question, reply_markup=kb)

        await db_module.set_pending_tool_call(
            user_id=user_id, tool_call_id=tool_call_id,
            tool_name="ask_user",
            options_json=json.dumps(options, ensure_ascii=False),
            message_id=sent.message_id,
        )
