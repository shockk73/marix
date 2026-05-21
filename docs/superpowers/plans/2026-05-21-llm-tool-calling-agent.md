# LLM Tool Calling Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить в Telegram-бота `marik-check` LLM-агента через OpenRouter API с tool calling: пользователь пишет естественным языком, модель сама вызывает функции бота для управления отслеживаниями.

**Architecture:** Новый пакет `llm/` (client, tools, agent, history, prompt). Fallback-хендлер в `handlers.py` ловит свободный текст, передаёт в `agent.run_turn`. История диалога и pending `ask_user`-состояние хранятся в SQLite (новые таблицы `chat_messages`, `pending_tool_calls`) — выживают рестарт бота.

**Tech Stack:** Python 3.11+, aiogram 3.13, aiosqlite, httpx (async, уже в `requirements.txt`), respx (для мока HTTP в тестах), pytest-asyncio. Дополнительных зависимостей не требуется.

**Reference spec:** `docs/superpowers/specs/2026-05-21-llm-tool-calling-design.md`

---

## File Structure

| Файл | Тип | Назначение |
|---|---|---|
| `config.py` | modify | + новые env-переменные (`OPENROUTER_*`, `LLM_*`) |
| `db.py` | modify | + DDL и CRUD для `chat_messages` и `pending_tool_calls` |
| `handlers.py` | modify | + fallback-хендлер на текст/фото, + callback-хендлер `ai:*` |
| `bot.py` | modify | передать `bot` в `llm.agent` при старте |
| `llm/__init__.py` | create | пакет |
| `llm/client.py` | create | OpenRouter async-клиент (httpx) |
| `llm/prompt.py` | create | сборка системного промпта (дата, провайдеры, направления) |
| `llm/tools.py` | create | JSON-схемы tools + sync/async dispatcher tool → handler |
| `llm/history.py` | create | конвертация записей БД в формат сообщений OpenAI |
| `llm/agent.py` | create | orchestration: `run_turn`, `continue_turn`, `cancel_pending` |
| `tests/conftest.py` | create | общий fixture `tmp_db` для всех тестов с БД |
| `tests/test_llm_history.py` | create | |
| `tests/test_llm_prompt.py` | create | |
| `tests/test_llm_client.py` | create | мок OpenRouter через respx |
| `tests/test_llm_tools.py` | create | |
| `tests/test_llm_agent.py` | create | мок client + БД |

---

## Task 1: Конфиг — новые env-переменные

**Files:**
- Modify: `config.py`
- Modify: `.env.example` (если есть; если нет — пропустить)

- [ ] **Step 1: Расширить `config.py`**

Открой `config.py` и замени содержимое целиком:

```python
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
DB_PATH: str = os.getenv("DB_PATH", "watches.db")
AUTH_CODE: str = os.environ["AUTH_CODE"]
ATLAS_PROXY: str = os.getenv("ATLAS_PROXY", "")

OPENROUTER_API_KEY: str = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_MODEL: str = os.environ["OPENROUTER_MODEL"]
OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MAX_TURNS: int = int(os.getenv("OPENROUTER_MAX_TURNS", "5"))
LLM_HISTORY_SIZE: int = int(os.getenv("LLM_HISTORY_SIZE", "50"))
LLM_VISION: bool = os.getenv("LLM_VISION", "false").lower() == "true"
LLM_AUDIO: bool = os.getenv("LLM_AUDIO", "false").lower() == "true"
LLM_STT_MODEL: str = os.getenv("LLM_STT_MODEL", "")
```

- [ ] **Step 2: Проверить что импорт работает**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model python -c "from config import OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_BASE_URL, OPENROUTER_MAX_TURNS, LLM_HISTORY_SIZE, LLM_VISION, LLM_AUDIO, LLM_STT_MODEL; print(OPENROUTER_BASE_URL, OPENROUTER_MAX_TURNS, LLM_HISTORY_SIZE, LLM_VISION, LLM_AUDIO, repr(LLM_STT_MODEL))"
```
Expected: `https://openrouter.ai/api/v1 5 50 False False ''`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat(config): add openrouter + llm env vars"
```

---

## Task 2: Общий conftest для тестов БД

**Files:**
- Create: `tests/conftest.py`

- [ ] **Step 1: Создать `tests/conftest.py`**

```python
import pytest_asyncio

import db as db_module


@pytest_asyncio.fixture
async def tmp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    await db_module.init_db()
    yield db_path
```

- [ ] **Step 2: Убедиться что существующие тесты всё ещё проходят**

В `tests/test_db.py` оставим локальный fixture как есть — он не конфликтует с conftest (имя совпадает, но pytest возьмёт ближайший — модульный, что нам и нужно для совместимости). Альтернативно можно удалить локальный — но не делаем, чтобы не трогать существующий код в этом таске.

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model pytest tests/test_db.py -v
```
Expected: все тесты PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: shared tmp_db fixture in conftest"
```

---

## Task 3: DB — таблица `chat_messages`

**Files:**
- Modify: `db.py`
- Create: `tests/test_llm_history.py`

- [ ] **Step 1: Написать тесты (TDD)**

Создай `tests/test_llm_history.py`:

```python
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
```

- [ ] **Step 2: Запустить тесты — ждать FAIL**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model pytest tests/test_llm_history.py -v
```
Expected: FAIL — `AttributeError: module 'db' has no attribute 'insert_chat_message'`.

- [ ] **Step 3: Добавить DDL и CRUD в `db.py`**

В `db.py` после блока `_CREATE_AUTH_ATTEMPTS = """..."""` добавь:

```python
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
```

В `init_db()` дoбавь после трёх существующих executes:

```python
        await conn.execute(_CREATE_CHAT_MESSAGES)
        await conn.execute(_CREATE_CHAT_MESSAGES_IDX)
```

В конец файла добавь функции:

```python
import time


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
```

Перенеси `import time` в начало `db.py` рядом с `import json`.

- [ ] **Step 4: Запустить тесты — все PASS**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model pytest tests/test_llm_history.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_llm_history.py
git commit -m "feat(db): chat_messages table + crud"
```

---

## Task 4: DB — таблицы `pending_tool_calls` и `user_profiles`

**Files:**
- Modify: `db.py`
- Modify: `tests/test_llm_history.py`

- [ ] **Step 1: Дописать тесты в `tests/test_llm_history.py`**

В конец файла добавь:

```python
@pytest.mark.asyncio
async def test_set_and_get_pending(tmp_db):
    await db_module.set_pending_tool_call(
        user_id=1, tool_call_id="call_42", tool_name="ask_user",
        options_json='["a", "b"]', message_id=999,
    )
    p = await db_module.get_pending_tool_call(user_id=1)
    assert p is not None
    assert p["tool_call_id"] == "call_42"
    assert p["tool_name"] == "ask_user"
    assert p["options_json"] == '["a", "b"]'
    assert p["message_id"] == 999


@pytest.mark.asyncio
async def test_get_pending_returns_none_when_absent(tmp_db):
    assert await db_module.get_pending_tool_call(user_id=999) is None


@pytest.mark.asyncio
async def test_set_pending_replaces_existing(tmp_db):
    await db_module.set_pending_tool_call(1, "call_1", "ask_user", "[]", 100)
    await db_module.set_pending_tool_call(1, "call_2", "ask_user", '["x"]', 200)
    p = await db_module.get_pending_tool_call(1)
    assert p["tool_call_id"] == "call_2"
    assert p["message_id"] == 200


@pytest.mark.asyncio
async def test_delete_pending(tmp_db):
    await db_module.set_pending_tool_call(1, "call_1", "ask_user", "[]", 100)
    await db_module.delete_pending_tool_call(1)
    assert await db_module.get_pending_tool_call(1) is None


@pytest.mark.asyncio
async def test_delete_pending_idempotent(tmp_db):
    await db_module.delete_pending_tool_call(999)


@pytest.mark.asyncio
async def test_set_and_get_user_name(tmp_db):
    await db_module.set_user_name(user_id=1, name="Маша")
    assert await db_module.get_user_name(1) == "Маша"


