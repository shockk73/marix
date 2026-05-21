# Дизайн: LLM-агент с tool calling через OpenRouter

**Дата:** 2026-05-21
**Автор:** brainstorming session

## Цель

Добавить в Telegram-бота интеграцию с OpenRouter API и моделью, поддерживающей tool calling. Юзер пишет естественным языком («удали отслеживание 5», «следи за местами Могилёв→Минск на 24 мая с 11 до 23 каждые 2 минуты у atlasbus», «что у меня сейчас отслеживается?»), а LLM сама вызывает нужные функции бота.

Существующие slash-команды (`/watch`, `/list`, `/stop`, `/auth`, `/start`) остаются как есть. LLM — параллельный путь для свободного текста.

## Решения, принятые на brainstorming

| Решение | Значение |
|---|---|
| Триггер LLM | Любое не-командное сообщение, не попавшее в активную FSM-форму |
| Контекст | Последние N сообщений из БД, N = `LLM_HISTORY_SIZE` (дефолт 50) |
| Tools | CRUD watches, разовая проверка мест, помощь, `ask_user` для уточнений |
| Тематика | LLM отвечает только по теме бота. Оффтопик → отказ |
| Модель | `OPENROUTER_MODEL` строго из .env (без хардкод-дефолта) |
| Подтверждение | Через tool `ask_user(question, options[])` — inline-кнопки с вариантами |
| Хранение истории | SQLite, новая таблица `chat_messages` |
| Pending state | SQLite, новая таблица `pending_tool_calls` — переживает рестарт |
| Новое сообщение поверх pending | Отменяет висящий `ask_user`, LLM получает tool_result `{canceled: true}` |
| Поддержка фото | Явный флаг `LLM_VISION=true/false` в .env; без авто-детекта |
| Поддержка аудио | Явный флаг `LLM_AUDIO=true/false`. Если `true` — отправляем напрямую в основную LLM как multimodal. Если `false` — используем STT-модель `LLM_STT_MODEL` (отдельный chat-completion вызов в OpenRouter); если она тоже пуста — отказ |
| Concurrency | `asyncio.Lock` per user_id |
| Лимит витков LLM | `OPENROUTER_MAX_TURNS` (дефолт 5) — защита от циклов tool_call ↔ tool_result |

## Архитектура

Новый пакет `llm/` рядом с существующими модулями:

```
llm/
  __init__.py
  client.py      OpenRouter HTTP-клиент (httpx async), tool calling
  tools.py       schemas tools + dispatcher: name → async handler(args, ctx)
  history.py     CRUD истории сообщений и pending tool calls
  agent.py       orchestration: run_turn / continue_turn / cancel_pending
  prompt.py      системный промпт + динамическая подстановка даты/провайдеров
```

В `handlers.py` добавляется fallback-хендлер на `@router.message(F.text)` и `@router.message(F.photo)`, который регистрируется **последним**, чтобы FSM-хендлеры и команды имели приоритет.

Tool handlers получают `ctx` с `user_id` и `bot` — никогда не доверяют `user_id` из args LLM. Все DB-операции, ограничивающие по юзеру (`stop_watch`, `get_user_watches`), переиспользуют существующий код из `db.py`.

## Конфиг (.env)

| Переменная | Обязательная | Дефолт | Назначение |
|---|---|---|---|
| `OPENROUTER_API_KEY` | да | — | Ключ OpenRouter |
| `OPENROUTER_MODEL` | да | — | ID модели, напр. `deepseek/deepseek-v4-flash:free` |
| `OPENROUTER_BASE_URL` | нет | `https://openrouter.ai/api/v1` | Для проксирования/тестов |
| `OPENROUTER_MAX_TURNS` | нет | `5` | Макс. итераций tool-call цикла за один `run_turn` |
| `LLM_HISTORY_SIZE` | нет | `50` | Сколько последних `chat_messages` подгружать в контекст |
| `LLM_VISION` | нет | `false` | `true` если выбранная модель умеет vision |
| `LLM_AUDIO` | нет | `false` | `true` если основная модель умеет аудио (multimodal input audio) |
| `LLM_STT_MODEL` | нет | пусто | ID OpenRouter-модели для speech-to-text, напр. `mistralai/voxtral-mini-transcribe` или `google/chirp-3`. Используется когда `LLM_AUDIO=false` |

