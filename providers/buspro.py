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