@pytest.mark.asyncio
async def test_get_user_name_returns_none_when_absent(tmp_db):
    assert await db_module.get_user_name(999) is None


@pytest.mark.asyncio
async def test_set_user_name_updates_existing(tmp_db):
    await db_module.set_user_name(1, "Маша")
    await db_module.set_user_name(1, "Маша Иванова")
    assert await db_module.get_user_name(1) == "Маша Иванова"


@pytest.mark.asyncio
async def test_set_user_name_ignores_empty(tmp_db):
    await db_module.set_user_name(1, "Маша")
    await db_module.set_user_name(1, "")  # пустая строка не должна затирать
    await db_module.set_user_name(1, None)
    assert await db_module.get_user_name(1) == "Маша"
```

- [ ] **Step 2: Запустить — ждать FAIL**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model pytest tests/test_llm_history.py -v -k pending
```
Expected: FAIL `AttributeError: module 'db' has no attribute 'set_pending_tool_call'`.

- [ ] **Step 3: Добавить DDL и CRUD в `db.py`**

После `_CREATE_CHAT_MESSAGES_IDX` добавь:

```python
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
```

В `init_db()` добавь:

```python
        await conn.execute(_CREATE_PENDING_TOOL_CALLS)
        await conn.execute(_CREATE_USER_PROFILES)
```

В конец `db.py` добавь:

```python
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
```

- [ ] **Step 4: Запустить — все PASS**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model pytest tests/test_llm_history.py -v
```
Expected: 16 passed (7 chat + 5 pending + 4 user_profiles).

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_llm_history.py
git commit -m "feat(db): pending_tool_calls + user_profiles tables"
```

---

## Task 5: LLM history converter

**Files:**
- Create: `llm/__init__.py`
- Create: `llm/history.py`
- Modify: `tests/test_llm_history.py`

- [ ] **Step 1: Создать пустой `llm/__init__.py`**

```python
```

(пустой файл)

- [ ] **Step 2: Написать тесты конвертации**

В конец `tests/test_llm_history.py` добавь:

```python
from llm.history import to_openai_messages


def test_to_openai_messages_user_text():
    rows = [
        {"role": "user", "content": "привет", "tool_calls": None, "tool_call_id": None},
    ]
    result = to_openai_messages(rows)
    assert result == [{"role": "user", "content": "привет"}]


def test_to_openai_messages_assistant_text():
    rows = [
        {"role": "assistant", "content": "ответ", "tool_calls": None, "tool_call_id": None},
    ]
    assert to_openai_messages(rows) == [{"role": "assistant", "content": "ответ"}]


def test_to_openai_messages_assistant_with_tool_calls():
    tc = [{"id": "c1", "type": "function",
           "function": {"name": "list_watches", "arguments": "{}"}}]
    rows = [
        {"role": "assistant", "content": None,
         "tool_calls": json.dumps(tc), "tool_call_id": None},
    ]
    result = to_openai_messages(rows)
    assert result == [{"role": "assistant", "content": None, "tool_calls": tc}]


def test_to_openai_messages_tool_result():
    rows = [
        {"role": "tool", "content": '{"ok": true}',
         "tool_calls": None, "tool_call_id": "c1"},
    ]
    result = to_openai_messages(rows)
    assert result == [{"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'}]


def test_to_openai_messages_mixed_sequence():
    tc = [{"id": "c1", "type": "function",
           "function": {"name": "list_watches", "arguments": "{}"}}]
    rows = [
        {"role": "user", "content": "что у меня?", "tool_calls": None, "tool_call_id": None},
        {"role": "assistant", "content": None,
         "tool_calls": json.dumps(tc), "tool_call_id": None},
        {"role": "tool", "content": "[]", "tool_calls": None, "tool_call_id": "c1"},
        {"role": "assistant", "content": "Ничего нет.", "tool_calls": None, "tool_call_id": None},
    ]
    result = to_openai_messages(rows)
    assert len(result) == 4
    assert result[0]["role"] == "user"
    assert result[1]["tool_calls"] == tc
    assert result[2]["role"] == "tool"
    assert result[3]["content"] == "Ничего нет."
```

- [ ] **Step 3: Запустить — FAIL**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model pytest tests/test_llm_history.py::test_to_openai_messages_user_text -v
```
Expected: FAIL `ModuleNotFoundError: No module named 'llm.history'`.

- [ ] **Step 4: Создать `llm/history.py`**

```python
import json
from typing import Any


def to_openai_messages(rows: list[dict]) -> list[dict[str, Any]]:
    """Преобразует записи из chat_messages в формат сообщений OpenAI Chat Completions."""
    out: list[dict[str, Any]] = []
    for r in rows:
        role = r["role"]
        if role == "user":
            out.append({"role": "user", "content": r["content"]})
        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": r["content"]}
            if r["tool_calls"]:
                msg["tool_calls"] = json.loads(r["tool_calls"])
            out.append(msg)
        elif role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": r["tool_call_id"],
                "content": r["content"],
            })
    return out
```

- [ ] **Step 5: Запустить — все PASS**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model pytest tests/test_llm_history.py -v
```
Expected: все passed.

- [ ] **Step 6: Commit**

```bash
git add llm/__init__.py llm/history.py tests/test_llm_history.py
git commit -m "feat(llm): history rows to openai message format"
```

---

## Task 6: LLM prompt builder

**Files:**
- Create: `llm/prompt.py`
- Create: `tests/test_llm_prompt.py`

- [ ] **Step 1: Тесты**

```python
from datetime import datetime, timezone, timedelta

from llm.prompt import build_system_prompt

MSK = timezone(timedelta(hours=3))  # Europe/Minsk без DST
NOW = datetime(2026, 5, 21, 14, 30, tzinfo=MSK)


def test_system_prompt_contains_role():
    prompt = build_system_prompt(now=NOW, user_name=None)
    assert "маршрут" in prompt.lower()
    assert "только по теме" in prompt.lower()


def test_system_prompt_contains_today_date_and_time():
    prompt = build_system_prompt(now=NOW, user_name=None)
    assert "2026-05-21" in prompt
    assert "14:30" in prompt


def test_system_prompt_includes_timezone_label():
    prompt = build_system_prompt(now=NOW, user_name=None)
    assert "Minsk" in prompt or "Минск" in prompt


def test_system_prompt_with_user_name():
    prompt = build_system_prompt(now=NOW, user_name="Маша")
    assert "Маша" in prompt
    assert "имя" in prompt.lower() or "собеседник" in prompt.lower()


def test_system_prompt_without_user_name_does_not_mention_unknown():
    prompt = build_system_prompt(now=NOW, user_name=None)
    # без имени — нет блока «Имя собеседника:»
    assert "Имя собеседника" not in prompt


def test_system_prompt_lists_providers():
    prompt = build_system_prompt(now=NOW, user_name=None)
    assert "mogilevminsk" in prompt
    assert "avto_slava" in prompt
    assert "buspro" in prompt
    assert "atlasbus" in prompt


def test_system_prompt_lists_directions():
    prompt = build_system_prompt(now=NOW, user_name=None)
    assert "mg_mnsk" in prompt
    assert "mnsk_mg" in prompt
    assert "Могилёв" in prompt
    assert "Минск" in prompt


def test_system_prompt_instructs_to_use_ask_user():
    prompt = build_system_prompt(now=NOW, user_name=None)
    assert "ask_user" in prompt
```

- [ ] **Step 2: Запустить — FAIL**

Expected: `ModuleNotFoundError: No module named 'llm.prompt'`.

- [ ] **Step 3: Создать `llm/prompt.py`**