При старте `config.py` проверяет наличие обязательных. Отсутствие → бот падает (как сейчас с `BOT_TOKEN`).

## Tools

| Tool | Параметры | Поведение |
|---|---|---|
| `list_watches` | — | Возвращает JSON-массив активных watch'ей юзера (id, provider, direction, date, time_from, time_to, interval_sec) |
| `create_watch` | `providers: string[]`, `direction: "mg_mnsk"\|"mnsk_mg"`, `date: YYYY-MM-DD`, `time_from: HH:MM`, `time_to: HH:MM`, `interval_sec: int>=60` | Создаёт по одному watch на каждого провайдера, запускает в scheduler. Возвращает массив созданных id |
| `stop_watch` | `watch_id: int` | Останавливает watch (с проверкой принадлежности юзеру). Возвращает `{ok: bool}` |
| `stop_all_watches` | — | Останавливает все watch'и юзера. Возвращает количество |
| `check_trips_now` | `provider`, `direction`, `date`, `time_from`, `time_to` | Разовый вызов провайдера, без БД. Возвращает массив рейсов: `[{time, seats_left, ...}]` |
| `list_providers` | — | Возвращает массив `{key, display_name}` доступных провайдеров |
| `ask_user` | `question: string`, `options: string[]` (2–8) | Особый — см. flow ниже |

**Валидация:** все параметры проверяются на стороне handler'ов. Невалидные значения (несуществующий провайдер, плохая дата, interval<60) → handler возвращает `{"error": "..."}` в tool_result. LLM сама переспрашивает или поправляется. Лимит витков защищает от циклов.

## Системный промпт

Содержит:
- Роль: ассистент бота отслеживания мест в маршрутках Беларуси.
- Жёсткое правило: отвечать **только по теме** (маршрутки, отслеживания, провайдеры). Оффтопик — вежливый отказ.
- **Текущая дата + время в часовом поясе Europe/Minsk** (UTC+3 без DST) — для парсинга «завтра», «через неделю», «в субботу», «через 30 минут», «сейчас». В Docker процесс часто работает в UTC — поэтому время берётся явно через `datetime.now(ZoneInfo("Europe/Minsk"))`, а не системное.
- **Имя собеседника** (если известно) — берётся из Telegram (`first_name` или `username`) и сохраняется в таблицу `user_profiles` при каждом сообщении. LLM может обращаться по имени.
- Список доступных провайдеров с display_name.
- Список направлений (`mg_mnsk`, `mnsk_mg`).
- Инструкция: при неопределённости параметров вызывать `ask_user`, а не угадывать.

Подставляется динамически каждый `run_turn`.

## Data model

```sql
CREATE TABLE chat_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    role         TEXT NOT NULL,         -- 'user' | 'assistant' | 'tool' | 'system' (system не пишем, но колонка позволяет)
    content      TEXT,                  -- текст или JSON tool_result
    tool_calls   TEXT,                  -- JSON массив tool_calls (для assistant), иначе NULL
    tool_call_id TEXT,                  -- id вызова (для role=tool), иначе NULL
    created_at   REAL NOT NULL
);
CREATE INDEX idx_chat_user_time ON chat_messages(user_id, created_at);

CREATE TABLE pending_tool_calls (
    user_id      INTEGER PRIMARY KEY,
    tool_call_id TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    options_json TEXT NOT NULL,
    message_id   INTEGER NOT NULL,
    created_at   REAL NOT NULL
);

CREATE TABLE user_profiles (
    user_id    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    updated_at REAL NOT NULL
);
```

`PRIMARY KEY (user_id)` в `pending_tool_calls` гарантирует один pending на юзера — новый ask_user стирает старый.

## Чистка истории

После каждого успешного `run_turn`:

