import json
from typing import Any


def to_openai_messages(rows: list[dict]) -> list[dict[str, Any]]:
    """Преобразует записи из chat_messages в формат сообщений OpenAI Chat Completions.

    Тонкости совместимости с разными моделями через OpenRouter:
    - assistant.content не должен быть null когда есть tool_calls — некоторые
      провайдеры возвращают 400. Подставляем пустую строку.
    - tool message требует name некоторым моделям. Достаём его из соответствующего
      tool_call в предшествующем assistant message.
    """
    out: list[dict[str, Any]] = []
    tool_call_names: dict[str, str] = {}

    for r in rows:
        role = r["role"]
        if role == "user":
            out.append({"role": "user", "content": r["content"] or ""})
        elif role == "assistant":
            content = r["content"] if r["content"] is not None else ""
            msg: dict[str, Any] = {"role": "assistant", "content": content}
            if r["tool_calls"]:
                tcs = json.loads(r["tool_calls"])
                msg["tool_calls"] = tcs
                for tc in tcs:
                    fn = tc.get("function") or {}
                    if tc.get("id") and fn.get("name"):
                        tool_call_names[tc["id"]] = fn["name"]
            out.append(msg)
        elif role == "tool":
            entry: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": r["tool_call_id"],
                "content": r["content"] or "",
            }
            name = tool_call_names.get(r["tool_call_id"])
            if name:
                entry["name"] = name
            out.append(entry)
    return out
