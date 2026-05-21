# Telegram Seat Notifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegram bot that polls 4 minibus providers and notifies users when seats appear in a watched date/time window.

**Architecture:** aiogram 3.x bot with FSM dialogs; each watch is an asyncio Task that polls a provider at a configurable interval; aiosqlite stores watches and survives restarts; provider layer is a Protocol so new providers are added as single files.

**Tech Stack:** Python 3.11+, aiogram 3.x, aiosqlite, httpx (async), python-dotenv, pytest, pytest-asyncio, respx

---

## File Map

| File | Responsibility |
|---|---|
| `bot.py` | Entry point: init DB, scheduler, start polling |
| `handlers.py` | /start /watch (FSM) /list /stop |
| `scheduler.py` | asyncio task per watch, notification sender |
| `db.py` | aiosqlite CRUD for watches table |
| `config.py` | reads .env |
| `providers/base.py` | `Trip` dataclass, `DIRECTION_*` constants, `Provider` Protocol |
| `providers/timetable_base.py` | Base for POST-multipart providers (mogilevminsk + avto-slava) |
| `providers/mogilevminsk.py` | Минск Экспресс |
| `providers/avto_slava.py` | Автослава |
| `providers/buspro.py` | Гранд Экспресс |
| `providers/atlasbus.py` | Атласбус |
| `providers/__init__.py` | `PROVIDERS` registry dict |
| `tests/test_providers.py` | Provider parsing tests (respx mocks) |
| `tests/test_db.py` | DB CRUD tests |
| `tests/test_scheduler.py` | Filtering + dedup logic tests |
| `requirements.txt` | Dependencies |
| `.env.example` | Token template |

---

## Task 1: Project Setup

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `config.py`

- [ ] **Step 1: Create requirements.txt**

```
aiogram==3.13.1
aiosqlite==0.20.0
httpx==0.27.2
python-dotenv==1.0.1
pytest==8.3.3
pytest-asyncio==0.24.0
respx==0.21.1
```

- [ ] **Step 2: Create .env.example**

```
BOT_TOKEN=1234567890:ABCdefGhIjKlmNoPQRstUvWxYz
DB_PATH=watches.db
```

- [ ] **Step 3: Create config.py**

```python
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
DB_PATH: str = os.getenv("DB_PATH", "watches.db")
```

- [ ] **Step 4: Install dependencies**

```bash
pip install -r requirements.txt
```

Expected: no errors.

- [ ] **Step 5: Copy .env.example to .env and fill BOT_TOKEN**

```bash
cp .env.example .env
# edit .env and set real BOT_TOKEN
```

- [ ] **Step 6: Commit**

```bash
git init
git add requirements.txt .env.example config.py .gitignore
git commit -m "feat: project setup"
```

---

## Task 2: Provider Base Types

**Files:**
- Create: `providers/__init__.py` (empty for now)
- Create: `providers/base.py`
- Create: `tests/__init__.py` (empty)
- Create: `tests/test_providers.py` (skeleton)

- [ ] **Step 1: Write failing test for Trip dataclass**

Create `tests/test_providers.py`:
```python
from providers.base import Trip, DIRECTION_MG_MNSK, DIRECTION_MNSK_MG, DIRECTION_LABELS


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
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_providers.py::test_trip_fields -v
```
Expected: `ModuleNotFoundError: No module named 'providers.base'`

- [ ] **Step 3: Create providers/__init__.py (empty)**

```python
```

- [ ] **Step 4: Create providers/base.py**

```python
from dataclasses import dataclass
from typing import Protocol
import httpx

DIRECTION_MG_MNSK = "mg_mnsk"
DIRECTION_MNSK_MG = "mnsk_mg"

DIRECTION_LABELS = {
    DIRECTION_MG_MNSK: "Могилёв → Минск",
    DIRECTION_MNSK_MG: "Минск → Могилёв",
}


@dataclass
class Trip:
    trip_id: str
    provider: str
    route: str
    date: str            # "YYYY-MM-DD"
    departure_time: str  # "HH:MM"
    free_seats: int
    price: float
    currency: str


class Provider(Protocol):
    name: str
    display_name: str
    directions: dict[str, tuple[str, str]]

    async def get_trips(
        self,
        client: httpx.AsyncClient,
        date: str,
        direction: str,
    ) -> list[Trip]: ...
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_providers.py -v
```
Expected: `2 passed`