```sql
DELETE FROM chat_messages
WHERE user_id = ?
  AND id NOT IN (
    SELECT id FROM chat_messages
    WHERE user_id = ?
    ORDER BY id DESC
    LIMIT ?  -- LLM_HISTORY_SIZE * 2 (буфер на assistant/tool записи)
  );
```

## Flow

### Случай A — обычное текстовое сообщение, без pending

1. AuthMiddleware пропускает (юзер авторизован).
2. Не команда, не FSM-state активен → fallback хендлер.
3. `agent.run_turn(user_id, text)`:
   - Берёт `asyncio.Lock` per-user.
   - Пишет `chat_messages(role=user, content=text)`.
   - В цикле до `OPENROUTER_MAX_TURNS`:
     - Достаёт последние `LLM_HISTORY_SIZE` сообщений + system prompt.
     - Шлёт в OpenRouter с `tools=[...]`.
     - Получает ответ.
       - Если `tool_calls` пуст → пишет `assistant content` в БД, шлёт юзеру, выходит.
       - Если есть tool_calls → исполняет:
         - Для `ask_user` — особый путь (см. B), цикл прерывается.
         - Для остальных — handler, пишет `assistant tool_calls` + `tool result`, следующая итерация цикла.
   - При превышении лимита витков → юзеру: «Запутался, попробуй переформулировать.»

### Случай B — LLM вызвал `ask_user`

1. Бот шлёт юзеру `question` с inline-клавиатурой (по кнопке на option).
2. `callback_data` = `ai:{tool_call_id}:{option_index}`.
3. Запись в `pending_tool_calls(user_id, tool_call_id, options_json, message_id)`. ON CONFLICT REPLACE.
4. Запись в `chat_messages(role=assistant, tool_calls=[ask_user...])`. **Tool result пока не пишется.**
5. `run_turn` завершается, lock освобождается.

### Случай C — юзер тыкает кнопку

1. Callback хендлер парсит `tool_call_id` и `option_index`.
2. Берёт `asyncio.Lock` per-user.
3. Достаёт `pending_tool_calls` по `user_id`.
4. Проверяет совпадение `tool_call_id` (если не совпадает — устаревшая кнопка, edit_reply_markup=None + молчим).
5. Пишет `chat_messages(role=tool, tool_call_id=..., content=выбранный_label)`.
6. Удаляет pending. Снимает клавиатуру у сообщения.
7. Вызывает `agent.continue_turn(user_id)` — продолжает цикл с того места.

### Случай D — висит pending, юзер шлёт новый текст или фото

1. Fallback хендлер берёт lock, видит pending в БД.
2. Снимает клавиатуру у старого сообщения.
3. Пишет в историю tool_result: `{"canceled": true, "reason": "user sent new message"}` для висящего `tool_call_id`. Это важно — LLM должна видеть, что вопрос отменён юзером, а не остался без ответа.
4. Удаляет pending.
5. Дальше как Случай A.

### Случай E — рестарт бота с pending

Pending остаётся в БД. Клавиатура у старого Telegram-сообщения тоже на месте. Юзер тыкает → Случай C работает без изменений. Восстанавливать ничего не надо.

### Случай F — сообщение с фото

- `LLM_VISION=false` (дефолт): бот отвечает «Текущая модель не умеет читать фото. Опиши текстом или поменяй модель в настройках.» В LLM ничего не идёт, в историю не пишем.
- `LLM_VISION=true`:
  - Бот скачивает самый большой `PhotoSize` через `bot.download`.
  - Кодирует в base64 data URL: `data:image/jpeg;base64,...`.
  - Шлёт в LLM в формате OpenAI multimodal: `content: [{type: "text", text: caption_или_""}, {type: "image_url", image_url: {url: ...}}]`.
  - В `chat_messages.content` пишет плейсхолдер `[photo] {caption}` — без base64, чтобы БД не пухла. LLM в следующих витках разговора видит, что фото было, но не сами пиксели. Это приемлемо: для текущего витка пиксели передаются, дальше работает с текстом.

### Случай G — голосовое сообщение / аудио

