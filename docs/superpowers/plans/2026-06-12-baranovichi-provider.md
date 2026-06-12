# Baranovichi Express Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Направления Минск↔Барановичи через новый провайдер tickets.baranovichi-express.by и существующий Атласбус.

**Architecture:** Новый класс `BaranovichiExpressProvider` (regex-парсинг серверного HTML, композитный trip_id `{date}_{HH:MM}`), две новые константы направлений в `base.py`, два ID в `atlasbus.py`, регистрация в `__init__.py`, упоминание в промпте + bump `LLM_SESSION_VERSION`. Хендлеры/тулзы/планировщик подхватывают всё из `DIRECTION_LABELS` автоматически.

**Tech Stack:** Python, httpx, re; тесты pytest + respx.

**Spec:** `docs/superpowers/specs/2026-06-12-baranovichi-provider-design.md`. Без TDD по требованию пользователя.

---

### Task 1: Константы направлений в providers/base.py

**Files:**
- Modify: `providers/base.py`

- [ ] **Step 1: После `DIRECTION_BOBR_MNSK` добавить**

```python
DIRECTION_MNSK_BARAN = "mnsk_baran"
DIRECTION_BARAN_MNSK = "baran_mnsk"
```

и в `DIRECTION_LABELS`:

```python
    DIRECTION_MNSK_BARAN: "Минск → Барановичи",
    DIRECTION_BARAN_MNSK: "Барановичи → Минск",
```

### Task 2: providers/baranovichi_express.py

**Files:**
- Create: `providers/baranovichi_express.py`

- [ ] **Step 1: Полный код провайдера**

```python
import re

import httpx

from .base import DIRECTION_BARAN_MNSK, DIRECTION_MNSK_BARAN, Trip

_TIME_RE = re.compile(r"tickets-way__point-time[^>]*>\s*(\d{1,2}:\d{2})")
_ROUTE_RE = re.compile(r"tickets-item__way-mini[^>]*>\s*([^<]+)")
_SEATS_RE = re.compile(r"Свободно мест:\s*(\d+)")
_PRICE_RE = re.compile(r"<b>\s*([\d.]+)\s*руб")


class BaranovichiExpressProvider:
    name = "baranovichi_express"
    display_name = "Барановичи Экспресс"
    url = "https://tickets.baranovichi-express.by/tickets/search"
    # (pickup, destination): 1 = Барановичи, 2 = Минск
    directions = {
        DIRECTION_MNSK_BARAN: ("2", "1"),
        DIRECTION_BARAN_MNSK: ("1", "2"),
    }

    async def get_trips(
        self,
        client: httpx.AsyncClient,
        date: str,
        direction: str,
    ) -> list[Trip]:
        pickup, destination = self.directions[direction]
        params = {
            "pickup": pickup,
            "destination": destination,
            "seats_limit": "1",
            "date_of_journey": f"{date[8:10]}.{date[5:7]}.{date[0:4]}",
        }
        resp = await client.get(self.url, params=params)
        resp.raise_for_status()

        trips: list[Trip] = []
        for chunk in resp.text.split("<article")[1:]:
            head, _, body = chunk.partition(">")
            if "tickets-item" not in head:
                continue
            time_m = _TIME_RE.search(body)
            seats_m = _SEATS_RE.search(body)
            if time_m is None or seats_m is None:
                continue
            hh, mm = time_m.group(1).split(":")
            departure = f"{int(hh):02d}:{mm}"  # «7:00» → «07:00», иначе фильтр окна ломается
            route_m = _ROUTE_RE.search(body)
            price_m = _PRICE_RE.search(body)
            trips.append(Trip(
                trip_id=f"{date}_{departure}",
                provider=self.name,
                route=route_m.group(1).strip() if route_m else "Минск ↔ Барановичи",
                date=date,
                departure_time=departure,
                free_seats=int(seats_m.group(1)),
                price=float(price_m.group(1)) if price_m else 0.0,
                currency="руб.",
            ))
        return trips
```

### Task 3: Атлас + регистрация + промпт

**Files:**
- Modify: `providers/atlasbus.py` (imports + directions)
- Modify: `providers/__init__.py`
- Modify: `llm/prompt.py`

- [ ] **Step 1: atlasbus.py — в импорт из .base добавить `DIRECTION_BARAN_MNSK, DIRECTION_MNSK_BARAN`, в `directions`:**

```python
        DIRECTION_MNSK_BARAN: ("c625144", "c630429"),
        DIRECTION_BARAN_MNSK: ("c630429", "c625144"),
```

- [ ] **Step 2: providers/__init__.py — импорт и регистрация**

```python
from .baranovichi_express import BaranovichiExpressProvider
# в PROVIDERS:
    "baranovichi_express": BaranovichiExpressProvider(),
```

- [ ] **Step 3: llm/prompt.py — первая строка и версия**

```python
LLM_SESSION_VERSION = "2026-06-12-baranovichi-v1"
# первая строка parts:
        "Ты — ассистент Telegram-бота, который отслеживает свободные места "
        "в маршрутках между Могилёвом, Минском, Бобруйском и Барановичами.",
```

### Task 4: Тесты

**Files:**
- Modify: `tests/test_providers.py`

- [ ] **Step 1: Импорты — добавить `DIRECTION_BARAN_MNSK, DIRECTION_MNSK_BARAN` в импорт из base, `from providers.baranovichi_express import BaranovichiExpressProvider`**

- [ ] **Step 2: Дополнить `test_direction_constants`**