- [ ] **Step 6: Commit**

```bash
git add providers/__init__.py providers/base.py tests/__init__.py tests/test_providers.py
git commit -m "feat: provider base types"
```

---

## Task 3: TimetableBaseProvider (mogilevminsk + avto-slava format)

**Files:**
- Create: `providers/timetable_base.py`
- Modify: `tests/test_providers.py`

- [ ] **Step 1: Add failing test for timetable parsing**

Add to `tests/test_providers.py`:
```python
import pytest
import httpx
import respx
from providers.timetable_base import TimetableBaseProvider
from providers.base import DIRECTION_MG_MNSK

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
            # trip from different date — must be filtered out
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

    assert len(trips) == 2  # 3rd trip is different date
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
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_providers.py::test_timetable_parses_trips -v
```
Expected: `ModuleNotFoundError: No module named 'providers.timetable_base'`

- [ ] **Step 3: Create providers/timetable_base.py**

```python
import httpx
from .base import Trip


class TimetableBaseProvider:
    name: str
    display_name: str
    url: str
    directions: dict[str, tuple[str, str]]

    async def get_trips(
        self,
        client: httpx.AsyncClient,
        date: str,
        direction: str,
    ) -> list[Trip]:
        from_city, dest_city = self.directions[direction]
        resp = await client.post(
            self.url,
            data={"date": date, "from_city": from_city, "dest_city": dest_city},
        )
        resp.raise_for_status()
        data = resp.json()
        trips = []
        for t in data["data"]["trips"].values():
            trip_date = t["datetime"][:10]
            if trip_date != date:
                continue
            trips.append(Trip(
                trip_id=t["trip_key"],
                provider=self.name,
                route=t["route"],
                date=trip_date,
                departure_time=t["departure_time"],
                free_seats=t["free_seats"],
                price=float(t["price"]),
                currency=t["currency"],
            ))
        return trips
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_providers.py -v
```
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add providers/timetable_base.py tests/test_providers.py
git commit -m "feat: timetable base provider"
```

---

## Task 4: Минск Экспресс + Автослава Providers

**Files:**
- Create: `providers/mogilevminsk.py`
- Create: `providers/avto_slava.py`

- [ ] **Step 1: Create providers/mogilevminsk.py**

```python
from .timetable_base import TimetableBaseProvider


class MogilevMinskProvider(TimetableBaseProvider):
    name = "mogilevminsk"
    display_name = "Минск Экспресс"
    url = "https://mogilevminsk.by/timetable/trips/"
    directions = {
        "mg_mnsk": ("2", "1"),
        "mnsk_mg": ("1", "2"),
    }
```

- [ ] **Step 2: Create providers/avto_slava.py**

```python
from .timetable_base import TimetableBaseProvider


class AvtoSlavaProvider(TimetableBaseProvider):
    name = "avto_slava"
    display_name = "Автослава"
    url = "https://avto-slava.by/timetable/trips/"
    directions = {
        "mg_mnsk": ("2", "1"),
        "mnsk_mg": ("1", "2"),
    }
```

- [ ] **Step 3: Add smoke tests**

Add to `tests/test_providers.py`:
```python
from providers.mogilevminsk import MogilevMinskProvider
from providers.avto_slava import AvtoSlavaProvider


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
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_providers.py -v
```
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add providers/mogilevminsk.py providers/avto_slava.py tests/test_providers.py
git commit -m "feat: mogilevminsk and avto-slava providers"
```

---

## Task 5: Гранд Экспресс Provider (buspro.by)

**Files:**
- Create: `providers/buspro.py`
- Modify: `tests/test_providers.py`

- [ ] **Step 1: Add failing test**

Add to `tests/test_providers.py`:
```python
from providers.buspro import BusProProvider

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
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_providers.py::test_buspro_parses_trips -v
```
Expected: `ModuleNotFoundError: No module named 'providers.buspro'`

