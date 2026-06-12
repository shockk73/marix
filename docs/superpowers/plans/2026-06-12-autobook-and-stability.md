# Autobook + Pipeline Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Автобронирование на tickets.baranovichi-express.by (режимы off/confirm/auto per-watch) + цели-группы слежек, приоритетные слоты с перебронированием, управление бронями, системные подтверждения «Да/Нет» для state-changing LLM-действий, плюс все фиксы спеки стабильности.

**Architecture:** Три коммита. **A (стабильность):** уведомление до `update_notified_trips`, `send_with_retry` (flood control), изоляция `dispatch_tool`, алерт+backoff по `consecutive_errors`, WAL, done-callback таски; `_poll_loop` рефакторится в тестируемый `_poll_once`. **B (бронь-ядро):** таблицы `site_credentials`/`bookings`, колонки `autobook`/`goal_id`/`pref_time_from`/`pref_time_to` в watches, клиент сайта `providers/baranovichi_session.py` (login по маске телефона, book по времени, cancel), синглтон Booker с кэшем куки и одним перелогином, creds-тулзы с затиранием пароля. **C (поток):** автобронь в `_poll_once` (auto/confirm/rebook-offer), цели (успех брони останавливает группу), авто-деактивация истёкших слежек, кнопки `bk:` в handlers, тулзы list_bookings/cancel_booking/book_trip_now, системные подтверждения в агенте (intercept до dispatch), промпт/state, bump session version.

**Tech Stack:** httpx+respx, aiogram 3, aiosqlite, pytest.

**Specs:** `2026-06-12-baranovichi-autobook-design.md`, `2026-06-12-pipeline-stability-design.md` + 5 доп. требований пользователя (цели, истечение, управление бронями, приоритетные слоты, подтверждения).

---

## Commit A — стабильность

### Task A1: db.py WAL

В `init_db` первой строкой после connect:

```python
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
```

### Task A2: scheduler.py — порядок уведомлений, retry, health, done-callback

- [ ] Импорт `TelegramRetryAfter` из aiogram.exceptions. Хелпер:

```python
async def send_with_retry(bot: Any, chat_id: int, text: str, **kwargs) -> Any:
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except TelegramRetryAfter as e:
        await asyncio.sleep(min(e.retry_after, 30))
        return await bot.send_message(chat_id, text, **kwargs)
```

- [ ] `_poll_loop` → тонкий цикл вокруг тестируемого `_poll_once`; состояние в dict:

```python
ALERT_THRESHOLD = 10

async def _poll_loop(watch: dict) -> None:
    state = {"notified": set(json.loads(watch["notified_trips"])),
             "consecutive_errors": 0}
    async with httpx.AsyncClient(...) as client:
        while True:
            try:
                await _poll_once(watch, client, state)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                await _handle_poll_error(watch, state, exc)
            sleep_for = watch["interval_sec"] * _backoff_multiplier(state["consecutive_errors"])
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break

def _backoff_multiplier(consecutive_errors: int) -> int:
    if consecutive_errors == 0:
        return 1
    return min(2 ** (consecutive_errors // 5), 8)
```

`_poll_once`: mark_started → get_trips → filter → compute_newly → mark_success → если было ≥10 ошибок: «✅ Слежка #N снова работает», сброс счётчика → `if newly: СНАЧАЛА _send_notification, ПОТОМ update_notified_trips`. `_handle_poll_error`: mark_error, счётчик++, на ==10 один алерт «⚠️ Слежка #N спотыкается…».

- [ ] `_send_notification` использует `send_with_retry`.
- [ ] `start_watch`: `task.add_done_callback(_on_task_done(watch))` — не-Cancelled исключение → `logger.critical` + create_task(уведомить юзера).

### Task A3: agent.py — изоляция dispatch_tool

```python
                else:
                    try:
                        result = await dispatch_tool(name, args, ctx)
                    except Exception as e:
                        logger.exception("Tool %s crashed: %s", name, e)
                        result = json.dumps(
                            {"error": f"Tool crashed: {type(e).__name__}: {e}"},
                            ensure_ascii=False)
                    await db_module.insert_chat_message(...)
```

