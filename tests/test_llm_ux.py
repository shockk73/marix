import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

import pytest

import db as db_module
from handlers import BTN_ADMIN, BTN_HELP, BTN_LIST, BTN_WATCH, main_reply_kb
from llm.prompt import build_system_prompt
from tests.test_llm_agent import FakeBot, _mk_agent, fake_bot, fake_scheduler

MSK = timezone(timedelta(hours=3))
NOW = datetime(2026, 6, 12, 14, 30, tzinfo=MSK)


def _screen_tool_call(args: dict, call_id: str = "s1") -> dict:
    return {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "show_screen",
                         "arguments": json.dumps(args, ensure_ascii=False)},
        }],
    }


@pytest.mark.asyncio
async def test_show_screen_renders_grid_and_saves_pending(tmp_db, fake_bot, fake_scheduler):
    client = AsyncMock()
    client.chat_completion = AsyncMock(return_value=_screen_tool_call({
        "text": "Когда едем?",
        "buttons": [
            [{"label": "🌅 Утро", "value": "утро 05:00-12:00"},
             {"label": "🌞 День", "value": "день 12:00-17:00"}],
            [{"label": "Весь день", "value": "весь день"}],
        ],
    }))
    agent = _mk_agent(fake_bot, client)

    await agent.run_turn(user_id=1, text="следи завтра", user_name=None)

    screen_msg = next(m for m in fake_bot.sent if m["reply_markup"] is not None)
    assert "Когда едем" in screen_msg["text"]
    kb = screen_msg["reply_markup"].inline_keyboard
    assert len(kb) == 2
    assert [b.text for b in kb[0]] == ["🌅 Утро", "🌞 День"]
    assert [b.callback_data for b in kb[0]] == ["ai:0", "ai:1"]
    assert kb[1][0].callback_data == "ai:2"

    pending = await db_module.get_pending_tool_call(1)
    assert pending is not None
    assert pending["tool_name"] == "show_screen"
    assert json.loads(pending["options_json"]) == [
        "утро 05:00-12:00", "день 12:00-17:00", "весь день",
    ]


@pytest.mark.asyncio
async def test_show_screen_click_returns_value(tmp_db, fake_bot, fake_scheduler):
    client = AsyncMock()
    client.chat_completion = AsyncMock(side_effect=[
        _screen_tool_call({
            "text": "Выбор",
            "buttons": [[{"label": "A", "value": "вариант А"}]],
        }),
        {"role": "assistant", "content": "Принял: вариант А"},
    ])
    agent = _mk_agent(fake_bot, client)

    await agent.run_turn(user_id=1, text="старт", user_name=None)
    await agent.continue_turn(user_id=1, selected_option="вариант А")

    assert await db_module.get_pending_tool_call(1) is None
    msgs = await db_module.get_recent_chat_messages(1, 100)
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert tool_msgs[-1]["content"] == "вариант А"
    assert tool_msgs[-1]["tool_call_id"] == "s1"
    assert fake_bot.sent[-1]["text"] == "Принял: вариант А"


@pytest.mark.asyncio
async def test_show_screen_too_many_rows_returns_error(tmp_db, fake_bot, fake_scheduler):
    rows = [[{"label": f"r{i}", "value": f"v{i}"}] for i in range(9)]
    client = AsyncMock()
    client.chat_completion = AsyncMock(side_effect=[
        _screen_tool_call({"text": "много", "buttons": rows}),
        {"role": "assistant", "content": "ок, по-другому"},
    ])
    agent = _mk_agent(fake_bot, client)

    await agent.run_turn(user_id=1, text="x", user_name=None)

    assert await db_module.get_pending_tool_call(1) is None
    msgs = await db_module.get_recent_chat_messages(1, 100)
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert "max 8 rows" in tool_msgs[0]["content"]


@pytest.mark.asyncio
async def test_show_screen_too_many_buttons_in_row_returns_error(tmp_db, fake_bot, fake_scheduler):
    row = [{"label": f"b{i}", "value": f"v{i}"} for i in range(5)]
    client = AsyncMock()
    client.chat_completion = AsyncMock(side_effect=[
        _screen_tool_call({"text": "широко", "buttons": [row]}),
        {"role": "assistant", "content": "переделал"},
    ])
    agent = _mk_agent(fake_bot, client)

    await agent.run_turn(user_id=1, text="x", user_name=None)

    msgs = await db_module.get_recent_chat_messages(1, 100)
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert "max 4 buttons" in tool_msgs[0]["content"]


def test_main_reply_kb_user_and_admin():
    user_kb = main_reply_kb("user")
    texts = [b.text for row in user_kb.keyboard for b in row]
    assert texts == [BTN_WATCH, BTN_LIST, BTN_HELP]
    assert user_kb.resize_keyboard is True
    assert user_kb.is_persistent is True

    admin_kb = main_reply_kb("admin")
    admin_texts = [b.text for row in admin_kb.keyboard for b in row]
    assert BTN_ADMIN in admin_texts


def test_prompt_includes_user_state():
    state = {
        "role": "admin",
        "watches": [{
            "id": 12, "provider": "atlasbus", "direction": "mnsk_baran",
            "date": "2026-06-14", "time_from": "12:00", "time_to": "16:00",
            "interval_sec": 120,
            "execution": {"status": "error", "consecutive_errors": 3,
                          "last_error": "429 Too Many Requests"},
        }],
        "callbacks": [{"id": 1, "run_at_iso": "2026-06-12T15:00:00+03:00"}],
    }
    prompt = build_system_prompt(now=NOW, user_name="Дима", user_state=state)
    assert "Роль пользователя: admin" in prompt
    assert "#12 atlasbus mnsk_baran 2026-06-14" in prompt
    assert "ОШИБКИ x3" in prompt
    assert "429" in prompt
    assert "self-callback" in prompt
    assert "2026-06-12T15:00:00+03:00" in prompt


def test_prompt_no_watches_mentions_empty():
    prompt = build_system_prompt(
        now=NOW, user_name=None,
        user_state={"role": "user", "watches": [], "callbacks": []},
    )
    assert "Активных слежек у пользователя нет" in prompt


def test_prompt_mentions_show_screen_and_onboarding():
    prompt = build_system_prompt(now=NOW, user_name=None)
    assert "show_screen" in prompt
    assert "по инвайту" in prompt
    assert "🔍 Следить за местами" in prompt


@pytest.mark.asyncio
async def test_agent_passes_state_to_prompt(tmp_db, fake_bot, fake_scheduler):
    await db_module.authorize_user(1, role="admin")
    captured = {}

    async def fake_completion(messages, tools):
        captured["messages"] = messages
        captured["tools"] = tools
        return {"role": "assistant", "content": "ok"}

    client = AsyncMock()
    client.chat_completion = fake_completion
    agent = _mk_agent(fake_bot, client)

    await agent.run_turn(user_id=1, text="привет", user_name=None)

    sys_prompt = captured["messages"][0]["content"]
    assert "Роль пользователя: admin" in sys_prompt
    tool_names = {t["function"]["name"] for t in captured["tools"]}
    assert "create_invite" in tool_names
    assert "show_screen" in tool_names
