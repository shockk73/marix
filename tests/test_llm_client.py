import json

import httpx
import pytest
import respx

from llm.client import OpenRouterClient


@pytest.fixture
def mock_openrouter():
    with respx.mock(base_url="https://openrouter.ai/api/v1", assert_all_called=False) as m:
        yield m


@pytest.mark.asyncio
async def test_chat_completion_returns_message(mock_openrouter):
    mock_openrouter.post("/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{
                "message": {"role": "assistant", "content": "привет"},
                "finish_reason": "stop",
            }],
        }),
    )
    client = OpenRouterClient(api_key="k", model="m", base_url="https://openrouter.ai/api/v1")
    msg = await client.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
    )
    assert msg["role"] == "assistant"
    assert msg["content"] == "привет"
    await client.close()


@pytest.mark.asyncio
async def test_chat_completion_sends_correct_payload(mock_openrouter):
    route = mock_openrouter.post("/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }),
    )
    client = OpenRouterClient(api_key="my-key", model="x/y", base_url="https://openrouter.ai/api/v1")
    await client.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
    )
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer my-key"
    body = json.loads(sent.content)
    assert body["model"] == "x/y"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["tools"][0]["function"]["name"] == "f"
    await client.close()


@pytest.mark.asyncio
async def test_chat_completion_returns_tool_calls(mock_openrouter):
    mock_openrouter.post("/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "list_watches", "arguments": "{}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }),
    )
    client = OpenRouterClient(api_key="k", model="m", base_url="https://openrouter.ai/api/v1")
    msg = await client.chat_completion(messages=[], tools=[])
    assert msg["tool_calls"][0]["function"]["name"] == "list_watches"
    await client.close()


@pytest.mark.asyncio
async def test_chat_completion_retries_on_5xx(mock_openrouter):
    route = mock_openrouter.post("/chat/completions").mock(
        side_effect=[
            httpx.Response(503, text="bad gateway"),
            httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            }),
        ],
    )
    client = OpenRouterClient(
        api_key="k", model="m",
        base_url="https://openrouter.ai/api/v1",
        retry_delay=0.0,
    )
    msg = await client.chat_completion(messages=[], tools=[])
    assert msg["content"] == "ok"
    assert route.call_count == 2
    await client.close()


@pytest.mark.asyncio
async def test_chat_completion_raises_after_retry(mock_openrouter):
    mock_openrouter.post("/chat/completions").mock(
        return_value=httpx.Response(500, text="oops"),
    )
    client = OpenRouterClient(
        api_key="k", model="m",
        base_url="https://openrouter.ai/api/v1",
        retry_delay=0.0,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.chat_completion(messages=[], tools=[])
    await client.close()


@pytest.mark.asyncio
async def test_transcribe_uses_stt_model(mock_openrouter):
    route = mock_openrouter.post("/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant",
                                     "content": "привет это транскрипция"}}],
        }),
    )
    client = OpenRouterClient(
        api_key="k", model="main/model",
        base_url="https://openrouter.ai/api/v1",
    )
    text = await client.transcribe(
        stt_model="mistralai/voxtral-mini-transcribe",
        audio_bytes=b"\x00\x01\x02", audio_format="ogg",
    )
    assert text == "привет это транскрипция"
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "mistralai/voxtral-mini-transcribe"
    user_msg = body["messages"][-1]
    parts = user_msg["content"]
    assert any(p.get("type") == "input_audio" for p in parts)
    await client.close()