```python
from datetime import datetime

from providers import PROVIDERS
from providers.base import DIRECTION_LABELS


def build_system_prompt(now: datetime, user_name: str | None) -> str:
    providers_lines = [
        f"  - {key} ({p.display_name})"
        for key, p in PROVIDERS.items()
    ]
    directions_lines = [
        f"  - {key} ({label})"
        for key, label in DIRECTION_LABELS.items()
    ]
    tz_label = now.tzname() or "Europe/Minsk"
    parts = [
        "Ты — ассистент Telegram-бота, который отслеживает свободные места "
        "в маршрутках между Могилёвом и Минском.",
        "",
        f"Сегодня: {now.date().isoformat()}, сейчас {now.strftime('%H:%M')} ({tz_label}).",
    ]
    if user_name:
        parts.append(f"Имя собеседника: {user_name}. Обращайся по имени, если уместно.")
    parts += [
        "",
        "Доступные провайдеры (используй ключи в tool calls):",
        *providers_lines,
        "",
        "Доступные направления:",
        *directions_lines,
        "",
        "Правила:",
        "1. Отвечай ТОЛЬКО по теме бота: отслеживания мест, провайдеры, направления, "
        "расписания, разовые проверки. На оффтопик вежливо отказывайся.",
        "2. Используй tools для выполнения действий пользователя. Не выдумывай "
        "результаты — всегда вызывай нужный tool.",
        "3. Если параметров недостаточно (не указана дата, время, провайдер) — "
        "вызови tool ask_user с понятным вопросом и вариантами ответа. "
        "НЕ угадывай и НЕ выдумывай значения.",
        "4. Парсь относительные даты и время («завтра», «в субботу», «через неделю», "
        "«через 30 минут», «сейчас») относительно текущих даты и времени выше.",
        "5. interval_sec — минимум 60. Если пользователь просит чаще — "
        "поставь 60 и предупреди.",
        "6. Отвечай на русском, кратко и по делу.",
    ]
    return "\n".join(parts)
```

- [ ] **Step 4: Запустить — все PASS**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model pytest tests/test_llm_prompt.py -v
```
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add llm/prompt.py tests/test_llm_prompt.py
git commit -m "feat(llm): system prompt with current datetime + user name"
```

---

## Task 7: LLM client — OpenRouter

**Files:**
- Create: `llm/client.py`
- Create: `tests/test_llm_client.py`

- [ ] **Step 1: Тесты с respx**

```python
import json

import httpx
import pytest
import respx

from llm.client import OpenRouterClient


@pytest.fixture
def mock_openrouter():
    with respx.mock(base_url="https://openrouter.ai/api/v1", assert_all_called=False) as m:
        yield m


@pytest.mark.asyncio
async def test_chat_completion_returns_message(mock_openrouter):
    mock_openrouter.post("/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{
                "message": {"role": "assistant", "content": "привет"},
                "finish_reason": "stop",
            }],
        }),
    )
    client = OpenRouterClient(api_key="k", model="m", base_url="https://openrouter.ai/api/v1")
    msg = await client.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
    )
    assert msg["role"] == "assistant"
    assert msg["content"] == "привет"
    await client.close()


@pytest.mark.asyncio
async def test_chat_completion_sends_correct_payload(mock_openrouter):
    route = mock_openrouter.post("/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }),
    )
    client = OpenRouterClient(api_key="my-key", model="x/y", base_url="https://openrouter.ai/api/v1")
    await client.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
    )
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer my-key"
    body = json.loads(sent.content)
    assert body["model"] == "x/y"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["tools"][0]["function"]["name"] == "f"
    await client.close()


@pytest.mark.asyncio
async def test_chat_completion_returns_tool_calls(mock_openrouter):
    mock_openrouter.post("/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "list_watches", "arguments": "{}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }),
    )
    client = OpenRouterClient(api_key="k", model="m", base_url="https://openrouter.ai/api/v1")
    msg = await client.chat_completion(messages=[], tools=[])
    assert msg["tool_calls"][0]["function"]["name"] == "list_watches"
    await client.close()


@pytest.mark.asyncio
async def test_chat_completion_retries_on_5xx(mock_openrouter):
    route = mock_openrouter.post("/chat/completions").mock(
        side_effect=[
            httpx.Response(503, text="bad gateway"),
            httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            }),
        ],
    )
    client = OpenRouterClient(
        api_key="k", model="m",
        base_url="https://openrouter.ai/api/v1",
        retry_delay=0.0,
    )
    msg = await client.chat_completion(messages=[], tools=[])
    assert msg["content"] == "ok"
    assert route.call_count == 2
    await client.close()


@pytest.mark.asyncio
async def test_chat_completion_raises_after_retry(mock_openrouter):
    mock_openrouter.post("/chat/completions").mock(
        return_value=httpx.Response(500, text="oops"),
    )
    client = OpenRouterClient(
        api_key="k", model="m",
        base_url="https://openrouter.ai/api/v1",
        retry_delay=0.0,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.chat_completion(messages=[], tools=[])
    await client.close()


@pytest.mark.asyncio
async def test_transcribe_uses_stt_model(mock_openrouter):
    route = mock_openrouter.post("/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant",
                                     "content": "привет это транскрипция"}}],
        }),
    )
    client = OpenRouterClient(
        api_key="k", model="main/model",
        base_url="https://openrouter.ai/api/v1",
    )
    text = await client.transcribe(
        stt_model="mistralai/voxtral-mini-transcribe",
        audio_bytes=b"\x00\x01\x02", audio_format="ogg",
    )
    assert text == "привет это транскрипция"
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "mistralai/voxtral-mini-transcribe"
    user_msg = body["messages"][-1]
    parts = user_msg["content"]
    assert any(p.get("type") == "input_audio" for p in parts)
    await client.close()
```

- [ ] **Step 2: Запустить — FAIL**

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Создать `llm/client.py`**

```python
import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        timeout: float = 60.0,
        retry_delay: float = 2.0,
    ) -> None:
        self._model = model
        self._retry_delay = retry_delay
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = await self._client.post("/chat/completions", json=payload)
                if resp.status_code >= 500 or resp.status_code == 429:
                    resp.raise_for_status()
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]
            except httpx.HTTPStatusError as e:
                last_exc = e
                if e.response.status_code < 500 and e.response.status_code != 429:
                    raise
                logger.warning("OpenRouter %s, retrying", e.response.status_code)
            except httpx.RequestError as e:
                last_exc = e
                logger.warning("OpenRouter network error: %s, retrying", e)

            if attempt == 0:
                await asyncio.sleep(self._retry_delay)

        assert last_exc is not None
        raise last_exc

    async def transcribe(
        self,
        stt_model: str,
        audio_bytes: bytes,
        audio_format: str,
    ) -> str:
        """Один STT-вызов через chat/completions с моделью stt_model.
        Возвращает только текст транскрипции."""
        import base64
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        payload = {
            "model": stt_model,
            "messages": [
                {"role": "system",
                 "content": "Транскрибируй это аудио в текст. Верни только текст без комментариев."},
                {"role": "user", "content": [
                    {"type": "input_audio",
                     "input_audio": {"data": b64, "format": audio_format}},
                ]},
            ],
        }
        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return (data["choices"][0]["message"].get("content") or "").strip()

    async def close(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Запустить — все PASS**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model pytest tests/test_llm_client.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add llm/client.py tests/test_llm_client.py
git commit -m "feat(llm): openrouter async client with chat + transcribe"
```

---

## Task 8: LLM tools — schemas + dispatchers

**Files:**
- Create: `llm/tools.py`
- Create: `tests/test_llm_tools.py`

- [ ] **Step 1: Тесты**

