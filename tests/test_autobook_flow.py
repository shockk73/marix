import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import booking_flow
import db as db_module
import scheduler
from providers import PROVIDERS
from providers.base import Trip
from providers.baranovichi_session import BookingResult, InvalidCredentials
from llm.tools import ToolContext, dispatch_tool
from tests.test_llm_agent import FakeBot, _mk_agent, fake_bot, fake_scheduler
from tests.test_stability import FakeProvider

MSK = timezone(timedelta(hours=3))


class FakeBooker:
    def __init__(self):
        self.book_result = BookingResult(status="booked", ticket_id="9911",
                                         trip_id="5147")
        self.book_error = None
        self.book_calls = []
        self.cancel_calls = []
        self.cancel_ok = True

    async def book(self, user_id, date, direction, departure_time):
        self.book_calls.append((user_id, date, direction, departure_time))
        if self.book_error:
            raise self.book_error
        return self.book_result

    async def cancel(self, user_id, ticket_id, trip_id):
        self.cancel_calls.append((user_id, ticket_id, trip_id))
        return self.cancel_ok


@pytest.fixture
def fake_booker(monkeypatch):
    b = FakeBooker()
    monkeypatch.setattr("booking_flow.BOOKER", b)
    monkeypatch.setattr("llm.tools.BOOKER", b)
    return b


@pytest.fixture
def baran_provider(monkeypatch):
    p = FakeProvider()
    monkeypatch.setitem(PROVIDERS, "baranovichi_express", p)
    return p


def _trip(trip_id, departure_time, free_seats=3):
    return Trip(
        trip_id=trip_id, provider="baranovichi_express", route="x",
        date="2099-06-14", departure_time=departure_time,
        free_seats=free_seats, price=20.0, currency="руб.",
    )


async def _mk_watch_row(user_id=1, provider="baranovichi_express",
                        autobook="auto", goal_id="g1",
                        pref_from=None, pref_to=None,
                        date="2099-06-14", time_from="10:00", time_to="20:00"):
    wid = await db_module.create_watch(
        user_id=user_id, provider=provider, direction="mnsk_baran",
        date=date, time_from=time_from, time_to=time_to, interval_sec=60,
        autobook=autobook, goal_id=goal_id,
        pref_time_from=pref_from, pref_time_to=pref_to,
    )
    return await db_module.get_watch(wid)


def test_watch_expired_boundaries():
    w = {"date": "2026-06-12", "time_to": "16:00"}
    before = datetime(2026, 6, 12, 15, 59, tzinfo=MSK)
    after = datetime(2026, 6, 12, 16, 1, tzinfo=MSK)
    next_day = datetime(2026, 6, 13, 0, 0, tzinfo=MSK)
    assert scheduler.watch_expired(w, before) is False
    assert scheduler.watch_expired(w, after) is True
    assert scheduler.watch_expired(w, next_day) is True


@pytest.mark.asyncio
async def test_poll_once_stops_expired_watch(tmp_db):
    bot = FakeBot()
    scheduler.init_scheduler(bot)
    watch = await _mk_watch_row(date="2020-01-01", autobook="off")
    state = {"notified": set(), "consecutive_errors": 0}

    with pytest.raises(scheduler.WatchStopped):
        await scheduler._poll_once(watch, None, state)

    assert (await db_module.get_watch(watch["id"]))["active"] == 0
    assert any("истекла" in m["text"] for m in bot.sent)


@pytest.mark.asyncio
async def test_auto_books_and_stops_goal(tmp_db, baran_provider, fake_booker):
    bot = FakeBot()
    scheduler.init_scheduler(bot)
    watch = await _mk_watch_row(autobook="auto", goal_id="g1")
    sibling = await _mk_watch_row(provider="atlasbus", autobook="off",
                                  goal_id="g1")
    baran_provider.trips = [_trip("t1", "12:30")]
    state = {"notified": set(), "consecutive_errors": 0}

    with pytest.raises(scheduler.WatchStopped):
        await scheduler._poll_once(watch, None, state)

    assert fake_booker.book_calls == [(1, "2099-06-14", "mnsk_baran", "12:30")]
    bookings = await db_module.get_user_bookings(1)
    assert len(bookings) == 1
    assert bookings[0]["departure_time"] == "12:30"
    assert bookings[0]["goal_id"] == "g1"
    assert (await db_module.get_watch(watch["id"]))["active"] == 0
    assert (await db_module.get_watch(sibling["id"]))["active"] == 0
    booked_msg = next(m for m in bot.sent if "Забронировал" in m["text"])
    assert "12:30" in booked_msg["text"]
    # стоп в БД раньше уведомления — рестарт между ними не даст двойную бронь
    assert "цель достигнута" in booked_msg["text"].lower() or "Остановил" in booked_msg["text"]


