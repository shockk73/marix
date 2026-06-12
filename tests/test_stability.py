import json
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.exceptions import TelegramRetryAfter

import db as db_module
import scheduler
from providers import PROVIDERS
from providers.base import Trip
from llm.agent import LLMAgent
from tests.test_llm_agent import FakeBot, _mk_agent, fake_bot, fake_scheduler


def _trip(trip_id="t1", departure_time="12:00", free_seats=3):
    return Trip(
        trip_id=trip_id, provider="fakeprov", route="x",
        date="2026-06-14", departure_time=departure_time,
        free_seats=free_seats, price=20.0, currency="руб.",
    )


class FakeProvider:
    name = "fakeprov"
    display_name = "Fake"
    directions = {"mg_mnsk": ("1", "2")}

    def __init__(self):
        self.trips = []
        self.error = None

    async def get_trips(self, client, date, direction):
        if self.error:
            raise self.error
        return self.trips


class FlakyBot(FakeBot):
    """Первый send_message кидает, дальше работает."""
    def __post_init__(self):
        super().__post_init__()
        self.fail_times = 0

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("telegram down")
        return await super().send_message(chat_id, text,
                                          reply_markup=reply_markup,
                                          parse_mode=parse_mode)


async def _mk_watch(user_id=1, provider="fakeprov", **kw):
    wid = await db_module.create_watch(
        user_id=user_id, provider=provider, direction="mg_mnsk",
        date="2026-06-14", time_from="10:00", time_to="20:00",
        interval_sec=60,
    )
    return {
        "id": wid, "user_id": user_id, "provider": provider,
        "direction": "mg_mnsk", "date": "2026-06-14",
        "time_from": "10:00", "time_to": "20:00", "interval_sec": 60,
        "notified_trips": "[]", "autobook": "off", "goal_id": None,
        "pref_time_from": None, "pref_time_to": None,
        **kw,
    }


@pytest.fixture
def fake_provider(monkeypatch):
    p = FakeProvider()
    monkeypatch.setitem(PROVIDERS, "fakeprov", p)
    return p


@pytest.mark.asyncio
async def test_failed_notification_retries_next_tick(tmp_db, fake_provider, monkeypatch):
    bot = FlakyBot()
    bot.fail_times = 1
    scheduler.init_scheduler(bot)
    fake_provider.trips = [_trip()]
    watch = await _mk_watch()
    state = {"notified": set(), "consecutive_errors": 0}

    with pytest.raises(RuntimeError):
        await scheduler._poll_once(watch, None, state)

    # отправка упала -> notified_trips в БД НЕ обновлён
    rows = await db_module.get_active_watches()
    assert json.loads(rows[0]["notified_trips"]) == []

    # следующий тик: рейс всё ещё «новый» с точки зрения БД
    state2 = {"notified": set(json.loads(rows[0]["notified_trips"])),
              "consecutive_errors": 1}
    await scheduler._poll_once(watch, None, state2)
    assert any("Появились места" in m["text"] for m in bot.sent)
    rows = await db_module.get_active_watches()
    assert json.loads(rows[0]["notified_trips"]) == ["t1"]


@pytest.mark.asyncio
async def test_send_with_retry_handles_flood(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)

    calls = {"n": 0}

    class FloodBot:
        async def send_message(self, chat_id, text, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TelegramRetryAfter(method=None, message="flood",
                                         retry_after=7)
            return "ok"

    result = await scheduler.send_with_retry(FloodBot(), 1, "hi")
    assert result == "ok"
    assert calls["n"] == 2
    assert sleeps == [7]


@pytest.mark.asyncio
async def test_tool_crash_isolated(tmp_db, fake_bot, fake_scheduler, monkeypatch):
    async def boom(name, args, ctx):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("llm.agent.dispatch_tool", boom)
    client = AsyncMock()
    client.chat_completion = AsyncMock(side_effect=[
        {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "list_watches", "arguments": "{}"},
            }],
        },
        {"role": "assistant", "content": "что-то пошло не так, но я жив"},
    ])
    agent = _mk_agent(fake_bot, client)

    await agent.run_turn(user_id=1, text="x", user_name=None)

    msgs = await db_module.get_recent_chat_messages(1, 100)
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert "Tool crashed" in tool_msgs[0]["content"]
    assert tool_msgs[0]["tool_call_id"] == "c1"
    assert fake_bot.sent[-1]["text"] == "что-то пошло не так, но я жив"


@pytest.mark.asyncio
async def test_ten_errors_one_alert_then_recovery_message(tmp_db, fake_provider):
    bot = FakeBot()
    scheduler.init_scheduler(bot)
    watch = await _mk_watch()
    state = {"notified": set(), "consecutive_errors": 0}

    fake_provider.error = RuntimeError("site down")
    for _ in range(12):
        try:
            await scheduler._poll_once(watch, None, state)
        except Exception as exc:
            await scheduler._handle_poll_error(watch, state, exc)

    alerts = [m for m in bot.sent if "спотыкается" in m["text"]]
    assert len(alerts) == 1
    assert state["consecutive_errors"] == 12

    fake_provider.error = None
    fake_provider.trips = []
    await scheduler._poll_once(watch, None, state)

    recovered = [m for m in bot.sent if "снова работает" in m["text"]]
    assert len(recovered) == 1
    assert state["consecutive_errors"] == 0


def test_backoff_multiplier():
    assert scheduler._backoff_multiplier(0) == 1
    assert scheduler._backoff_multiplier(4) == 1
    assert scheduler._backoff_multiplier(5) == 2
    assert scheduler._backoff_multiplier(10) == 4
    assert scheduler._backoff_multiplier(15) == 8
    assert scheduler._backoff_multiplier(40) == 8