### Task A4: тесты (tests/test_stability.py)

- упавшая отправка → notified НЕ обновлён; второй тик шлёт снова (бот: 1-й raise, 2-й ок) — через `_poll_once` с замоканым провайдером.
- `TelegramRetryAfter` → sleep+повтор (monkeypatch asyncio.sleep).
- dispatch_tool кидает → история содержит tool-error, модель отвечает, нет висячего call.
- 10 ошибок → ровно один алерт; восстановление → одно «снова работает».
- `_backoff_multiplier`: 0→1, 4→1, 5→2, 10→4, 15→8, 40→8.

Commit: `fix: at-least-once notifications, tool isolation, watch health alerts/backoff, WAL`

## Commit B — клиент сайта и креды

### Task B1: db.py — схемы

```sql
CREATE TABLE IF NOT EXISTS site_credentials (
    user_id    INTEGER PRIMARY KEY,
    site       TEXT NOT NULL DEFAULT 'baranovichi_express',
    phone      TEXT NOT NULL,
    password   TEXT NOT NULL,
    verified_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    site        TEXT NOT NULL DEFAULT 'baranovichi_express',
    ticket_id   TEXT,
    trip_id     TEXT,
    date        TEXT NOT NULL,
    direction   TEXT NOT NULL,
    departure_time TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    watch_id    INTEGER,
    goal_id     TEXT,
    created_at  TEXT NOT NULL,
    canceled_at TEXT
);
```

`watches` миграции: `autobook TEXT NOT NULL DEFAULT 'off'`, `goal_id TEXT`,
`pref_time_from TEXT`, `pref_time_to TEXT` (через `_ensure_column`).

Функции: `save_site_credentials(user_id, phone, password)` (upsert + verified_at=now),
`get_site_credentials(user_id)`, `delete_site_credentials(user_id) -> bool`,
`scrub_chat_secret(user_id, secret)` (REPLACE в content И tool_calls),
`create_booking(...) -> id`, `get_user_bookings(user_id, active_only=True)`,
`get_booking(booking_id)`, `get_active_goal_booking(user_id, goal_id)`,
`mark_booking_canceled(booking_id)`, `stop_goal_watches(goal_id) -> list[int]`
(active=0 WHERE goal_id, вернуть ids), `set_watch_autobook(watch_id, mode)`.
`create_watch` + параметры `autobook='off', goal_id=None, pref_time_from=None, pref_time_to=None`.

### Task B2: providers/baranovichi_session.py

`normalize_phone(raw)` (+375XXXXXXXXX/80…/9 цифр → `+375 (29) 177-62-96`, мусор → ValueError);
исключения `InvalidCredentials`, `SessionExpired`; `BookingResult(status: booked|gone, ticket_id, trip_id)`;
`async login(client, phone, password)` (GET /login → `_token`+`partner_id`, POST,
успех = 30x на /user/account, иначе InvalidCredentials);
`async book_trip(client, date, direction, departure_time)`:
поиск (как provider, но ищем `href` с `/confirm` в карточке с нужным временем;
нет «Выйти» на странице → SessionExpired; карточка без confirm-ссылки или не нашлась → gone)
→ GET confirm (редирект на /login → SessionExpired) → парс `<form action="...store...">`:
все input name/value + первые option селектов pickup/destination → POST →
GET /user/tickets/active → искать `cancel/{ticket_id}?route_trip_id={trip_id}` нашего trip_id;
нет → gone. `async cancel_ticket(client, ticket_id, trip_id) -> bool`
(GET active → `_token`, POST cancel).
`class BaranovichiBooker`: кэш `httpx.AsyncClient` per user (куки), `book(user_id, ...)` /
`cancel(user_id, ...)`: достаёт креды из БД (нет → InvalidCredentials), логин при первом
обращении, при SessionExpired — один relogin+retry. Синглтон `BOOKER`.

