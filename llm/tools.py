import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

import db as db_module
import scheduler
from providers import PROVIDERS
from providers.base import DIRECTION_LABELS


@dataclass
class ToolContext:
    user_id: int


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_watches",
            "description": "Вернуть все активные отслеживания пользователя.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_watch",
            "description": (
                "Создать отслеживание свободных мест. Можно указать несколько "
                "провайдеров — будет создан отдельный watch на каждого."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "providers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ключи провайдеров",
                    },
                    "direction": {"type": "string",
                                  "description": "mg_mnsk или mnsk_mg"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "time_from": {"type": "string", "description": "HH:MM"},
                    "time_to": {"type": "string", "description": "HH:MM"},
                    "interval_sec": {"type": "integer",
                                     "description": "Минимум 60"},
                },
                "required": ["providers", "direction", "date",
                             "time_from", "time_to", "interval_sec"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_watch",
            "description": "Остановить одно отслеживание по его id.",
            "parameters": {
                "type": "object",
                "properties": {"watch_id": {"type": "integer"}},
                "required": ["watch_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_all_watches",
            "description": "Остановить все активные отслеживания пользователя.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_trips_now",
            "description": (
                "Разовая проверка рейсов в окне времени БЕЗ создания "
                "отслеживания. Возвращает список рейсов и свободные места."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {"type": "string"},
                    "direction": {"type": "string"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "time_from": {"type": "string", "description": "HH:MM"},
                    "time_to": {"type": "string", "description": "HH:MM"},
                },
                "required": ["provider", "direction", "date",
                             "time_from", "time_to"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Задать пользователю уточняющий вопрос с готовыми вариантами "
                "ответа в виде inline-кнопок. Используй когда параметров "
                "недостаточно или нужен выбор."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 8,
                    },
                },
                "required": ["question", "options"],
            },
        },
    },
]


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def _valid_date(s: str) -> bool:
    if not _DATE_RE.match(s):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _valid_time(s: str) -> bool:
    if not _TIME_RE.match(s):
        return False
    try:
        datetime.strptime(s, "%H:%M")
        return True
    except ValueError:
        return False


async def _tool_list_watches(args: dict, ctx: ToolContext) -> str:
    watches = await db_module.get_user_watches(ctx.user_id)
    out = [{
        "id": w["id"],
        "provider": w["provider"],
        "provider_display": PROVIDERS[w["provider"]].display_name,
        "direction": w["direction"],
        "direction_label": DIRECTION_LABELS[w["direction"]],
        "date": w["date"],
        "time_from": w["time_from"],
        "time_to": w["time_to"],
        "interval_sec": w["interval_sec"],
    } for w in watches]
    return json.dumps({"watches": out}, ensure_ascii=False)


async def _tool_create_watch(args: dict, ctx: ToolContext) -> str:
    providers = args.get("providers")
    if not isinstance(providers, list) or not providers:
        return _err("providers: непустой массив ключей")
    unknown = [p for p in providers if p not in PROVIDERS]
    if unknown:
        return _err(
            f"Unknown providers: {unknown}. Available: {list(PROVIDERS.keys())}"
        )

    direction = args.get("direction")
    if direction not in DIRECTION_LABELS:
        return _err(
            f"direction must be one of {list(DIRECTION_LABELS.keys())}"
        )

    date_s = args.get("date", "")
    if not _valid_date(date_s):
        return _err("date must be YYYY-MM-DD")

    tf = args.get("time_from", "")
    tt = args.get("time_to", "")
    if not _valid_time(tf) or not _valid_time(tt):
        return _err("time_from/time_to must be HH:MM")

    interval = args.get("interval_sec")
    if not isinstance(interval, int) or interval < 60:
        return _err("interval_sec must be integer >= 60")

    created_ids = []
    for p in providers:
        wid = await db_module.create_watch(
            user_id=ctx.user_id, provider=p, direction=direction,
            date=date_s, time_from=tf, time_to=tt, interval_sec=interval,
        )
        await scheduler.start_watch({
            "id": wid, "user_id": ctx.user_id, "notified_trips": "[]",
            "provider": p, "direction": direction, "date": date_s,
            "time_from": tf, "time_to": tt, "interval_sec": interval,
        })
        created_ids.append(wid)

    return json.dumps({"created_ids": created_ids}, ensure_ascii=False)


async def _tool_stop_watch(args: dict, ctx: ToolContext) -> str:
    wid = args.get("watch_id")
    if not isinstance(wid, int):
        return _err("watch_id must be integer")
    ok = await db_module.stop_watch(wid, ctx.user_id)
    if ok:
        await scheduler.cancel_watch(wid)
    return json.dumps({"ok": ok, "watch_id": wid})


async def _tool_stop_all_watches(args: dict, ctx: ToolContext) -> str:
    watches = await db_module.get_user_watches(ctx.user_id)
    count = 0
    for w in watches:
        if await db_module.stop_watch(w["id"], ctx.user_id):
            await scheduler.cancel_watch(w["id"])
            count += 1
    return json.dumps({"stopped": count})


async def _tool_check_trips_now(args: dict, ctx: ToolContext) -> str:
    provider_key = args.get("provider")
    if provider_key not in PROVIDERS:
        return _err(f"Unknown provider: {provider_key}")
    direction = args.get("direction")
    if direction not in DIRECTION_LABELS:
        return _err(f"direction must be one of {list(DIRECTION_LABELS.keys())}")
    date_s = args.get("date", "")
    if not _valid_date(date_s):
        return _err("date must be YYYY-MM-DD")
    tf = args.get("time_from", "")
    tt = args.get("time_to", "")
    if not _valid_time(tf) or not _valid_time(tt):
        return _err("time_from/time_to must be HH:MM")

    provider = PROVIDERS[provider_key]
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            trips = await provider.get_trips(client, date_s, direction)
        except Exception as e:
            return _err(f"Provider call failed: {e}")

    filtered = [t for t in trips if tf <= t.departure_time <= tt]
    return json.dumps({
        "trips": [{
            "trip_id": t.trip_id, "departure_time": t.departure_time,
            "free_seats": t.free_seats, "price": t.price, "currency": t.currency,
        } for t in filtered],
    }, ensure_ascii=False)


_HANDLERS = {
    "list_watches": _tool_list_watches,
    "create_watch": _tool_create_watch,
    "stop_watch": _tool_stop_watch,
    "stop_all_watches": _tool_stop_all_watches,
    "check_trips_now": _tool_check_trips_now,
}


async def dispatch_tool(name: str, args: dict, ctx: ToolContext) -> str:
    """Запускает tool и возвращает JSON-строку для tool_result.
    ask_user НЕ обрабатывается здесь — это делает agent."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return _err(f"Unknown tool: {name}")
    return await handler(args, ctx)