```python
import json
from dataclasses import dataclass

import pytest

import db as db_module
import scheduler
from llm.tools import TOOL_SCHEMAS, dispatch_tool, ToolContext


@dataclass
class FakeScheduler:
    started: list = None
    cancelled: list = None

    def __post_init__(self):
        self.started = []
        self.cancelled = []

    async def start_watch(self, w):
        self.started.append(w["id"])

    async def cancel_watch(self, wid):
        self.cancelled.append(wid)


@pytest.fixture
def fake_scheduler(monkeypatch):
    fs = FakeScheduler()
    monkeypatch.setattr(scheduler, "start_watch", fs.start_watch)
    monkeypatch.setattr(scheduler, "cancel_watch", fs.cancel_watch)
    return fs


def test_tool_schemas_contains_all_expected_names():
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert names == {
        "list_watches", "create_watch", "stop_watch",
        "stop_all_watches", "check_trips_now", "ask_user",
    }


def test_tool_schemas_valid_structure():
    for t in TOOL_SCHEMAS:
        assert t["type"] == "function"
        assert "name" in t["function"]
        assert "description" in t["function"]
        assert "parameters" in t["function"]
        assert t["function"]["parameters"]["type"] == "object"


@pytest.mark.asyncio
async def test_dispatch_list_watches_empty(tmp_db, fake_scheduler):
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("list_watches", {}, ctx)
    assert json.loads(result) == {"watches": []}


@pytest.mark.asyncio
async def test_dispatch_list_watches_returns_active(tmp_db, fake_scheduler):
    await db_module.create_watch(1, "atlasbus", "mg_mnsk", "2026-05-24", "11:00", "23:00", 120)
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("list_watches", {}, ctx)
    data = json.loads(result)
    assert len(data["watches"]) == 1
    assert data["watches"][0]["provider"] == "atlasbus"


@pytest.mark.asyncio
async def test_dispatch_create_watch_creates_and_starts(tmp_db, fake_scheduler):
    ctx = ToolContext(user_id=1)
    args = {
        "providers": ["atlasbus", "mogilevminsk"],
        "direction": "mg_mnsk",
        "date": "2026-05-24",
        "time_from": "11:00",
        "time_to": "23:00",
        "interval_sec": 120,
    }
    result = await dispatch_tool("create_watch", args, ctx)
    data = json.loads(result)
    assert len(data["created_ids"]) == 2
    assert len(fake_scheduler.started) == 2
    watches = await db_module.get_user_watches(1)
    assert len(watches) == 2


@pytest.mark.asyncio
async def test_dispatch_create_watch_invalid_provider(tmp_db, fake_scheduler):
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("create_watch", {
        "providers": ["unknown"], "direction": "mg_mnsk",
        "date": "2026-05-24", "time_from": "11:00", "time_to": "23:00",
        "interval_sec": 120,
    }, ctx)
    data = json.loads(result)
    assert "error" in data
    assert "unknown" in data["error"].lower()


@pytest.mark.asyncio
async def test_dispatch_create_watch_invalid_date(tmp_db, fake_scheduler):
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("create_watch", {
        "providers": ["atlasbus"], "direction": "mg_mnsk",
        "date": "tomorrow", "time_from": "11:00", "time_to": "23:00",
        "interval_sec": 120,
    }, ctx)
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_dispatch_create_watch_invalid_interval(tmp_db, fake_scheduler):
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("create_watch", {
        "providers": ["atlasbus"], "direction": "mg_mnsk",
        "date": "2026-05-24", "time_from": "11:00", "time_to": "23:00",
        "interval_sec": 30,
    }, ctx)
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_dispatch_stop_watch_success(tmp_db, fake_scheduler):
    wid = await db_module.create_watch(1, "atlasbus", "mg_mnsk", "2026-05-24", "11:00", "23:00", 120)
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("stop_watch", {"watch_id": wid}, ctx)
    data = json.loads(result)
    assert data["ok"] is True
    assert wid in fake_scheduler.cancelled
    assert len(await db_module.get_user_watches(1)) == 0


@pytest.mark.asyncio
async def test_dispatch_stop_watch_not_owned(tmp_db, fake_scheduler):
    wid = await db_module.create_watch(2, "atlasbus", "mg_mnsk", "2026-05-24", "11:00", "23:00", 120)
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("stop_watch", {"watch_id": wid}, ctx)
    data = json.loads(result)
    assert data["ok"] is False


@pytest.mark.asyncio
async def test_dispatch_stop_all_watches(tmp_db, fake_scheduler):
    await db_module.create_watch(1, "atlasbus", "mg_mnsk", "2026-05-24", "11:00", "23:00", 120)
    await db_module.create_watch(1, "buspro", "mg_mnsk", "2026-05-24", "11:00", "23:00", 120)
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("stop_all_watches", {}, ctx)
    data = json.loads(result)
    assert data["stopped"] == 2
    assert len(await db_module.get_user_watches(1)) == 0


@pytest.mark.asyncio
async def test_dispatch_check_trips_now(monkeypatch, tmp_db):
    from providers.base import Trip

    async def fake_get_trips(client, date, direction):
        return [
            Trip(trip_id="t1", provider="atlasbus", route="r", date=date,
                 departure_time="12:00", free_seats=5, price=20, currency="BYN"),
            Trip(trip_id="t2", provider="atlasbus", route="r", date=date,
                 departure_time="14:00", free_seats=0, price=20, currency="BYN"),
        ]
    from providers import PROVIDERS
    monkeypatch.setattr(PROVIDERS["atlasbus"], "get_trips", fake_get_trips)

    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("check_trips_now", {
        "provider": "atlasbus", "direction": "mg_mnsk",
        "date": "2026-05-24", "time_from": "11:00", "time_to": "15:00",
    }, ctx)
    data = json.loads(result)
    assert len(data["trips"]) == 2
    assert data["trips"][0]["departure_time"] == "12:00"


@pytest.mark.asyncio
async def test_dispatch_unknown_tool(tmp_db, fake_scheduler):
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("nonexistent_xyz", {}, ctx)
    data = json.loads(result)
    assert "error" in data
```

- [ ] **Step 2: Запустить — FAIL**

Expected: `ModuleNotFoundError: No module named 'llm.tools'`.

- [ ] **Step 3: Создать `llm/tools.py`**