- [ ] **Step 3: Create providers/buspro.py**

```python
import httpx
from .base import Trip


class BusProProvider:
    name = "buspro"
    display_name = "Гранд Экспресс"
    directions = {
        "mg_mnsk": ("30", "37"),
        "mnsk_mg": ("37", "30"),
    }
    _url = "https://buspro.by/api/trip"

    async def get_trips(
        self,
        client: httpx.AsyncClient,
        date: str,
        direction: str,
    ) -> list[Trip]:
        city_dep, city_dest = self.directions[direction]
        resp = await client.get(
            self._url,
            params={
                "s[company_id]": "8",
                "s[city_departure_id]": city_dep,
                "s[city_destination_id]": city_dest,
                "s[date_departure]": date,
                "actual": "1",
            },
        )
        resp.raise_for_status()
        trips = []
        for t in resp.json():
            d, m, y = t["dateDeparture"].split(".")
            trip_date = f"{y}-{m}-{d}"
            if trip_date != date:
                continue
            trips.append(Trip(
                trip_id=str(t["id"]),
                provider=self.name,
                route=t["route"],
                date=trip_date,
                departure_time=t["timeDeparture"],
                free_seats=t["freePlaces"],
                price=float(t["price"]),
                currency="руб.",
            ))
        return trips
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_providers.py -v
```
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add providers/buspro.py tests/test_providers.py
git commit -m "feat: buspro (Гранд Экспресс) provider"
```

---

## Task 6: Атласбус Provider (atlasbus.by)

**Files:**
- Create: `providers/atlasbus.py`
- Modify: `tests/test_providers.py`

- [ ] **Step 1: Add failing test**

Add to `tests/test_providers.py`:
```python
from providers.atlasbus import AtlasBusProvider

ATLASBUS_RESPONSE = {
    "calendar": [],
    "rides": [
        {
            "id": "ims4:abc:1:625665:625144",
            "name": "Могилев- Минск",
            "carrier": "ООО Фиарт",
            "departure": "2026-05-24T04:00:00",
            "arrival": "2026-05-24T07:00:00",
            "freeSeats": 9,
            "price": 30,
            "currency": "BYN",
            "status": "sale",
        },
        {
            "id": "ims4:def:1:625665:625144",
            "name": "Могилев- Минск",
            "carrier": "ООО СапсанВит",
            "departure": "2026-05-24T06:00:00",
            "arrival": "2026-05-24T09:00:00",
            "freeSeats": 0,
            "price": 25,
            "currency": "BYN",
            "status": "sale",
        },
        # different date — must be filtered
        {
            "id": "ims4:ghi:1:625665:625144",
            "name": "Могилев- Минск",
            "carrier": "ООО Фиарт",
            "departure": "2026-05-25T04:00:00",
            "arrival": "2026-05-25T07:00:00",
            "freeSeats": 5,
            "price": 30,
            "currency": "BYN",
            "status": "sale",
        },
    ],
}


@pytest.mark.asyncio
@respx.mock
async def test_atlasbus_parses_trips():
    respx.get(url__startswith="https://atlasbus.by/api/search").mock(
        return_value=httpx.Response(200, json=ATLASBUS_RESPONSE)
    )
    provider = AtlasBusProvider()
    async with httpx.AsyncClient() as client:
        trips = await provider.get_trips(client, "2026-05-24", DIRECTION_MG_MNSK)

    assert len(trips) == 2  # 3rd is different date
    assert trips[0].trip_id == "ims4:abc:1:625665:625144"
    assert trips[0].departure_time == "04:00"
    assert trips[0].free_seats == 9
    assert trips[0].price == 30.0
    assert trips[0].currency == "BYN"
    assert trips[1].free_seats == 0


def test_atlasbus_config():
    p = AtlasBusProvider()
    assert p.display_name == "Атласбус"
    assert p.directions["mg_mnsk"] == ("c625665", "c625144")
    assert p.directions["mnsk_mg"] == ("c625144", "c625665")
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_providers.py::test_atlasbus_parses_trips -v
```
Expected: `ModuleNotFoundError: No module named 'providers.atlasbus'`

- [ ] **Step 3: Create providers/atlasbus.py**

```python
import httpx
from .base import Trip


