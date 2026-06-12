import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable

import httpx
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    OPENROUTER_MAX_TURNS, LLM_HISTORY_SIZE, LLM_VISION,
    LLM_AUDIO, LLM_STT_MODEL,
)
import db as db_module
from llm.client import OpenRouterClient
from llm.history import to_openai_messages
from llm.prompt import build_system_prompt
from llm.tools import ToolContext, build_tools_for_role, dispatch_tool

logger = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))


def _default_now() -> datetime:
    return datetime.now(_MSK)


_TOOL_THINKING_LABELS = {
    "list_watches": "Смотрю активные отслеживания…",
    "create_watch": "Создаю отслеживание…",
    "stop_watch": "Останавливаю отслеживание…",
    "stop_all_watches": "Останавливаю все отслеживания…",
    "check_trips_now": "Проверяю рейсы сейчас…",
    "ask_user": "Уточняю…",
    "get_atlas_proxy_status": "Смотрю настройки Atlas proxy…",
    "set_atlas_proxy_target": "Меняю Atlas proxy target…",
    "schedule_self_callback": "Планирую callback…",
    "list_self_callbacks": "Смотрю отложенные callback-и…",
    "create_invite": "Создаю инвайт…",
    "list_invites": "Смотрю инвайты…",
    "show_screen": "Собираю экран…",
    "generate_sessions_report": "Готовлю отчёт…",
    "save_baranovichi_credentials": "Подключаю аккаунт…",
    "get_credentials_status": "Смотрю статус автоброни…",
    "delete_credentials": "Отключаю аккаунт…",
}


