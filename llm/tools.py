import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import httpx
from aiogram.types import BufferedInputFile

import db as db_module
import scheduler
from report import build_sessions_report, collect_sessions_data
from providers import PROVIDERS
from providers.atlas_proxy import (
    get_atlas_proxy_status,
    set_atlas_proxy_target,
)
from providers.base import DIRECTION_LABELS


@dataclass
class ToolContext:
    user_id: int
    schedule_self_callback: Callable[[int, float, str], Awaitable[int]] | None = None
    role: str = "user"
    bot_username: str | None = None
    bot: Any | None = None


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_watches",
            "description": (
                "Вернуть все активные отслеживания пользователя вместе со статусом "
                "последней проверки, ошибками и метриками выполнения."
            ),
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
                    "direction": {
                        "type": "string",
                        "enum": list(DIRECTION_LABELS),
                        "description": "Ключ направления",
                    },
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
                    "direction": {
                        "type": "string",
                        "enum": list(DIRECTION_LABELS),
                    },
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
    {
        "type": "function",
        "function": {
            "name": "show_screen",
            "description": (
                "Показать экран: markdown-текст + сетка inline-кнопок (до 8 рядов "
                "по до 4 кнопки). Клик по кнопке вернёт её value как ответ "
                "пользователя. Используй для выбора из конечного набора: даты, "
                "окна времени, интервалы, подтверждения, карточки слежек с "
                "действиями. Для свободного ввода НЕ используй."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string",
                             "description": "Markdown-текст экрана"},
                    "buttons": {
                        "type": "array",
                        "maxItems": 8,
                        "description": "Ряды кнопок: до 8 рядов, в ряду до 4 кнопок",
                        "items": {
                            "type": "array",
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "value": {"type": "string"},
                                },
                                "required": ["label", "value"],
                            },
                        },
                    },
                },
                "required": ["text", "buttons"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_atlas_proxy_status",
            "description": (
                "Показать безопасный статус Atlas proxy: включён ли proxy, "
                "какая страна/ASN используются. Логин и пароль не возвращаются."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_atlas_proxy_target",
            "description": (
                "Изменить runtime-target для Atlas proxy, если Atlas даёт 429 "
                "или текущий пул плохо работает. Меняет только country/ASN, "
                "секреты из .env не раскрывает. BY/RU запрещены."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "country": {
                        "type": "string",
                        "description": "Двухбуквенный country code, например at, ch, sk, ua, cz, pl.",
                    },
                    "asn": {
                        "type": "string",
                        "description": "Опциональный ASN без AS-prefix, например 8412.",
                    },
                },
                "required": ["country"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_self_callback",
            "description": (
                "Запланировать self-callback: агент сам вернётся в этот чат позже "
                "и выполнит указанную инструкцию. Используй для напоминаний, "
                "отложенных проверок и повторных действий через время."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "delay_seconds": {
                        "type": "integer",
                        "description": "Через сколько секунд выполнить callback. Минимум 10.",
                    },
                    "run_at": {
                        "type": "string",
                        "description": "Альтернатива delay_seconds: ISO-8601 дата/время с timezone.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Что агент должен сделать, когда callback сработает.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_self_callbacks",
            "description": "Показать pending self-callbacks агента для этого пользователя.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


ADMIN_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_invite",
            "description": (
                "Создать одноразовую инвайт-ссылку для нового пользователя. "
                "Только для админа."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_invites",
            "description": (
                "Показать все инвайты и их статус (ожидает / кем использован). "
                "Только для админа."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_sessions_report",
            "description": (
                "Сгенерировать HTML-отчёт по всем пользователям (последние "
                "сообщения с LLM, слежки) и прислать файлом в чат. "
                "Только для админа."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def build_tools_for_role(role: str | None) -> list[dict[str, Any]]:
    if role == "admin":
        return TOOL_SCHEMAS + ADMIN_TOOL_SCHEMAS
    return TOOL_SCHEMAS


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


def _unsupported_providers_for_direction(
    provider_keys: list[str],
    direction: str,
) -> list[str]:
    return [
        provider_key
        for provider_key in provider_keys
        if direction not in PROVIDERS[provider_key].directions
    ]


def _ts_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _parse_run_at(args: dict) -> tuple[float | None, str | None]:
    delay = args.get("delay_seconds")
    run_at = args.get("run_at")
    if delay is None and not run_at:
        return None, "delay_seconds or run_at is required"
    if delay is not None:
        if not isinstance(delay, int) or delay < 10:
            return None, "delay_seconds must be integer >= 10"
        return (datetime.now(timezone.utc) + timedelta(seconds=delay)).timestamp(), None
    if not isinstance(run_at, str):
        return None, "run_at must be an ISO-8601 string"
    try:
        dt = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
    except ValueError:
        return None, "run_at must be ISO-8601"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=3)))
    timestamp = dt.astimezone(timezone.utc).timestamp()
    if timestamp < datetime.now(timezone.utc).timestamp() + 10:
        return None, "run_at must be at least 10 seconds in the future"
    return timestamp, None


def _watch_execution_payload(status: dict | None) -> dict:
    if not status:
        return {"status": "not_started"}
    return {
        "status": status["status"],
        "last_check_started_at": _ts_iso(status["last_check_started_at"]),
        "last_check_finished_at": _ts_iso(status["last_check_finished_at"]),
        "last_success_at": _ts_iso(status["last_success_at"]),
        "last_error_at": _ts_iso(status["last_error_at"]),
        "last_error": status["last_error"],
        "total_trips": status["total_trips"],
        "window_trips": status["window_trips"],
        "available_trips": status["available_trips"],
        "newly_available": status["newly_available"],
        "consecutive_errors": status["consecutive_errors"],
    }


async def _tool_list_watches(args: dict, ctx: ToolContext) -> str:
    watches = await db_module.get_user_watches(ctx.user_id)
    statuses = await db_module.get_watch_statuses([w["id"] for w in watches])
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
        "execution": _watch_execution_payload(statuses.get(w["id"])),
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
    unsupported = _unsupported_providers_for_direction(providers, direction)
    if unsupported:
        return _err(
            f"direction {direction} is not supported by providers: {unsupported}"
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
    if direction not in PROVIDERS[provider_key].directions:
        return _err(
            f"direction {direction} is not supported by provider: {provider_key}"
        )
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


async def _tool_get_atlas_proxy_status(args: dict, ctx: ToolContext) -> str:
    return json.dumps(await get_atlas_proxy_status(), ensure_ascii=False)


async def _tool_set_atlas_proxy_target(args: dict, ctx: ToolContext) -> str:
    country = args.get("country")
    if not isinstance(country, str) or not country:
        return _err("country must be a non-empty string")
    asn = args.get("asn")
    if asn is not None and not isinstance(asn, str):
        return _err("asn must be a string")
    try:
        status = await set_atlas_proxy_target(country, asn)
    except ValueError as e:
        return _err(str(e))
    return json.dumps(status, ensure_ascii=False)


async def _tool_schedule_self_callback(args: dict, ctx: ToolContext) -> str:
    if ctx.schedule_self_callback is None:
        return _err("schedule_self_callback is not available in this context")
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _err("prompt must be a non-empty string")
    run_at, error = _parse_run_at(args)
    if error:
        return _err(error)
    assert run_at is not None
    callback_id = await ctx.schedule_self_callback(ctx.user_id, run_at, prompt.strip())
    return json.dumps({
        "callback_id": callback_id,
        "run_at": _ts_iso(run_at),
        "prompt": prompt.strip(),
    }, ensure_ascii=False)


async def _tool_list_self_callbacks(args: dict, ctx: ToolContext) -> str:
    callbacks = await db_module.get_user_agent_callbacks(ctx.user_id)
    return json.dumps({
        "callbacks": [{
            "id": cb["id"],
            "run_at": _ts_iso(cb["run_at"]),
            "prompt": cb["prompt"],
            "status": cb["status"],
        } for cb in callbacks],
    }, ensure_ascii=False)


async def _tool_create_invite(args: dict, ctx: ToolContext) -> str:
    if ctx.role != "admin":
        return _err("create_invite доступен только админу")
    token = await db_module.create_invite(ctx.user_id)
    link = (f"https://t.me/{ctx.bot_username}?start={token}"
            if ctx.bot_username else None)
    return json.dumps({"token": token, "link": link}, ensure_ascii=False)


async def _tool_list_invites(args: dict, ctx: ToolContext) -> str:
    if ctx.role != "admin":
        return _err("list_invites доступен только админу")
    return json.dumps({"invites": await db_module.list_invites()},
                      ensure_ascii=False)


async def _tool_generate_sessions_report(args: dict, ctx: ToolContext) -> str:
    if ctx.role != "admin":
        return _err("generate_sessions_report доступен только админу")
    if ctx.bot is None:
        return _err("bot недоступен в этом контексте")
    users = await collect_sessions_data()
    now = datetime.now(timezone(timedelta(hours=3)))
    html_str = build_sessions_report(users, now=now)
    doc = BufferedInputFile(
        html_str.encode("utf-8"),
        filename=f"sessions-{now.date().isoformat()}.html",
    )
    await ctx.bot.send_document(ctx.user_id, doc)
    return json.dumps({"sent": True, "users": len(users)}, ensure_ascii=False)


_HANDLERS = {
    "list_watches": _tool_list_watches,
    "create_watch": _tool_create_watch,
    "stop_watch": _tool_stop_watch,
    "stop_all_watches": _tool_stop_all_watches,
    "check_trips_now": _tool_check_trips_now,
    "get_atlas_proxy_status": _tool_get_atlas_proxy_status,
    "set_atlas_proxy_target": _tool_set_atlas_proxy_target,
    "schedule_self_callback": _tool_schedule_self_callback,
    "list_self_callbacks": _tool_list_self_callbacks,
    "create_invite": _tool_create_invite,
    "list_invites": _tool_list_invites,
    "generate_sessions_report": _tool_generate_sessions_report,
}


async def dispatch_tool(name: str, args: dict, ctx: ToolContext) -> str:
    """Запускает tool и возвращает JSON-строку для tool_result.
    ask_user и show_screen НЕ обрабатываются здесь — это делает agent."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return _err(f"Unknown tool: {name}")
    return await handler(args, ctx)