```python
    assert DIRECTION_MNSK_BARAN == "mnsk_baran"
    assert DIRECTION_BARAN_MNSK == "baran_mnsk"
    assert "Барановичи" in DIRECTION_LABELS[DIRECTION_MNSK_BARAN]
    assert "Барановичи" in DIRECTION_LABELS[DIRECTION_BARAN_MNSK]
```

- [ ] **Step 3: HTML-фикстура и тесты парсинга (в конец файла, перед registry-тестами по вкусу)**

```python
BARAN_HTML = """
<html><body>
<article class="tickets-item">
  <div class="tickets-item__way-mini">Минск — Барановичи</div>
  <div class="tickets-way__point-time">7:00</div>
  <div class="tickets-way__point-time">9:05</div>
  <div>Свободно мест: 5</div>
  <footer><b>20.00 руб.</b></footer>
</article>
<article class="tickets-item">
  <div class="tickets-item__way-mini">Минск — Барановичи</div>
  <div class="tickets-way__point-time">14:30</div>
  <div class="tickets-way__point-time">16:35</div>
  <div>Свободно мест: 0</div>
  <footer><b>20.00 руб.</b></footer>
</article>
<article class="tickets-item">
  <div class="tickets-item__way-mini">Минск — Барановичи</div>
  <div class="tickets-way__point-time">18:00</div>
  <div class="tickets-way__point-time">20:05</div>
  <div>Свободно мест: 12</div>
  <footer><b>25.50 руб.</b></footer>
</article>
</body></html>
"""

EMPTY_BARAN_HTML = "<html><body><div>Ничего не найдено</div></body></html>"


@pytest.mark.asyncio
@respx.mock
async def test_baranovichi_parses_trips():
    respx.get(url__startswith="https://tickets.baranovichi-express.by/tickets/search").mock(
        return_value=httpx.Response(200, text=BARAN_HTML)
    )
    provider = BaranovichiExpressProvider()
    async with httpx.AsyncClient() as client:
        trips = await provider.get_trips(client, "2026-06-15", DIRECTION_MNSK_BARAN)

    assert len(trips) == 3
    assert trips[0].trip_id == "2026-06-15_07:00"
    assert trips[0].departure_time == "07:00"  # нормализация «7:00»
    assert trips[0].free_seats == 5
    assert trips[0].price == 20.0
    assert trips[0].route == "Минск — Барановичи"
    assert trips[1].free_seats == 0
    assert trips[2].departure_time == "18:00"
    assert trips[2].price == 25.5


@pytest.mark.asyncio
@respx.mock
async def test_baranovichi_query_params():
    route = respx.get(url__startswith="https://tickets.baranovichi-express.by/tickets/search").mock(
        return_value=httpx.Response(200, text=BARAN_HTML)
    )
    provider = BaranovichiExpressProvider()
    async with httpx.AsyncClient() as client:
        await provider.get_trips(client, "2026-06-15", DIRECTION_MNSK_BARAN)
        await provider.get_trips(client, "2026-06-15", DIRECTION_BARAN_MNSK)

    p1 = route.calls[0].request.url.params
    assert p1["pickup"] == "2"
    assert p1["destination"] == "1"
    assert p1["date_of_journey"] == "15.06.2026"
    assert p1["seats_limit"] == "1"
    p2 = route.calls[1].request.url.params
    assert p2["pickup"] == "1"
    assert p2["destination"] == "2"


@pytest.mark.asyncio
@respx.mock
async def test_baranovichi_empty_page():
    respx.get(url__startswith="https://tickets.baranovichi-express.by/tickets/search").mock(
        return_value=httpx.Response(200, text=EMPTY_BARAN_HTML)
    )
    provider = BaranovichiExpressProvider()
    async with httpx.AsyncClient() as client:
        trips = await provider.get_trips(client, "2026-06-15", DIRECTION_BARAN_MNSK)
    assert trips == []


def test_baranovichi_config():
    p = BaranovichiExpressProvider()
    assert p.name == "baranovichi_express"
    assert p.display_name == "Барановичи Экспресс"
    assert p.directions["mnsk_baran"] == ("2", "1")
    assert p.directions["baran_mnsk"] == ("1", "2")
    assert "tickets.baranovichi-express.by" in p.url
```

- [ ] **Step 4: Обновить существующие тесты**

`test_atlasbus_config` — добавить:

```python
    assert p.directions["mnsk_baran"] == ("c625144", "c630429")
    assert p.directions["baran_mnsk"] == ("c630429", "c625144")
```

`test_registry_has_all_providers` — новый ключ и исключение для mg_mnsk-проверки:

```python
    assert set(PROVIDERS.keys()) == {
        "mogilevminsk", "avto_slava", "buspro", "magnitplus", "atlasbus",
        "baranovichi_express",
    }
    ...
        if key not in ("magnitplus", "baranovichi_express"):
            assert "mg_mnsk" in p.directions
```

Новый `test_baranovichi_direction_support` (по образцу бобруйского):

```python
def test_baranovichi_direction_support():
    baran_directions = {"mnsk_baran", "baran_mnsk"}
    for key, provider in PROVIDERS.items():
        if key in ("atlasbus", "baranovichi_express"):
            assert baran_directions <= set(provider.directions)
        else:
            assert set(provider.directions).isdisjoint(baran_directions)
```

- [ ] **Step 5: Прогнать**

Run: `python -m pytest tests/ -q`
Expected: все зелёные.

- [ ] **Step 6: Commit**

```bash
git add providers/ llm/prompt.py tests/test_providers.py docs/superpowers/plans/2026-06-12-baranovichi-provider.md
git commit -m "feat: baranovichi-express provider + Minsk-Baranovichi directions in Atlas"
```
