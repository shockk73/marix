"""Общая логика бронирования: используется планировщиком (auto-режим),
кнопками в уведомлениях (confirm/rebook) и LLM-тулзой book_trip_now."""
import asyncio
import logging

import db as db_module
import scheduler
from providers.base import DIRECTION_LABELS
from providers.baranovichi_session import BOOKER, InvalidCredentials

logger = logging.getLogger(__name__)

_booking_locks: dict[str, asyncio.Lock] = {}


def _booking_lock(user_id: int, goal_id: str | None) -> asyncio.Lock:
    """Один замок на цель (или на юзера, если бронь без цели)."""
    key = goal_id or f"user:{user_id}"
    lock = _booking_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _booking_locks[key] = lock
    return lock


def in_pref_window(watch: dict | None, departure_time: str) -> bool:
    """True, если у watch нет приоритетного окна или время в него попадает."""
    if not watch:
        return True
    pref_from = watch.get("pref_time_from")
    pref_to = watch.get("pref_time_to")
    if not (pref_from and pref_to):
        return True
    return pref_from <= departure_time <= pref_to


async def execute_booking(
    user_id: int,
    date: str,
    direction: str,
    departure_time: str,
    watch: dict | None = None,
    rebook_from: dict | None = None,
    skip_cancel_watch_id: int | None = None,
) -> tuple[str, str, list[int]]:
    """Бронирует место и ведёт сопутствующее состояние (bookings, слежки).

    Возвращает (status, текст_для_юзера, остановленные_watch_id).
    status: booked | already_booked | gone | creds | error.

    Бронь по одной цели НЕ конкурентна: per-goal asyncio.Lock плюс повторная
    проверка активной брони внутри лока — тик шедулера, клик по кнопке,
    LLM-тулза или второй бронирующий провайдер не сделают две брони на одну
    цель. Цель отмены при перебронировании тоже берётся заново внутри лока:
    переданный rebook_from мог устареть в гонке.
    """
    goal_id = watch.get("goal_id") if watch else None
    async with _booking_lock(user_id, goal_id):
        if goal_id:
            current = await db_module.get_active_goal_booking(user_id, goal_id)
            if rebook_from is not None:
                rebook_from = current
            elif current is not None:
                return ("already_booked",
                        f"Уже забронировано {current['departure_time']} "
                        f"({current['date']}) ✅",
                        [])
        if (rebook_from is not None
                and rebook_from["departure_time"] == departure_time):
            return ("already_booked",
                    f"Уже забронировано {departure_time} ✅", [])
        return await _execute_booking_locked(
            user_id=user_id, date=date, direction=direction,
            departure_time=departure_time, watch=watch, goal_id=goal_id,
            rebook_from=rebook_from,
            skip_cancel_watch_id=skip_cancel_watch_id,
        )


async def _execute_booking_locked(
    user_id: int,
    date: str,
    direction: str,
    departure_time: str,
    watch: dict | None,
    goal_id: str | None,
    rebook_from: dict | None,
    skip_cancel_watch_id: int | None,
) -> tuple[str, str, list[int]]:
    """Тело брони. Слежки цели останавливаются в БД ДО отправки текста
    (защита от двойной брони при рестарте); таска skip_cancel_watch_id не
    отменяется — вызывающий poll-цикл завершает себя сам."""
    try:
        result = await BOOKER.book(user_id, date, direction, departure_time)
    except InvalidCredentials:
        if watch and (watch.get("autobook") or "off") != "off":
            await db_module.set_watch_autobook(watch["id"], "off")
            return ("creds",
                    "🚫 Логин на baranovichi-express не работает — автобронь "
                    "выключена. Проверь аккаунт («подключи бронь») и включи снова.",
                    [])
        return ("creds",
                "🚫 Аккаунт сайта не подключён или креды не работают. "
                "Напиши «подключи бронь».",
                [])
    except Exception as e:
        logger.exception("Booking %s %s %s failed: %s",
                         date, direction, departure_time, e)
        return ("error",
                f"🚫 Бронь не удалась ({type(e).__name__}). Попробую обычные "
                f"уведомления, бронь можно повторить вручную.",
                [])

    if result.status != "booked":
        return ("gone", "😔 Не успели — место уже заняли."
                + (" Продолжаю следить." if watch else ""), [])

    booking_id = await db_module.create_booking(
        user_id=user_id, date=date, direction=direction,
        departure_time=departure_time,
        ticket_id=result.ticket_id, trip_id=result.trip_id,
        watch_id=watch["id"] if watch else None,
        goal_id=goal_id,
    )

    lines = [
        f"✅ Забронировал рейс {departure_time}, "
        f"{DIRECTION_LABELS[direction]}, {date} (бронь #{booking_id}).",
        "Оплата водителю при посадке; он сверит твой номер телефона.",
    ]

    if rebook_from:
        canceled_old = False
        try:
            canceled_old = await BOOKER.cancel(
                user_id, rebook_from["ticket_id"], rebook_from["trip_id"])
        except Exception as e:
            logger.exception("Cancel of old booking %s failed: %s",
                             rebook_from["id"], e)
        if canceled_old:
            await db_module.mark_booking_canceled(rebook_from["id"])
            lines.append(f"♻️ Старая бронь {rebook_from['departure_time']} отменена.")
        else:
            lines.append(
                f"⚠️ Старую бронь {rebook_from['departure_time']} отменить "
                f"не удалось — скажи «покажи брони» и отмени вручную.")

    stopped: list[int] = []
    if watch:
        booked_in_pref = in_pref_window(watch, departure_time)
        if not booked_in_pref and not rebook_from:
            # Бронь вне приоритетного окна: эта слежка остаётся искать
            # слот получше, остальные в цели больше не нужны.
            if goal_id:
                stopped = await db_module.stop_goal_watches(
                    goal_id, except_id=watch["id"])
            lines.append(
                f"👀 Это вне приоритетного окна "
                f"{watch.get('pref_time_from')}–{watch.get('pref_time_to')} — "
                f"продолжаю следить и предложу перебронировать, если появится "
                f"слот получше.")
        else:
            if goal_id:
                stopped = await db_module.stop_goal_watches(goal_id)
            else:
                if await db_module.stop_watch(watch["id"], user_id):
                    stopped = [watch["id"]]
            if len(stopped) > 1:
                lines.append("⏹ Цель достигнута — остановил слежки: "
                             + ", ".join(f"#{i}" for i in stopped) + ".")
            elif stopped:
                lines.append(f"⏹ Слежка #{stopped[0]} остановлена — цель достигнута.")

    for wid in stopped:
        if wid != skip_cancel_watch_id:
            await scheduler.cancel_watch(wid)

    return ("booked", "\n".join(lines), stopped)