### Task B3: creds-тулзы (llm/tools.py)

`save_baranovichi_credentials(phone, password)`: normalize → пробный login fresh-клиентом →
save_site_credentials → **scrub_chat_secret(user_id, password)** → `{connected: true, phone}`.
ValueError телефона / InvalidCredentials → _err дружелюбный.
`get_credentials_status()` → `{connected, phone_masked}` (`+375…96`: первые 4 + … + последние 2).
`delete_credentials()` → `{deleted: bool}`.
Все три — в TOOL_SCHEMAS (не админские). Labels в агенте.

### Task B4: тесты B (tests/test_baranovichi_session.py)

normalize_phone варианты; login успех/провал (respx HTML-фикстуры с _token);
book_trip: booked (полный мок-флоу search→confirm→store→active) / gone (нет времени;
нет билета после store) / SessionExpired→relogin→booked (Booker);
cancel_ticket; save_credentials тулза: успех+скраб пароля из chat_messages
(insert user message с паролем, после тулзы — `***`), провал кредов.

Commit: `feat: baranovichi-express booking client, per-user credentials with history scrubbing`

## Commit C — поток автоброни

### Task C1: scheduler.py — autobook + цели + истечение

- `watch_expired(watch, now) -> bool`: `now > date+time_to` (MSK).
- В `_poll_once` первым делом: истекла → `stop_watch` в БД, cancel task, msg
  «⏳ Слежка #N истекла (окно прошло) — остановил.», return.
- После compute_newly: если `provider == baranovichi_express` и `autobook != off`
  и newly → `_handle_autobook(watch, newly)` (вернул True → подавить обычное
  уведомление); потом (если не подавлено) `_send_notification`; потом
  `update_notified_trips`.
- `_handle_autobook`:
  - `booking = get_active_goal_booking(user_id, goal_id)`;
  - бронь есть и (pref нет или бронь в pref) → стоп себя (хвост гонки), True;
  - бронь есть вне pref → кандидаты: newly в pref-окне; пусто → True (молчим);
    иначе msg «уже забронировано HH:MM, появился слот лучше» + кнопка
    «🔁 Перебронировать на HH:MM» `bk:r:{watch_id}:{HH:MM}` (первые ≤4), True;
  - брони нет, `auto`: кандидат = ранний в pref (если pref) иначе ранний;
    `BOOKER.book` → booked: `create_booking`, бронь в pref/без pref →
    `stop_goal_watches`+cancel; вне pref → группу стопаем кроме себя (остаёмся
    для апгрейда); msg «✅ Забронировал HH:MM …» (стоп в БД ДО сообщения), True.
    gone → True (молчим, место ушло). InvalidCredentials → msg «креды не
    работают, автобронь выключена», `set_watch_autobook(off)`, False (обычное
    уведомление уйдёт). Прочие ошибки → log + False.
  - брони нет, `confirm`: уведомление с кнопками «🎫 Забронировать HH:MM»
    (`bk:b:{watch_id}:{HH:MM}`, ≤4 ранних, pref-кандидаты первыми), True.

### Task C2: booking_flow.py — общая логика кнопок и тулзов

```python
async def execute_booking(user_id, watch: dict | None, date, direction,
                          departure_time, rebook_from: dict | None) -> str
```
бронирует, пишет bookings, при rebook отменяет старую (сайт+БД) ПОСЛЕ успеха новой,
стопает группу (`goal_id` из watch), возвращает текст для юзера. Все ветки ошибок —
человеческие тексты («Не успели — место уже заняли…», «Креды не работают…»).
Используется хендлером кнопок и тулзой book_trip_now.

### Task C3: handlers.py — callback `bk:`

`@router.callback_query(F.data.startswith("bk:"))`: парс `bk:{kind}:{watch_id}:{HH:MM}`;
load watch (нет/неактивна и kind=b → «слежка уже остановлена»); идемпотентность:
активная бронь цели на это же время → «Уже забронировано ✅»; снять клавиатуру;
`execute_booking(...)`; ответ текстом.

