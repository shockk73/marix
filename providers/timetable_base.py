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
