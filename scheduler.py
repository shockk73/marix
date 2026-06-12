import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import db as db_module
from db import (
    get_active_watches,
    mark_watch_check_error,
    mark_watch_check_started,
    mark_watch_check_success,
    update_notified_trips,
)
from providers import PROVIDERS
from providers.base import Trip, DIRECTION_LABELS

logger = logging.getLogger(__name__)

_tasks: dict[int, asyncio.Task] = {}
_bot: Any = None

ALERT_THRESHOLD = 10

_MSK = timezone(timedelta(hours=3))


def _now() -> datetime:
    return datetime.now(_MSK)


class WatchStopped(Exception):
    """Слежка остановлена изнутри итерации (истекла или цель достигнута)."""

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru,en-US;q=0.9,en;q=0.8",
}


def init_scheduler(bot: Any) -> None:
    global _bot
    _bot = bot


async def send_with_retry(bot: Any, chat_id: int, text: str, **kwargs) -> Any:
    """Отправка с одним повтором при флуд-контроле Telegram."""
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except TelegramRetryAfter as e:
        wait = min(e.retry_after, 30)
        logger.warning("Flood control for chat %s, retry in %ss", chat_id, wait)
        await asyncio.sleep(wait)
        return await bot.send_message(chat_id, text, **kwargs)


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


