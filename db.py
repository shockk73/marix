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


async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(_CREATE_TABLE)
        await conn.commit()


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
