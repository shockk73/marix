# Photo/Voice Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Включить обработку фото и голосовых через мультимодальную модель + грейсфул-фоллбек при 400 от OpenRouter.

**Architecture:** Код мультимодальности в `llm/agent.py` уже рабочий, выключен флагами. Включаем `LLM_VISION`/`LLM_AUDIO` в `.env`, добавляем проброс 4xx из `_drive_turn` (опц. параметр) и ловим его в `handle_photo`/`handle_audio` с дружелюбным сообщением.

**Tech Stack:** Python, aiogram 3, httpx, pytest (asyncio_mode=auto), AsyncMock.

**Spec:** `docs/superpowers/specs/2026-06-12-photo-voice-fix-design.md`. По требованию пользователя — без TDD: сначала код, потом тесты.

---

### Task 1: Флаги в .env и .env.example

**Files:**
- Modify: `.env` (не в git — локально)
- Modify: `.env.example`

- [ ] **Step 1: Добавить в `.env` строки**

```
LLM_VISION=true
LLM_AUDIO=true
```

- [ ] **Step 2: Добавить в `.env.example` (после OPENROUTER-блока, которого там нет — добавить весь LLM-блок)**

```
# OpenRouter LLM agent
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=google/gemini-3.1-flash-lite
# Мультимодальность: модель принимает фото (image_url data-URI) и голосовые
# (input_audio, ogg/opus). Проверено на google/gemini-3.1-flash-lite 2026-06-12.
LLM_VISION=true
LLM_AUDIO=true
# Запасной вариант, если модель не умеет аудио: отдельная STT-модель (пусто = выкл)
LLM_STT_MODEL=
```

### Task 2: Грейсфул-фоллбек 4xx в llm/agent.py

**Files:**
- Modify: `llm/agent.py` (`_drive_turn` ~298, `handle_photo` ~163, `handle_audio` ~204)

- [ ] **Step 1: `_drive_turn` — параметр `_raise_client_errors`**

В сигнатуру добавить `_raise_client_errors: bool = False`; в начало ветки `except httpx.HTTPError as e:` добавить re-raise 4xx:

```python
    async def _drive_turn(
        self,
        user_id: int,
        _override_last_user: dict[str, Any] | None = None,
        _raise_client_errors: bool = False,
    ) -> None:
```

```python
            except httpx.HTTPError as e:
                if (
                    _raise_client_errors
                    and isinstance(e, httpx.HTTPStatusError)
                    and 400 <= e.response.status_code < 500
                ):
                    raise
                logger.warning("LLM HTTP error: %s", e)
                ...
```

- [ ] **Step 2: `handle_photo` — обернуть `_drive_turn`**

```python
            try:
                await self._drive_turn(
                    user_id,
                    _override_last_user=self._build_multimodal_user(image_bytes, mime, caption_text),
                    _raise_client_errors=True,
                )
            except httpx.HTTPStatusError as e:
                logger.warning("Photo request rejected %s: %s", e.response.status_code, e)
                msg = "Не получилось разобрать фото. Напиши текстом, пожалуйста."
                await db_module.insert_chat_message(user_id, "assistant", content=msg)
                await self._bot.send_message(user_id, msg)
```

- [ ] **Step 3: `handle_audio` (ветка LLM_AUDIO) — аналогично**

```python
                try:
                    await self._drive_turn(
                        user_id,
                        _override_last_user=self._build_audio_user(
                            audio_bytes, audio_format, caption_text,
                        ),
                        _raise_client_errors=True,
                    )
                except httpx.HTTPStatusError as e:
                    logger.warning("Audio request rejected %s: %s", e.response.status_code, e)
                    msg = "Не получилось разобрать голосовое. Напиши текстом, пожалуйста."
                    await db_module.insert_chat_message(user_id, "assistant", content=msg)
                    await self._bot.send_message(user_id, msg)
```

### Task 3: Тесты фоллбека

**Files:**
- Modify: `tests/test_llm_agent.py` (в конец)

- [ ] **Step 1: Хелпер + 3 теста**

```python
def _http_400() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://openrouter.test/chat/completions")
    response = httpx.Response(400, request=request, text="no multimodal")
    return httpx.HTTPStatusError("400", request=request, response=response)


@pytest.mark.asyncio
async def test_handle_photo_400_friendly_fallback(tmp_db, fake_bot, fake_scheduler, monkeypatch):
    monkeypatch.setattr("llm.agent.LLM_VISION", True)
    client = AsyncMock()
    client.chat_completion = AsyncMock(side_effect=_http_400())
    agent = _mk_agent(fake_bot, client)

    await agent.handle_photo(user_id=1, image_bytes=b"\xff", mime="image/jpeg",
                             caption=None, user_name=None)

    assert "фото" in fake_bot.sent[-1]["text"].lower()
    assert "текст" in fake_bot.sent[-1]["text"].lower()
    msgs = await db_module.get_recent_chat_messages(1, 100)
    assert msgs[-1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_handle_audio_400_friendly_fallback(tmp_db, fake_bot, fake_scheduler, monkeypatch):
    monkeypatch.setattr("llm.agent.LLM_AUDIO", True)
    client = AsyncMock()
    client.chat_completion = AsyncMock(side_effect=_http_400())
    agent = _mk_agent(fake_bot, client)

    await agent.handle_audio(user_id=1, audio_bytes=b"\x00", audio_format="ogg",
                             caption=None, user_name=None)

    assert "голосовое" in fake_bot.sent[-1]["text"].lower()
    msgs = await db_module.get_recent_chat_messages(1, 100)
    assert msgs[-1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_handle_photo_network_error_generic_message(tmp_db, fake_bot, fake_scheduler, monkeypatch):
    monkeypatch.setattr("llm.agent.LLM_VISION", True)
    client = AsyncMock()
    client.chat_completion = AsyncMock(side_effect=httpx.RequestError("conn refused"))
    agent = _mk_agent(fake_bot, client)

    await agent.handle_photo(user_id=1, image_bytes=b"\xff", mime="image/jpeg",
                             caption=None, user_name=None)

    assert "AI" in fake_bot.sent[-1]["text"]
```

`import httpx` добавить в шапку теста (там импортируется локально в двух тестах — поднять наверх).

- [ ] **Step 2: Прогнать**

Run: `python -m pytest tests/ -q`
Expected: все зелёные.

- [ ] **Step 3: Commit**

```bash
git add .env.example llm/agent.py tests/test_llm_agent.py docs/superpowers/plans/2026-06-12-photo-voice-fix.md
git commit -m "feat: enable photo/voice multimodal input with graceful 4xx fallback"
```
