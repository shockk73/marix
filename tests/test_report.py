import json
from datetime import datetime, timedelta, timezone

import pytest

import db as db_module
from report import build_sessions_report, collect_sessions_data
from llm.tools import ToolContext, dispatch_tool
from tests.test_llm_agent import FakeBot

MSK = timezone(timedelta(hours=3))
NOW = datetime(2026, 6, 12, 15, 0, tzinfo=MSK)


def _user(uid=1, name="Дима", role="admin", messages=None, watches=None):
    return {
        "user_id": uid, "role": role, "authorized_at": "2026-06-01T10:00:00",
        "name": name, "watches": watches or [], "messages": messages or [],
    }


def _msg(role, content=None, tool_calls=None, tool_call_id=None, ts=1780000000.0):
    return {
        "role": role, "content": content, "tool_calls": tool_calls,
        "tool_call_id": tool_call_id, "created_at": ts,
    }


def test_report_renders_user_messages_and_tools():
    tool_calls = json.dumps([{
        "id": "c1", "type": "function",
        "function": {"name": "list_watches", "arguments": "{}"},
    }])
    users = [_user(messages=[
        _msg("user", "что у меня?"),
        _msg("assistant", None, tool_calls=tool_calls),
        _msg("tool", '{"watches": []}', tool_call_id="c1"),
        _msg("assistant", "Пусто."),
    ], watches=[{
        "id": 5, "provider": "atlasbus", "direction": "mnsk_baran",
        "date": "2026-06-14", "time_from": "12:00", "time_to": "16:00",
        "interval_sec": 120,
    }])]

    html_out = build_sessions_report(users, now=NOW)

    assert "Дима" in html_out
    assert "что у меня?" in html_out
    assert "<details>" in html_out
    assert "list_watches" in html_out          # имя тулзы в details
    assert "Минск → Барановичи" in html_out    # слежка строкой
    assert "пользователей: 1" in html_out
    assert "2026-06-12 15:00" in html_out


def test_report_escapes_html_in_messages():
    users = [_user(messages=[_msg("user", "<script>alert(1)</script>")])]
    html_out = build_sessions_report(users, now=NOW)
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out


def test_report_user_without_messages():
    users = [_user(name=None, messages=[])]
    html_out = build_sessions_report(users, now=NOW)
    assert "нет сообщений" in html_out
    assert "без имени" in html_out


async def test_collect_sorts_by_last_activity(tmp_db):
    await db_module.authorize_user(1)
    await db_module.authorize_user(2)
    await db_module.insert_chat_message(1, "user", content="старое")
    await db_module.insert_chat_message(2, "user", content="новое")

    users = await collect_sessions_data()

    assert [u["user_id"] for u in users] == [2, 1]
    assert users[0]["messages"][0]["content"] == "новое"


async def test_report_tool_requires_admin(tmp_db):
    ctx = ToolContext(user_id=1, role="user", bot=FakeBot())
    out = json.loads(await dispatch_tool("generate_sessions_report", {}, ctx))
    assert "error" in out


async def test_report_tool_sends_document(tmp_db):
    await db_module.authorize_user(1, role="admin")
    await db_module.set_user_name(1, "Дима")
    await db_module.insert_chat_message(1, "user", content="привет")
    bot = FakeBot()
    ctx = ToolContext(user_id=1, role="admin", bot=bot)

    out = json.loads(await dispatch_tool("generate_sessions_report", {}, ctx))

    assert out == {"sent": True, "users": 1}
    assert len(bot.documents) == 1
    doc = bot.documents[0]["document"]
    assert doc.filename.startswith("sessions-")
    html_out = doc.data.decode("utf-8")
    assert "Дима" in html_out
    assert "привет" in html_out
