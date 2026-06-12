# LLM Worldview Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Переписать системный промпт в структуру «миссия → память → модель мира → флоу → стиль → справочник», убрать лишние вопросы (интервал = дефолт 120) и «🔧»-шум быстрых тулзов.

**Architecture:** Перезапись хвоста `build_system_prompt` (рендер среза состояния сохраняется), `interval_sec` опционален в create_watch, `_TOOL_THINKING_LABELS` сокращается до медленных операций без fallback-лейбла.

**Spec:** `docs/superpowers/specs/2026-06-12-llm-worldview-redesign-design.md`. Без TDD по требованию пользователя.

---

### Task 1: prompt.py — перезапись

- [ ] Новый текст по структуре спеки; сохранить строки, на которые завязаны существующие тесты («только по теме», «execution», atlas-тулзы, markdown-правила, «ЦЕЛЬ», «один вызов create_watch», «ask_user_form», «Перебронировать», «не должен её знать», «ОСТАНОВКИ», «get_baranovichi_stops», «вслепую», «по инвайту», кнопки). `LLM_SESSION_VERSION = "2026-06-12-worldview-v1"`.

### Task 2: tools.py — interval_sec опционален

- [ ] Схема: убрать из required, описание «по умолчанию 120 — пользователя НЕ спрашивать». Хендлер: `interval = args.get("interval_sec") or 120`, валидация остаётся (int >= 60).

### Task 3: agent.py — тихие быстрые тулзы

- [ ] `_TOOL_THINKING_LABELS` = только {check_trips_now, generate_sessions_report, save_baranovichi_credentials, cancel_booking, book_trip_now, get_baranovichi_stops}; в цикле слать лейбл только если он есть (без fallback).

### Task 4: тесты

- [ ] `test_run_turn_ask_user_creates_pending`: sent == 1 (без «🔧 Уточняю…»).
- [ ] Новый: create_watch без interval_sec → 120.
- [ ] Прогон `python -m pytest tests/ -q` — зелёный.

### Task 5: живой прогон

- [ ] Скрипт `_smoke_dialog.py` (удалить после): tmp sqlite, реальный OpenRouterClient из .env, LLMAgent + бот-коллектор; сценарии: привет / «надо завтра утром из Минска в Барановичи» / «следи» / «что у меня» / «как ты устроен внутри» / оффтоп. Критерии руками по транскрипту: ≤3 предложений, без ключей/id, верные тулзы.
- [ ] Commit + push.
