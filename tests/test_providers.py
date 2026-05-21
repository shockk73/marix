import pytest
import httpx
import respx

from providers.base import Trip, DIRECTION_MG_MNSK, DIRECTION_MNSK_MG, DIRECTION_LABELS
from providers.timetable_base import TimetableBaseProvider
from providers.mogilevminsk import MogilevMinskProvider
from providers.avto_slava import AvtoSlavaProvider
from providers.buspro import BusProProvider


def test_trip_fields():
    t = Trip(
        trip_id="abc",
        provider="test",
        route="Могилёв -> Минск",
        date="2026-05-24",
        departure_time="14:20",
        free_seats=2,
        price=20.0,
        currency="руб.",
    )
    assert t.trip_id == "abc"
    assert t.free_seats == 2
    assert t.departure_time == "14:20"


def test_direction_constants():
    assert DIRECTION_MG_MNSK == "mg_mnsk"
    assert DIRECTION_MNSK_MG == "mnsk_mg"
    assert "Могилёв" in DIRECTION_LABELS[DIRECTION_MG_MNSK]
    assert "Минск" in DIRECTION_LABELS[DIRECTION_MNSK_MG]


TIMETABLE_RESPONSE = {
    "result": "success",
    "messages": [],
    "data": {
        "trips": {
            "118756_2_1": {
                "id": "118756",
                "route": "Могилев -> Минск",
                "trip_key": "118756_2_1",
                "departure_time": "12:20",
                "free_seats": 2,
                "price": "20.00",
                "currency": "руб.",
                "date": "24-05-2026",
                "datetime": "2026-05-24T12:20:00",
                "active": True,
            },
            "118759_2_1": {
                "id": "118759",
                "route": "Могилев -> Минск",
                "trip_key": "118759_2_1",
                "departure_time": "14:20",
                "free_seats": 0,
                "price": "20.00",
                "currency": "руб.",
                "date": "24-05-2026",
                "datetime": "2026-05-24T14:20:00",
                "active": True,
            },
            "118771_2_1": {
                "id": "118771",
                "route": "Могилев -> Минск",
                "trip_key": "118771_2_1",
                "departure_time": "05:40",
                "free_seats": 2,
                "price": "15.00",
                "currency": "руб.",
                "date": "25-05-2026",
                "datetime": "2026-05-25T05:40:00",
                "active": True,
            },
        },
        "stations": {},
    },
}


class _TestTimetableProvider(TimetableBaseProvider):
    name = "test_timetable"
    display_name = "Test"
    url = "https://test.example.by/timetable/trips/"
    directions = {DIRECTION_MG_MNSK: ("2", "1")}


@pytest.mark.asyncio
@respx.mock
async def test_timetable_parses_trips():
    respx.post("https://test.example.by/timetable/trips/").mock(
        return_value=httpx.Response(200, json=TIMETABLE_RESPONSE)
    )
    provider = _TestTimetableProvider()
    async with httpx.AsyncClient() as client:
        trips = await provider.get_trips(client, "2026-05-24", DIRECTION_MG_MNSK)

    assert len(trips) == 2
    assert trips[0].trip_id == "118756_2_1"
    assert trips[0].departure_time == "12:20"
    assert trips[0].free_seats == 2
    assert trips[0].price == 20.0
    assert trips[1].free_seats == 0


@pytest.mark.asyncio
@respx.mock
async def test_timetable_posts_correct_form():
    route = respx.post("https://test.example.by/timetable/trips/").mock(
        return_value=httpx.Response(200, json=TIMETABLE_RESPONSE)
    )
    provider = _TestTimetableProvider()
    async with httpx.AsyncClient() as client:
        await provider.get_trips(client, "2026-05-24", DIRECTION_MG_MNSK)

    assert route.called
    request = route.calls[0].request
    body = request.content.decode()
    assert "from_city" in body
    assert "dest_city" in body
    assert "2026-05-24" in body


def test_mogilevminsk_config():
    p = MogilevMinskProvider()
    assert p.display_name == "Минск Экспресс"
    assert p.directions["mg_mnsk"] == ("2", "1")
    assert p.directions["mnsk_mg"] == ("1", "2")
    assert "mogilevminsk.by" in p.url


def test_avto_slava_config():
    p = AvtoSlavaProvider()
    assert p.display_name == "Автослава"
    assert p.directions["mg_mnsk"] == ("2", "1")
    assert "avto-slava.by" in p.url


BUSPRO_RESPONSE = [
    {
        "id": 3601390,
        "route": "Могилёв -Минск",
        "dateDeparture": "24.05.2026",
        "timeDeparture": "08:00",
        "price": 25,
        "freePlaces": 10,
        "allPlaces": 18,
        "status": 1,
        "availableForReservation": 1,
    },
    {
        "id": 3601397,
        "route": "Могилёв -Минск",
        "dateDeparture": "24.05.2026",
        "timeDeparture": "10:00",
        "price": 30,
        "freePlaces": 0,
        "allPlaces": 18,
        "status": 1,
        "availableForReservation": 1,
    },
]


@pytest.mark.asyncio
@respx.mock
async def test_buspro_parses_trips():
    respx.get(url__startswith="https://buspro.by/api/trip").mock(
        return_value=httpx.Response(200, json=BUSPRO_RESPONSE)
    )
    provider = BusProProvider()
    async with httpx.AsyncClient() as client:
        trips = await provider.get_trips(client, "2026-05-24", DIRECTION_MG_MNSK)

    assert len(trips) == 2
    assert trips[0].trip_id == "3601390"
    assert trips[0].departure_time == "08:00"
    assert trips[0].free_seats == 10
    assert trips[0].price == 25.0
    assert trips[1].free_seats == 0


def test_buspro_config():
    p = BusProProvider()
    assert p.display_name == "Гранд Экспресс"
    assert p.directions["mg_mnsk"] == ("30", "37")
    assert p.directions["mnsk_mg"] == ("37", "30")
