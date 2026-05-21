import json
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

MAX_AUTH_ATTEMPTS = 3


async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(_CREATE_TABLE)
        await conn.execute(_CREATE_AUTHORIZED)
        await conn.execute(_CREATE_AUTH_ATTEMPTS)
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
