# LLM-first UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** LLM — главный интерфейс: tool `show_screen` (модель сама собирает экраны с сетками кнопок), постоянная reply-клавиатура с каннед-фразами, онбординг агентом после инвайта, срез состояния юзера в системпромпт каждый ход.

**Architecture:** `show_screen` обрабатывается агентом (как `ask_user`) через существующий механизм `pending_tool_calls`; `options_json` остаётся плоским списком values (callback_data `ai:<idx>` — `on_ai_callback` не меняется). Reply-кнопки шлют готовые фразы в обычный LLM-флоу. Онбординг: после `use_invite` middleware шлёт приветствие с клавиатурой и дергает `agent.run_turn` с триггер-текстом. Состояние юзера (роль, слежки со статусами, callbacks) собирается в `_drive_turn` и передаётся в `build_system_prompt`.

**Tech Stack:** aiogram 3 (ReplyKeyboardMarkup), pytest.

**Spec:** `docs/superpowers/specs/2026-06-12-llm-first-ux-design.md`. Без TDD по требованию пользователя.

---

### Task 1: Схема show_screen в llm/tools.py

**Files:**
- Modify: `llm/tools.py` (TOOL_SCHEMAS, docstring dispatch_tool)

- [ ] **Step 1: В TOOL_SCHEMAS после ask_user добавить**

```python
    {
        "type": "function",
        "function": {
            "name": "show_screen",
            "description": (
                "Показать экран: markdown-текст + сетка inline-кнопок (до 8 рядов "
                "по до 4 кнопки). Клик по кнопке вернёт её value как ответ "
                "пользователя. Используй для выбора из конечного набора: даты, "
                "окна времени, интервалы, подтверждения, карточки слежек с "
                "действиями. Для свободного ввода НЕ используй."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string",
                             "description": "Markdown-текст экрана"},
                    "buttons": {
                        "type": "array",
                        "maxItems": 8,
                        "description": "Ряды кнопок: до 8 рядов, в ряду до 4 кнопок",
                        "items": {
                            "type": "array",
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "value": {"type": "string"},
                                },
                                "required": ["label", "value"],
                            },
                        },
                    },
                },
                "required": ["text", "buttons"],
            },
        },
    },
```

`dispatch_tool` docstring: «ask_user и show_screen НЕ обрабатываются здесь — это делает agent.»

### Task 2: agent.py — _handle_screen

**Files:**
- Modify: `llm/agent.py`

- [ ] **Step 1: _TOOL_THINKING_LABELS += `"show_screen": "Собираю экран…"`**

- [ ] **Step 2: В цикле tool_calls ветка**

```python
                if name == "ask_user":
                    await self._handle_ask_user(user_id, tc_id, args)
                    ask_user_pending = True
                    break
                elif name == "show_screen":
                    pending = await self._handle_screen(user_id, tc_id, args)
                    if pending:
                        ask_user_pending = True
                        break
                else:
                    ...
```

`_handle_screen` возвращает bool: False — невалидные аргументы, в историю ушёл tool-error, цикл продолжает (модель переделает на следующем ходе).

- [ ] **Step 3: Метод _handle_screen (после _handle_ask_user)**

```python
    async def _handle_screen(self, user_id: int, tool_call_id: str, args: dict) -> bool:
        text = str(args.get("text") or "").strip()
        rows_in = args.get("buttons")
        error = None
        if not text:
            error = "show_screen: text must be a non-empty string"
        elif not isinstance(rows_in, list) or not rows_in:
            error = "show_screen: buttons must be a non-empty array of rows"
        elif len(rows_in) > 8:
            error = "show_screen: max 8 rows"
        else:
            for row in rows_in:
                if not isinstance(row, list) or not row:
                    error = "show_screen: each row must be a non-empty array"
                    break
                if len(row) > 4:
                    error = "show_screen: max 4 buttons per row"
                    break
                for btn in row:
                    if (not isinstance(btn, dict)
                            or not str(btn.get("label") or "").strip()
                            or not str(btn.get("value") or "").strip()):
                        error = "show_screen: each button needs label and value"
                        break
                if error:
                    break
        if error:
            await db_module.insert_chat_message(
                user_id, "tool", content=json.dumps({"error": error}),
                tool_call_id=tool_call_id,
            )
            return False

        flat_values: list[str] = []
        kb_rows: list[list[InlineKeyboardButton]] = []
        for row in rows_in:
            kb_row = []
            for btn in row:
                kb_row.append(InlineKeyboardButton(
                    text=str(btn["label"])[:64],
                    callback_data=f"ai:{len(flat_values)}",
                ))
                flat_values.append(str(btn["value"]))
            kb_rows.append(kb_row)
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        sent = await self._send_markdown_message(user_id, text, reply_markup=kb)
        await db_module.set_pending_tool_call(
            user_id=user_id, tool_call_id=tool_call_id,
            tool_name="show_screen",
            options_json=json.dumps(flat_values, ensure_ascii=False),
            message_id=sent.message_id,
        )
        return True
```