class LLMAgent:
    def __init__(
        self,
        bot: Any,
        client: OpenRouterClient,
        now_provider: Callable[[], datetime] = _default_now,
        bot_username: str | None = None,
    ) -> None:
        self._bot = bot
        self._client = client
        self._now = now_provider
        self._bot_username = bot_username
        self._locks: dict[int, asyncio.Lock] = {}
        self._callback_tasks: dict[int, asyncio.Task] = {}

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

    async def restore_scheduled_callbacks(self) -> None:
        callbacks = await db_module.get_pending_agent_callbacks()
        for callback in callbacks:
            self._start_callback_task(callback)
        logger.info("Restored %d pending agent callbacks", len(callbacks))

    async def schedule_self_callback(self, user_id: int, run_at: float, prompt: str) -> int:
        callback_id = await db_module.create_agent_callback(user_id, run_at, prompt)
        self._start_callback_task({
            "id": callback_id,
            "user_id": user_id,
            "run_at": run_at,
            "prompt": prompt,
        })
        return callback_id

    def _start_callback_task(self, callback: dict) -> None:
        callback_id = callback["id"]
        task = self._callback_tasks.get(callback_id)
        if task and not task.done():
            return
        self._callback_tasks[callback_id] = asyncio.create_task(
            self._run_callback_task(callback),
            name=f"agent-callback-{callback_id}",
        )

    async def _run_callback_task(self, callback: dict) -> None:
        callback_id = callback["id"]
        try:
            delay = max(0.0, float(callback["run_at"]) - time.time())
            await asyncio.sleep(delay)
            fresh = await db_module.get_agent_callback(callback_id)
            if fresh is None or fresh["status"] != "pending":
                return

            user_id = fresh["user_id"]
            async with self._lock_for(user_id):
                await db_module.insert_chat_message(
                    user_id,
                    "user",
                    content=(
                        f"[scheduled callback #{callback_id}]\n"
                        f"{fresh['prompt']}"
                    ),
                )
                await self._drive_turn(user_id)
            await db_module.mark_agent_callback_done(callback_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Agent callback %s failed: %s", callback_id, e)
            await db_module.mark_agent_callback_error(callback_id, f"{type(e).__name__}: {e}")
        finally:
            self._callback_tasks.pop(callback_id, None)

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

    async def handle_photo(
        self,
        user_id: int,
        image_bytes: bytes,
        mime: str,
        caption: str | None,
        user_name: str | None,
    ) -> None:
        if not LLM_VISION:
            msg = ("Текущая модель не умеет читать фото. "
                   "Опиши текстом или поменяй модель в .env.")
            await self._bot.send_message(user_id, msg)
            return

        async with self._lock_for(user_id):
            await db_module.set_user_name(user_id, user_name)
            await self._auto_cancel_pending(user_id)
            caption_text = caption or ""
            placeholder = f"[photo] {caption_text}".strip()
            await db_module.insert_chat_message(user_id, "user", content=placeholder)
            try:
                await self._drive_turn(
                    user_id,
                    _override_last_user=self._build_multimodal_user(image_bytes, mime, caption_text),
                    _raise_client_errors=True,
                )
            except httpx.HTTPStatusError as e:
                logger.warning("Photo request rejected %s: %s",
                               e.response.status_code, e)
                msg = "Не получилось разобрать фото. Напиши текстом, пожалуйста."
                await db_module.insert_chat_message(user_id, "assistant", content=msg)
                await self._bot.send_message(user_id, msg)

    def _build_multimodal_user(
        self,
        image_bytes: bytes,
        mime: str,
        caption: str,
    ) -> dict[str, Any]:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        parts: list[dict[str, Any]] = []
        if caption:
            parts.append({"type": "text", "text": caption})
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
        return {"role": "user", "content": parts}

    async def handle_audio(
        self,
        user_id: int,
        audio_bytes: bytes,
        audio_format: str,
        caption: str | None,
        user_name: str | None,
    ) -> None:
        if LLM_AUDIO:
            async with self._lock_for(user_id):
                await db_module.set_user_name(user_id, user_name)
                await self._auto_cancel_pending(user_id)
                caption_text = caption or ""
                placeholder = f"[audio] {caption_text}".strip()
                await db_module.insert_chat_message(user_id, "user", content=placeholder)
                try:
                    await self._drive_turn(
                        user_id,
                        _override_last_user=self._build_audio_user(
                            audio_bytes, audio_format, caption_text,
                        ),
                        _raise_client_errors=True,
                    )
                except httpx.HTTPStatusError as e:
                    logger.warning("Audio request rejected %s: %s",
                                   e.response.status_code, e)
                    msg = "Не получилось разобрать голосовое. Напиши текстом, пожалуйста."
                    await db_module.insert_chat_message(user_id, "assistant", content=msg)
                    await self._bot.send_message(user_id, msg)
            return

        if LLM_STT_MODEL:
            try:
                text = await self._client.transcribe(
                    stt_model=LLM_STT_MODEL,
                    audio_bytes=audio_bytes,
                    audio_format=audio_format,
                )
            except Exception as e:
                logger.warning("STT failed: %s", e)
                await self._bot.send_message(
                    user_id,
                    "Не удалось распознать голосовое. Попробуй ещё раз или напиши текстом.",
                )
                return
            if not text:
                await self._bot.send_message(
                    user_id,
                    "Не разобрал голосовое. Скажи ещё раз или напиши текстом.",
                )
                return
            async with self._lock_for(user_id):
                await db_module.set_user_name(user_id, user_name)
                await self._auto_cancel_pending(user_id)
                placeholder = f"[voice] {text}"
                await db_module.insert_chat_message(user_id, "user", content=placeholder)
                await self._drive_turn(user_id)
            return

        await self._bot.send_message(
            user_id,
            "Я не умею слушать голосовые. Напиши текстом или настрой "
            "LLM_AUDIO/LLM_STT_MODEL в .env.",
        )

    def _build_audio_user(
        self,
        audio_bytes: bytes,
        audio_format: str,
        caption: str,
    ) -> dict[str, Any]:
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        parts: list[dict[str, Any]] = []
        if caption:
            parts.append({"type": "text", "text": caption})
        parts.append({
            "type": "input_audio",
            "input_audio": {"data": b64, "format": audio_format},
        })
        return {"role": "user", "content": parts}

    async def _collect_user_state(self, user_id: int, role: str) -> dict:
        """Срез состояния юзера для системного промпта (state-awareness)."""
        watches = await db_module.get_user_watches(user_id)
        statuses = await db_module.get_watch_statuses([w["id"] for w in watches])
        callbacks = await db_module.get_user_agent_callbacks(user_id)
        return {
            "role": role,
            "watches": [{
                "id": w["id"], "provider": w["provider"],
                "direction": w["direction"], "date": w["date"],
                "time_from": w["time_from"], "time_to": w["time_to"],
                "interval_sec": w["interval_sec"],
                "execution": statuses.get(w["id"]) or {},
            } for w in watches],
            "callbacks": [{
                "id": cb["id"],
                "run_at_iso": datetime.fromtimestamp(
                    cb["run_at"], tz=_MSK).isoformat(),
            } for cb in callbacks],
        }

    async def _send_markdown_message(
        self,
        user_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Any:
        try:
            return await self._bot.send_message(
                user_id,
                text,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
        except TelegramBadRequest as e:
            logger.warning("Telegram Markdown parse failed, retrying plain text: %s", e)
            return await self._bot.send_message(
                user_id,
                text,
                reply_markup=reply_markup,
            )

    async def _drive_turn(
        self,
        user_id: int,
        _override_last_user: dict[str, Any] | None = None,
        _raise_client_errors: bool = False,
    ) -> None:
        role = await db_module.get_user_role(user_id) or "user"
        for _turn in range(OPENROUTER_MAX_TURNS):
            try:
                rows = await db_module.get_recent_chat_messages(user_id, LLM_HISTORY_SIZE)
                stored_name = await db_module.get_user_name(user_id)
                user_state = await self._collect_user_state(user_id, role)
                messages = [{"role": "system",
                             "content": build_system_prompt(
                                 now=self._now(), user_name=stored_name,
                                 user_state=user_state,
                             )}]
                messages.extend(to_openai_messages(rows))
                if _override_last_user is not None:
                    for i in range(len(messages) - 1, -1, -1):
                        if messages[i]["role"] == "user":
                            messages[i] = _override_last_user
                            break
                    _override_last_user = None
                msg = await self._client.chat_completion(
                    messages=messages, tools=build_tools_for_role(role),
                )
            except httpx.HTTPError as e:
                if (
                    _raise_client_errors
                    and isinstance(e, httpx.HTTPStatusError)
                    and 400 <= e.response.status_code < 500
                ):
                    raise
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
                    await self._send_markdown_message(user_id, content)
                await db_module.prune_chat_messages(user_id, LLM_HISTORY_SIZE * 2)
                return

            await db_module.insert_chat_message(
                user_id, "assistant", content=content,
                tool_calls=json.dumps(tool_calls),
            )

            if content:
                try:
                    await self._send_markdown_message(user_id, content)
                except Exception as e:
                    logger.debug("preface message send failed: %s", e)

            ctx = ToolContext(
                user_id=user_id,
                schedule_self_callback=self.schedule_self_callback,
                role=role,
                bot_username=self._bot_username,
                bot=self._bot,
            )
            ask_user_pending = False
            for tc in tool_calls:
                tc_id = tc["id"]
                fn = tc["function"]
                name = fn["name"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                label = _TOOL_THINKING_LABELS.get(name, f"Выполняю {name}…")
                try:
                    await self._bot.send_message(user_id, f"🔧 {label}")
                except Exception as e:
                    logger.debug("thinking label send failed: %s", e)

                if name == "ask_user":
                    await self._handle_ask_user(user_id, tc_id, args)
                    ask_user_pending = True
                    break
                elif name == "show_screen":
                    if await self._handle_screen(user_id, tc_id, args):
                        ask_user_pending = True
                        break
                else:
                    try:
                        result = await dispatch_tool(name, args, ctx)
                    except Exception as e:
                        logger.exception("Tool %s crashed: %s", name, e)
                        result = json.dumps(
                            {"error": f"Tool crashed: {type(e).__name__}: {e}"},
                            ensure_ascii=False,
                        )
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
        sent = await self._send_markdown_message(user_id, question, reply_markup=kb)

        await db_module.set_pending_tool_call(
            user_id=user_id, tool_call_id=tool_call_id,
            tool_name="ask_user",
            options_json=json.dumps(options, ensure_ascii=False),
            message_id=sent.message_id,
        )

    async def _handle_screen(self, user_id: int, tool_call_id: str, args: dict) -> bool:
        """Рендерит экран show_screen. False — аргументы невалидны, в историю
        записан tool-error и turn продолжается (модель переделает)."""
        text = str(args.get("text") or "").strip()
        rows_in = args.get("buttons")
        error = None
        if not text:
            error = "show_screen: text must be a non-empty string"
        elif not isinstance(rows_in, list) or not rows_in:
            error = "show_screen: buttons must be a non-empty array of rows"
        elif len(rows_in) > 8:
            error = "show_screen: max 8 rows"
        else:
            for row in rows_in:
                if not isinstance(row, list) or not row:
                    error = "show_screen: each row must be a non-empty array"
                    break
                if len(row) > 4:
                    error = "show_screen: max 4 buttons per row"
                    break
                for btn in row:
                    if (not isinstance(btn, dict)
                            or not str(btn.get("label") or "").strip()
                            or not str(btn.get("value") or "").strip()):
                        error = "show_screen: each button needs label and value"
                        break
                if error:
                    break
        if error:
            await db_module.insert_chat_message(
                user_id, "tool", content=json.dumps({"error": error}),
                tool_call_id=tool_call_id,
            )
            return False

        flat_values: list[str] = []
        kb_rows: list[list[InlineKeyboardButton]] = []
        for row in rows_in:
            kb_row = []
            for btn in row:
                kb_row.append(InlineKeyboardButton(
                    text=str(btn["label"])[:64],
                    callback_data=f"ai:{len(flat_values)}",
                ))
                flat_values.append(str(btn["value"]))
            kb_rows.append(kb_row)
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        sent = await self._send_markdown_message(user_id, text, reply_markup=kb)
        await db_module.set_pending_tool_call(
            user_id=user_id, tool_call_id=tool_call_id,
            tool_name="show_screen",
            options_json=json.dumps(flat_values, ensure_ascii=False),
            message_id=sent.message_id,
        )
        return True