@pytest.mark.asyncio
async def test_auto_pref_books_outside_then_offers_rebook(tmp_db, baran_provider, fake_booker):
    bot = FakeBot()
    scheduler.init_scheduler(bot)
    watch = await _mk_watch_row(autobook="auto", goal_id="g2",
                                pref_from="14:00", pref_to="15:00",
                                time_from="12:00", time_to="16:00")
    sibling = await _mk_watch_row(provider="atlasbus", autobook="off",
                                  goal_id="g2",
                                  time_from="12:00", time_to="16:00")
    # тик 1: только слот вне приоритетного окна
    baran_provider.trips = [_trip("t1", "12:30")]
    state = {"notified": set(), "consecutive_errors": 0}
    await scheduler._poll_once(watch, None, state)

    bookings = await db_module.get_user_bookings(1)
    assert bookings[0]["departure_time"] == "12:30"
    # бронь вне pref: сам watch жив (ждёт апгрейда), сосед остановлен
    assert (await db_module.get_watch(watch["id"]))["active"] == 1
    assert (await db_module.get_watch(sibling["id"]))["active"] == 0
    assert any("вне приоритетного окна" in m["text"] for m in bot.sent)

    # тик 2: появился слот в приоритетном окне -> предложение перебронировать
    baran_provider.trips = [_trip("t1", "12:30"), _trip("t2", "14:30")]
    await scheduler._poll_once(watch, None, state)

    offer = next(m for m in bot.sent if "Перебронировать" in str(m))
    kb = offer["reply_markup"].inline_keyboard
    assert kb[0][0].callback_data == f"bk:r:{watch['id']}:14:30"
    # вторая бронь сама не сделана — только предложение
    assert len(await db_module.get_user_bookings(1)) == 1
    assert len(fake_booker.book_calls) == 1


@pytest.mark.asyncio
async def test_auto_gone_stays_silent_and_alive(tmp_db, baran_provider, fake_booker):
    bot = FakeBot()
    scheduler.init_scheduler(bot)
    fake_booker.book_result = BookingResult(status="gone")
    watch = await _mk_watch_row(autobook="auto", goal_id="g3")
    baran_provider.trips = [_trip("t1", "12:30")]
    state = {"notified": set(), "consecutive_errors": 0}

    await scheduler._poll_once(watch, None, state)

    assert bot.sent == []
    assert (await db_module.get_watch(watch["id"]))["active"] == 1
    assert await db_module.get_user_bookings(1) == []


@pytest.mark.asyncio
async def test_auto_bad_creds_disables_autobook_and_notifies(tmp_db, baran_provider, fake_booker):
    bot = FakeBot()
    scheduler.init_scheduler(bot)
    fake_booker.book_error = InvalidCredentials("bad")
    watch = await _mk_watch_row(autobook="auto", goal_id="g4")
    baran_provider.trips = [_trip("t1", "12:30")]
    state = {"notified": set(), "consecutive_errors": 0}

    await scheduler._poll_once(watch, None, state)

    assert (await db_module.get_watch(watch["id"]))["autobook"] == "off"
    assert any("автобронь" in m["text"].lower() and "выключена" in m["text"].lower()
               for m in bot.sent)
    # обычное уведомление тоже ушло
    assert any("Появились места" in m["text"] for m in bot.sent)


@pytest.mark.asyncio
async def test_confirm_mode_sends_booking_buttons(tmp_db, baran_provider, fake_booker):
    bot = FakeBot()
    scheduler.init_scheduler(bot)
    watch = await _mk_watch_row(autobook="confirm", goal_id="g5")
    baran_provider.trips = [_trip("t1", "12:30"), _trip("t2", "11:00")]
    state = {"notified": set(), "consecutive_errors": 0}

    await scheduler._poll_once(watch, None, state)

    msg = next(m for m in bot.sent if m["reply_markup"] is not None)
    assert "Появились места" in msg["text"]
    kb = msg["reply_markup"].inline_keyboard
    assert kb[0][0].callback_data == f"bk:b:{watch['id']}:11:00"
    assert kb[1][0].callback_data == f"bk:b:{watch['id']}:12:30"
    assert fake_booker.book_calls == []


