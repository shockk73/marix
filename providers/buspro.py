import httpx
from .base import (
    DIRECTION_BOBR_MG,
    DIRECTION_MG_BOBR,
    DIRECTION_MG_MNSK,
    DIRECTION_MNSK_MG,
    Trip,
)


class BusProApiProvider:
    name: str
    display_name: str
    company_id: str
    directions: dict[str, tuple[str, str]]
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
                "s[company_id]": self.company_id,
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


class BusProProvider(BusProApiProvider):
    name = "buspro"
    display_name = "Гранд Экспресс"
    company_id = "8"
    directions = {
        DIRECTION_MG_MNSK: ("30", "37"),
        DIRECTION_MNSK_MG: ("37", "30"),
    }


class MagnitPlusProvider(BusProApiProvider):
    name = "magnitplus"
    display_name = "Магнит Плюс"
    company_id = "5"
    directions = {
        DIRECTION_MG_BOBR: ("16", "17"),
        DIRECTION_BOBR_MG: ("17", "16"),
    }