```python
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

import db as db_module
import scheduler
from providers import PROVIDERS
from providers.base import DIRECTION_LABELS


@dataclass
class ToolContext:
    user_id: int


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_watches",
            "description": "Вернуть все активные отслеживания пользователя.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_watch",
            "description": (
                "Создать отслеживание свободных мест. Можно указать несколько "
                "провайдеров — будет создан отдельный watch на каждого."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "providers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ключи провайдеров",
                    },
                    "direction": {"type": "string",
                                  "description": "mg_mnsk или mnsk_mg"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "time_from": {"type": "string", "description": "HH:MM"},
                    "time_to": {"type": "string", "description": "HH:MM"},
                    "interval_sec": {"type": "integer",
                                     "description": "Минимум 60"},
                },
                "required": ["providers", "direction", "date",
                             "time_from", "time_to", "interval_sec"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_watch",
            "description": "Остановить одно отслеживание по его id.",
            "parameters": {
                "type": "object",
                "properties": {"watch_id": {"type": "integer"}},
                "required": ["watch_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_all_watches",
            "description": "Остановить все активные отслеживания пользователя.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_trips_now",
            "description": (
                "Разовая проверка рейсов в окне времени БЕЗ создания "
                "отслеживания. Возвращает список рейсов и свободные места."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {"type": "string"},
                    "direction": {"type": "string"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "time_from": {"type": "string", "description": "HH:MM"},
                    "time_to": {"type": "string", "description": "HH:MM"},
                },
                "required": ["provider", "direction", "date",
                             "time_from", "time_to"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Задать пользователю уточняющий вопрос с готовыми вариантами "
                "ответа в виде inline-кнопок. Используй когда параметров "
                "недостаточно или нужен выбор."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 8,
                    },
                },
                "required": ["question", "options"],
            },
        },
    },
]


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def _valid_date(s: str) -> bool:
    if not _DATE_RE.match(s):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _valid_time(s: str) -> bool:
    if not _TIME_RE.match(s):
        return False
    try:
        datetime.strptime(s, "%H:%M")
        return True
    except ValueError:
        return False


async def _tool_list_watches(args: dict, ctx: ToolContext) -> str:
    watches = await db_module.get_user_watches(ctx.user_id)
    out = [{
        "id": w["id"],
        "provider": w["provider"],
        "provider_display": PROVIDERS[w["provider"]].display_name,
        "direction": w["direction"],
        "direction_label": DIRECTION_LABELS[w["direction"]],
        "date": w["date"],
        "time_from": w["time_from"],
        "time_to": w["time_to"],
        "interval_sec": w["interval_sec"],
    } for w in watches]
    return json.dumps({"watches": out}, ensure_ascii=False)


async def _tool_create_watch(args: dict, ctx: ToolContext) -> str:
    providers = args.get("providers")
    if not isinstance(providers, list) or not providers:
        return _err("providers: непустой массив ключей")
    unknown = [p for p in providers if p not in PROVIDERS]
    if unknown:
        return _err(
            f"Unknown providers: {unknown}. Available: {list(PROVIDERS.keys())}"
        )

    direction = args.get("direction")
    if direction not in DIRECTION_LABELS:
        return _err(
            f"direction must be one of {list(DIRECTION_LABELS.keys())}"
        )

    date_s = args.get("date", "")
    if not _valid_date(date_s):
        return _err("date must be YYYY-MM-DD")

    tf = args.get("time_from", "")
    tt = args.get("time_to", "")
    if not _valid_time(tf) or not _valid_time(tt):
        return _err("time_from/time_to must be HH:MM")

    interval = args.get("interval_sec")
    if not isinstance(interval, int) or interval < 60:
        return _err("interval_sec must be integer >= 60")

    created_ids = []
    for p in providers:
        wid = await db_module.create_watch(
            user_id=ctx.user_id, provider=p, direction=direction,
            date=date_s, time_from=tf, time_to=tt, interval_sec=interval,
        )
        await scheduler.start_watch({
            "id": wid, "user_id": ctx.user_id, "notified_trips": "[]",
            "provider": p, "direction": direction, "date": date_s,
            "time_from": tf, "time_to": tt, "interval_sec": interval,
        })
        created_ids.append(wid)

    return json.dumps({"created_ids": created_ids}, ensure_ascii=False)


async def _tool_stop_watch(args: dict, ctx: ToolContext) -> str:
    wid = args.get("watch_id")
    if not isinstance(wid, int):
        return _err("watch_id must be integer")
    ok = await db_module.stop_watch(wid, ctx.user_id)
    if ok:
        await scheduler.cancel_watch(wid)
    return json.dumps({"ok": ok, "watch_id": wid})


async def _tool_stop_all_watches(args: dict, ctx: ToolContext) -> str:
    watches = await db_module.get_user_watches(ctx.user_id)
    count = 0
    for w in watches:
        if await db_module.stop_watch(w["id"], ctx.user_id):
            await scheduler.cancel_watch(w["id"])
            count += 1
    return json.dumps({"stopped": count})


async def _tool_check_trips_now(args: dict, ctx: ToolContext) -> str:
    provider_key = args.get("provider")
    if provider_key not in PROVIDERS:
        return _err(f"Unknown provider: {provider_key}")
    direction = args.get("direction")
    if direction not in DIRECTION_LABELS:
        return _err(f"direction must be one of {list(DIRECTION_LABELS.keys())}")
    date_s = args.get("date", "")
    if not _valid_date(date_s):
        return _err("date must be YYYY-MM-DD")
    tf = args.get("time_from", "")
    tt = args.get("time_to", "")
    if not _valid_time(tf) or not _valid_time(tt):
        return _err("time_from/time_to must be HH:MM")

    provider = PROVIDERS[provider_key]
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            trips = await provider.get_trips(client, date_s, direction)
        except Exception as e:
            return _err(f"Provider call failed: {e}")

    filtered = [t for t in trips if tf <= t.departure_time <= tt]
    return json.dumps({
        "trips": [{
            "trip_id": t.trip_id, "departure_time": t.departure_time,
            "free_seats": t.free_seats, "price": t.price, "currency": t.currency,
        } for t in filtered],
    }, ensure_ascii=False)


_HANDLERS = {
    "list_watches": _tool_list_watches,
    "create_watch": _tool_create_watch,
    "stop_watch": _tool_stop_watch,
    "stop_all_watches": _tool_stop_all_watches,
    "check_trips_now": _tool_check_trips_now,
}


async def dispatch_tool(name: str, args: dict, ctx: ToolContext) -> str:
    """Запускает tool и возвращает JSON-строку для tool_result.
    ask_user НЕ обрабатывается здесь — это делает agent."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return _err(f"Unknown tool: {name}")
    return await handler(args, ctx)
```

- [ ] **Step 4: Запустить — все PASS**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model pytest tests/test_llm_tools.py -v
```
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add llm/tools.py tests/test_llm_tools.py
git commit -m "feat(llm): tool schemas and dispatcher"
```

---

## Task 9: LLM agent — базовый `run_turn` (без ask_user, без фото)

**Files:**
- Create: `llm/agent.py`
- Create: `tests/test_llm_agent.py`

- [ ] **Step 1: Тесты**

```python
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
        side_effect=httpx.RequestError("connection refused", request=None),
    )
    agent = _mk_agent(fake_bot, client)
    await agent.run_turn(user_id=1, text="ping", user_name=None)
    assert len(fake_bot.sent) == 1
    assert "AI" in fake_bot.sent[0]["text"] or "ошибк" in fake_bot.sent[0]["text"].lower()


@pytest.mark.asyncio
async def test_run_turn_per_user_lock_is_per_user(tmp_db, fake_bot, fake_scheduler):
    """Locks разных юзеров не блокируют друг друга — проверяем что для разных user_id
    нет ожидания (грубый smoke-test: оба run_turn выполняются последовательно но без падения)."""
    client = AsyncMock()
    client.chat_completion = AsyncMock(return_value={
        "role": "assistant", "content": "ok"
    })
    agent = _mk_agent(fake_bot, client)
    await agent.run_turn(user_id=1, text="a", user_name=None)
    await agent.run_turn(user_id=2, text="b", user_name=None)
    assert len(fake_bot.sent) == 2
```

- [ ] **Step 2: Запустить — FAIL**

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Создать `llm/agent.py`**

```python
import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Callable

import httpx

from config import OPENROUTER_MAX_TURNS, LLM_HISTORY_SIZE
import db as db_module
from llm.client import OpenRouterClient
from llm.history import to_openai_messages
from llm.prompt import build_system_prompt
from llm.tools import TOOL_SCHEMAS, ToolContext, dispatch_tool

logger = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))


def _default_now() -> datetime:
    return datetime.now(_MSK)


class LLMAgent:
    def __init__(
        self,
        bot: Any,
        client: OpenRouterClient,
        now_provider: Callable[[], datetime] = _default_now,
    ) -> None:
        self._bot = bot
        self._client = client
        self._now = now_provider
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    async def run_turn(self, user_id: int, text: str, user_name: str | None) -> None:
        async with self._lock_for(user_id):
            await db_module.set_user_name(user_id, user_name)
            await db_module.insert_chat_message(user_id, "user", content=text)
            await self._drive_turn(user_id)

    async def _drive_turn(self, user_id: int) -> None:
        for _turn in range(OPENROUTER_MAX_TURNS):
            try:
                rows = await db_module.get_recent_chat_messages(user_id, LLM_HISTORY_SIZE)
                stored_name = await db_module.get_user_name(user_id)
                messages = [{"role": "system",
                             "content": build_system_prompt(
                                 now=self._now(), user_name=stored_name,
                             )}]
                messages.extend(to_openai_messages(rows))
                msg = await self._client.chat_completion(
                    messages=messages, tools=TOOL_SCHEMAS,
                )
            except httpx.HTTPError as e:
                logger.warning("LLM HTTP error: %s", e)
                err = "Не получилось связаться с AI, попробуй ещё раз позже."
                await db_module.insert_chat_message(user_id, "assistant", content=err)
                await self._bot.send_message(user_id, err)
                return
            except Exception as e:
                logger.exception("LLM unexpected error: %s", e)
                err = "Что-то сломалось на стороне AI. Попробуй ещё раз."
                await db_module.insert_chat_message(user_id, "assistant", content=err)
                await self._bot.send_message(user_id, err)
                return

            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")

            if not tool_calls:
                await db_module.insert_chat_message(user_id, "assistant", content=content)
                if content:
                    await self._bot.send_message(user_id, content)
                await db_module.prune_chat_messages(user_id, LLM_HISTORY_SIZE * 2)
                return

            await db_module.insert_chat_message(
                user_id, "assistant", content=content,
                tool_calls=json.dumps(tool_calls),
            )

            ctx = ToolContext(user_id=user_id)
            for tc in tool_calls:
                tc_id = tc["id"]
                fn = tc["function"]
                name = fn["name"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name == "ask_user":
                    # Placeholder — реальная обработка добавится в следующем таске.
                    result = json.dumps({"error": "ask_user not yet wired"})
                else:
                    result = await dispatch_tool(name, args, ctx)
                await db_module.insert_chat_message(
                    user_id, "tool", content=result, tool_call_id=tc_id,
                )
            # следующая итерация цикла — пусть LLM посмотрит результаты

        # лимит витков превышен
        msg_text = "Запутался — попробуй переформулировать."
        await db_module.insert_chat_message(user_id, "assistant", content=msg_text)
        await self._bot.send_message(user_id, msg_text)
        await db_module.prune_chat_messages(user_id, LLM_HISTORY_SIZE * 2)
```

