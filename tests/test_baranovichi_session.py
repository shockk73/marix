import json

import httpx
import pytest
import respx

import db as db_module
from providers.baranovichi_session import (
    BASE,
    BaranovichiBooker,
    BookingResult,
    InvalidCredentials,
    book_trip,
    cancel_ticket,
    login,
    normalize_phone,
)
from llm.tools import ToolContext, dispatch_tool


def test_normalize_phone_variants():
    expected = "+375 (29) 177-62-96"
    assert normalize_phone("+375291776296") == expected
    assert normalize_phone("375291776296") == expected
    assert normalize_phone("80291776296") == expected
    assert normalize_phone("291776296") == expected
    assert normalize_phone("+375 (29) 177-62-96") == expected
    assert normalize_phone("8 029 177 62 96") == expected
    with pytest.raises(ValueError):
        normalize_phone("12345")
    with pytest.raises(ValueError):
        normalize_phone("")


LOGIN_PAGE = """
<html><body><form method="POST" action="/login">
<input type="hidden" name="_token" value="tok123">
<input type="hidden" name="partner_id" value="2">
<input name="username"><input name="password" type="password">
</form></body></html>
"""

SEARCH_LOGGED = """
<html><body><a href="/logout">Выйти</a>
<article class="tickets-item">
  <div class="tickets-item__way-mini">Минск - Барановичи</div>
  <div class="tickets-way__point-time">7:00</div>
  <div class="tickets-way__point-time">9:05</div>
  <div>Свободно мест: 3</div>
  <a class="btn" href="/tickets/5147/minsk-baranovici/confirm?sl_ignore=1&amp;pickup=2&amp;destination=1&amp;seats_limit=1&amp;date_of_journey=14.06.2026">Купить</a>
  <footer><b>20.00 руб.</b></footer>
</article>
<article class="tickets-item">
  <div class="tickets-item__way-mini">Минск - Барановичи</div>
  <div class="tickets-way__point-time">14:30</div>
  <div>Свободно мест: 0</div>
  <footer><b>20.00 руб.</b></footer>
</article>
</body></html>
"""

SEARCH_ANON = SEARCH_LOGGED.replace('<a href="/logout">Выйти</a>', "")

CONFIRM_PAGE = """
<html><body><a href="/logout">Выйти</a>
<form method="POST" action="/tickets/store?id=5147&amp;date_of_journey=14.06.2026">
<input type="hidden" name="_token" value="tok456">
<input type="hidden" name="seats" value="1">
<input type="hidden" name="seat_labels" value="">
<input type="hidden" name="price" value="20.00">
<input type="hidden" name="sub_total" value="20.00">
<input type="hidden" name="total" value="20.00">
<input type="hidden" name="seats_limit" value="1">
<input type="hidden" name="pickup_dt_departure" value="2026-06-14 07:00">
<input type="hidden" name="dest_dt_arrival" value="2026-06-14 09:05">
<select name="pickup">
  <option value="11">ИНСТИТУТ КУЛЬТУРЫ</option>
  <option value="12">ДРУГАЯ</option>
</select>
<select name="destination">
  <option value="21">АВТОВОКЗАЛ</option>
</select>
<input name="passengers[0][firstname]" value="Дима">
<input name="passengers[0][lastname]" value="Иванов">
<input name="passengers[0][mobile]" value="+375 (29) 177-62-96">
<input type="hidden" name="payment" value="cash">
</form></body></html>
"""

ACTIVE_WITH_TICKET = """
<html><body><a href="/logout">Выйти</a>
<input type="hidden" name="_token" value="tok789">
<form action="/user/tickets/cancel/9911?route_trip_id=5147" method="POST"></form>
</body></html>
"""

ACTIVE_EMPTY = """
<html><body><a href="/logout">Выйти</a>
<input type="hidden" name="_token" value="tok789">
<div>Билетов нет</div>
</body></html>
"""


def _mock_login_ok():
    respx.get(f"{BASE}/login").mock(
        return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/login").mock(
        return_value=httpx.Response(302, headers={
            "location": f"{BASE}/user/account"}))


