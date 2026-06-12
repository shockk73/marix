import asyncio
import json

import aiosqlite
import pytest

import db as db_module
from handlers import (
    ADMIN_GREETING, INVITE_REJECT, WELCOME_AFTER_INVITE,
    handle_unauthorized_message,
)
from llm.tools import (
    ADMIN_TOOL_SCHEMAS, ToolContext, build_tools_for_role, dispatch_tool,
)


async def test_create_and_use_invite(tmp_db):
    token = await db_module.create_invite(created_by=10)
    assert len(token) >= 12
    assert await db_module.use_invite(token, 20) is True
    assert await db_module.is_authorized(20)
    assert await db_module.get_user_role(20) == "user"


async def test_use_invite_only_once(tmp_db):
    token = await db_module.create_invite(10)
    assert await db_module.use_invite(token, 20) is True
    assert await db_module.use_invite(token, 30) is False
    assert not await db_module.is_authorized(30)


async def test_use_invite_unknown_token(tmp_db):
    assert await db_module.use_invite("nope", 20) is False


async def test_use_invite_race(tmp_db):
    token = await db_module.create_invite(10)
    results = await asyncio.gather(
        db_module.use_invite(token, 21),
        db_module.use_invite(token, 22),
    )
    assert sorted(results) == [False, True]


async def test_role_migration_adds_user_role(tmp_path, monkeypatch):
    db_path = str(tmp_path / "old.db")
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """CREATE TABLE authorized_users (
                   user_id INTEGER PRIMARY KEY,
                   authorized_at TEXT NOT NULL)""")
        await conn.execute(
            "INSERT INTO authorized_users VALUES (7, '2026-01-01')")
        await conn.commit()
    await db_module.init_db()
    assert await db_module.get_user_role(7) == "user"


async def test_authorize_admin_upgrades_but_never_downgrades(tmp_db):
    await db_module.authorize_user(5, role="admin")
    await db_module.authorize_user(5)          # повторный вход юзером
    assert await db_module.get_user_role(5) == "admin"
    await db_module.authorize_user(6)
    await db_module.authorize_user(6, role="admin")
    assert await db_module.get_user_role(6) == "admin"


async def test_unauthorized_valid_invite(tmp_db):
    token = await db_module.create_invite(1)
    reply, authorized = await handle_unauthorized_message(50, f"/start {token}")
    assert authorized is True
    assert reply == WELCOME_AFTER_INVITE
    assert await db_module.is_authorized(50)


async def test_unauthorized_bad_invite_no_ban(tmp_db):
    reply, authorized = await handle_unauthorized_message(50, "/start badtoken")
    assert authorized is False
    assert reply == INVITE_REJECT
    assert await db_module.get_failed_attempts(50) == 0


async def test_unauthorized_auth_code_plain_text_makes_admin(tmp_db, monkeypatch):
    monkeypatch.setattr("handlers.AUTH_CODE", "s3cret")
    reply, authorized = await handle_unauthorized_message(50, "s3cret")
    assert authorized is True
    assert reply == ADMIN_GREETING
    assert await db_module.get_user_role(50) == "admin"


async def test_unauthorized_auth_command_makes_admin(tmp_db, monkeypatch):
    monkeypatch.setattr("handlers.AUTH_CODE", "s3cret")
    reply, authorized = await handle_unauthorized_message(50, "/auth s3cret")
    assert authorized is True
    assert await db_module.get_user_role(50) == "admin"


async def test_unauthorized_wrong_auth_code_increments(tmp_db, monkeypatch):
    monkeypatch.setattr("handlers.AUTH_CODE", "s3cret")
    reply, authorized = await handle_unauthorized_message(50, "/auth wrong")
    assert authorized is False
    assert "Осталось попыток" in reply
    assert await db_module.get_failed_attempts(50) == 1


async def test_unauthorized_other_text_returns_none(tmp_db):
    assert await handle_unauthorized_message(50, "привет") is None
    assert await handle_unauthorized_message(50, "/start") is None


async def test_create_invite_tool_requires_admin(tmp_db):
    ctx = ToolContext(user_id=1, role="user")
    out = json.loads(await dispatch_tool("create_invite", {}, ctx))
    assert "error" in out
    out = json.loads(await dispatch_tool("list_invites", {}, ctx))
    assert "error" in out


async def test_create_invite_tool_admin_returns_link(tmp_db):
    ctx = ToolContext(user_id=1, role="admin", bot_username="marik_bot")
    out = json.loads(await dispatch_tool("create_invite", {}, ctx))
    assert out["token"]
    assert out["link"] == f"https://t.me/marik_bot?start={out['token']}"
    listed = json.loads(await dispatch_tool("list_invites", {}, ctx))
    assert len(listed["invites"]) == 1
    assert listed["invites"][0]["used_by"] is None
    assert listed["invites"][0]["created_by"] == 1


def test_build_tools_for_role():
    admin_names = {t["function"]["name"] for t in build_tools_for_role("admin")}
    user_names = {t["function"]["name"] for t in build_tools_for_role("user")}
    assert {"create_invite", "list_invites"} <= admin_names
    assert {"create_invite", "list_invites"}.isdisjoint(user_names)
    assert "create_watch" in user_names
    assert len(ADMIN_TOOL_SCHEMAS) == 2
