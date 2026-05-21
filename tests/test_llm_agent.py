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


@pytest.mark.asyncio
async def test_run_turn_ask_user_creates_pending(tmp_db, fake_bot, fake_scheduler):
    client = AsyncMock()
    client.chat_completion = AsyncMock(return_value={
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "ask_user", "arguments": json.dumps({
                "question": "Какое отслеживание удалить?",
                "options": ["#5 Mogilev", "#7 Minsk"],
            })},
        }],
    })
    agent = _mk_agent(fake_bot, client)

    await agent.run_turn(user_id=1, text="удали", user_name=None)

    # 1) "🔧 Уточняю…" preamble, 2) сам вопрос с клавиатурой
    assert len(fake_bot.sent) == 2
    question_msg = next(m for m in fake_bot.sent if m["reply_markup"] is not None)
    assert "Какое отслеживание" in question_msg["text"]
    kb = question_msg["reply_markup"]
    assert kb is not None
    p = await db_module.get_pending_tool_call(1)
    assert p is not None
    assert p["tool_call_id"] == "c1"
    msgs = await db_module.get_recent_chat_messages(1, 100)
    assert all(m["role"] != "tool" for m in msgs)


@pytest.mark.asyncio
async def test_continue_turn_after_callback(tmp_db, fake_bot, fake_scheduler):
    client = AsyncMock()
    client.chat_completion = AsyncMock(return_value={
        "role": "assistant", "content": "Отлично, удалил #5",
    })
    agent = _mk_agent(fake_bot, client)
    await db_module.insert_chat_message(
        1, "user", content="удали",
    )
    await db_module.insert_chat_message(
        1, "assistant", content=None,
        tool_calls=json.dumps([{"id": "c1", "type": "function",
                                "function": {"name": "ask_user",
                                             "arguments": "{}"}}]),
    )
    await db_module.set_pending_tool_call(
        1, "c1", "ask_user", json.dumps(["#5", "#7"]), 999,
    )

    await agent.continue_turn(user_id=1, selected_option="#5")

    assert await db_module.get_pending_tool_call(1) is None
    msgs = await db_module.get_recent_chat_messages(1, 100)
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "c1"
    assert tool_msgs[0]["content"] == "#5"
    assert fake_bot.sent[-1]["text"] == "Отлично, удалил #5"


@pytest.mark.asyncio
async def test_cancel_pending_writes_canceled_marker(tmp_db, fake_bot, fake_scheduler):
    agent = _mk_agent(fake_bot, AsyncMock())
    await db_module.set_pending_tool_call(
        1, "c42", "ask_user", json.dumps(["a", "b"]), 555,
    )

    await agent.cancel_pending(user_id=1)

    assert await db_module.get_pending_tool_call(1) is None
    msgs = await db_module.get_recent_chat_messages(1, 100)
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "c42"
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["canceled"] is True
    assert len(fake_bot.edited) == 1
    assert fake_bot.edited[0]["message_id"] == 555


@pytest.mark.asyncio
async def test_run_turn_auto_cancels_pending(tmp_db, fake_bot, fake_scheduler):
    client = AsyncMock()
    client.chat_completion = AsyncMock(return_value={
        "role": "assistant", "content": "ок"
    })
    agent = _mk_agent(fake_bot, client)
    await db_module.set_pending_tool_call(
        1, "c1", "ask_user", json.dumps(["x"]), 100,
    )

    await agent.run_turn(user_id=1, text="забей, что у меня?", user_name=None)

    assert await db_module.get_pending_tool_call(1) is None
    msgs = await db_module.get_recent_chat_messages(1, 100)
    canceled = [m for m in msgs if m["role"] == "tool"
                and m["tool_call_id"] == "c1"]
    assert len(canceled) == 1
    assert json.loads(canceled[0]["content"])["canceled"] is True


@pytest.mark.asyncio
async def test_handle_photo_vision_disabled(tmp_db, fake_bot, fake_scheduler, monkeypatch):
    monkeypatch.setattr("llm.agent.LLM_VISION", False)
    client = AsyncMock()
    agent = _mk_agent(fake_bot, client)

    await agent.handle_photo(
        user_id=1, image_bytes=b"\x00\x01", mime="image/jpeg",
        caption="что это?", user_name=None,
    )

    assert len(fake_bot.sent) == 1
    assert "не умеет" in fake_bot.sent[0]["text"].lower() or "vision" in fake_bot.sent[0]["text"].lower()
    msgs = await db_module.get_recent_chat_messages(1, 100)
    assert len(msgs) == 0
    client.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_handle_photo_vision_enabled_sends_to_llm(tmp_db, fake_bot, fake_scheduler, monkeypatch):
    monkeypatch.setattr("llm.agent.LLM_VISION", True)
    captured = {}

    async def fake_completion(messages, tools):
        captured["messages"] = messages
        return {"role": "assistant", "content": "Вижу фото."}

    client = AsyncMock()
    client.chat_completion = fake_completion
    agent = _mk_agent(fake_bot, client)

    await agent.handle_photo(
        user_id=1, image_bytes=b"\xff\xd8\xff", mime="image/jpeg",
        caption="опиши", user_name="Маша",
    )

    user_msg = next(m for m in reversed(captured["messages"]) if m["role"] == "user")
    assert isinstance(user_msg["content"], list)
    assert any(p.get("type") == "image_url" for p in user_msg["content"])
    msgs = await db_module.get_recent_chat_messages(1, 100)
    user_rows = [m for m in msgs if m["role"] == "user"]
    assert len(user_rows) == 1
    assert "[photo]" in user_rows[0]["content"]
    assert "опиши" in user_rows[0]["content"]
    assert "data:image" not in user_rows[0]["content"]


