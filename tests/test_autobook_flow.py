import asyncio
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
        self.stop_args = []
        self.cancel_calls = []
        self.cancel_ok = True
        self.stops = {"pickup": ["ИНСТИТУТ КУЛЬТУРЫ", "ДРУГАЯ"],
                      "dropoff": ["АВТОВОКЗАЛ"]}

    async def book(self, user_id, date, direction, departure_time,
                   pickup_stop=None, dropoff_stop=None):
        self.book_calls.append((user_id, date, direction, departure_time))
        self.stop_args.append((pickup_stop, dropoff_stop))
        await asyncio.sleep(0.01)  # окно для гонок — лок обязан их закрывать
        if self.book_error:
            raise self.book_error
        return self.book_result

    async def cancel(self, user_id, ticket_id, trip_id):
        self.cancel_calls.append((user_id, ticket_id, trip_id))
        return self.cancel_ok

    async def get_stops(self, user_id, date, direction):
        return self.stops

    async def verify_login(self, phone, password):
        self.verify_calls = getattr(self, "verify_calls", [])
        self.verify_calls.append(phone)
        if getattr(self, "verify_error", None):
            raise self.verify_error


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
                        date="2099-06-14", time_from="10:00", time_to="20:00",
                        pickup_stop=None, dropoff_stop=None):
    wid = await db_module.create_watch(
        user_id=user_id, provider=provider, direction="mnsk_baran",
        date=date, time_from=time_from, time_to=time_to, interval_sec=60,
        autobook=autobook, goal_id=goal_id,
        pref_time_from=pref_from, pref_time_to=pref_to,
        pickup_stop=pickup_stop, dropoff_stop=dropoff_stop,
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
async def test_auto_passes_watch_stops_to_booker(tmp_db, baran_provider, fake_booker):
    bot = FakeBot()
    scheduler.init_scheduler(bot)
    watch = await _mk_watch_row(autobook="auto", goal_id="g14",
                                pickup_stop="другая", dropoff_stop="полесье")
    baran_provider.trips = [_trip("t1", "12:30")]
    state = {"notified": set(), "consecutive_errors": 0}

    with pytest.raises(scheduler.WatchStopped):
        await scheduler._poll_once(watch, None, state)

    assert fake_booker.stop_args == [("другая", "полесье")]


@pytest.mark.asyncio
async def test_auto_stop_not_found_disables_autobook(tmp_db, baran_provider, fake_booker):
    bot = FakeBot()
    scheduler.init_scheduler(bot)
    fake_booker.book_result = BookingResult(
        status="stop_not_found",
        available_pickups=["ИНСТИТУТ КУЛЬТУРЫ", "ДРУГАЯ"],
        available_dropoffs=["АВТОВОКЗАЛ"])
    watch = await _mk_watch_row(autobook="auto", goal_id="g15",
                                pickup_stop="космодром")
    baran_provider.trips = [_trip("t1", "12:30")]
    state = {"notified": set(), "consecutive_errors": 0}

    await scheduler._poll_once(watch, None, state)

    assert (await db_module.get_watch(watch["id"]))["autobook"] == "off"
    alert = next(m for m in bot.sent if "Не нашёл остановку" in m["text"])
    assert "ИНСТИТУТ КУЛЬТУРЫ" in alert["text"]
    assert "вслепую" in alert["text"]
    # деградация: обычное уведомление о местах тоже ушло
    assert any("Появились места" in m["text"] for m in bot.sent)
    assert await db_module.get_user_bookings(1) == []


@pytest.mark.asyncio
async def test_booked_message_names_stops(tmp_db, fake_booker):
    fake_booker.book_result = BookingResult(
        status="booked", ticket_id="9911", trip_id="5147",
        pickup_label="ИНСТИТУТ КУЛЬТУРЫ", dropoff_label="АВТОВОКЗАЛ")
    watch = await _mk_watch_row(autobook="auto", goal_id="g16")

    status, text, _ = await booking_flow.execute_booking(
        user_id=1, date="2099-06-14", direction="mnsk_baran",
        departure_time="12:30", watch=watch)

    assert status == "booked"
    assert "ИНСТИТУТ КУЛЬТУРЫ" in text
    assert "АВТОВОКЗАЛ" in text
    booking = (await db_module.get_user_bookings(1))[0]
    assert booking["pickup_stop"] == "ИНСТИТУТ КУЛЬТУРЫ"
    assert booking["dropoff_stop"] == "АВТОВОКЗАЛ"


@pytest.mark.asyncio
async def test_create_watch_stores_stops_and_validates(tmp_db, fake_scheduler, fake_booker):
    await db_module.save_site_credentials(1, "+375 (29) 177-62-96", "pw")
    ctx = ToolContext(user_id=1)
    base = {
        "direction": "mnsk_baran", "date": "2099-06-14",
        "time_from": "10:00", "time_to": "20:00", "interval_sec": 60,
    }
    # остановки без барановичей — ошибка
    out = json.loads(await dispatch_tool("create_watch", {
        **base, "providers": ["atlasbus"], "pickup_stop": "другая"}, ctx))
    assert "error" in out

    out = json.loads(await dispatch_tool("create_watch", {
        **base, "providers": ["baranovichi_express", "atlasbus"],
        "autobook": "confirm",
        "pickup_stop": "ДРУГАЯ", "dropoff_stop": "АВТОВОКЗАЛ"}, ctx))
    w_baran = await db_module.get_watch(out["created_ids"][0])
    w_atlas = await db_module.get_watch(out["created_ids"][1])
    assert w_baran["pickup_stop"] == "ДРУГАЯ"
    assert w_baran["dropoff_stop"] == "АВТОВОКЗАЛ"
    assert w_atlas["pickup_stop"] is None


@pytest.mark.asyncio
async def test_get_baranovichi_stops_tool(tmp_db, fake_booker):
    ctx = ToolContext(user_id=1)
    out = json.loads(await dispatch_tool("get_baranovichi_stops", {
        "date": "2099-06-14", "direction": "mnsk_baran"}, ctx))
    assert out["pickup"] == ["ИНСТИТУТ КУЛЬТУРЫ", "ДРУГАЯ"]
    assert out["dropoff"] == ["АВТОВОКЗАЛ"]

    fake_booker.stops = {"pickup": [], "dropoff": []}
    out = json.loads(await dispatch_tool("get_baranovichi_stops", {
        "date": "2099-06-14", "direction": "mnsk_baran"}, ctx))
    assert "error" in out


@pytest.mark.asyncio
async def test_list_bookings_includes_stops(tmp_db):
    await db_module.create_booking(
        user_id=1, date="2099-06-14", direction="mnsk_baran",
        departure_time="12:30", pickup_stop="ДРУГАЯ", dropoff_stop="АВТОВОКЗАЛ")
    ctx = ToolContext(user_id=1)
    out = json.loads(await dispatch_tool("list_bookings", {}, ctx))
    assert out["bookings"][0]["pickup_stop"] == "ДРУГАЯ"
    assert out["bookings"][0]["dropoff_stop"] == "АВТОВОКЗАЛ"


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
async def test_off_mode_with_creds_gets_booking_buttons(tmp_db, baran_provider, fake_booker):
    bot = FakeBot()
    scheduler.init_scheduler(bot)
    await db_module.save_site_credentials(1, "+375 (29) 177-62-96", "pw")
    watch = await _mk_watch_row(autobook="off", goal_id="g20")
    baran_provider.trips = [_trip("t1", "12:30")]
    state = {"notified": set(), "consecutive_errors": 0}

    await scheduler._poll_once(watch, None, state)

    msg = next(m for m in bot.sent if m["reply_markup"] is not None)
    assert "Появились места" in msg["text"]
    kb = msg["reply_markup"].inline_keyboard
    assert kb[0][0].callback_data == f"bk:b:{watch['id']}:12:30"
    assert fake_booker.book_calls == []   # сам не бронировал


@pytest.mark.asyncio
async def test_off_mode_without_creds_plain_notification(tmp_db, baran_provider, fake_booker):
    bot = FakeBot()
    scheduler.init_scheduler(bot)
    watch = await _mk_watch_row(autobook="off", goal_id="g21")
    baran_provider.trips = [_trip("t1", "12:30")]
    state = {"notified": set(), "consecutive_errors": 0}

    await scheduler._poll_once(watch, None, state)

    msg = next(m for m in bot.sent if "Появились места" in m["text"])
    assert msg["reply_markup"] is None    # кнопки нет — аккаунта нет


@pytest.mark.asyncio
async def test_clear_chat_session(tmp_db):
    await db_module.insert_chat_message(1, "user", content="привет")
    await db_module.insert_chat_message(1, "assistant", content="привет!")
    await db_module.set_pending_tool_call(1, "c1", "ask_user", "[]", 5)
    await db_module.insert_chat_message(2, "user", content="чужое")

    await db_module.clear_chat_session(1)

    assert await db_module.get_recent_chat_messages(1, 100) == []
    assert await db_module.get_pending_tool_call(1) is None
    assert len(await db_module.get_recent_chat_messages(2, 100)) == 1


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
async def test_concurrent_goal_booking_books_once(tmp_db, fake_booker):
    watch = await _mk_watch_row(autobook="auto", goal_id="g10")

    results = await asyncio.gather(
        booking_flow.execute_booking(
            user_id=1, date="2099-06-14", direction="mnsk_baran",
            departure_time="12:30", watch=watch),
        booking_flow.execute_booking(
            user_id=1, date="2099-06-14", direction="mnsk_baran",
            departure_time="13:00", watch=watch),
    )

    statuses = sorted(r[0] for r in results)
    assert statuses == ["already_booked", "booked"]
    assert len(fake_booker.book_calls) == 1       # на сайт сходили один раз
    assert len(await db_module.get_user_bookings(1)) == 1


@pytest.mark.asyncio
async def test_rebook_race_targets_current_active_booking(tmp_db, fake_booker):
    watch = await _mk_watch_row(autobook="auto", goal_id="g11",
                                pref_from="14:00", pref_to="15:00",
                                time_from="12:00", time_to="16:00")
    stale_id = await db_module.create_booking(
        user_id=1, date="2099-06-14", direction="mnsk_baran",
        departure_time="12:30", ticket_id="111", trip_id="222", goal_id="g11")
    stale = await db_module.get_booking(stale_id)
    # параллельная перебронь уже заменила 12:30 на 13:00 — ссылка протухла
    await db_module.mark_booking_canceled(stale_id)
    current_id = await db_module.create_booking(
        user_id=1, date="2099-06-14", direction="mnsk_baran",
        departure_time="13:00", ticket_id="333", trip_id="444", goal_id="g11")

    status, text, _ = await booking_flow.execute_booking(
        user_id=1, date="2099-06-14", direction="mnsk_baran",
        departure_time="14:30", watch=watch, rebook_from=stale)

    assert status == "booked"
    # отменили актуальную бронь (13:00), а не по протухшей ссылке
    assert fake_booker.cancel_calls == [(1, "333", "444")]
    assert (await db_module.get_booking(current_id))["status"] == "canceled"
    active = await db_module.get_user_bookings(1)
    assert [b["departure_time"] for b in active] == ["14:30"]


@pytest.mark.asyncio
async def test_rebook_same_time_is_idempotent(tmp_db, fake_booker):
    watch = await _mk_watch_row(autobook="auto", goal_id="g12",
                                pref_from="14:00", pref_to="15:00",
                                time_from="12:00", time_to="16:00")
    bid = await db_module.create_booking(
        user_id=1, date="2099-06-14", direction="mnsk_baran",
        departure_time="14:30", ticket_id="111", trip_id="222", goal_id="g12")
    old = await db_module.get_booking(bid)

    status, _, _ = await booking_flow.execute_booking(
        user_id=1, date="2099-06-14", direction="mnsk_baran",
        departure_time="14:30", watch=watch, rebook_from=old)

    assert status == "already_booked"
    assert fake_booker.book_calls == []
    assert (await db_module.get_booking(bid))["status"] == "active"


@pytest.mark.asyncio
async def test_create_watch_tool_autobook_validation(tmp_db, fake_scheduler, fake_booker):
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

    # креды есть, но логин не проходит → агенту точная причина
    await db_module.save_site_credentials(1, "+375 (29) 177-62-96", "old-pw")
    fake_booker.verify_error = InvalidCredentials("bad")
    out = json.loads(await dispatch_tool(
        "create_watch", {**base, "autobook": "auto"}, ctx))
    assert "error" in out
    assert "не подходят" in out["error"]
    fake_booker.verify_error = None
    await db_module.delete_site_credentials(1)

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
async def test_create_watch_joins_existing_goal(tmp_db, fake_scheduler, fake_booker):
    await db_module.save_site_credentials(1, "+375 (29) 177-62-96", "pw")
    ctx = ToolContext(user_id=1)
    base = {
        "direction": "mnsk_baran", "date": "2099-06-14",
        "time_from": "10:00", "time_to": "20:00", "interval_sec": 60,
    }
    first = json.loads(await dispatch_tool("create_watch", {
        **base, "providers": ["baranovichi_express"], "autobook": "auto"}, ctx))
    goal = first["goal_id"]

    # добавляем провайдера к той же поездке
    second = json.loads(await dispatch_tool("create_watch", {
        **base, "providers": ["atlasbus"], "goal_id": goal}, ctx))
    w = await db_module.get_watch(second["created_ids"][0])
    assert w["goal_id"] == goal
    assert len(await db_module.get_goal_watches(1, goal)) == 2

    # несуществующая цель
    out = json.loads(await dispatch_tool("create_watch", {
        **base, "providers": ["atlasbus"], "goal_id": "nope"}, ctx))
    assert "error" in out

    # другая дата — не та поездка
    out = json.loads(await dispatch_tool("create_watch", {
        **base, "providers": ["atlasbus"], "goal_id": goal,
        "date": "2099-06-15"}, ctx))
    assert "error" in out


@pytest.mark.asyncio
async def test_check_trips_now_marks_bookable(tmp_db, baran_provider, monkeypatch):
    from tests.test_stability import FakeProvider as FP
    atlas_fake = FP()
    atlas_fake.directions = {"mnsk_baran": ("1", "2")}
    atlas_fake.trips = [_trip("a1", "13:30")]
    monkeypatch.setitem(PROVIDERS, "atlasbus", atlas_fake)
    baran_provider.directions = {"mnsk_baran": ("2", "1")}
    baran_provider.trips = [_trip("b1", "14:00")]
    ctx = ToolContext(user_id=1)
    args = {"direction": "mnsk_baran", "date": "2099-06-14",
            "time_from": "00:00", "time_to": "23:59"}

    atlas_out = json.loads(await dispatch_tool(
        "check_trips_now", {**args, "provider": "atlasbus"}, ctx))
    baran_out = json.loads(await dispatch_tool(
        "check_trips_now", {**args, "provider": "baranovichi_express"}, ctx))

    assert atlas_out["bookable"] is False
    assert "не предлагай" in atlas_out["note"].lower()
    assert baran_out["bookable"] is True
    assert "show_screen" in baran_out["note"]      # выбор рейса — кнопками

    baran_provider.trips = [_trip("b2", "15:00", free_seats=0)]
    empty_out = json.loads(await dispatch_tool(
        "check_trips_now", {**args, "provider": "baranovichi_express"}, ctx))
    assert "Мест нет" in empty_out["note"]
    assert "🔔 Следить" in empty_out["note"]


@pytest.mark.asyncio
async def test_create_watch_bounces_without_autobook_choice(tmp_db, fake_scheduler, fake_booker):
    await db_module.save_site_credentials(1, "+375 (29) 177-62-96", "pw")
    ctx = ToolContext(user_id=1)
    base = {
        "providers": ["baranovichi_express", "atlasbus"],
        "direction": "mnsk_baran", "date": "2099-06-14",
        "time_from": "10:00", "time_to": "20:00",
    }
    # аккаунт есть, autobook не передан → отбой с инструкцией спросить кнопками
    out = json.loads(await dispatch_tool("create_watch", dict(base), ctx))
    assert "error" in out
    assert "show_screen" in out["error"]

    # явный выбор off → создаётся
    out = json.loads(await dispatch_tool(
        "create_watch", {**base, "autobook": "off"}, ctx))
    assert len(out["created_ids"]) == 2

    # без аккаунта вопрос не нужен — создаётся сразу
    await db_module.delete_site_credentials(1)
    out = json.loads(await dispatch_tool("create_watch", dict(base), ctx))
    assert "created_ids" in out


@pytest.mark.asyncio
async def test_list_all_watches_admin_only(tmp_db, fake_scheduler):
    await db_module.set_user_name(1, "Дима")
    await db_module.set_user_name(2, "Маша")
    await db_module.create_watch(
        user_id=1, provider="atlasbus", direction="mnsk_baran",
        date="2099-06-14", time_from="10:00", time_to="20:00", interval_sec=60)
    await db_module.create_watch(
        user_id=2, provider="baranovichi_express", direction="baran_mnsk",
        date="2099-06-15", time_from="08:00", time_to="12:00", interval_sec=60,
        autobook="auto", goal_id="g77")

    out = json.loads(await dispatch_tool(
        "list_all_watches", {}, ToolContext(user_id=1, role="user")))
    assert "error" in out

    out = json.loads(await dispatch_tool(
        "list_all_watches", {}, ToolContext(user_id=1, role="admin")))
    assert len(out["watches"]) == 2
    by_user = {w["user_id"]: w for w in out["watches"]}
    assert by_user[1]["user_name"] == "Дима"
    assert by_user[2]["user_name"] == "Маша"
    assert by_user[2]["autobook"] == "auto"
    assert "execution" in by_user[1]

    from llm.tools import build_tools_for_role
    assert "list_all_watches" in {
        t["function"]["name"] for t in build_tools_for_role("admin")}
    assert "list_all_watches" not in {
        t["function"]["name"] for t in build_tools_for_role("user")}


@pytest.mark.asyncio
async def test_create_watch_default_interval_60(tmp_db, fake_scheduler):
    ctx = ToolContext(user_id=1)
    out = json.loads(await dispatch_tool("create_watch", {
        "providers": ["atlasbus"], "direction": "mnsk_baran",
        "date": "2099-06-14", "time_from": "00:00", "time_to": "23:59",
    }, ctx))
    w = await db_module.get_watch(out["created_ids"][0])
    assert w["interval_sec"] == 60


@pytest.mark.asyncio
async def test_list_watches_exposes_goal_autobook_pref(tmp_db, fake_scheduler):
    await db_module.create_watch(
        user_id=1, provider="baranovichi_express", direction="mnsk_baran",
        date="2099-06-14", time_from="10:00", time_to="20:00",
        interval_sec=60, autobook="auto", goal_id="g42",
        pref_time_from="14:00", pref_time_to="15:00")
    ctx = ToolContext(user_id=1)
    out = json.loads(await dispatch_tool("list_watches", {}, ctx))
    w = out["watches"][0]
    assert w["autobook"] == "auto"
    assert w["goal_id"] == "g42"
    assert w["pref_time_from"] == "14:00"
    assert w["pref_time_to"] == "15:00"


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
