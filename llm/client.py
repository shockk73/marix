import asyncio
import base64
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        timeout: float = 60.0,
        retry_delay: float = 2.0,
    ) -> None:
        self._model = model
        self._retry_delay = retry_delay
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = await self._client.post("/chat/completions", json=payload)
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]
            except httpx.HTTPStatusError as e:
                last_exc = e
                if e.response.status_code < 500 and e.response.status_code != 429:
                    raise
                logger.warning("OpenRouter %s, retrying", e.response.status_code)
            except httpx.RequestError as e:
                last_exc = e
                logger.warning("OpenRouter network error: %s, retrying", e)

            if attempt == 0:
                await asyncio.sleep(self._retry_delay)

        assert last_exc is not None
        raise last_exc

    async def transcribe(
        self,
        stt_model: str,
        audio_bytes: bytes,
        audio_format: str,
    ) -> str:
        """Один STT-вызов через chat/completions с моделью stt_model.
        Возвращает только текст транскрипции."""
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        payload = {
            "model": stt_model,
            "messages": [
                {"role": "system",
                 "content": "Транскрибируй это аудио в текст. Верни только текст без комментариев."},
                {"role": "user", "content": [
                    {"type": "input_audio",
                     "input_audio": {"data": b64, "format": audio_format}},
                ]},
            ],
        }
        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return (data["choices"][0]["message"].get("content") or "").strip()

    async def close(self) -> None:
        await self._client.aclose()