@pytest.mark.asyncio
async def test_handle_audio_disabled_no_stt(tmp_db, fake_bot, fake_scheduler, monkeypatch):
    monkeypatch.setattr("llm.agent.LLM_AUDIO", False)
    monkeypatch.setattr("llm.agent.LLM_STT_MODEL", "")
    client = AsyncMock()
    agent = _mk_agent(fake_bot, client)

    await agent.handle_audio(
        user_id=1, audio_bytes=b"\x00", audio_format="ogg",
        caption=None, user_name=None,
    )

    assert len(fake_bot.sent) == 1
    assert "голос" in fake_bot.sent[0]["text"].lower() or "audio" in fake_bot.sent[0]["text"].lower()
    msgs = await db_module.get_recent_chat_messages(1, 100)
    assert len(msgs) == 0
    client.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_handle_audio_via_stt_then_run_turn(tmp_db, fake_bot, fake_scheduler, monkeypatch):
    monkeypatch.setattr("llm.agent.LLM_AUDIO", False)
    monkeypatch.setattr("llm.agent.LLM_STT_MODEL", "mistralai/voxtral-mini-transcribe")
    client = AsyncMock()
    client.transcribe = AsyncMock(return_value="что у меня?")
    client.chat_completion = AsyncMock(return_value={
        "role": "assistant", "content": "пусто",
    })
    agent = _mk_agent(fake_bot, client)

    await agent.handle_audio(
        user_id=1, audio_bytes=b"\xff", audio_format="ogg",
        caption=None, user_name="Маша",
    )

    client.transcribe.assert_called_once()
    msgs = await db_module.get_recent_chat_messages(1, 100)
    user_rows = [m for m in msgs if m["role"] == "user"]
    assert len(user_rows) == 1
    assert "[voice]" in user_rows[0]["content"]
    assert "что у меня?" in user_rows[0]["content"]
    assert fake_bot.sent[-1]["text"] == "пусто"


@pytest.mark.asyncio
async def test_handle_audio_native_multimodal(tmp_db, fake_bot, fake_scheduler, monkeypatch):
    monkeypatch.setattr("llm.agent.LLM_AUDIO", True)
    captured = {}

    async def fake_completion(messages, tools):
        captured["messages"] = messages
        return {"role": "assistant", "content": "услышал"}

    client = AsyncMock()
    client.chat_completion = fake_completion
    agent = _mk_agent(fake_bot, client)

    await agent.handle_audio(
        user_id=1, audio_bytes=b"\xff\xd8", audio_format="ogg",
        caption=None, user_name=None,
    )

    user_msg = next(m for m in reversed(captured["messages"]) if m["role"] == "user")
    assert isinstance(user_msg["content"], list)
    assert any(p.get("type") == "input_audio" for p in user_msg["content"])
    msgs = await db_module.get_recent_chat_messages(1, 100)
    user_rows = [m for m in msgs if m["role"] == "user"]
    assert "[audio]" in user_rows[0]["content"]
    assert "data:" not in user_rows[0]["content"]


@pytest.mark.asyncio
async def test_handle_audio_stt_failure(tmp_db, fake_bot, fake_scheduler, monkeypatch):
    import httpx
    monkeypatch.setattr("llm.agent.LLM_AUDIO", False)
    monkeypatch.setattr("llm.agent.LLM_STT_MODEL", "stt/model")
    client = AsyncMock()
    client.transcribe = AsyncMock(
        side_effect=httpx.RequestError("boom"),
    )
    agent = _mk_agent(fake_bot, client)

    await agent.handle_audio(
        user_id=1, audio_bytes=b"\x00", audio_format="ogg",
        caption=None, user_name=None,
    )

    assert "не удалось" in fake_bot.sent[-1]["text"].lower() or "ошибк" in fake_bot.sent[-1]["text"].lower()
    msgs = await db_module.get_recent_chat_messages(1, 100)
    assert len(msgs) == 0