Telegram присылает `voice` (OGG/Opus) или `audio` (MP3 и пр.). Самый частый случай — voice. Используем `message.voice` или `message.audio`, скачиваем bytes.

- `LLM_AUDIO=true`: основная модель умеет audio input напрямую.
  - Кодируем в base64, шлём как OpenAI multimodal: `content: [{type: "text", text: caption}, {type: "input_audio", input_audio: {data: base64, format: "ogg"|"mp3"}}]`.
  - В историю пишем плейсхолдер `[audio] {caption}`.
- `LLM_AUDIO=false` и `LLM_STT_MODEL` задан: транскрибируем отдельным вызовом OpenRouter — `chat/completions` с моделью `LLM_STT_MODEL` и аудио в content (тот же multimodal формат). Промпт STT-вызова — простая инструкция: «Транскрибируй это аудио в текст. Верни только текст без комментариев.» Полученный текст уходит в `run_turn` как обычное текстовое сообщение, в историю пишется `[voice] <транскрипция>`.
- `LLM_AUDIO=false` и `LLM_STT_MODEL` пуст: бот отвечает «Я не умею слушать голосовые. Напиши текстом или настрой LLM_STT_MODEL в .env.»

Конвертация форматов (OGG→WAV) не делается — отправляем как есть. Если STT-модель не поддерживает формат — её ошибка просочится как обычная HTTP-ошибка, юзер увидит сообщение об ошибке STT.

## Безопасность

- API-ключ только из env.
- Tool handlers берут `user_id` из контекста (`ctx.user_id`), а не из args LLM. LLM физически не может удалить чужой watch.
- `db.stop_watch(watch_id, user_id)` уже проверяет принадлежность — переиспользуем.
- При парсинге callback_data валидируем `tool_call_id` (alphanumeric + дефис, длина < 64). Защита от инъекций в callback.

## Ошибки

| Ситуация | Поведение |
|---|---|
| httpx exception / timeout | Один ретрай через 2 сек. При повторе — юзеру «Не получилось связаться с AI», в историю кладём `assistant content` с этим текстом |
| HTTP 429 / 5xx | Тот же путь, что timeout |
| HTTP 401/403 | Юзеру «AI временно недоступен», в логи WARNING |
| Невалидные tool args | Tool handler возвращает `{"error": "..."}` в tool_result. LLM поправится или переспросит |
| Лимит витков превышен | «Запутался, попробуй переформулировать» |
| LLM вернул tool_call с неизвестным именем | tool_result `{"error": "unknown tool"}` |
| Pending существует, но `tool_call_id` callback'а не совпадает | Тихо снимаем клавиатуру, не отвечаем |

## Тесты

- `tests/test_llm_tools.py` — каждый tool handler с моковыми DB/scheduler.
- `tests/test_llm_agent.py` — flow `run_turn` через мок OpenRouter-клиента (никаких реальных HTTP в CI). Кейсы: простой ответ без tools, цепочка из 2 tool_calls, ask_user + callback, отмена pending новым сообщением, лимит витков.
- `tests/test_llm_history.py` — запись/чтение/чистка истории.
- Существующие тесты (`test_providers.py`, `test_scheduler.py`, `test_db.py`) не трогаются.

## Зависимости

- `httpx` — async HTTP-клиент (явно добавим в requirements).
- `pydantic` — опционально, для валидации tool args. Можно ручной валидацией обойтись.

## Что НЕ делаем (out of scope)

- Авто-детект vision-моделей через `/api/v1/models` OpenRouter — лишняя точка отказа. Юзер сам ставит флаг.
- Streaming ответов LLM — не нужно для Telegram (всё равно отправляем целым сообщением).
- Подсчёт токенов / биллинг / лимиты по юзерам — пока не требуется, free-модель.
- Vector store / RAG / долгая память — последних N сообщений достаточно.
- Multi-turn ask_user (когда LLM сразу несколько вопросов задаёт) — поддерживаем один pending на юзера.
- Голосовые сообщения — отдельная задача, не сейчас.