@pytest.mark.asyncio
@respx.mock
async def test_login_success():
    respx.get(f"{BASE}/login").mock(
        return_value=httpx.Response(200, text=LOGIN_PAGE))
    route = respx.post(f"{BASE}/login").mock(
        return_value=httpx.Response(302, headers={
            "location": f"{BASE}/user/account"}))
    async with httpx.AsyncClient(follow_redirects=False) as client:
        await login(client, "+375 (29) 177-62-96", "pass123")
    body = route.calls[0].request.content.decode()
    assert "tok123" in body
    assert "partner_id=2" in body
    # маска телефона уходит url-encoded
    assert "%2B375+%2829%29+177-62-96" in body or "+375 (29) 177-62-96" in body


@pytest.mark.asyncio
@respx.mock
async def test_login_invalid_credentials():
    respx.get(f"{BASE}/login").mock(
        return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/login").mock(
        return_value=httpx.Response(302, headers={"location": f"{BASE}/login"}))
    async with httpx.AsyncClient(follow_redirects=False) as client:
        with pytest.raises(InvalidCredentials):
            await login(client, "+375 (29) 177-62-96", "wrong")


@pytest.mark.asyncio
@respx.mock
async def test_book_trip_success():
    respx.get(f"{BASE}/tickets/search").mock(
        return_value=httpx.Response(200, text=SEARCH_LOGGED))
    confirm_route = respx.get(
        url__startswith=f"{BASE}/tickets/5147/minsk-baranovici/confirm").mock(
        return_value=httpx.Response(200, text=CONFIRM_PAGE))
    store_route = respx.post(url__startswith=f"{BASE}/tickets/store").mock(
        return_value=httpx.Response(302, headers={
            "location": f"{BASE}/user/tickets/active"}))
    respx.get(f"{BASE}/user/tickets/active").mock(
        return_value=httpx.Response(200, text=ACTIVE_WITH_TICKET))

    async with httpx.AsyncClient(follow_redirects=False) as client:
        result = await book_trip(client, "2026-06-14", "mnsk_baran", "07:00")

    assert result.status == "booked"
    assert result.ticket_id == "9911"
    assert result.trip_id == "5147"
    assert confirm_route.called
    body = store_route.calls[0].request.content.decode()
    assert "tok456" in body
    assert "pickup=11" in body          # первая остановка из селекта
    assert "destination=21" in body
    assert "payment=cash" in body


@pytest.mark.asyncio
@respx.mock
async def test_book_trip_time_not_found_is_gone():
    respx.get(f"{BASE}/tickets/search").mock(
        return_value=httpx.Response(200, text=SEARCH_LOGGED))
    async with httpx.AsyncClient(follow_redirects=False) as client:
        result = await book_trip(client, "2026-06-14", "mnsk_baran", "23:55")
    assert result.status == "gone"


@pytest.mark.asyncio
@respx.mock
async def test_book_trip_card_without_confirm_link_is_gone():
    # 14:30 в фикстуре без ссылки «Купить» (0 мест)
    respx.get(f"{BASE}/tickets/search").mock(
        return_value=httpx.Response(200, text=SEARCH_LOGGED))
    async with httpx.AsyncClient(follow_redirects=False) as client:
        result = await book_trip(client, "2026-06-14", "mnsk_baran", "14:30")
    assert result.status == "gone"


@pytest.mark.asyncio
@respx.mock
async def test_book_trip_no_ticket_after_store_is_gone():
    respx.get(f"{BASE}/tickets/search").mock(
        return_value=httpx.Response(200, text=SEARCH_LOGGED))
    respx.get(url__startswith=f"{BASE}/tickets/5147/").mock(
        return_value=httpx.Response(200, text=CONFIRM_PAGE))
    respx.post(url__startswith=f"{BASE}/tickets/store").mock(
        return_value=httpx.Response(302, headers={
            "location": f"{BASE}/user/tickets/active"}))
    respx.get(f"{BASE}/user/tickets/active").mock(
        return_value=httpx.Response(200, text=ACTIVE_EMPTY))

    async with httpx.AsyncClient(follow_redirects=False) as client:
        result = await book_trip(client, "2026-06-14", "mnsk_baran", "07:00")
    assert result.status == "gone"
    assert result.trip_id == "5147"


