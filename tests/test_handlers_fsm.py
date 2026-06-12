"""Застрявший FSM-мастер /watch не должен глотать сообщения молча."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import handlers
from handlers import on_text_fallback


class FakeState:
    def __init__(self, current: str | None):
        self._state = current
        self.cleared = False

    async def get_state(self):
        return self._state

    async def clear(self):
        self.cleared = True
        self._state = None


class FakeMessage:
    def __init__(self, text: str):
        self.text = text
        self.from_user = SimpleNamespace(id=1, first_name="Дима",
                                         username=None)
        self.answers: list[str] = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


@pytest.mark.asyncio
async def test_text_during_fsm_state_breaks_out_to_agent(monkeypatch):
    agent = SimpleNamespace(run_turn=AsyncMock())
    monkeypatch.setattr(handlers, "_agent", agent)
    state = FakeState("WatchForm:provider")
    message = FakeMessage("ты тут марикс?")

    await on_text_fallback(message, state)

    # мастер сброшен, сообщение ушло агенту, а не проглочено
    assert state.cleared is True
    agent.run_turn.assert_awaited_once()
    assert agent.run_turn.await_args.kwargs["text"] == "ты тут марикс?"


@pytest.mark.asyncio
async def test_text_without_fsm_state_goes_to_agent(monkeypatch):
    agent = SimpleNamespace(run_turn=AsyncMock())
    monkeypatch.setattr(handlers, "_agent", agent)
    state = FakeState(None)
    message = FakeMessage("привет")

    await on_text_fallback(message, state)

    assert state.cleared is False
    agent.run_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_command_text_still_skipped(monkeypatch):
    agent = SimpleNamespace(run_turn=AsyncMock())
    monkeypatch.setattr(handlers, "_agent", agent)
    state = FakeState(None)
    message = FakeMessage("/watch")

    await on_text_fallback(message, state)

    agent.run_turn.assert_not_awaited()
