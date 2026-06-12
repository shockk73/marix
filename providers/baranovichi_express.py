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
