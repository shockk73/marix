import asyncio
import json
import logging
from typing import Any

import httpx

from db import get_active_watches, update_notified_trips
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
