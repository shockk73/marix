# Invite Auth & Roles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Вход по одноразовой deep-link `t.me/<бот>?start=<токен>`, роли user/admin, AUTH_CODE поднимает до админа, LLM-инструменты create_invite/list_invites для админа.

**Architecture:** Роль — колонка в `authorized_users` (миграция PRAGMA table_info). Инвайты — таблица `invites`, атомарный `use_invite` (UPDATE WHERE used_by IS NULL). Логика входа неавторизованных вынесена в чистую функцию `handle_unauthorized_message` (тестируется без aiogram-объектов), middleware — тонкая обёртка. Админ-тулзы фильтруются `build_tools_for_role`, плюс проверка роли внутри хендлеров (защита в глубину). Онбординг агентом — в под-проекте 4; здесь простое приветствие.

**Tech Stack:** aiosqlite, aiogram 3, pytest.

**Spec:** `docs/superpowers/specs/2026-06-12-invite-auth-design.md`. Без TDD по требованию пользователя.

---

### Task 1: db.py — роли и инвайты

**Files:**
- Modify: `db.py`

- [ ] **Step 1: импорт `secrets` в шапку; DDL инвайтов после `_CREATE_AGENT_CALLBACKS`**

```python
_CREATE_INVITES = """
CREATE TABLE IF NOT EXISTS invites (
    token      TEXT PRIMARY KEY,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    used_by    INTEGER,
    used_at    TEXT
)
"""
```

- [ ] **Step 2: миграция роли + регистрация таблицы в `init_db`**

```python
async def _ensure_column(conn, table: str, ddl_column: str) -> None:
    column = ddl_column.split()[0]
    cur = await conn.execute(f"PRAGMA table_info({table})")
    existing = [row[1] for row in await cur.fetchall()]
    if column not in existing:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl_column}")
```

в `init_db` перед `commit`:

```python
        await conn.execute(_CREATE_INVITES)
        await _ensure_column(conn, "authorized_users",
                             "role TEXT NOT NULL DEFAULT 'user'")
```

- [ ] **Step 3: `authorize_user` с ролью (upgrade, не downgrade) + get/set роли**

```python
async def authorize_user(user_id: int, role: str = "user") -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO authorized_users (user_id, authorized_at, role)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET role = excluded.role
               WHERE excluded.role = 'admin'""",
            (user_id, datetime.now().isoformat(), role),
        )
        await conn.commit()


async def get_user_role(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT role FROM authorized_users WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def set_user_role(user_id: int, role: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE authorized_users SET role = ? WHERE user_id = ?",
            (role, user_id),
        )
        await conn.commit()
```

- [ ] **Step 4: функции инвайтов**

```python
async def create_invite(created_by: int) -> str:
    token = secrets.token_urlsafe(12)
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO invites (token, created_by, created_at) VALUES (?, ?, ?)",
            (created_by, ...),  # см. код в реализации: (token, created_by, iso-дата)
        )
        await conn.commit()
    return token


async def use_invite(token: str, user_id: int) -> bool:
    """Атомарно: помечает токен использованным и авторизует юзера."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """UPDATE invites SET used_by = ?, used_at = ?
               WHERE token = ? AND used_by IS NULL""",
            (user_id, datetime.now().isoformat(), token),
        )
        await conn.commit()
        if cur.rowcount == 0:
            return False
    await authorize_user(user_id)
    return True


async def list_invites() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM invites ORDER BY created_at DESC")
        return [dict(r) for r in await cur.fetchall()]
```

### Task 2: handlers.py — вход по инвайту и подъём до админа

**Files:**
- Modify: `handlers.py`

- [ ] **Step 1: импорты + константы ответов**

В импорт из db добавить `set_user_role, use_invite`. После `_agent`-блока:

```python
WELCOME_AFTER_INVITE = (
    "Доступ открыт! Я слежу за свободными местами в маршрутках Беларуси.\n"
    "Напиши, что нужно — например: «следи за Минск → Барановичи завтра утром»."
)
ADMIN_GREETING = "Ты админ. Напиши «дай инвайт», чтобы пригласить человека."
INVITE_REJECT = ("Ссылка не работает или уже использована. "
                 "Попроси новую у того, кто тебя пригласил.")
```

- [ ] **Step 2: чистая функция логики неавторизованного входа**

```python
async def handle_unauthorized_message(user_id: int, text: str) -> tuple[str, bool] | None:
    """Возвращает (ответ, авторизован_ли) или None — стандартный отказ.
    Подбор инвайт-токена НЕ инкрементит счётчик бана, подбор /auth — да."""
    text = text.strip()
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        token = parts[1].strip() if len(parts) > 1 else ""
        if not token:
            return None
        if await use_invite(token, user_id):
            return (WELCOME_AFTER_INVITE, True)
        return (INVITE_REJECT, False)
    if text == AUTH_CODE:
        await authorize_user(user_id, role="admin")
        return (ADMIN_GREETING, True)
    if text.startswith("/auth"):
        parts = text.split(maxsplit=1)
        code = parts[1].strip() if len(parts) > 1 else ""
        if not code:
            return ("Использование: /auth <код>", False)
        if code == AUTH_CODE:
            await authorize_user(user_id, role="admin")
            return (ADMIN_GREETING, True)
        failed = await increment_failed_attempts(user_id)
        remaining = MAX_AUTH_ATTEMPTS - failed
        if remaining <= 0:
            return ("Неверный код. Вы заблокированы.", False)
        return (f"Неверный код. Осталось попыток: {remaining}", False)
    return None
```

- [ ] **Step 3: middleware использует функцию (заменить старый /auth-блок)**