@pytest.mark.asyncio
async def test_goal_achieved_tail_watch_stops_itself(tmp_db, baran_provider, fake_booker):
    bot = FakeBot()
    scheduler.init_scheduler(bot)
    watch = await _mk_watch_row(autobook="auto", goal_id="g6")
    await db_module.create_booking(
        user_id=1, date="2099-06-14", direction="mnsk_baran",
        departure_time="12:30", goal_id="g6")
    baran_provider.trips = [_trip("t9", "13:00")]
    state = {"notified": set(), "consecutive_errors": 0}

    with pytest.raises(scheduler.WatchStopped):
        await scheduler._poll_once(watch, None, state)

    assert (await db_module.get_watch(watch["id"]))["active"] == 0
    assert fake_booker.book_calls == []


@pytest.mark.asyncio
async def test_execute_rebooking_cancels_old(tmp_db, fake_booker):
    watch = await _mk_watch_row(autobook="auto", goal_id="g7",
                                pref_from="14:00", pref_to="15:00",
                                time_from="12:00", time_to="16:00")
    old_id = await db_module.create_booking(
        user_id=1, date="2099-06-14", direction="mnsk_baran",
        departure_time="12:30", ticket_id="111", trip_id="222",
        goal_id="g7")
    old = await db_module.get_booking(old_id)

    status, text, stopped = await booking_flow.execute_booking(
        user_id=1, date="2099-06-14", direction="mnsk_baran",
        departure_time="14:30", watch=watch, rebook_from=old,
    )

    assert status == "booked"
    assert fake_booker.cancel_calls == [(1, "111", "222")]
    assert (await db_module.get_booking(old_id))["status"] == "canceled"
    active = await db_module.get_user_bookings(1)
    assert len(active) == 1
    assert active[0]["departure_time"] == "14:30"
    assert (await db_module.get_watch(watch["id"]))["active"] == 0
    assert "Старая бронь 12:30 отменена" in text


@pytest.mark.asyncio
async def test_create_watch_tool_autobook_validation(tmp_db, fake_scheduler):
    ctx = ToolContext(user_id=1)
    base = {
        "providers": ["baranovichi_express"], "direction": "mnsk_baran",
        "date": "2099-06-14", "time_from": "10:00", "time_to": "20:00",
        "interval_sec": 60,
    }
    # без кредов
    out = json.loads(await dispatch_tool(
        "create_watch", {**base, "autobook": "auto"}, ctx))
    assert "error" in out

    # не-барановичи
    out = json.loads(await dispatch_tool("create_watch", {
        **base, "providers": ["atlasbus"], "autobook": "auto"}, ctx))
    assert "error" in out

    # pref вне окна
    await db_module.save_site_credentials(1, "+375 (29) 177-62-96", "pw")
    out = json.loads(await dispatch_tool("create_watch", {
        **base, "autobook": "auto",
        "pref_time_from": "08:00", "pref_time_to": "09:00"}, ctx))
    assert "error" in out

    # валидный вызов: общий goal_id, autobook только на барановичах
    out = json.loads(await dispatch_tool("create_watch", {
        **base, "providers": ["baranovichi_express", "atlasbus"],
        "autobook": "auto",
        "pref_time_from": "14:00", "pref_time_to": "15:00"}, ctx))
    assert len(out["created_ids"]) == 2
    w1 = await db_module.get_watch(out["created_ids"][0])
    w2 = await db_module.get_watch(out["created_ids"][1])
    assert w1["goal_id"] == w2["goal_id"] == out["goal_id"]
    assert w1["autobook"] == "auto"          # baranovichi_express
    assert w2["autobook"] == "off"           # atlasbus
    assert w1["pref_time_from"] == "14:00"


@pytest.mark.asyncio
async def test_stop_watch_requires_confirmation(tmp_db, fake_bot, fake_scheduler):
    wid = await db_module.create_watch(
        user_id=1, provider="atlasbus", direction="mg_mnsk",
        date="2099-06-14", time_from="10:00", time_to="20:00",
        interval_sec=60)
    client = AsyncMock()
    client.chat_completion = AsyncMock(return_value={
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "stop_watch",
                         "arguments": json.dumps({"watch_id": wid})},
        }],
    })
    agent = _mk_agent(fake_bot, client)

    await agent.run_turn(user_id=1, text="останови", user_name=None)

    # слежка НЕ остановлена, юзеру кнопки Да/Нет
    assert (await db_module.get_watch(wid))["active"] == 1
    confirm_msg = next(m for m in fake_bot.sent if m["reply_markup"] is not None)
    assert f"#{wid}" in confirm_msg["text"]
    kb = confirm_msg["reply_markup"].inline_keyboard
    assert [b.callback_data for b in kb[0]] == ["aic:yes", "aic:no"]
    pending = await db_module.get_pending_tool_call(1)
    payload = json.loads(pending["options_json"])
    assert payload["kind"] == "confirm"
    assert payload["name"] == "stop_watch"