@pytest.mark.asyncio
@respx.mock
async def test_booker_relogins_on_expired_session(tmp_db):
    await db_module.save_site_credentials(1, "+375 (29) 177-62-96", "pw")
    _mock_login_ok()
    # 1-й поиск — анонимная страница (сессия протухла), после перелогина — залогиненная
    respx.get(f"{BASE}/tickets/search").mock(side_effect=[
        httpx.Response(200, text=SEARCH_ANON),
        httpx.Response(200, text=SEARCH_LOGGED),
    ])
    respx.get(url__startswith=f"{BASE}/tickets/5147/").mock(
        return_value=httpx.Response(200, text=CONFIRM_PAGE))
    respx.post(url__startswith=f"{BASE}/tickets/store").mock(
        return_value=httpx.Response(302, headers={
            "location": f"{BASE}/user/tickets/active"}))
    respx.get(f"{BASE}/user/tickets/active").mock(
        return_value=httpx.Response(200, text=ACTIVE_WITH_TICKET))

    booker = BaranovichiBooker()
    result = await booker.book(1, "2026-06-14", "mnsk_baran", "07:00")
    assert result.status == "booked"
    assert result.ticket_id == "9911"


@pytest.mark.asyncio
@respx.mock
async def test_booker_without_credentials_raises(tmp_db):
    booker = BaranovichiBooker()
    with pytest.raises(InvalidCredentials):
        await booker.book(99, "2026-06-14", "mnsk_baran", "07:00")


@pytest.mark.asyncio
@respx.mock
async def test_cancel_ticket():
    respx.get(f"{BASE}/user/tickets/active").mock(
        return_value=httpx.Response(200, text=ACTIVE_WITH_TICKET))
    cancel_route = respx.post(
        url__startswith=f"{BASE}/user/tickets/cancel/9911").mock(
        return_value=httpx.Response(302, headers={
            "location": f"{BASE}/user/tickets/active"}))

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ok = await cancel_ticket(client, "9911", "5147")

    assert ok is True
    req = cancel_route.calls[0].request
    assert "route_trip_id=5147" in str(req.url)
    assert "tok789" in req.content.decode()


@pytest.mark.asyncio
@respx.mock
async def test_save_credentials_tool_scrubs_password(tmp_db, monkeypatch):
    _mock_login_ok()
    await db_module.insert_chat_message(
        1, "user", content="подключи бронь, пароль SuperSecret99")
    await db_module.insert_chat_message(
        1, "assistant", content=None,
        tool_calls=json.dumps([{
            "id": "c1", "type": "function",
            "function": {"name": "save_baranovichi_credentials",
                         "arguments": json.dumps({
                             "phone": "+375291776296",
                             "password": "SuperSecret99"})},
        }]),
    )
    ctx = ToolContext(user_id=1)

    out = json.loads(await dispatch_tool(
        "save_baranovichi_credentials",
        {"phone": "+375291776296", "password": "SuperSecret99"}, ctx))

    assert out["connected"] is True
    assert out["phone"] == "+375…96"
    creds = await db_module.get_site_credentials(1)
    assert creds["phone"] == "+375 (29) 177-62-96"
    assert creds["password"] == "SuperSecret99"
    msgs = await db_module.get_recent_chat_messages(1, 100)
    joined = json.dumps(msgs, ensure_ascii=False)
    assert "SuperSecret99" not in joined
    assert "***" in joined


@pytest.mark.asyncio
@respx.mock
async def test_save_credentials_tool_rejects_bad_login(tmp_db):
    respx.get(f"{BASE}/login").mock(
        return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/login").mock(
        return_value=httpx.Response(302, headers={"location": f"{BASE}/login"}))
    ctx = ToolContext(user_id=1)

    out = json.loads(await dispatch_tool(
        "save_baranovichi_credentials",
        {"phone": "+375291776296", "password": "bad"}, ctx))

    assert "error" in out
    assert await db_module.get_site_credentials(1) is None


@pytest.mark.asyncio
async def test_credentials_status_and_delete(tmp_db):
    ctx = ToolContext(user_id=1)
    out = json.loads(await dispatch_tool("get_credentials_status", {}, ctx))
    assert out == {"connected": False}

    await db_module.save_site_credentials(1, "+375 (29) 177-62-96", "pw")
    out = json.loads(await dispatch_tool("get_credentials_status", {}, ctx))
    assert out["connected"] is True
    assert out["phone_masked"] == "+375…96"

    out = json.loads(await dispatch_tool("delete_credentials", {}, ctx))
    assert out["deleted"] is True
    assert await db_module.get_site_credentials(1) is None
