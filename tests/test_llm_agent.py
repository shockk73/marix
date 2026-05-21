import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

import pytest

import db as db_module
import scheduler
from llm.agent import LLMAgent

MSK = timezone(timedelta(hours=3))
FIXED_NOW = datetime(2026, 5, 21, 14, 30, tzinfo=MSK)


@dataclass
class FakeBot:
    sent: list = None
    edited: list = None
    _next_msg_id: int = 1000

    def __post_init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self._next_msg_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return type("M", (), {"message_id": self._next_msg_id})()

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup})


@pytest.fixture
def fake_bot():
    return FakeBot()


@pytest.fixture
def fake_scheduler(monkeypatch):
    async def noop_start(w): pass
    async def noop_cancel(wid): pass
    monkeypatch.setattr(scheduler, "start_watch", noop_start)
    monkeypatch.setattr(scheduler, "cancel_watch", noop_cancel)


def _mk_agent(bot, client):
    return LLMAgent(bot=bot, client=client, now_provider=lambda: FIXED_NOW)


@pytest.mark.asyncio
async def test_run_turn_simple_text_response(tmp_db, fake_bot, fake_scheduler):
    client = AsyncMock()
    client.chat_completion = AsyncMock(return_value={
        "role": "assistant", "content": "Привет! Чем помочь?"
    })
    agent = _mk_agent(fake_bot, client)

    await agent.run_turn(user_id=1, text="привет", user_name=None)

    assert len(fake_bot.sent) == 1
    assert fake_bot.sent[0]["text"] == "Привет! Чем помочь?"
    msgs = await db_module.get_recent_chat_messages(1, 100)
    assert [m["role"] for m in msgs] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_run_turn_persists_user_name(tmp_db, fake_bot, fake_scheduler):
    client = AsyncMock()
    client.chat_completion = AsyncMock(return_value={
        "role": "assistant", "content": "ок"
    })
    agent = _mk_agent(fake_bot, client)

    await agent.run_turn(user_id=42, text="hi", user_name="Маша")

    assert await db_module.get_user_name(42) == "Маша"


@pytest.mark.asyncio
async def test_run_turn_passes_name_to_system_prompt(tmp_db, fake_bot, fake_scheduler):
    captured = {}

    async def fake_completion(messages, tools):
        captured["messages"] = messages
        return {"role": "assistant", "content": "ok"}

    client = AsyncMock()
    client.chat_completion = fake_completion
    agent = _mk_agent(fake_bot, client)

    await agent.run_turn(user_id=1, text="hi", user_name="Петя")

    sys_msg = captured["messages"][0]
    assert sys_msg["role"] == "system"
    assert "Петя" in sys_msg["content"]
    assert "2026-05-21" in sys_msg["content"]
    assert "14:30" in sys_msg["content"]


@pytest.mark.asyncio
async def test_run_turn_tool_call_then_response(tmp_db, fake_bot, fake_scheduler):
    client = AsyncMock()
    client.chat_completion = AsyncMock(side_effect=[
        {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "list_watches", "arguments": "{}"},
            }],
        },
        {"role": "assistant", "content": "У тебя нет активных отслеживаний."},
    ])
    agent = _mk_agent(fake_bot, client)

    await agent.run_turn(user_id=1, text="что у меня?", user_name=None)

    assert fake_bot.sent[-1]["text"] == "У тебя нет активных отслеживаний."
    msgs = await db_module.get_recent_chat_messages(1, 100)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
    assert msgs[1]["tool_calls"] is not None
    assert msgs[2]["tool_call_id"] == "c1"


@pytest.mark.asyncio
async def test_run_turn_max_turns_safeguard(tmp_db, fake_bot, fake_scheduler, monkeypatch):
    monkeypatch.setattr("llm.agent.OPENROUTER_MAX_TURNS", 2)
    client = AsyncMock()
    client.chat_completion = AsyncMock(return_value={
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": "c", "type": "function",
            "function": {"name": "list_watches", "arguments": "{}"},
        }],
    })
    agent = _mk_agent(fake_bot, client)
    await agent.run_turn(user_id=1, text="loop", user_name=None)
    assert "Запутался" in fake_bot.sent[-1]["text"] or "переформулируй" in fake_bot.sent[-1]["text"].lower()


@pytest.mark.asyncio
async def test_run_turn_http_error_yields_friendly_message(tmp_db, fake_bot, fake_scheduler):
    import httpx
    client = AsyncMock()
    client.chat_completion = AsyncMock(
        side_effect=httpx.RequestError("connection refused"),
    )
    agent = _mk_agent(fake_bot, client)
    await agent.run_turn(user_id=1, text="ping", user_name=None)
    assert len(fake_bot.sent) == 1
    assert "AI" in fake_bot.sent[0]["text"] or "ошибк" in fake_bot.sent[0]["text"].lower()


@pytest.mark.asyncio
async def test_run_turn_per_user_lock_is_per_user(tmp_db, fake_bot, fake_scheduler):
    client = AsyncMock()
    client.chat_completion = AsyncMock(return_value={
        "role": "assistant", "content": "ok"
    })
    agent = _mk_agent(fake_bot, client)
    await agent.run_turn(user_id=1, text="a", user_name=None)
    await agent.run_turn(user_id=2, text="b", user_name=None)
    assert len(fake_bot.sent) == 2