@pytest.mark.asyncio
async def test_resolve_confirmation_yes_executes(tmp_db, fake_bot, fake_scheduler):
    wid = await db_module.create_watch(
        user_id=1, provider="atlasbus", direction="mg_mnsk",
        date="2099-06-14", time_from="10:00", time_to="20:00",
        interval_sec=60)
    client = AsyncMock()
    client.chat_completion = AsyncMock(side_effect=[
        {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "stop_watch",
                             "arguments": json.dumps({"watch_id": wid})},
            }],
        },
        {"role": "assistant", "content": "Остановил."},
    ])
    agent = _mk_agent(fake_bot, client)
    await agent.run_turn(user_id=1, text="останови", user_name=None)

    await agent.resolve_confirmation(user_id=1, approved=True)

    assert (await db_module.get_watch(wid))["active"] == 0
    assert await db_module.get_pending_tool_call(1) is None
    msgs = await db_module.get_recent_chat_messages(1, 100)
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert json.loads(tool_msgs[-1]["content"])["ok"] is True
    assert fake_bot.sent[-1]["text"] == "Остановил."


@pytest.mark.asyncio
async def test_resolve_confirmation_no_declines(tmp_db, fake_bot, fake_scheduler):
    wid = await db_module.create_watch(
        user_id=1, provider="atlasbus", direction="mg_mnsk",
        date="2099-06-14", time_from="10:00", time_to="20:00",
        interval_sec=60)
    client = AsyncMock()
    client.chat_completion = AsyncMock(side_effect=[
        {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "stop_watch",
                             "arguments": json.dumps({"watch_id": wid})},
            }],
        },
        {"role": "assistant", "content": "Ок, не трогаю."},
    ])
    agent = _mk_agent(fake_bot, client)
    await agent.run_turn(user_id=1, text="останови", user_name=None)

    await agent.resolve_confirmation(user_id=1, approved=False)

    assert (await db_module.get_watch(wid))["active"] == 1
    msgs = await db_module.get_recent_chat_messages(1, 100)
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert json.loads(tool_msgs[-1]["content"])["canceled"] is True


@pytest.mark.asyncio
async def test_cancel_booking_tool(tmp_db, fake_booker):
    bid = await db_module.create_booking(
        user_id=1, date="2099-06-14", direction="mnsk_baran",
        departure_time="12:30", ticket_id="111", trip_id="222")
    ctx = ToolContext(user_id=1)

    out = json.loads(await dispatch_tool("cancel_booking",
                                         {"booking_id": bid}, ctx))

    assert out == {"canceled": True, "booking_id": bid}
    assert fake_booker.cancel_calls == [(1, "111", "222")]
    assert (await db_module.get_booking(bid))["status"] == "canceled"

    # чужая/несуществующая бронь
    out = json.loads(await dispatch_tool("cancel_booking",
                                         {"booking_id": 999}, ctx))
    assert "error" in out


@pytest.mark.asyncio
async def test_book_trip_now_tool(tmp_db, fake_booker):
    ctx = ToolContext(user_id=1)
    out = json.loads(await dispatch_tool("book_trip_now", {
        "date": "2099-06-14", "direction": "mnsk_baran",
        "departure_time": "12:30"}, ctx))

    assert out["status"] == "booked"
    assert "Забронировал" in out["message"]
    bookings = await db_module.get_user_bookings(1)
    assert len(bookings) == 1


@pytest.mark.asyncio
async def test_list_bookings_tool(tmp_db):
    await db_module.create_booking(
        user_id=1, date="2099-06-14", direction="mnsk_baran",
        departure_time="12:30")
    ctx = ToolContext(user_id=1)
    out = json.loads(await dispatch_tool("list_bookings", {}, ctx))
    assert len(out["bookings"]) == 1
    assert out["bookings"][0]["departure_time"] == "12:30"
