import asyncio

import httpx
from .base import (
    DIRECTION_BOBR_MG,
    DIRECTION_BOBR_MNSK,
    DIRECTION_MG_BOBR,
    DIRECTION_MG_MNSK,
    DIRECTION_MNSK_BOBR,
    DIRECTION_MNSK_MG,
    Trip,
)
from .atlas_proxy import get_effective_atlas_proxy


class AtlasBusProvider:
    name = "atlasbus"
    display_name = "Атласбус"
    directions = {
        DIRECTION_MG_MNSK: ("c625665", "c625144"),
        DIRECTION_MNSK_MG: ("c625144", "c625665"),
        DIRECTION_MG_BOBR: ("c625665", "c630468"),
        DIRECTION_BOBR_MG: ("c630468", "c625665"),
        DIRECTION_MNSK_BOBR: ("c625144", "c630468"),
        DIRECTION_BOBR_MNSK: ("c630468", "c625144"),
    }
    _url = "https://atlasbus.by/api/search"
    _max_attempts = 10
    _retry_statuses = {429}

    async def get_trips(
        self,
        client: httpx.AsyncClient,
        date: str,
        direction: str,
    ) -> list[Trip]:
        from_id, to_id = self.directions[direction]
        params = {
            "from_id": from_id,
            "to_id": to_id,
            "calendar_width": "1",
            "date": date,
            "passengers": "1",
            "operatorId": "",
        }
        headers = {
            "Referer": "https://atlasbus.by/",
            "Origin": "https://atlasbus.by",
        }

        resp = await self._request_with_retries(client, params, headers)

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

    async def _request_with_retries(
        self,
        client: httpx.AsyncClient,
        params: dict[str, str],
        headers: dict[str, str],
    ) -> httpx.Response:
        last_response: httpx.Response | None = None
        for attempt in range(1, self._max_attempts + 1):
            proxy = await get_effective_atlas_proxy()
            if proxy:
                async with httpx.AsyncClient(
                    proxy=proxy,
                    timeout=client.timeout,
                    headers=client.headers,
                ) as proxied:
                    resp = await proxied.get(self._url, params=params, headers=headers)
            else:
                resp = await client.get(self._url, params=params, headers=headers)

            if resp.status_code not in self._retry_statuses:
                return resp
            last_response = resp
            if attempt < self._max_attempts:
                await asyncio.sleep(min(0.2 * attempt, 1.0))

        assert last_response is not None
        return last_response