class AtlasBusProvider:
    name = "atlasbus"
    display_name = "Атласбус"
    directions = {
        "mg_mnsk": ("c625665", "c625144"),
        "mnsk_mg": ("c625144", "c625665"),
    }
    _url = "https://atlasbus.by/api/search"

    async def get_trips(
        self,
        client: httpx.AsyncClient,
        date: str,
        direction: str,
    ) -> list[Trip]:
        from_id, to_id = self.directions[direction]
        resp = await client.get(
            self._url,
            params={
                "from_id": from_id,
                "to_id": to_id,
                "calendar_width": "1",
                "date": date,
                "passengers": "1",
                "operatorId": "",
            },
        )
        resp.raise_for_status()
        trips = []
        for t in resp.json().get("rides", []):
            trip_date = t["departure"][:10]
            if trip_date != date:
                continue
            trips.append(Trip(
                trip_id=t["id"],
                provider=self.name,
                route=t["name"],
                date=trip_date,
                departure_time=t["departure"][11:16],
                free_seats=t["freeSeats"],
                price=float(t["price"]),
                currency=t["currency"],
            ))
        return trips
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_providers.py -v
```
Expected: `10 passed`

- [ ] **Step 5: Commit**

```bash
git add providers/atlasbus.py tests/test_providers.py
git commit -m "feat: atlasbus (Атласбус) provider"
```

---

## Task 7: Provider Registry

**Files:**
- Modify: `providers/__init__.py`

- [ ] **Step 1: Update providers/__init__.py**

```python
from .mogilevminsk import MogilevMinskProvider
from .avto_slava import AvtoSlavaProvider
from .buspro import BusProProvider
from .atlasbus import AtlasBusProvider

PROVIDERS: dict[str, object] = {
    "mogilevminsk": MogilevMinskProvider(),
    "avto_slava": AvtoSlavaProvider(),
    "buspro": BusProProvider(),
    "atlasbus": AtlasBusProvider(),
}
```

- [ ] **Step 2: Add registry test**

Add to `tests/test_providers.py`:
```python
from providers import PROVIDERS


def test_registry_has_all_providers():
    assert set(PROVIDERS.keys()) == {"mogilevminsk", "avto_slava", "buspro", "atlasbus"}
    for key, p in PROVIDERS.items():
        assert hasattr(p, "name")
        assert hasattr(p, "display_name")
        assert hasattr(p, "directions")
        assert hasattr(p, "get_trips")
        assert "mg_mnsk" in p.directions
        assert "mnsk_mg" in p.directions
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_providers.py -v
```
Expected: `11 passed`

- [ ] **Step 4: Commit**

```bash
git add providers/__init__.py tests/test_providers.py
git commit -m "feat: provider registry"
```

---

## Task 8: Database Layer

**Files:**
- Create: `db.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_db.py`:
```python
import pytest
import pytest_asyncio
import os
import tempfile

import db as db_module


@pytest_asyncio.fixture
async def tmp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    await db_module.init_db()
    yield db_path


@pytest.mark.asyncio
async def test_create_and_get_watch(tmp_db):
    watch_id = await db_module.create_watch(
        user_id=123,
        provider="mogilevminsk",
        direction="mg_mnsk",
        date="2026-05-24",
        time_from="11:00",
        time_to="23:00",
        interval_sec=120,
    )
    assert watch_id == 1
    watches = await db_module.get_active_watches()
    assert len(watches) == 1
    w = watches[0]
    assert w["user_id"] == 123
    assert w["provider"] == "mogilevminsk"
    assert w["direction"] == "mg_mnsk"
    assert w["notified_trips"] == "[]"


@pytest.mark.asyncio
async def test_stop_watch(tmp_db):
    watch_id = await db_module.create_watch(
        user_id=456, provider="buspro", direction="mg_mnsk",
        date="2026-05-24", time_from="10:00", time_to="20:00", interval_sec=60,
    )
    result = await db_module.stop_watch(watch_id, 456)
    assert result is True
    watches = await db_module.get_active_watches()
    assert len(watches) == 0


