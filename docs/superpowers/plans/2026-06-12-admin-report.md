# Admin Sessions Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Админ просит «покажи отчёт по юзерам» → tool `generate_sessions_report` шлёт самодостаточный HTML-файл (все юзеры, последние 50 сообщений) документом в TG.

**Architecture:** `db.get_authorized_users()`; новый модуль `report.py`: `collect_sessions_data()` (async, тянет данные) + `build_sessions_report(users, now)` (чистая, HTML f-string + html.escape, без зависимостей). Тулза admin-only (фильтр+проверка), шлёт `BufferedInputFile` через `ctx.bot` (новое поле ToolContext).

**Tech Stack:** stdlib html/json, aiogram BufferedInputFile, pytest.

**Spec:** `docs/superpowers/specs/2026-06-12-admin-report-design.md`. Без TDD по требованию пользователя.

---

### Task 1: db.get_authorized_users

**Files:**
- Modify: `db.py` (после list_invites)

- [ ] **Step 1:**

```python
async def get_authorized_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT user_id, role, authorized_at FROM authorized_users")
        return [dict(r) for r in await cur.fetchall()]
```

### Task 2: report.py

**Files:**
- Create: `report.py`

- [ ] **Step 1: Полный модуль**

```python
import html
import json
from datetime import datetime, timedelta, timezone

import db as db_module
from config import LLM_HISTORY_SIZE
from llm.prompt import LLM_SESSION_VERSION

_MSK = timezone(timedelta(hours=3))

_CSS = """ (тёмная тема: фон #1117, пузыри user/assistant, details/pre моноширинно) """


async def collect_sessions_data() -> list[dict]:
    """Все авторизованные юзеры + их слежки/имя/последние 50 сообщений.
    Сортировка: последняя активность сверху."""
    users = await db_module.get_authorized_users()
    out = []
    for u in users:
        uid = u["user_id"]
        out.append({
            **u,
            "name": await db_module.get_user_name(uid),
            "watches": await db_module.get_user_watches(uid),
            "messages": await db_module.get_recent_chat_messages(
                uid, LLM_HISTORY_SIZE),
        })
    out.sort(key=lambda x: x["messages"][-1]["created_at"] if x["messages"] else 0.0,
             reverse=True)
    return out


def _fmt_ts(ts: float | None) -> str: ...  # REAL → "%Y-%m-%d %H:%M:%S" MSK


def _render_message(m: dict, tool_names: dict[str, str]) -> str:
    # user/assistant content → пузырь с таймстампом
    # assistant.tool_calls → <details><summary>🔧 name</summary><pre>args</pre></details>
    # tool → <details><summary>📋 name → результат</summary><pre>content</pre></details>


def build_sessions_report(users: list[dict], now: datetime) -> str:
    # шапка: дата генерации, число юзеров, LLM_SESSION_VERSION
    # секция юзера: имя/«без имени», id, роль, дата авторизации, N слежек
    # слежки строками; затем сообщения через _render_message
    # юзер без сообщений → «нет сообщений»
    # всё через html.escape
```

### Task 3: тулза + ToolContext.bot

**Files:**
- Modify: `llm/tools.py`, `llm/agent.py`

- [ ] **Step 1: tools.py** — импорт `BufferedInputFile` из aiogram.types и `collect_sessions_data, build_sessions_report` из report; `ToolContext` += `bot: Any | None = None`; в `ADMIN_TOOL_SCHEMAS` схема `generate_sessions_report` (без параметров, описание «HTML-отчёт по всем пользователям…, только для админа»); хендлер:

```python
async def _tool_generate_sessions_report(args: dict, ctx: ToolContext) -> str:
    if ctx.role != "admin":
        return _err("generate_sessions_report доступен только админу")
    if ctx.bot is None:
        return _err("bot недоступен в этом контексте")
    users = await collect_sessions_data()
    now = datetime.now(timezone(timedelta(hours=3)))
    html_str = build_sessions_report(users, now=now)
    doc = BufferedInputFile(html_str.encode("utf-8"),
                            filename=f"sessions-{now.date().isoformat()}.html")
    await ctx.bot.send_document(ctx.user_id, doc)
    return json.dumps({"sent": True, "users": len(users)}, ensure_ascii=False)
```

регистрация в `_HANDLERS`.

- [ ] **Step 2: agent.py** — ctx += `bot=self._bot`; `_TOOL_THINKING_LABELS["generate_sessions_report"] = "Готовлю отчёт…"`.

### Task 4: Тесты

**Files:**
- Create: `tests/test_report.py`
- Modify: `tests/test_llm_agent.py` (FakeBot.send_document)

- [ ] **Step 1:** FakeBot += `documents: list`, `async def send_document(chat_id, document)` → append.

- [ ] **Step 2: test_report.py**: build: имя/пузыри/details/escape `<script>`; «нет сообщений»; collect сортирует по активности; тулза: не-админ → error, админ → send_document вызван, HTML внутри валиден (содержит имя юзера), ответ {"sent": true}.

- [ ] **Step 3:** `python -m pytest tests/ -q` → зелёные; commit.

```bash
git add db.py report.py llm/tools.py llm/agent.py tests/ docs/superpowers/plans/2026-06-12-admin-report.md
git commit -m "feat: admin HTML sessions report via generate_sessions_report tool"
```
