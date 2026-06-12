"""Логин и бронирование на tickets.baranovichi-express.by под аккаунтом юзера.

Флоу снят живой бронью с отменой (2026-06-12), детали в спеке
docs/superpowers/specs/2026-06-12-baranovichi-autobook-design.md.
"""
import html as html_mod
import logging
import re
from dataclasses import dataclass

import httpx

import db as db_module
from .baranovichi_express import BaranovichiExpressProvider

logger = logging.getLogger(__name__)

BASE = "https://tickets.baranovichi-express.by"

_DIRECTIONS = BaranovichiExpressProvider.directions

_INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.I)
_NAME_RE = re.compile(r'name="([^"]*)"')
_VALUE_RE = re.compile(r'value="([^"]*)"')
_TIME_RE = re.compile(r"tickets-way__point-time[^>]*>\s*(\d{1,2}:\d{2})")
_CONFIRM_HREF_RE = re.compile(r'href="([^"]*?/confirm[^"]*)"')
_STORE_FORM_RE = re.compile(
    r'<form[^>]*action="([^"]*?/tickets/store[^"]*)"[^>]*>(.*?)</form>',
    re.S | re.I,
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en-US;q=0.9,en;q=0.8",
}


class InvalidCredentials(Exception):
    pass


class SessionExpired(Exception):
    pass


@dataclass
class BookingResult:
    status: str  # "booked" | "gone"
    ticket_id: str | None = None
    trip_id: str | None = None