@pytest.mark.asyncio
async def test_stop_watch_wrong_user(tmp_db):
    watch_id = await db_module.create_watch(
        user_id=789, provider="atlasbus", direction="mg_mnsk",
        date="2026-05-24", time_from="10:00", time_to="20:00", interval_sec=60,
    )
    result = await db_module.stop_watch(watch_id, 999)
    assert result is False
    watches = await db_module.get_active_watches()
    assert len(watches) == 1


@pytest.mark.asyncio
async def test_update_notified_trips(tmp_db):
    watch_id = await db_module.create_watch(
        user_id=123, provider="mogilevminsk", direction="mg_mnsk",
        date="2026-05-24", time_from="11:00", time_to="23:00", interval_sec=120,
    )
    await db_module.update_notified_trips(watch_id, ["trip_1", "trip_2"])
    watches = await db_module.get_active_watches()
    import json
    assert json.loads(watches[0]["notified_trips"]) == ["trip_1", "trip_2"]


@pytest.mark.asyncio
async def test_get_user_watches(tmp_db):
    await db_module.create_watch(123, "mogilevminsk", "mg_mnsk", "2026-05-24", "11:00", "23:00", 120)
    await db_module.create_watch(123, "atlasbus", "mg_mnsk", "2026-05-24", "11:00", "23:00", 60)
    await db_module.create_watch(456, "buspro", "mg_mnsk", "2026-05-24", "11:00", "23:00", 60)
    watches = await db_module.get_user_watches(123)
    assert len(watches) == 2
    assert all(w["user_id"] == 123 for w in watches)
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_db.py -v
```
Expected: `ModuleNotFoundError: No module named 'db'`

- [ ] **Step 3: Create db.py**

```python
import json
import aiosqlite
from datetime import datetime
from config import DB_PATH

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS watches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    provider    TEXT    NOT NULL,
    direction   TEXT    NOT NULL,
    date        TEXT    NOT NULL,
    time_from   TEXT    NOT NULL,
    time_to     TEXT    NOT NULL,
    interval_sec INTEGER NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    notified_trips TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT    NOT NULL
)
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(_CREATE_TABLE)
        await conn.commit()