- [ ] **Step 4: Запустить — все PASS**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model pytest tests/test_llm_agent.py -v
```
Expected: 7 passed (включая 2 новых: persist name + name in prompt).

- [ ] **Step 5: Commit**

```bash
git add llm/agent.py tests/test_llm_agent.py
git commit -m "feat(llm): agent run_turn with user_name + tz-aware now"
```

---

## Task 10: LLM agent — обработка `ask_user`

**Files:**
- Modify: `llm/agent.py`
- Modify: `tests/test_llm_agent.py`

- [ ] **Step 1: Дописать тесты**

В `tests/test_llm_agent.py` добавь:

```python
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

    assert len(fake_bot.sent) == 1
    assert "Какое отслеживание" in fake_bot.sent[0]["text"]
    kb = fake_bot.sent[0]["reply_markup"]
    assert kb is not None
    p = await db_module.get_pending_tool_call(1)
    assert p is not None
    assert p["tool_call_id"] == "c1"
    msgs = await db_module.get_recent_chat_messages(1, 100)
    # tool result для ask_user пока НЕ записан
    assert all(m["role"] != "tool" for m in msgs)


@pytest.mark.asyncio
async def test_continue_turn_after_callback(tmp_db, fake_bot, fake_scheduler):
    client = AsyncMock()
    client.chat_completion = AsyncMock(return_value={
        "role": "assistant", "content": "Отлично, удалил #5",
    })
    agent = _mk_agent(fake_bot, client)
    # подготавливаем pending вручную
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
    # клавиатура снята
    assert len(fake_bot.edited) == 1
    assert fake_bot.edited[0]["message_id"] == 555


@pytest.mark.asyncio
async def test_run_turn_auto_cancels_pending(tmp_db, fake_bot, fake_scheduler):
    """Если приходит новый текст пока висит pending — отменить и продолжить."""
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
    # должна быть запись tool с canceled
    canceled = [m for m in msgs if m["role"] == "tool"
                and m["tool_call_id"] == "c1"]
    assert len(canceled) == 1
    assert json.loads(canceled[0]["content"])["canceled"] is True
```

- [ ] **Step 2: Запустить — FAIL**

Expected: либо `AttributeError: 'LLMAgent' object has no attribute 'continue_turn'`, либо неверный поток (placeholder).

- [ ] **Step 3: Обновить `llm/agent.py`**

В начале файла добавь импорты:

```python
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
```

Замени метод `run_turn` и добавь новые методы. Полная новая версия методов класса (после `_lock_for`):

```python
    async def run_turn(self, user_id: int, text: str) -> None:
        async with self._lock_for(user_id):
            await self._auto_cancel_pending(user_id)
            await db_module.insert_chat_message(user_id, "user", content=text)
            await self._drive_turn(user_id)

    async def continue_turn(self, user_id: int, selected_option: str) -> None:
        async with self._lock_for(user_id):
            pending = await db_module.get_pending_tool_call(user_id)
            if pending is None:
                return
            await db_module.insert_chat_message(
                user_id, "tool", content=selected_option,
                tool_call_id=pending["tool_call_id"],
            )
            await db_module.delete_pending_tool_call(user_id)
            await self._drive_turn(user_id)

    async def cancel_pending(self, user_id: int) -> None:
        async with self._lock_for(user_id):
            await self._auto_cancel_pending(user_id)

    async def _auto_cancel_pending(self, user_id: int) -> None:
        pending = await db_module.get_pending_tool_call(user_id)
        if pending is None:
            return
        try:
            await self._bot.edit_message_reply_markup(
                chat_id=user_id,
                message_id=pending["message_id"],
                reply_markup=None,
            )
        except Exception as e:
            logger.debug("Could not strip keyboard from msg %s: %s",
                         pending["message_id"], e)
        await db_module.insert_chat_message(
            user_id, "tool",
            content=json.dumps({"canceled": True,
                                "reason": "user sent new message"}),
            tool_call_id=pending["tool_call_id"],
        )
        await db_module.delete_pending_tool_call(user_id)
```

И в цикле `_drive_turn` замени блок с `if name == "ask_user":` на:

```python
                if name == "ask_user":
                    await self._handle_ask_user(user_id, tc_id, args)
                    return  # выходим из turn — ждём callback
                else:
                    result = await dispatch_tool(name, args, ctx)
                    await db_module.insert_chat_message(
                        user_id, "tool", content=result, tool_call_id=tc_id,
                    )
```

(Обрати внимание: для non-ask_user веток оставляем существующее поведение — записать tool result. Если в текущей версии оно уже выполняется ниже общим путём, перенеси/исправь чтобы не было двойной записи.)

Добавь новый приватный метод:

```python
    async def _handle_ask_user(self, user_id: int, tool_call_id: str, args: dict) -> None:
        question = str(args.get("question", "Уточни, пожалуйста"))
        options = args.get("options") or []
        if not isinstance(options, list) or len(options) < 2:
            # модель прислала мусор — фолбэк: запишем ошибку как tool result
            await db_module.insert_chat_message(
                user_id, "tool",
                content=json.dumps({"error": "ask_user needs 2..8 options"}),
                tool_call_id=tool_call_id,
            )
            await self._drive_turn(user_id)
            return
        options = [str(o) for o in options][:8]

        rows = [[InlineKeyboardButton(text=opt[:64], callback_data=f"ai:{i}")]
                for i, opt in enumerate(options)]
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        sent = await self._bot.send_message(user_id, question, reply_markup=kb)

        await db_module.set_pending_tool_call(
            user_id=user_id, tool_call_id=tool_call_id,
            tool_name="ask_user",
            options_json=json.dumps(options, ensure_ascii=False),
            message_id=sent.message_id,
        )
```

- [ ] **Step 4: Запустить все тесты agent**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model pytest tests/test_llm_agent.py -v
```
Expected: все passed (11 total — 7 базовых + 4 ask_user).

- [ ] **Step 5: Commit**

```bash
git add llm/agent.py tests/test_llm_agent.py
git commit -m "feat(llm): ask_user tool with pending state and cancel"
```

---

## Task 11: LLM agent — поддержка vision (фото)

**Files:**
- Modify: `llm/agent.py`
- Modify: `tests/test_llm_agent.py`

- [ ] **Step 1: Тесты**

В `tests/test_llm_agent.py` добавь:

```python
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
    # ничего не записано в LLM, в БД пусто
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

    # последнее user-сообщение в LLM — multimodal
    user_msg = next(m for m in reversed(captured["messages"]) if m["role"] == "user")
    assert isinstance(user_msg["content"], list)
    assert any(p.get("type") == "image_url" for p in user_msg["content"])
    # в БД сохранён плейсхолдер без base64
    msgs = await db_module.get_recent_chat_messages(1, 100)
    user_rows = [m for m in msgs if m["role"] == "user"]
    assert len(user_rows) == 1
    assert "[photo]" in user_rows[0]["content"]
    assert "опиши" in user_rows[0]["content"]
    assert "data:image" not in user_rows[0]["content"]
```

- [ ] **Step 2: Запустить — FAIL**

Expected: `AttributeError: 'LLMAgent' object has no attribute 'handle_photo'`.

- [ ] **Step 3: Обновить `llm/agent.py`**

В импорты добавь:

```python
import base64
from config import OPENROUTER_MAX_TURNS, LLM_HISTORY_SIZE, LLM_VISION
```

Добавь метод в класс `LLMAgent`:

```python
    async def handle_photo(
        self,
        user_id: int,
        image_bytes: bytes,
        mime: str,
        caption: str | None,
        user_name: str | None,
    ) -> None:
        if not LLM_VISION:
            msg = ("Текущая модель не умеет читать фото. "
                   "Опиши текстом или поменяй модель в .env.")
            await self._bot.send_message(user_id, msg)
            return

        async with self._lock_for(user_id):
            await db_module.set_user_name(user_id, user_name)
            await self._auto_cancel_pending(user_id)
            caption_text = caption or ""
            placeholder = f"[photo] {caption_text}".strip()
            await db_module.insert_chat_message(user_id, "user", content=placeholder)
            await self._drive_turn(
                user_id,
                _override_last_user=self._build_multimodal_user(image_bytes, mime, caption_text),
            )

    def _build_multimodal_user(
        self,
        image_bytes: bytes,
        mime: str,
        caption: str,
    ) -> dict[str, Any]:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        parts: list[dict[str, Any]] = []
        if caption:
            parts.append({"type": "text", "text": caption})
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
        return {"role": "user", "content": parts}
```

Затем измени сигнатуру `_drive_turn`:

```python
    async def _drive_turn(
        self,
        user_id: int,
        _override_last_user: dict[str, Any] | None = None,
    ) -> None:
        for _turn in range(OPENROUTER_MAX_TURNS):
            try:
                rows = await db_module.get_recent_chat_messages(user_id, LLM_HISTORY_SIZE)
                messages = [{"role": "system",
                             "content": build_system_prompt(today=self._today())}]
                messages.extend(to_openai_messages(rows))
                if _override_last_user is not None:
                    # заменить последний user-msg на multimodal вариант (только первый виток)
                    for i in range(len(messages) - 1, -1, -1):
                        if messages[i]["role"] == "user":
                            messages[i] = _override_last_user
                            break
                    _override_last_user = None  # только в первом витке
                msg = await self._client.chat_completion(
                    messages=messages, tools=TOOL_SCHEMAS,
                )
            # ... остальное без изменений
```

(Остальное тело метода уже есть — оставь как было.)

- [ ] **Step 4: Запустить все тесты agent**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model pytest tests/test_llm_agent.py -v
```
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add llm/agent.py tests/test_llm_agent.py
git commit -m "feat(llm): vision photo handling (LLM_VISION flag)"
```

---

## Task 12: LLM agent — поддержка аудио (voice / audio)

Два режима:
- `LLM_AUDIO=true` — основная модель сама умеет аудио input (multimodal). Шлём напрямую.
- `LLM_AUDIO=false`, `LLM_STT_MODEL` задан — отдельный STT-вызов, потом текст в `run_turn`.
- `LLM_AUDIO=false`, `LLM_STT_MODEL` пуст — отказ.

**Files:**
- Modify: `llm/agent.py`
- Modify: `tests/test_llm_agent.py`

- [ ] **Step 1: Тесты**

В `tests/test_llm_agent.py` добавь:

```python
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
        side_effect=httpx.RequestError("boom", request=None),
    )
    agent = _mk_agent(fake_bot, client)

    await agent.handle_audio(
        user_id=1, audio_bytes=b"\x00", audio_format="ogg",
        caption=None, user_name=None,
    )

    assert "не удалось" in fake_bot.sent[-1]["text"].lower() or "ошибк" in fake_bot.sent[-1]["text"].lower()
    msgs = await db_module.get_recent_chat_messages(1, 100)
    assert len(msgs) == 0  # ничего не записываем при провале STT
```

- [ ] **Step 2: Запустить — FAIL**

Expected: `AttributeError: 'LLMAgent' object has no attribute 'handle_audio'`.

- [ ] **Step 3: Обновить `llm/agent.py`**

В импортах добавь `LLM_AUDIO`, `LLM_STT_MODEL`:

```python
from config import (
    OPENROUTER_MAX_TURNS, LLM_HISTORY_SIZE, LLM_VISION,
    LLM_AUDIO, LLM_STT_MODEL,
)
```

Добавь метод в класс `LLMAgent`:

```python
    async def handle_audio(
        self,
        user_id: int,
        audio_bytes: bytes,
        audio_format: str,
        caption: str | None,
        user_name: str | None,
    ) -> None:
        # Случай 1: основная модель умеет аудио → multimodal как у фото
        if LLM_AUDIO:
            async with self._lock_for(user_id):
                await db_module.set_user_name(user_id, user_name)
                await self._auto_cancel_pending(user_id)
                caption_text = caption or ""
                placeholder = f"[audio] {caption_text}".strip()
                await db_module.insert_chat_message(user_id, "user", content=placeholder)
                await self._drive_turn(
                    user_id,
                    _override_last_user=self._build_audio_user(
                        audio_bytes, audio_format, caption_text,
                    ),
                )
            return

        # Случай 2: STT включён → транскрибируем и идём через обычный run_turn
        if LLM_STT_MODEL:
            try:
                text = await self._client.transcribe(
                    stt_model=LLM_STT_MODEL,
                    audio_bytes=audio_bytes,
                    audio_format=audio_format,
                )
            except Exception as e:
                logger.warning("STT failed: %s", e)
                await self._bot.send_message(
                    user_id,
                    "Не удалось распознать голосовое. Попробуй ещё раз или напиши текстом.",
                )
                return
            if not text:
                await self._bot.send_message(
                    user_id,
                    "Не разобрал голосовое. Скажи ещё раз или напиши текстом.",
                )
                return
            async with self._lock_for(user_id):
                await db_module.set_user_name(user_id, user_name)
                await self._auto_cancel_pending(user_id)
                placeholder = f"[voice] {text}"
                await db_module.insert_chat_message(user_id, "user", content=placeholder)
                await self._drive_turn(user_id)
            return

        # Случай 3: ничего не настроено
        await self._bot.send_message(
            user_id,
            "Я не умею слушать голосовые. Напиши текстом или настрой "
            "LLM_AUDIO/LLM_STT_MODEL в .env.",
        )

    def _build_audio_user(
        self,
        audio_bytes: bytes,
        audio_format: str,
        caption: str,
    ) -> dict[str, Any]:
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        parts: list[dict[str, Any]] = []
        if caption:
            parts.append({"type": "text", "text": caption})
        parts.append({
            "type": "input_audio",
            "input_audio": {"data": b64, "format": audio_format},
        })
        return {"role": "user", "content": parts}
```

- [ ] **Step 4: Запустить все тесты agent**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model pytest tests/test_llm_agent.py -v
```
Expected: 15 passed (11 предыдущих + 4 новых).

- [ ] **Step 5: Commit**

```bash
git add llm/agent.py tests/test_llm_agent.py
git commit -m "feat(llm): audio handling — native multimodal or stt fallback"
```

---

## Task 13: handlers.py — fallback на свободный текст

**Files:**
- Modify: `handlers.py`

Этот таск — wiring, без новых юнит-тестов (aiogram-хендлеры лучше валидировать ручным smoke-тестом). Логику ядра мы уже покрыли.

- [ ] **Step 1: Добавить fallback-хендлер в `handlers.py`**

В начале `handlers.py` после существующих импортов добавь:

```python
from llm.agent import LLMAgent