def normalize_phone(raw: str) -> str:
    """Любой ввод → формат маски сайта «+375 (29) 177-62-96».
    Сырой +375291776296 сайт отвергает."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("80") and len(digits) == 11:
        digits = "375" + digits[2:]
    if len(digits) == 9:
        digits = "375" + digits
    if not (digits.startswith("375") and len(digits) == 12):
        raise ValueError(f"Не похоже на белорусский номер телефона: {raw!r}")
    return f"+375 ({digits[3:5]}) {digits[5:8]}-{digits[8:10]}-{digits[10:12]}"


def _norm_time(t: str) -> str:
    hh, mm = t.split(":")
    return f"{int(hh):02d}:{mm}"


def _extract_input_value(page: str, name: str) -> str | None:
    for tag in _INPUT_TAG_RE.findall(page):
        m = _NAME_RE.search(tag)
        if m and m.group(1) == name:
            v = _VALUE_RE.search(tag)
            return html_mod.unescape(v.group(1)) if v else ""
    return None


def _form_inputs(form_body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tag in _INPUT_TAG_RE.findall(form_body):
        m = _NAME_RE.search(tag)
        if not m or not m.group(1):
            continue
        v = _VALUE_RE.search(tag)
        out[m.group(1)] = html_mod.unescape(v.group(1)) if v else ""
    return out


def _first_select_option(form_body: str, name: str) -> str | None:
    m = re.search(
        rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>',
        form_body, re.S | re.I,
    )
    if not m:
        return None
    opt = re.search(r'<option[^>]*value="([^"]*)"', m.group(1))
    return html_mod.unescape(opt.group(1)) if opt else None


def _abs_url(href: str) -> str:
    return href if href.startswith("http") else f"{BASE}{href}"


async def login(client: httpx.AsyncClient, phone: str, password: str) -> None:
    resp = await client.get(f"{BASE}/login")
    token = _extract_input_value(resp.text, "_token")
    partner = _extract_input_value(resp.text, "partner_id") or "2"
    if not token:
        raise SessionExpired("login page without _token")
    resp = await client.post(f"{BASE}/login", data={
        "_token": token,
        "partner_id": partner,
        "username": phone,
        "password": password,
    })
    location = resp.headers.get("location", "")
    if resp.status_code in (301, 302, 303) and "/user/account" in location:
        return
    raise InvalidCredentials("login failed")


async def book_trip(
    client: httpx.AsyncClient,
    date: str,
    direction: str,
    departure_time: str,
) -> BookingResult:
    """Поиск под логином → матч рейса по времени → confirm → store → проверка
    появления билета. Сессию не восстанавливает — это делает Booker."""
    pickup, destination = _DIRECTIONS[direction]
    resp = await client.get(f"{BASE}/tickets/search", params={
        "pickup": pickup,
        "destination": destination,
        "seats_limit": "1",
        "date_of_journey": f"{date[8:10]}.{date[5:7]}.{date[0:4]}",
    })
    page = resp.text
    if "Выйти" not in page:
        raise SessionExpired("search page is anonymous")

    target = _norm_time(departure_time)
    confirm_href = None
    for chunk in page.split("<article")[1:]:
        head, _, body = chunk.partition(">")
        if "tickets-item" not in head:
            continue
        tm = _TIME_RE.search(body)
        if not tm or _norm_time(tm.group(1)) != target:
            continue
        hm = _CONFIRM_HREF_RE.search(body)
        if hm:
            confirm_href = html_mod.unescape(hm.group(1))
        break
    if not confirm_href:
        return BookingResult(status="gone")

    trip_m = re.search(r"/tickets/(\d+)/", confirm_href)
    trip_id = trip_m.group(1) if trip_m else None
    if trip_id is None:
        return BookingResult(status="gone")

    resp = await client.get(_abs_url(confirm_href))
    if resp.status_code in (301, 302, 303):
        raise SessionExpired("confirm redirected to login")
    form_m = _STORE_FORM_RE.search(resp.text)
    if not form_m:
        return BookingResult(status="gone", trip_id=trip_id)

    action = html_mod.unescape(form_m.group(1))
    form_body = form_m.group(2)
    fields = _form_inputs(form_body)
    for select_name in ("pickup", "destination"):
        v = _first_select_option(form_body, select_name)
        if v is not None:
            fields[select_name] = v
    fields.setdefault("comment", "")

    await client.post(_abs_url(action), data=fields)

    resp = await client.get(f"{BASE}/user/tickets/active")
    cancel_m = re.search(
        r"cancel/(\d+)\?[^\"']*route_trip_id=" + re.escape(trip_id),
        resp.text,
    )
    if not cancel_m:
        return BookingResult(status="gone", trip_id=trip_id)
    return BookingResult(status="booked", ticket_id=cancel_m.group(1),
                         trip_id=trip_id)


async def cancel_ticket(
    client: httpx.AsyncClient,
    ticket_id: str,
    trip_id: str | None,
) -> bool:
    resp = await client.get(f"{BASE}/user/tickets/active")
    if "Выйти" not in resp.text:
        raise SessionExpired("active page is anonymous")
    token = _extract_input_value(resp.text, "_token")
    if not token:
        return False
    resp = await client.post(
        f"{BASE}/user/tickets/cancel/{ticket_id}",
        params={"route_trip_id": trip_id or ""},
        data={"_token": token},
    )
    return resp.status_code in (200, 301, 302, 303)


class BaranovichiBooker:
    """Бронирование под аккаунтами юзеров: куки кэшируются per-user,
    протухшая сессия лечится одним перелогином."""

    def __init__(self) -> None:
        self._clients: dict[int, httpx.AsyncClient] = {}

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=20.0, follow_redirects=False,
                                 headers=_HEADERS)

    async def _logged_client(self, user_id: int, force: bool = False) -> httpx.AsyncClient:
        creds = await db_module.get_site_credentials(user_id)
        if creds is None:
            raise InvalidCredentials("Аккаунт сайта не подключён")
        client = self._clients.get(user_id)
        if client is None or force:
            if client is not None:
                await client.aclose()
                self._clients.pop(user_id, None)
            client = self._new_client()
            await login(client, creds["phone"], creds["password"])
            self._clients[user_id] = client
        return client

    async def book(
        self,
        user_id: int,
        date: str,
        direction: str,
        departure_time: str,
    ) -> BookingResult:
        client = await self._logged_client(user_id)
        try:
            return await book_trip(client, date, direction, departure_time)
        except SessionExpired:
            client = await self._logged_client(user_id, force=True)
            return await book_trip(client, date, direction, departure_time)

    async def cancel(self, user_id: int, ticket_id: str, trip_id: str | None) -> bool:
        client = await self._logged_client(user_id)
        try:
            return await cancel_ticket(client, ticket_id, trip_id)
        except SessionExpired:
            client = await self._logged_client(user_id, force=True)
            return await cancel_ticket(client, ticket_id, trip_id)

    async def verify_login(self, phone: str, password: str) -> None:
        """Пробный логин без кэширования (для подключения кредов)."""
        async with self._new_client() as client:
            await login(client, phone, password)


BOOKER = BaranovichiBooker()
