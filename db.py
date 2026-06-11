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

_CREATE_APP_STATE = """
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
)
"""

_CREATE_WATCH_STATUSES = """
CREATE TABLE IF NOT EXISTS watch_statuses (
    watch_id             INTEGER PRIMARY KEY,
    status               TEXT NOT NULL,
    last_check_started_at REAL,
    last_check_finished_at REAL,
    last_success_at      REAL,
    last_error_at        REAL,
    last_error           TEXT,
    total_trips          INTEGER,
    window_trips         INTEGER,
    available_trips      INTEGER,
    newly_available      INTEGER,
    consecutive_errors   INTEGER NOT NULL DEFAULT 0,
    updated_at           REAL NOT NULL
)
"""

_CREATE_AGENT_CALLBACKS = """
CREATE TABLE IF NOT EXISTS agent_callbacks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    run_at      REAL NOT NULL,
    prompt      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  REAL NOT NULL,
    executed_at REAL,
    last_error  TEXT
)
"""

MAX_AUTH_ATTEMPTS = 3
LLM_SESSION_VERSION_KEY = "llm_session_version"
ATLAS_PROXY_TARGET_KEY = "atlas_proxy_target"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(_CREATE_TABLE)
        await conn.execute(_CREATE_AUTHORIZED)
        await conn.execute(_CREATE_AUTH_ATTEMPTS)
        await conn.execute(_CREATE_CHAT_MESSAGES)
        await conn.execute(_CREATE_CHAT_MESSAGES_IDX)
        await conn.execute(_CREATE_PENDING_TOOL_CALLS)
        await conn.execute(_CREATE_USER_PROFILES)
        await conn.execute(_CREATE_APP_STATE)
        await conn.execute(_CREATE_WATCH_STATUSES)
        await conn.execute(_CREATE_AGENT_CALLBACKS)
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


async def mark_watch_check_started(watch_id: int) -> None:
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO watch_statuses
               (watch_id, status, last_check_started_at, consecutive_errors, updated_at)
               VALUES (?, 'checking', ?, 0, ?)
               ON CONFLICT(watch_id) DO UPDATE SET
                 status                = excluded.status,
                 last_check_started_at = excluded.last_check_started_at,
                 updated_at            = excluded.updated_at""",
            (watch_id, now, now),
        )
        await conn.commit()


async def mark_watch_check_success(
    watch_id: int,
    total_trips: int,
    window_trips: int,
    available_trips: int,
    newly_available: int,
) -> None:
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO watch_statuses
               (watch_id, status, last_check_finished_at, last_success_at, last_error,
                total_trips, window_trips, available_trips, newly_available,
                consecutive_errors, updated_at)
               VALUES (?, 'ok', ?, ?, NULL, ?, ?, ?, ?, 0, ?)
               ON CONFLICT(watch_id) DO UPDATE SET
                 status                 = excluded.status,
                 last_check_finished_at = excluded.last_check_finished_at,
                 last_success_at        = excluded.last_success_at,
                 last_error             = excluded.last_error,
                 total_trips            = excluded.total_trips,
                 window_trips           = excluded.window_trips,
                 available_trips        = excluded.available_trips,
                 newly_available        = excluded.newly_available,
                 consecutive_errors     = 0,
                 updated_at             = excluded.updated_at""",
            (
                watch_id, now, now, total_trips, window_trips,
                available_trips, newly_available, now,
            ),
        )
        await conn.commit()


async def mark_watch_check_error(watch_id: int, error: str) -> None:
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO watch_statuses
               (watch_id, status, last_check_finished_at, last_error_at, last_error,
                consecutive_errors, updated_at)
               VALUES (?, 'error', ?, ?, ?, 1, ?)
               ON CONFLICT(watch_id) DO UPDATE SET
                 status                 = excluded.status,
                 last_check_finished_at = excluded.last_check_finished_at,
                 last_error_at          = excluded.last_error_at,
                 last_error             = excluded.last_error,
                 consecutive_errors     = watch_statuses.consecutive_errors + 1,
                 updated_at             = excluded.updated_at""",
            (watch_id, now, now, error[:1000], now),
        )
        await conn.commit()


async def get_watch_statuses(watch_ids: list[int]) -> dict[int, dict]:
    if not watch_ids:
        return {}
    placeholders = ",".join("?" for _ in watch_ids)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            f"SELECT * FROM watch_statuses WHERE watch_id IN ({placeholders})",
            watch_ids,
        )
        rows = await cur.fetchall()
        return {row["watch_id"]: dict(row) for row in rows}


async def create_agent_callback(user_id: int, run_at: float, prompt: str) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """INSERT INTO agent_callbacks
               (user_id, run_at, prompt, status, created_at)
               VALUES (?, ?, ?, 'pending', ?)""",
            (user_id, run_at, prompt, time.time()),
        )
        await conn.commit()
        return cur.lastrowid


async def get_pending_agent_callbacks() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT * FROM agent_callbacks
               WHERE status = 'pending'
               ORDER BY run_at, id""",
        )
        return [dict(row) for row in await cur.fetchall()]


async def get_user_agent_callbacks(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT * FROM agent_callbacks
               WHERE user_id = ? AND status = 'pending'
               ORDER BY run_at, id""",
            (user_id,),
        )
        return [dict(row) for row in await cur.fetchall()]


async def get_agent_callback(callback_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM agent_callbacks WHERE id = ?",
            (callback_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def mark_agent_callback_done(callback_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """UPDATE agent_callbacks
               SET status = 'done', executed_at = ?, last_error = NULL
               WHERE id = ?""",
            (time.time(), callback_id),
        )
        await conn.commit()


async def mark_agent_callback_error(callback_id: int, error: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """UPDATE agent_callbacks
               SET status = 'error', executed_at = ?, last_error = ?
               WHERE id = ?""",
            (time.time(), error[:1000], callback_id),
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


async def reset_llm_sessions() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM chat_messages")
        await conn.execute("DELETE FROM pending_tool_calls")
        await conn.commit()


async def ensure_llm_session_version(version: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT value FROM app_state WHERE key = ?",
            (LLM_SESSION_VERSION_KEY,),
        )
        row = await cur.fetchone()
        if row and row[0] == version:
            return False

        await conn.execute("DELETE FROM chat_messages")
        await conn.execute("DELETE FROM pending_tool_calls")
        await conn.execute(
            """INSERT INTO app_state (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value      = excluded.value,
                 updated_at = excluded.updated_at""",
            (LLM_SESSION_VERSION_KEY, version, time.time()),
        )
        await conn.commit()
        return True


async def get_app_state(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT value FROM app_state WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_app_state(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO app_state (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value      = excluded.value,
                 updated_at = excluded.updated_at""",
            (key, value, time.time()),
        )
        await conn.commit()


async def get_atlas_proxy_target() -> dict:
    value = await get_app_state(ATLAS_PROXY_TARGET_KEY)
    if not value:
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def set_atlas_proxy_target(country: str, asn: str | None = None) -> None:
    data = {"country": country.lower()}
    if asn:
        data["asn"] = asn
    await set_app_state(ATLAS_PROXY_TARGET_KEY, json.dumps(data))


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