_agent: LLMAgent | None = None


def set_agent(agent: LLMAgent) -> None:
    global _agent
    _agent = agent
```

Перед закрывающей строкой (после всех существующих `@router.message(...)` и `cmd_stop`) добавь fallback. **Важно: регистрируется ПОСЛЕДНИМ — приоритет команд и FSM не нарушится.**

```python
def _user_display_name(u) -> str | None:
    return (u.first_name or u.username or None) if u else None


@router.message(F.photo)
async def on_photo(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        return  # FSM-форма активна — не вмешиваемся
    if _agent is None:
        return
    photo = message.photo[-1]  # самое крупное
    file = await message.bot.get_file(photo.file_id)
    buf = await message.bot.download_file(file.file_path)
    image_bytes = buf.read() if hasattr(buf, "read") else bytes(buf)
    await _agent.handle_photo(
        user_id=message.from_user.id,
        image_bytes=image_bytes,
        mime="image/jpeg",
        caption=message.caption,
        user_name=_user_display_name(message.from_user),
    )


@router.message(F.voice | F.audio)
async def on_voice_or_audio(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        return
    if _agent is None:
        return
    if message.voice:
        file_id = message.voice.file_id
        audio_format = "ogg"
    else:
        file_id = message.audio.file_id
        mime = (message.audio.mime_type or "").lower()
        if "mp3" in mime or "mpeg" in mime:
            audio_format = "mp3"
        elif "wav" in mime:
            audio_format = "wav"
        elif "ogg" in mime or "opus" in mime:
            audio_format = "ogg"
        else:
            audio_format = "mp3"  # разумный дефолт для произвольных audio
    file = await message.bot.get_file(file_id)
    buf = await message.bot.download_file(file.file_path)
    audio_bytes = buf.read() if hasattr(buf, "read") else bytes(buf)
    await _agent.handle_audio(
        user_id=message.from_user.id,
        audio_bytes=audio_bytes,
        audio_format=audio_format,
        caption=message.caption,
        user_name=_user_display_name(message.from_user),
    )


@router.message(F.text)
async def on_text_fallback(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        return  # FSM-форма активна
    if message.text and message.text.startswith("/"):
        return  # команды обрабатываются раньше; на всякий случай страхуемся
    if _agent is None:
        await message.answer("AI пока не настроен.")
        return
    await _agent.run_turn(
        user_id=message.from_user.id,
        text=message.text,
        user_name=_user_display_name(message.from_user),
    )


@router.callback_query(F.data.startswith("ai:"))
async def on_ai_callback(cb: CallbackQuery):
    if _agent is None:
        await cb.answer()
        return
    try:
        idx = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer()
        return
    user_id = cb.from_user.id

    import json as _json
    pending = await __import__("db").get_pending_tool_call(user_id)
    if pending is None:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.answer("Вопрос устарел.", show_alert=False)
        return

    try:
        options = _json.loads(pending["options_json"])
    except Exception:
        options = []
    if not (0 <= idx < len(options)):
        await cb.answer()
        return

    selected = options[idx]
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.answer()
    await _agent.continue_turn(user_id=user_id, selected_option=selected)
```

- [ ] **Step 2: Проверить что регистрация порядка корректная**

В `handlers.py` `router = Router()` стоит в начале. Хендлеры регистрируются по порядку определения. Команды `/start /watch /list /stop /auth` определены ВЫШЕ fallback'а, FSM-хендлеры тоже. Значит fallback срабатывает последним — корректно.

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model python -c "import handlers; print('ok')"
```
Expected: `ok`.

- [ ] **Step 3: Прогнать ВСЕ существующие тесты**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model pytest -v
```
Expected: все passed (включая старые test_db, test_scheduler, test_providers).

- [ ] **Step 4: Commit**

```bash
git add handlers.py
git commit -m "feat(handlers): llm fallback for text/photo + ai callback"
```

---

## Task 14: bot.py — собрать всё вместе

**Files:**
- Modify: `bot.py`

- [ ] **Step 1: Обновить `bot.py`**

Полная новая версия:

```python
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import (
    BOT_TOKEN, OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_BASE_URL,
)
from db import init_db
from handlers import router, set_agent
from llm.agent import LLMAgent
from llm.client import OpenRouterClient
from scheduler import init_scheduler, restore_watches

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await init_db()
    init_scheduler(bot)
    await restore_watches()

    llm_client = OpenRouterClient(
        api_key=OPENROUTER_API_KEY,
        model=OPENROUTER_MODEL,
        base_url=OPENROUTER_BASE_URL,
    )
    agent = LLMAgent(bot=bot, client=llm_client)
    set_agent(agent)

    try:
        await dp.start_polling(bot)
    finally:
        await llm_client.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Smoke-import**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model python -c "import bot; print('ok')"
```
Expected: `ok`.

- [ ] **Step 3: Прогнать все тесты ещё раз**

Run:
```bash
OPENROUTER_API_KEY=test OPENROUTER_MODEL=test/model pytest -v
```
Expected: все passed.

- [ ] **Step 4: Manual smoke-test (ручной)**

Поставь в `.env`:
- `OPENROUTER_API_KEY=...` — свой ключ
- `OPENROUTER_MODEL=deepseek/deepseek-v4-flash:free` (или другая модель с tool calling)
- (опционально) `LLM_VISION=true` — если модель умеет vision
- (опционально) `LLM_AUDIO=true` — если основная модель умеет audio input
- (опционально) `LLM_STT_MODEL=mistralai/voxtral-mini-transcribe` или `google/chirp-3` — если основная не умеет аудио, но хочется голосовых сообщений

Запусти бота:

```bash
python bot.py
```

В Telegram (после `/auth <код>`):
- Напиши: «что у меня?» → должен вызваться `list_watches` → ответ с пустым списком или с активными.
- Напиши: «следи за местами на atlasbus Могилёв→Минск завтра с 11 до 23 каждые 2 минуты» → должно создаться отслеживание.
- Напиши: «удали» → должен прийти `ask_user` с inline-кнопками вариантов отслеживаний.
- Тыкни кнопку → отслеживание удалится, бот подтвердит.
- Пока висят кнопки, напиши новое сообщение → клавиатура снимется, новое сообщение обработается.
- Старые команды `/watch`, `/list`, `/stop` — должны работать как раньше.
- (если включил аудио) Отправь голосовое: «следи за местами завтра» → должен либо транскрибироваться через STT, либо обработаться напрямую — и LLM создаст watch / спросит уточнения через `ask_user`.

- [ ] **Step 5: Commit**

```bash
git add bot.py
git commit -m "feat(bot): wire openrouter llm agent into bot startup"
```

---

## Self-Review (для автора плана — не задача для исполнителя)

**Spec coverage:** все секции спеки покрыты:
- Архитектура (`llm/` пакет) → Tasks 5–12
- Конфиг → Task 1
- Tools (6 штук, выкинул `list_providers` в пользу подстановки в системный промпт) → Tasks 6, 8
- Data model → Tasks 3, 4
- Чистка истории → Task 9 (`prune_chat_messages` вызывается в `_drive_turn`)
- Flow A/B/C/D/E/F (фото) → Tasks 9, 10, 11, 13
- Flow G (аудио — native multimodal или STT-fallback) → Tasks 7 (`transcribe`), 12, 13
- Безопасность (`user_id` из контекста, не из args) → Task 8
- Ошибки → Tasks 7, 9, 12
- Тесты → во всех task'ах TDD

**Минус против спеки:** убрал tool `list_providers` — провайдеры подставляются в системный промпт, отдельный tool избыточен. Это упрощение, не противоречие.

**Type consistency:** `ToolContext.user_id` (Task 8) → используется в Task 9 `dispatch_tool(name, args, ctx)`. `LLMAgent.run_turn/continue_turn/cancel_pending/handle_photo` — единые имена во всех тестах и теле.