async def create_watch(
    user_id: int,
    provider: str,
    direction: str,
    date: str,
    time_from: str,
    time_to: str,
    interval_sec: int,
) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """INSERT INTO watches
               (user_id, provider, direction, date, time_from, time_to, interval_sec, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, provider, direction, date, time_from, time_to, interval_sec,
             datetime.now().isoformat()),
        )
        await conn.commit()
        return cur.lastrowid


async def get_active_watches() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM watches WHERE active = 1")
        return [dict(r) for r in await cur.fetchall()]


async def get_user_watches(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM watches WHERE user_id = ? AND active = 1 ORDER BY id",
            (user_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def stop_watch(watch_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "UPDATE watches SET active = 0 WHERE id = ? AND user_id = ?",
            (watch_id, user_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def update_notified_trips(watch_id: int, trip_ids: list[str]):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE watches SET notified_trips = ? WHERE id = ?",
            (json.dumps(trip_ids), watch_id),
        )
        await conn.commit()
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_db.py -v
```
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "feat: database layer"
```

---

## Task 9: Scheduler

**Files:**
- Create: `scheduler.py`
- Create: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing tests for filtering logic**

Create `tests/test_scheduler.py`:
```python
import pytest
from providers.base import Trip

# We test the pure filtering logic extracted to a helper
from scheduler import filter_trips_in_window, compute_newly_available


def _trip(trip_id: str, departure_time: str, free_seats: int) -> Trip:
    return Trip(
        trip_id=trip_id, provider="test", route="test",
        date="2026-05-24", departure_time=departure_time,
        free_seats=free_seats, price=20.0, currency="руб.",
    )


def test_filter_trips_in_window():
    trips = [
        _trip("a", "10:00", 2),
        _trip("b", "14:00", 1),
        _trip("c", "23:30", 0),
    ]
    result = filter_trips_in_window(trips, "11:00", "23:00")
    assert len(result) == 1
    assert result[0].trip_id == "b"


def test_filter_includes_boundary():
    trips = [
        _trip("a", "11:00", 2),
        _trip("b", "23:00", 1),
    ]
    result = filter_trips_in_window(trips, "11:00", "23:00")
    assert len(result) == 2


def test_newly_available_detects_new():
    trips = [
        _trip("a", "14:00", 2),
        _trip("b", "15:00", 1),
    ]
    notified = {"a"}
    newly, updated_notified = compute_newly_available(trips, notified)
    assert len(newly) == 1
    assert newly[0].trip_id == "b"
    assert "a" in updated_notified
    assert "b" in updated_notified


def test_newly_available_removes_gone():
    trips = [
        _trip("a", "14:00", 0),  # was notified, now 0 seats
    ]
    notified = {"a"}
    newly, updated_notified = compute_newly_available(trips, notified)
    assert len(newly) == 0
    assert "a" not in updated_notified  # removed so next appearance triggers notify


def test_newly_available_no_double_notify():
    trips = [_trip("a", "14:00", 2)]
    notified = {"a"}
    newly, updated_notified = compute_newly_available(trips, notified)
    assert len(newly) == 0
    assert "a" in updated_notified
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_scheduler.py -v
```
Expected: `ImportError: cannot import name 'filter_trips_in_window' from 'scheduler'`

- [ ] **Step 3: Create scheduler.py**

```python
import asyncio
import json
import logging
from typing import Any

import httpx

from db import get_active_watches, update_notified_trips, stop_watch
from providers import PROVIDERS
from providers.base import Trip, DIRECTION_LABELS

logger = logging.getLogger(__name__)

_tasks: dict[int, asyncio.Task] = {}
_bot: Any = None


def init_scheduler(bot: Any) -> None:
    global _bot
    _bot = bot


def filter_trips_in_window(trips: list[Trip], time_from: str, time_to: str) -> list[Trip]:
    return [t for t in trips if time_from <= t.departure_time <= time_to]


def compute_newly_available(
    trips: list[Trip],
    notified: set[str],
) -> tuple[list[Trip], set[str]]:
    available = [t for t in trips if t.free_seats > 0]
    available_ids = {t.trip_id for t in available}

    # remove trips that lost seats — so they'll trigger again if seats return
    updated = {tid for tid in notified if tid in available_ids}

    newly = [t for t in available if t.trip_id not in notified]
    updated.update(t.trip_id for t in newly)

    return newly, updated


async def start_watch(watch: dict) -> None:
    watch_id = watch["id"]
    if watch_id in _tasks and not _tasks[watch_id].done():
        return
    task = asyncio.create_task(_poll_loop(watch), name=f"watch-{watch_id}")
    _tasks[watch_id] = task


async def cancel_watch(watch_id: int) -> None:
    task = _tasks.pop(watch_id, None)
    if task and not task.done():
        task.cancel()


async def restore_watches() -> None:
    watches = await get_active_watches()
    for w in watches:
        await start_watch(w)
    logger.info("Restored %d active watches", len(watches))


async def _poll_loop(watch: dict) -> None:
    watch_id = watch["id"]
    provider = PROVIDERS[watch["provider"]]
    notified: set[str] = set(json.loads(watch["notified_trips"]))

    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            try:
                all_trips = await provider.get_trips(client, watch["date"], watch["direction"])
                in_window = filter_trips_in_window(all_trips, watch["time_from"], watch["time_to"])
                newly, notified = compute_newly_available(in_window, notified)

                if newly:
                    await update_notified_trips(watch_id, list(notified))
                    await _send_notification(watch, newly)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Watch %d error: %s", watch_id, exc)

            try:
                await asyncio.sleep(watch["interval_sec"])
            except asyncio.CancelledError:
                break


async def _send_notification(watch: dict, trips: list[Trip]) -> None:
    direction_label = DIRECTION_LABELS[watch["direction"]]
    provider = PROVIDERS[watch["provider"]]
    lines = [
        f"🚌 Появились места! [{provider.display_name}]",
        f"{direction_label}, {watch['date']}",
        "",
    ]
    for t in sorted(trips, key=lambda x: x.departure_time):
        lines.append(f"  {t.departure_time} — {t.free_seats} мест, {t.price:.0f} {t.currency}")
    lines += ["", f"Задача #{watch['id']} | /stop {watch['id']}"]
    await _bot.send_message(watch["user_id"], "\n".join(lines))
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_scheduler.py -v
```
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add scheduler.py tests/test_scheduler.py
git commit -m "feat: scheduler with filtering and dedup logic"
```

---

## Task 10: Bot Handlers (FSM)

**Files:**
- Create: `handlers.py`

- [ ] **Step 1: Create handlers.py**

```python
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from datetime import datetime

from db import create_watch, get_user_watches, stop_watch as db_stop_watch
from providers import PROVIDERS
from providers.base import DIRECTION_LABELS, DIRECTION_MG_MNSK, DIRECTION_MNSK_MG
from scheduler import start_watch, cancel_watch

router = Router()

_DIRECTIONS = [
    (DIRECTION_MG_MNSK, DIRECTION_LABELS[DIRECTION_MG_MNSK]),
    (DIRECTION_MNSK_MG, DIRECTION_LABELS[DIRECTION_MNSK_MG]),
]


class WatchForm(StatesGroup):
    provider = State()
    direction = State()
    date = State()
    time_from = State()
    time_to = State()
    interval = State()


def _providers_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=p.display_name, callback_data=f"prov:{key}")]
        for key, p in PROVIDERS.items()
    ])


def _directions_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"dir:{key}")]
        for key, label in _DIRECTIONS
    ])


@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Слежу за местами в маршрутках.\n\n"
        "/watch — создать отслеживание\n"
        "/list — активные задачи\n"
        "/stop <id> — остановить задачу"
    )


@router.message(Command("watch"))
async def cmd_watch(message: Message, state: FSMContext):
    await state.set_state(WatchForm.provider)
    await message.answer("Выбери провайдера:", reply_markup=_providers_kb())


@router.callback_query(WatchForm.provider, F.data.startswith("prov:"))
async def on_provider(cb: CallbackQuery, state: FSMContext):
    await state.update_data(provider=cb.data.split(":")[1])
    await state.set_state(WatchForm.direction)
    await cb.message.edit_text("Направление:", reply_markup=_directions_kb())


@router.callback_query(WatchForm.direction, F.data.startswith("dir:"))
async def on_direction(cb: CallbackQuery, state: FSMContext):
    await state.update_data(direction=cb.data.split(":")[1])
    await state.set_state(WatchForm.date)
    await cb.message.edit_text("Введи дату (ГГГГ-ММ-ДД), например 2026-05-24:")


@router.message(WatchForm.date)
async def on_date(message: Message, state: FSMContext):
    text = message.text.strip()
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        await message.answer("Неверный формат. Пример: 2026-05-24")
        return
    await state.update_data(date=text)
    await state.set_state(WatchForm.time_from)
    await message.answer("Время ОТ (ЧЧ:ММ), например 11:00:")


@router.message(WatchForm.time_from)
async def on_time_from(message: Message, state: FSMContext):
    t = message.text.strip()
    if not _valid_time(t):
        await message.answer("Неверный формат. Пример: 11:00")
        return
    await state.update_data(time_from=t)
    await state.set_state(WatchForm.time_to)
    await message.answer("Время ДО (ЧЧ:ММ), например 23:00:")


@router.message(WatchForm.time_to)
async def on_time_to(message: Message, state: FSMContext):
    t = message.text.strip()
    if not _valid_time(t):
        await message.answer("Неверный формат. Пример: 23:00")
        return
    await state.update_data(time_to=t)
    await state.set_state(WatchForm.interval)
    await message.answer("Интервал проверки в секундах (минимум 60), например 120:")


@router.message(WatchForm.interval)
async def on_interval(message: Message, state: FSMContext):
    try:
        interval = int(message.text.strip())
        if interval < 60:
            raise ValueError
    except ValueError:
        await message.answer("Введи целое число секунд (минимум 60):")
        return

    data = await state.get_data()
    await state.clear()

    watch_id = await create_watch(
        user_id=message.from_user.id,
        provider=data["provider"],
        direction=data["direction"],
        date=data["date"],
        time_from=data["time_from"],
        time_to=data["time_to"],
        interval_sec=interval,
    )
    watch = {
        "id": watch_id,
        "user_id": message.from_user.id,
        "notified_trips": "[]",
        **data,
        "interval_sec": interval,
    }
    await start_watch(watch)

    p = PROVIDERS[data["provider"]]
    dir_label = DIRECTION_LABELS[data["direction"]]
    await message.answer(
        f"Отслеживание #{watch_id} запущено!\n"
        f"{p.display_name} | {dir_label}\n"
        f"{data['date']}, {data['time_from']}–{data['time_to']}\n"
        f"Интервал: {interval} сек\n\n"
        f"Остановить: /stop {watch_id}"
    )


@router.message(Command("list"))
async def cmd_list(message: Message):
    watches = await get_user_watches(message.from_user.id)
    if not watches:
        await message.answer("Нет активных отслеживаний.")
        return
    lines = ["Активные отслеживания:\n"]
    for w in watches:
        p = PROVIDERS[w["provider"]]
        dir_label = DIRECTION_LABELS[w["direction"]]
        lines.append(
            f"#{w['id']} {p.display_name} | {dir_label}\n"
            f"  {w['date']}, {w['time_from']}–{w['time_to']}, каждые {w['interval_sec']}с\n"
            f"  /stop {w['id']}"
        )
    await message.answer("\n\n".join(lines))


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /stop <id>")
        return
    watch_id = int(parts[1])
    if await db_stop_watch(watch_id, message.from_user.id):
        await cancel_watch(watch_id)
        await message.answer(f"Отслеживание #{watch_id} остановлено.")
    else:
        await message.answer(f"Отслеживание #{watch_id} не найдено.")


def _valid_time(t: str) -> bool:
    if len(t) != 5 or t[2] != ":":
        return False
    try:
        datetime.strptime(t, "%H:%M")
        return True
    except ValueError:
        return False
```

- [ ] **Step 2: Verify no import errors**

```bash
python -c "from handlers import router; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add handlers.py
git commit -m "feat: bot handlers with FSM watch dialog"
```

---

## Task 11: Bot Entry Point

**Files:**
- Create: `bot.py`

- [ ] **Step 1: Create bot.py**

```python
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN
from db import init_db
from handlers import router
from scheduler import init_scheduler, restore_watches

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await init_db()
    init_scheduler(bot)
    await restore_watches()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run all tests**

```bash
pytest tests/ -v
```
Expected: all tests pass (16+)

- [ ] **Step 3: Add pytest.ini for asyncio mode**

Create `pytest.ini`:
```ini
[pytest]
asyncio_mode = auto
```

- [ ] **Step 4: Start the bot**

```bash
python bot.py
```
Expected: `INFO ... Started polling`

- [ ] **Step 5: Manual smoke test in Telegram**
  - Send `/start` — should get welcome message
  - Send `/watch` — should see provider buttons
  - Select provider → direction → enter date → time_from → time_to → interval
  - Should get confirmation with watch ID
  - Send `/list` — should see the watch
  - Send `/stop <id>` — should get "остановлено"
  - Send `/list` — should get "Нет активных отслеживаний"

- [ ] **Step 6: Commit**

```bash
git add bot.py pytest.ini
git commit -m "feat: bot entry point — notifier complete"
```

---

## Self-Review Notes

**Spec coverage:**
- ✅ 4 providers (mogilevminsk, avto-slava, buspro, atlasbus)
- ✅ Both directions (mg_mnsk / mnsk_mg)
- ✅ Date + time range filter (time_from / time_to)
- ✅ Configurable polling interval (min 60s)
- ✅ Notify on 0→>0 transition, continue watching
- ✅ /stop <id> cancels specific watch
- ✅ No limit on simultaneous watches
- ✅ Restart-safe (restore from DB on startup)
- ✅ Provider abstraction (new provider = new file)

**Type consistency:** `Trip`, `filter_trips_in_window`, `compute_newly_available`, `start_watch`, `cancel_watch` are consistent across scheduler.py, handlers.py, and tests.
