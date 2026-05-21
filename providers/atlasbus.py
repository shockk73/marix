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
