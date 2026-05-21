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
    date: str
    departure_time: str
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