### Task C4: llm/tools.py + agent.py — тулзы броней и подтверждения

- `create_watch`: новые параметры `autobook` (enum, default off; только при
  `baranovichi_express` в providers; confirm/auto требуют кредов),
  `pref_time_from/pref_time_to` (валидное HH:MM, внутри окна); один `goal_id =
  uuid4().hex[:12]` на все созданные в вызове watches; autobook ставится только
  watch'у барановичей, остальным off.
- `list_bookings` → активные брони юзера.
- `cancel_booking(booking_id)` → BOOKER.cancel + mark_booking_canceled.
- `book_trip_now(date, direction, departure_time)` → execute_booking без watch.
- **Подтверждения** (в `llm/agent.py`): `CONFIRM_REQUIRED = {"stop_watch",
  "stop_all_watches", "cancel_booking", "book_trip_now", "delete_credentials"}`.
  В цикле tool_calls: имя в списке → НЕ выполнять; `_handle_confirmation`:
  человекочитаемое описание действия + кнопки «✅ Да» (`aic:yes`) / «❌ Нет»
  (`aic:no`), `set_pending_tool_call(tool_name=имя, options_json={"kind":
  "confirm", "name": имя, "args": args})`, break (как ask_user).
  `resolve_confirmation(user_id, approved)`: pending kind=confirm → yes:
  dispatch_tool с сохранёнными args (с изоляцией ошибок) → tool result; no:
  result `{"canceled": true, "reason": "user declined"}`; затем `_drive_turn`.
  Хендлер callback `aic:` в handlers.py. `_auto_cancel_pending` уже корректно
  гасит подтверждение при новом сообщении.
  В `on_ai_callback` (ai:) pending с kind=confirm не трогаем (это не его формат —
  options_json теперь dict → защититься isinstance).
- `_TOOL_THINKING_LABELS` для новых тулзов.

### Task C5: prompt.py + state

- user_state += `credentials` (`{connected, phone_masked}`) и `bookings`
  (активные: id, дата, время, направление). Рендер в системный промпт.
- Правила: автобронь (режимы off/confirm/auto; предлагать подключить аккаунт
  после слежки на барановичи без кредов — один раз, не каждое сообщение);
  pref-окно («приоритетно 14:00–15:00» → pref_time_from/to; система сама
  предложит перебронирование); подтверждения — «state-changing инструменты
  система сама подтверждает у пользователя кнопками, НЕ спрашивай дополнительно»;
  управление бронями (list_bookings/cancel_booking).
- `LLM_SESSION_VERSION = "2026-06-12-autobook-v1"`.

### Task C6: тесты C (tests/test_autobook_flow.py)

- expiry: `watch_expired` границы; `_poll_once` с истёкшей слежкой → stop+msg.
- auto: newly → бронь → booking row, msg «Забронировал», группа остановлена
  (оба watch одной цели inactive), notified-обновление не падает.
- auto+pref: бронь вне pref (только слот 12:30 при pref 14–15) → группа кроме
  себя остановлена; следующий тик с newly 14:30 → rebook-offer с кнопкой.
- auto gone → молчим, слежка живёт. InvalidCredentials → autobook=off + msg.
- confirm: уведомление содержит кнопки `bk:b:`.
- create_watch tool: autobook без кредов → error; с кредами → watches с одним
  goal_id; pref вне окна → error; autobook без барановичей → error.
- подтверждение: stop_watch tool call → кнопки, watch ЖИВ; resolve(yes) →
  остановлен + tool result; resolve(no) → жив, result canceled.
- cancel_booking через подтверждение; book_trip_now confirm-флоу.
- идемпотентность кнопки: повторная бронь того же времени → «Уже забронировано».

Commit: `feat: autobooking flow - auto/confirm modes, goals, preferred-slot rebooking, expiry, confirmations`