### Task 3: handlers.py — reply-клавиатура и онбординг

**Files:**
- Modify: `handlers.py`, `tests/test_invite_auth.py` (сигнатура→триple)

- [ ] **Step 1: Импорты aiogram types += `KeyboardButton, ReplyKeyboardMarkup`; из db += `get_user_role`**

- [ ] **Step 2: Константы и клавиатура**

```python
ONBOARDING_TRIGGER = "[новый пользователь вошёл по инвайту — поздоровайся и покажи стартовый экран]"

BTN_WATCH = "🔍 Следить за местами"
BTN_LIST = "📋 Мои слежки"
BTN_HELP = "❓ Что ты умеешь"
BTN_ADMIN = "🛠 Админка"


def main_reply_kb(role: str | None) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_WATCH), KeyboardButton(text=BTN_LIST)],
        [KeyboardButton(text=BTN_HELP)],
    ]
    if role == "admin":
        rows[1].append(KeyboardButton(text=BTN_ADMIN))
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True,
                               is_persistent=True)
```

- [ ] **Step 3: `handle_unauthorized_message` возвращает триple `(reply, authorized, invited)`** — все return-точки дополнить третьим элементом (`True` только в ветке успешного инвайта). Обновить docstring и тесты test_invite_auth.py (распаковка трёх значений).

- [ ] **Step 4: Middleware: при authorized — клавиатура по роли + онбординг**

```python
            if result is not None:
                reply, authorized, invited = result
                if not authorized:
                    await event.answer(reply)
                    return None
                state: FSMContext | None = data.get("state")
                if state is not None:
                    await state.clear()
                role = await get_user_role(user.id)
                await event.answer(reply, reply_markup=main_reply_kb(role))
                if invited and _agent is not None:
                    await _agent.run_turn(
                        user_id=user.id,
                        text=ONBOARDING_TRIGGER,
                        user_name=_user_display_name(user),
                    )
                return None
```

- [ ] **Step 5: `/start` показывает клавиатуру**

```python
@router.message(Command("start"))
async def cmd_start(message: Message):
    role = await get_user_role(message.from_user.id)
    await message.answer(
        "Привет! Слежу за местами в маршрутках. Просто напиши, что нужно.\n\n"
        "Команды-фоллбек: /watch, /list, /stop <id>",
        reply_markup=main_reply_kb(role),
    )
```

### Task 4: prompt.py — state-awareness + правила UX

**Files:**
- Modify: `llm/prompt.py`, `llm/agent.py`

- [ ] **Step 1: build_system_prompt(..., user_state: dict | None = None)**

После блока имени вставить:

```python
    if user_state:
        parts.append("")
        parts.append(f"Роль пользователя: {user_state.get('role', 'user')}.")
        watches = user_state.get("watches") or []
        if watches:
            parts.append("Активные слежки пользователя:")
            for w in watches:
                status = w.get("execution") or {}
                if status.get("consecutive_errors"):
                    s = (f"ОШИБКИ x{status['consecutive_errors']}: "
                         f"{(status.get('last_error') or '')[:120]}")
                else:
                    s = status.get("status", "not_started")
                parts.append(
                    f"  - #{w['id']} {w['provider']} {w['direction']} {w['date']} "
                    f"{w['time_from']}–{w['time_to']} каждые {w['interval_sec']}с; "
                    f"статус: {s}"
                )
        else:
            parts.append("Активных слежек у пользователя нет.")
        callbacks = user_state.get("callbacks") or []
        if callbacks:
            parts.append(
                f"Отложенных self-callback-ов: {len(callbacks)}, "
                f"ближайший: {callbacks[0]['run_at_iso']}."
            )
```

- [ ] **Step 2: Правила UX в parts (после правила 10)**