def _backoff_multiplier(consecutive_errors: int) -> int:
    if consecutive_errors == 0:
        return 1
    return min(2 ** (consecutive_errors // 5), 8)


def watch_expired(watch: dict, now: datetime) -> bool:
    """Окно слежки целиком в прошлом — следить больше не за чем."""
    end = datetime.strptime(
        f"{watch['date']} {watch['time_to']}", "%Y-%m-%d %H:%M",
    ).replace(tzinfo=_MSK)
    return now > end


async def start_watch(watch: dict) -> None:
    watch_id = watch["id"]
    if watch_id in _tasks and not _tasks[watch_id].done():
        return
    task = asyncio.create_task(_poll_loop(watch), name=f"watch-{watch_id}")
    task.add_done_callback(_make_done_callback(watch))
    _tasks[watch_id] = task


def _make_done_callback(watch: dict):
    def _cb(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger.critical("Watch %d task died: %s", watch["id"], exc)
        if _bot is not None:
            asyncio.ensure_future(_notify_task_death(watch))
    return _cb


async def _notify_task_death(watch: dict) -> None:
    try:
        await send_with_retry(
            _bot, watch["user_id"],
            f"💥 Слежка #{watch['id']} аварийно остановилась. "
            f"Создай её заново или перезапусти бота.",
        )
    except Exception as e:
        logger.error("Could not notify user about dead watch %d: %s",
                     watch["id"], e)


async def cancel_watch(watch_id: int) -> None:
    task = _tasks.pop(watch_id, None)
    if task and not task.done():
        task.cancel()


async def restore_watches() -> None:
    watches = await get_active_watches()
    for w in watches:
        await start_watch(w)
    logger.info("Restored %d active watches", len(watches))


def _format_watch_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text[:300] if exc.response is not None else ""
        return f"HTTP {exc.response.status_code}: {body}"
    return f"{type(exc).__name__}: {exc}"


async def _poll_loop(watch: dict) -> None:
    state = {
        "notified": set(json.loads(watch["notified_trips"])),
        "consecutive_errors": 0,
    }
    async with httpx.AsyncClient(timeout=15.0, headers=_HTTP_HEADERS) as client:
        while True:
            try:
                await _poll_once(watch, client, state)
            except asyncio.CancelledError:
                break
            except WatchStopped:
                break
            except Exception as exc:
                await _handle_poll_error(watch, state, exc)

            sleep_for = watch["interval_sec"] * _backoff_multiplier(
                state["consecutive_errors"])
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break


async def _poll_once(watch: dict, client: httpx.AsyncClient, state: dict) -> None:
    """Одна итерация проверки. Бросает исключения наружу — их обрабатывает
    _poll_loop через _handle_poll_error."""
    watch_id = watch["id"]
    provider = PROVIDERS[watch["provider"]]

    if watch_expired(watch, _now()):
        await db_module.stop_watch(watch_id, watch["user_id"])
        try:
            await send_with_retry(
                _bot, watch["user_id"],
                f"⏳ Слежка #{watch_id} истекла (окно {watch['date']} "
                f"{watch['time_from']}–{watch['time_to']} прошло) — остановил.",
            )
        except Exception as e:
            logger.warning("Expiry notice for watch %d failed: %s", watch_id, e)
        raise WatchStopped()

    await mark_watch_check_started(watch_id)
    all_trips = await provider.get_trips(client, watch["date"], watch["direction"])
    in_window = filter_trips_in_window(all_trips, watch["time_from"], watch["time_to"])
    newly, updated_notified = compute_newly_available(in_window, state["notified"])
    state["notified"] = updated_notified
    await mark_watch_check_success(
        watch_id=watch_id,
        total_trips=len(all_trips),
        window_trips=len(in_window),
        available_trips=sum(1 for t in in_window if t.free_seats > 0),
        newly_available=len(newly),
    )

    if state["consecutive_errors"] >= ALERT_THRESHOLD:
        try:
            await send_with_retry(
                _bot, watch["user_id"],
                f"✅ Слежка #{watch_id} снова работает.",
            )
        except Exception as e:
            logger.warning("Recovery alert for watch %d failed: %s", watch_id, e)
    state["consecutive_errors"] = 0

    if newly:
        handled = False
        if (watch["provider"] == "baranovichi_express"
                and (watch.get("autobook") or "off") != "off"):
            handled = await _handle_autobook(watch, newly)
        # Сначала уведомление, потом фиксация — упавшая отправка
        # повторится на следующем тике (at-least-once).
        if not handled:
            await _send_notification(watch, newly)
        await update_notified_trips(watch_id, list(state["notified"]))


async def _handle_autobook(watch: dict, newly: list[Trip]) -> bool:
    """Автобронь/предложения брони для baranovichi_express.
    True — уведомление уже отправлено (или намеренно подавлено),
    False — пусть уйдёт обычное уведомление."""
    from booking_flow import execute_booking

    user_id = watch["user_id"]
    goal_id = watch.get("goal_id")
    pref_from = watch.get("pref_time_from")
    pref_to = watch.get("pref_time_to")
    has_pref = bool(pref_from and pref_to)

    def in_pref(t: Trip) -> bool:
        return has_pref and pref_from <= t.departure_time <= pref_to

    booking = await db_module.get_active_goal_booking(user_id, goal_id)
    if booking is not None:
        if not has_pref or (pref_from <= booking["departure_time"] <= pref_to):
            # цель уже достигнута — слежка осталась от гонки, гасим себя
            await db_module.stop_watch(watch["id"], user_id)
            raise WatchStopped()
        candidates = sorted((t for t in newly if in_pref(t)),
                            key=lambda t: t.departure_time)
        if not candidates:
            return True  # бронь уже есть, ничего лучше не появилось — молчим
        rows = [[InlineKeyboardButton(
            text=f"🔁 Перебронировать на {t.departure_time}",
            callback_data=f"bk:r:{watch['id']}:{t.departure_time}",
        )] for t in candidates[:4]]
        await send_with_retry(
            _bot, user_id,
            f"🎯 Появился слот в приоритетном окне {pref_from}–{pref_to}!\n"
            f"Сейчас забронировано {booking['departure_time']}, {watch['date']}. "
            f"Могу перебронировать:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return True

    mode = watch.get("autobook") or "off"
    if mode == "auto":
        candidates = sorted(newly, key=lambda t: (not in_pref(t), t.departure_time))
        candidate = candidates[0]
        status, text, stopped = await execute_booking(
            user_id=user_id, date=watch["date"], direction=watch["direction"],
            departure_time=candidate.departure_time, watch=watch,
            skip_cancel_watch_id=watch["id"],
        )
        if status == "booked":
            await send_with_retry(_bot, user_id, text)
            if watch["id"] in stopped:
                raise WatchStopped()
            return True
        if status == "already_booked":
            # гонка: бронь по цели уже сделана другим путём (кнопка/тулза/
            # другой watch) — молчим, следующий тик увидит бронь и разрулит
            return True
        if status == "gone":
            return True  # место уже ушло — не дёргаем юзера
        if status == "creds":
            try:
                await send_with_retry(_bot, user_id, text)
            except Exception as e:
                logger.warning("Creds alert failed: %s", e)
            return False  # дальше обычное уведомление
        return False  # error: залогировано, уйдёт обычное уведомление

    # mode == "confirm": обычное уведомление + кнопки брони
    candidates = sorted(newly, key=lambda t: (not in_pref(t), t.departure_time))
    rows = [[InlineKeyboardButton(
        text=f"🎫 Забронировать {t.departure_time}",
        callback_data=f"bk:b:{watch['id']}:{t.departure_time}",
    )] for t in candidates[:4]]
    await send_with_retry(
        _bot, user_id, _notification_text(watch, newly),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    return True


async def _handle_poll_error(watch: dict, state: dict, exc: Exception) -> None:
    watch_id = watch["id"]
    await mark_watch_check_error(watch_id, _format_watch_error(exc))
    logger.error("Watch %d error: %s", watch_id, exc)
    state["consecutive_errors"] += 1
    if state["consecutive_errors"] == ALERT_THRESHOLD:
        try:
            await send_with_retry(
                _bot, watch["user_id"],
                f"⚠️ Слежка #{watch_id} спотыкается: "
                f"{_format_watch_error(exc)[:200]}. Продолжаю пытаться, "
                f"но реже.",
            )
        except Exception as e:
            logger.warning("Health alert for watch %d failed: %s", watch_id, e)


def _notification_text(watch: dict, trips: list[Trip]) -> str:
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
    return "\n".join(lines)


async def _send_notification(watch: dict, trips: list[Trip]) -> None:
    await send_with_retry(_bot, watch["user_id"], _notification_text(watch, trips))
