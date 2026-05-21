import json
import time
import aiosqlite
from datetime import datetime
from config import DB_PATH

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS watches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    provider    TEXT    NOT NULL,
    direction   TEXT    NOT NULL,
    date        TEXT    NOT NULL,
    time_from   TEXT    NOT NULL,
    time_to     TEXT    NOT NULL,
    interval_sec INTEGER NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    notified_trips TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT    NOT NULL
)
"""

_CREATE_AUTHORIZED = """
CREATE TABLE IF NOT EXISTS authorized_users (
    user_id       INTEGER PRIMARY KEY,
    authorized_at TEXT NOT NULL
)
"""

_CREATE_AUTH_ATTEMPTS = """
CREATE TABLE IF NOT EXISTS auth_attempts (
    user_id      INTEGER PRIMARY KEY,
    failed_count INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_CHAT_MESSAGES = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT,
    tool_calls   TEXT,
    tool_call_id TEXT,
    created_at   REAL NOT NULL
)
"""

_CREATE_CHAT_MESSAGES_IDX = """
CREATE INDEX IF NOT EXISTS idx_chat_user_time
ON chat_messages(user_id, id)
"""

_CREATE_PENDING_TOOL_CALLS = """
CREATE TABLE IF NOT EXISTS pending_tool_calls (
    user_id      INTEGER PRIMARY KEY,
    tool_call_id TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    options_json TEXT NOT NULL,
    message_id   INTEGER NOT NULL,
    created_at   REAL NOT NULL
)
"""

_CREATE_USER_PROFILES = """
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    updated_at REAL NOT NULL
)
"""

MAX_AUTH_ATTEMPTS = 3


async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(_CREATE_TABLE)
        await conn.execute(_CREATE_AUTHORIZED)
        await conn.execute(_CREATE_AUTH_ATTEMPTS)
        await conn.execute(_CREATE_CHAT_MESSAGES)
        await conn.execute(_CREATE_CHAT_MESSAGES_IDX)
        await conn.execute(_CREATE_PENDING_TOOL_CALLS)
        await conn.execute(_CREATE_USER_PROFILES)
        await conn.commit()


async def is_authorized(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT 1 FROM authorized_users WHERE user_id = ?",
            (user_id,),
        )
        return await cur.fetchone() is not None


async def authorize_user(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO authorized_users (user_id, authorized_at) VALUES (?, ?)",
            (user_id, datetime.now().isoformat()),
        )
        await conn.commit()


async def get_failed_attempts(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT failed_count FROM auth_attempts WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else 0


async def increment_failed_attempts(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO auth_attempts (user_id, failed_count) VALUES (?, 1)
               ON CONFLICT(user_id) DO UPDATE SET failed_count = failed_count + 1""",
            (user_id,),
        )
        await conn.commit()
        cur = await conn.execute(
            "SELECT failed_count FROM auth_attempts WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        return row[0]


async def is_banned(user_id: int) -> bool:
    return await get_failed_attempts(user_id) >= MAX_AUTH_ATTEMPTS


async def create_watch(
    user_id: int,
    provider: str,
    direction: str,
    date: str,
    time_from: str,
    time_to: str,
    interval_sec: int,
) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """INSERT INTO watches
               (user_id, provider, direction, date, time_from, time_to, interval_sec, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, provider, direction, date, time_from, time_to, interval_sec,
             datetime.now().isoformat()),
        )
        await conn.commit()
        return cur.lastrowid


async def get_active_watches() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM watches WHERE active = 1")
        return [dict(r) for r in await cur.fetchall()]


async def get_user_watches(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM watches WHERE user_id = ? AND active = 1 ORDER BY id",
            (user_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def stop_watch(watch_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "UPDATE watches SET active = 0 WHERE id = ? AND user_id = ?",
            (watch_id, user_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def update_notified_trips(watch_id: int, trip_ids: list[str]):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE watches SET notified_trips = ? WHERE id = ?",
            (json.dumps(trip_ids), watch_id),
        )
        await conn.commit()


async def insert_chat_message(
    user_id: int,
    role: str,
    content: str | None = None,
    tool_calls: str | None = None,
    tool_call_id: str | None = None,
) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO chat_messages
               (user_id, role, content, tool_calls, tool_call_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, role, content, tool_calls, tool_call_id, time.time()),
        )
        await conn.commit()


async def get_recent_chat_messages(user_id: int, limit: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT * FROM (
                 SELECT * FROM chat_messages
                 WHERE user_id = ?
                 ORDER BY id DESC
                 LIMIT ?
               ) ORDER BY id ASC""",
            (user_id, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


async def prune_chat_messages(user_id: int, keep: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """DELETE FROM chat_messages
               WHERE user_id = ?
                 AND id NOT IN (
                   SELECT id FROM chat_messages
                   WHERE user_id = ?
                   ORDER BY id DESC
                   LIMIT ?
                 )""",
            (user_id, user_id, keep),
        )
        await conn.commit()


async def set_pending_tool_call(
    user_id: int,
    tool_call_id: str,
    tool_name: str,
    options_json: str,
    message_id: int,
) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO pending_tool_calls
               (user_id, tool_call_id, tool_name, options_json, message_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 tool_call_id = excluded.tool_call_id,
                 tool_name    = excluded.tool_name,
                 options_json = excluded.options_json,
                 message_id   = excluded.message_id,
                 created_at   = excluded.created_at""",
            (user_id, tool_call_id, tool_name, options_json, message_id, time.time()),
        )
        await conn.commit()


async def get_pending_tool_call(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM pending_tool_calls WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def delete_pending_tool_call(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "DELETE FROM pending_tool_calls WHERE user_id = ?",
            (user_id,),
        )
        await conn.commit()


async def set_user_name(user_id: int, name: str | None) -> None:
    """Сохраняет имя; пустые значения игнорирует, чтобы не затирать существующее."""
    if not name:
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO user_profiles (user_id, name, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 name       = excluded.name,
                 updated_at = excluded.updated_at""",
            (user_id, name, time.time()),
        )
        await conn.commit()


async def get_user_name(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT name FROM user_profiles WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else None
