import html
import json
from datetime import datetime, timedelta, timezone

import db as db_module
from config import LLM_HISTORY_SIZE
from llm.prompt import LLM_SESSION_VERSION
from providers import PROVIDERS
from providers.base import DIRECTION_LABELS

_MSK = timezone(timedelta(hours=3))

_CSS = """
body { background: #111418; color: #e6e6e6; font-family: -apple-system,
       'Segoe UI', Roboto, sans-serif; margin: 0; padding: 24px; }
h1 { font-size: 20px; }
.meta { color: #8a939f; font-size: 13px; margin-bottom: 24px; }
.user { background: #1a1f26; border-radius: 12px; padding: 16px;
        margin-bottom: 24px; }
.user h2 { font-size: 16px; margin: 0 0 4px 0; }
.user .sub { color: #8a939f; font-size: 12px; margin-bottom: 12px; }
.watch { color: #9fb6d4; font-size: 13px; margin: 2px 0; }
.msg { max-width: 80%; padding: 8px 12px; border-radius: 10px;
       margin: 6px 0; white-space: pre-wrap; word-break: break-word;
       font-size: 14px; }
.msg.user { background: #2b5278; margin-left: auto; }
.msg.assistant { background: #232a33; }
.msg .ts { display: block; color: #8a939f; font-size: 11px; margin-top: 4px; }
details { margin: 4px 0 4px 12px; font-size: 13px; color: #b9c2cc; }
details pre { background: #0d1014; border-radius: 8px; padding: 8px;
              overflow-x: auto; font-size: 12px; }
.empty { color: #8a939f; font-style: italic; }
.dialog { display: flex; flex-direction: column; }
"""


async def collect_sessions_data() -> list[dict]:
    """Все авторизованные юзеры + имя/слежки/последние сообщения.
    Сортировка: последняя активность сверху."""
    users = await db_module.get_authorized_users()
    out = []
    for u in users:
        uid = u["user_id"]
        out.append({
            **u,
            "name": await db_module.get_user_name(uid),
            "watches": await db_module.get_user_watches(uid),
            "messages": await db_module.get_recent_chat_messages(
                uid, LLM_HISTORY_SIZE),
        })
    def _last_activity(item: dict) -> tuple[float, int]:
        msgs = item["messages"]
        if not msgs:
            return (0.0, 0)
        return (msgs[-1]["created_at"], msgs[-1]["id"])

    out.sort(key=_last_activity, reverse=True)
    return out


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=_MSK).strftime("%Y-%m-%d %H:%M:%S")


def _pretty_json(raw: str) -> str:
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, TypeError):
        return raw or ""


def _render_message(m: dict, tool_names: dict[str, str]) -> str:
    role = m["role"]
    ts = _fmt_ts(m["created_at"])
    chunks: list[str] = []
    if role in ("user", "assistant") and m["content"]:
        chunks.append(
            f'<div class="msg {role}">{html.escape(m["content"])}'
            f'<span class="ts">{role} · {ts}</span></div>'
        )
    if role == "assistant" and m["tool_calls"]:
        try:
            tool_calls = json.loads(m["tool_calls"])
        except json.JSONDecodeError:
            tool_calls = []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "?")
            if tc.get("id"):
                tool_names[tc["id"]] = name
            args = _pretty_json(fn.get("arguments") or "{}")
            chunks.append(
                f"<details><summary>🔧 {html.escape(name)} · {ts}</summary>"
                f"<pre>{html.escape(args)}</pre></details>"
            )
    if role == "tool":
        name = tool_names.get(m["tool_call_id"] or "", "tool")
        body = _pretty_json(m["content"] or "")
        chunks.append(
            f"<details><summary>📋 {html.escape(name)} → результат · {ts}</summary>"
            f"<pre>{html.escape(body)}</pre></details>"
        )
    return "".join(chunks)


def _render_user(u: dict) -> str:
    name = u.get("name") or "без имени"
    watches = u.get("watches") or []
    messages = u.get("messages") or []
    head = (
        f"<h2>{html.escape(name)}</h2>"
        f'<div class="sub">id {u["user_id"]} · роль {html.escape(u["role"])} · '
        f'авторизован {html.escape(str(u.get("authorized_at") or "?"))} · '
        f"активных слежек: {len(watches)}</div>"
    )
    watch_lines = []
    for w in watches:
        provider = PROVIDERS.get(w["provider"])
        p_name = provider.display_name if provider else w["provider"]
        d_label = DIRECTION_LABELS.get(w["direction"], w["direction"])
        watch_lines.append(
            f'<div class="watch">#{w["id"]} {html.escape(p_name)} · '
            f"{html.escape(d_label)} · {w['date']} "
            f"{w['time_from']}–{w['time_to']} · каждые {w['interval_sec']}с</div>"
        )
    if messages:
        tool_names: dict[str, str] = {}
        dialog = "".join(_render_message(m, tool_names) for m in messages)
        dialog = f'<div class="dialog">{dialog}</div>'
    else:
        dialog = '<div class="empty">нет сообщений</div>'
    return f'<div class="user">{head}{"".join(watch_lines)}{dialog}</div>'


def build_sessions_report(users: list[dict], now: datetime) -> str:
    body = "".join(_render_user(u) for u in users)
    return (
        "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<title>Сессии пользователей</title>"
        f"<style>{_CSS}</style></head><body>"
        "<h1>Отчёт по сессиям пользователей</h1>"
        f'<div class="meta">Сгенерирован {now.strftime("%Y-%m-%d %H:%M:%S %Z")} · '
        f"пользователей: {len(users)} · "
        f"версия сессий: {html.escape(LLM_SESSION_VERSION)}</div>"
        f"{body}</body></html>"
    )
