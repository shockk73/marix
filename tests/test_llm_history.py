import json
import pytest

import db as db_module


@pytest.mark.asyncio
async def test_insert_and_get_user_message(tmp_db):
    await db_module.insert_chat_message(user_id=1, role="user", content="привет")
    msgs = await db_module.get_recent_chat_messages(user_id=1, limit=10)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "привет"
    assert msgs[0]["tool_calls"] is None
    assert msgs[0]["tool_call_id"] is None


@pytest.mark.asyncio
async def test_insert_assistant_with_tool_calls(tmp_db):
    tool_calls = [{"id": "call_1", "type": "function",
                   "function": {"name": "list_watches", "arguments": "{}"}}]
    await db_module.insert_chat_message(
        user_id=2, role="assistant", content=None,
        tool_calls=json.dumps(tool_calls),
    )
    msgs = await db_module.get_recent_chat_messages(user_id=2, limit=10)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["content"] is None
    assert json.loads(msgs[0]["tool_calls"]) == tool_calls


@pytest.mark.asyncio
async def test_insert_tool_result(tmp_db):
    await db_module.insert_chat_message(
        user_id=3, role="tool", content='{"watches": []}',
        tool_call_id="call_1",
    )
    msgs = await db_module.get_recent_chat_messages(user_id=3, limit=10)
    assert msgs[0]["role"] == "tool"
    assert msgs[0]["tool_call_id"] == "call_1"


@pytest.mark.asyncio
async def test_get_recent_returns_chronological_order(tmp_db):
    for i in range(5):
        await db_module.insert_chat_message(user_id=10, role="user", content=f"msg{i}")
    msgs = await db_module.get_recent_chat_messages(user_id=10, limit=3)
    assert len(msgs) == 3
    assert [m["content"] for m in msgs] == ["msg2", "msg3", "msg4"]


@pytest.mark.asyncio
async def test_get_recent_isolated_per_user(tmp_db):
    await db_module.insert_chat_message(user_id=1, role="user", content="user1")
    await db_module.insert_chat_message(user_id=2, role="user", content="user2")
    msgs = await db_module.get_recent_chat_messages(user_id=1, limit=10)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "user1"


@pytest.mark.asyncio
async def test_prune_chat_messages_keeps_only_n_latest(tmp_db):
    for i in range(10):
        await db_module.insert_chat_message(user_id=1, role="user", content=f"m{i}")
    await db_module.prune_chat_messages(user_id=1, keep=4)
    msgs = await db_module.get_recent_chat_messages(user_id=1, limit=100)
    assert len(msgs) == 4
    assert [m["content"] for m in msgs] == ["m6", "m7", "m8", "m9"]


@pytest.mark.asyncio
async def test_prune_does_not_affect_other_users(tmp_db):
    for i in range(5):
        await db_module.insert_chat_message(user_id=1, role="user", content=f"a{i}")
        await db_module.insert_chat_message(user_id=2, role="user", content=f"b{i}")
    await db_module.prune_chat_messages(user_id=1, keep=2)
    assert len(await db_module.get_recent_chat_messages(1, 100)) == 2
    assert len(await db_module.get_recent_chat_messages(2, 100)) == 5
