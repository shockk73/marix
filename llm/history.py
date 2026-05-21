import json
from typing import Any


def to_openai_messages(rows: list[dict]) -> list[dict[str, Any]]:
    """Преобразует записи из chat_messages в формат сообщений OpenAI Chat Completions."""
    out: list[dict[str, Any]] = []
    for r in rows:
        role = r["role"]
        if role == "user":
            out.append({"role": "user", "content": r["content"]})
        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": r["content"]}
            if r["tool_calls"]:
                msg["tool_calls"] = json.loads(r["tool_calls"])
            out.append(msg)
        elif role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": r["tool_call_id"],
                "content": r["content"],
            })
    return out