```python
        "11. Тебе виден срез состояния пользователя выше (роль, слежки, статусы, "
        "callback-и) — это твоя память о том, где находится пользователь. Если у "
        "слежки ошибки подряд — упомяни это при любом обращении. После успешного "
        "действия подскажи логичный следующий шаг. Не повторяй подсказки каждое "
        "сообщение.",
        "12. show_screen строит экран с сеткой кнопок — используй для выбора из "
        "конечного набора: дата (ближайшие 7 дней), окно времени (🌅 Утро 05:00–12:00 / "
        "🌞 День 12:00–17:00 / 🌆 Вечер 17:00–23:00 / Весь день), интервал (1/2/5/10 мин), "
        "подтверждения, карточки слежек с кнопкой остановки (value: «останови слежку N»). "
        "Для свободного ввода экран не строй; пользователь всегда может ответить текстом. "
        "ask_user — для простых вопросов одним столбиком.",
        "13. Сценарий «создать слежку»: провайдер(ы) → направление → дата → окно "
        "времени → интервал → create_watch. Недостающее спрашивай экранами по одному шагу. "
        "Сценарий «мои слежки»: list_watches → краткие карточки + экран с кнопками остановки.",
        "14. Кнопки клавиатуры пользователя: «🔍 Следить за местами» — сценарий создания "
        "слежки; «📋 Мои слежки» — сценарий списка; «❓ Что ты умеешь» — краткий обзор "
        "возможностей + стартовый экран; «🛠 Админка» (только админ) — инвайты, отчёты. "
        "Сообщение «[новый пользователь вошёл по инвайту…]» — онбординг: поздоровайся, "
        "двумя фразами объясни, что умеешь, покажи стартовый экран show_screen.",
```

- [ ] **Step 3: bump `LLM_SESSION_VERSION = "2026-06-12-llm-first-ux-v1"`**

- [ ] **Step 4: agent.py собирает state в _drive_turn (вместо просто role)**

```python
        role = await db_module.get_user_role(user_id) or "user"
        ...
        # в начале каждой итерации цикла, перед build_system_prompt:
                user_state = await self._collect_user_state(user_id, role)
                messages = [{"role": "system",
                             "content": build_system_prompt(
                                 now=self._now(), user_name=stored_name,
                                 user_state=user_state,
                             )}]
```

Метод:

```python
    async def _collect_user_state(self, user_id: int, role: str) -> dict:
        watches = await db_module.get_user_watches(user_id)
        statuses = await db_module.get_watch_statuses([w["id"] for w in watches])
        callbacks = await db_module.get_user_agent_callbacks(user_id)
        return {
            "role": role,
            "watches": [{
                "id": w["id"], "provider": w["provider"],
                "direction": w["direction"], "date": w["date"],
                "time_from": w["time_from"], "time_to": w["time_to"],
                "interval_sec": w["interval_sec"],
                "execution": statuses.get(w["id"]) or {},
            } for w in watches],
            "callbacks": [{
                "id": cb["id"],
                "run_at_iso": datetime.fromtimestamp(
                    cb["run_at"], tz=_MSK).isoformat(),
            } for cb in callbacks],
        }
```

### Task 5: Тесты

**Files:**
- Create: `tests/test_llm_ux.py`
- Modify: `tests/test_invite_auth.py` (триple-распаковка)

- [ ] **Step 1: test_llm_ux.py** — show_screen рендер/лимиты/клик, клавиатура, prompt state, онбординг-триггер в prompt-правилах. Полный код в реализации задачи; ключевые случаи:

```python
# рендер: tool_call show_screen c 2 рядами → fake_bot.sent[-1] имеет
#   InlineKeyboardMarkup 2 ряда, callback_data ai:0..2, pending options_json
#   = плоский список values
# клик: continue_turn(selected_option=value) → tool message с value
# >8 рядов → в истории tool message с error, pending нет
# >4 кнопок в ряду → error
# main_reply_kb("user") → 3 кнопки, main_reply_kb("admin") → 4 (🛠 Админка)
# build_system_prompt(user_state={...слежка с consecutive_errors=3...})
#   содержит «ОШИБКИ x3» и роль
# build_system_prompt упоминает show_screen и онбординг-триггер
```

- [ ] **Step 2: Прогнать**

Run: `python -m pytest tests/ -q`
Expected: все зелёные (старые ask_user-тесты не тронуты).

- [ ] **Step 3: Commit**

```bash
git add llm/ handlers.py tests/ docs/superpowers/plans/2026-06-12-llm-first-ux.md
git commit -m "feat: LLM-first UX - show_screen tool, reply keyboard, onboarding, state-aware prompt"
```