```python
        if isinstance(event, Message) and event.text:
            result = await handle_unauthorized_message(user.id, event.text)
            if result is not None:
                reply, authorized = result
                if authorized:
                    state: FSMContext | None = data.get("state")
                    if state is not None:
                        await state.clear()
                await event.answer(reply)
                return None
```

- [ ] **Step 4: авторизованные — код текстом до LLM + /auth**

В начало `on_text_fallback` (после проверки state, до команды-проверки):

```python
    if message.text and message.text.strip() == AUTH_CODE:
        await set_user_role(message.from_user.id, "admin")
        await message.answer(ADMIN_GREETING)
        return
```

Новый хендлер (рядом с cmd_start):

```python
@router.message(Command("auth"))
async def cmd_auth(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    if code == AUTH_CODE:
        await set_user_role(message.from_user.id, "admin")
        await message.answer(ADMIN_GREETING)
    else:
        await message.answer("Неверный код.")
```

### Task 3: LLM-тулзы create_invite / list_invites

**Files:**
- Modify: `llm/tools.py`, `llm/agent.py`, `bot.py`

- [ ] **Step 1: tools.py — ToolContext + схемы + хендлеры + фильтр**

ToolContext:

```python
@dataclass
class ToolContext:
    user_id: int
    schedule_self_callback: Callable[[int, float, str], Awaitable[int]] | None = None
    role: str = "user"
    bot_username: str | None = None
```

После TOOL_SCHEMAS:

```python
ADMIN_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_invite",
            "description": (
                "Создать одноразовую инвайт-ссылку для нового пользователя. "
                "Только для админа."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_invites",
            "description": (
                "Показать все инвайты и их статус (ожидает / кем использован). "
                "Только для админа."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def build_tools_for_role(role: str | None) -> list[dict[str, Any]]:
    if role == "admin":
        return TOOL_SCHEMAS + ADMIN_TOOL_SCHEMAS
    return TOOL_SCHEMAS
```

Хендлеры:

```python
async def _tool_create_invite(args: dict, ctx: ToolContext) -> str:
    if ctx.role != "admin":
        return _err("create_invite доступен только админу")
    token = await db_module.create_invite(ctx.user_id)
    link = (f"https://t.me/{ctx.bot_username}?start={token}"
            if ctx.bot_username else None)
    return json.dumps({"token": token, "link": link}, ensure_ascii=False)


async def _tool_list_invites(args: dict, ctx: ToolContext) -> str:
    if ctx.role != "admin":
        return _err("list_invites доступен только админу")
    return json.dumps({"invites": await db_module.list_invites()},
                      ensure_ascii=False)
```

В `_HANDLERS` добавить обе записи.

- [ ] **Step 2: agent.py — роль и username в контексте**

Конструктор: параметр `bot_username: str | None = None` → `self._bot_username`.
В `_drive_turn` в начале (до цикла): `role = await db_module.get_user_role(user_id) or "user"`.
Вызов: `tools=build_tools_for_role(role)` (импорт из llm.tools).
ctx: `ToolContext(user_id=user_id, schedule_self_callback=..., role=role, bot_username=self._bot_username)`.
`_TOOL_THINKING_LABELS` += `"create_invite": "Создаю инвайт…", "list_invites": "Смотрю инвайты…"`.

- [ ] **Step 3: bot.py — username**

```python
    me = await bot.get_me()
    agent = LLMAgent(bot=bot, client=llm_client, bot_username=me.username)
```

### Task 4: Тесты

**Files:**
- Create: `tests/test_invite_auth.py`

- [ ] **Step 1: написать тесты**

```python
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


async def test_unauthorized_wrong_auth_code_increments(tmp_db, monkeypatch):
    monkeypatch.setattr("handlers.AUTH_CODE", "s3cret")
    reply, authorized = await handle_unauthorized_message(50, "/auth wrong")
    assert authorized is False
    assert await db_module.get_failed_attempts(50) == 1


async def test_unauthorized_other_text_returns_none(tmp_db):
    assert await handle_unauthorized_message(50, "привет") is None
    assert await handle_unauthorized_message(50, "/start") is None


async def test_create_invite_tool_requires_admin(tmp_db):
    ctx = ToolContext(user_id=1, role="user")
    out = json.loads(await dispatch_tool("create_invite", {}, ctx))
    assert "error" in out


async def test_create_invite_tool_admin_returns_link(tmp_db):
    ctx = ToolContext(user_id=1, role="admin", bot_username="marik_bot")
    out = json.loads(await dispatch_tool("create_invite", {}, ctx))
    assert out["token"]
    assert out["link"] == f"https://t.me/marik_bot?start={out['token']}"
    listed = json.loads(await dispatch_tool("list_invites", {}, ctx))
    assert len(listed["invites"]) == 1
    assert listed["invites"][0]["used_by"] is None


async def test_build_tools_for_role():
    admin_names = {t["function"]["name"] for t in build_tools_for_role("admin")}
    user_names = {t["function"]["name"] for t in build_tools_for_role("user")}
    assert {"create_invite", "list_invites"} <= admin_names
    assert {"create_invite", "list_invites"}.isdisjoint(user_names)
    assert "create_watch" in user_names
```

- [ ] **Step 2: Прогнать**

Run: `python -m pytest tests/ -q`
Expected: все зелёные (в т.ч. старые тесты middleware-потока, если есть).

- [ ] **Step 3: Commit**

```bash
git add db.py handlers.py llm/tools.py llm/agent.py bot.py tests/test_invite_auth.py docs/superpowers/plans/2026-06-12-invite-auth.md
git commit -m "feat: one-time invite links, user/admin roles, admin LLM tools"
```
