import json
from dataclasses import dataclass

import pytest

import db as db_module
import scheduler
from llm.tools import TOOL_SCHEMAS, dispatch_tool, ToolContext


@dataclass
class FakeScheduler:
    started: list = None
    cancelled: list = None

    def __post_init__(self):
        self.started = []
        self.cancelled = []

    async def start_watch(self, w):
        self.started.append(w["id"])

    async def cancel_watch(self, wid):
        self.cancelled.append(wid)


@pytest.fixture
def fake_scheduler(monkeypatch):
    fs = FakeScheduler()
    monkeypatch.setattr(scheduler, "start_watch", fs.start_watch)
    monkeypatch.setattr(scheduler, "cancel_watch", fs.cancel_watch)
    return fs


def test_tool_schemas_contains_all_expected_names():
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert names == {
        "list_watches", "create_watch", "stop_watch",
        "stop_all_watches", "check_trips_now", "ask_user", "ask_user_form",
        "show_screen",
        "get_atlas_proxy_status", "set_atlas_proxy_target",
        "schedule_self_callback", "list_self_callbacks",
        "save_baranovichi_credentials", "get_credentials_status",
        "delete_credentials", "list_bookings", "cancel_booking",
        "book_trip_now", "get_baranovichi_stops",
    }


def test_tool_schemas_valid_structure():
    for t in TOOL_SCHEMAS:
        assert t["type"] == "function"
        assert "name" in t["function"]
        assert "description" in t["function"]
        assert "parameters" in t["function"]
        assert t["function"]["parameters"]["type"] == "object"


@pytest.mark.asyncio
async def test_dispatch_list_watches_empty(tmp_db, fake_scheduler):
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("list_watches", {}, ctx)
    assert json.loads(result) == {"watches": []}


@pytest.mark.asyncio
async def test_dispatch_list_watches_returns_active(tmp_db, fake_scheduler):
    wid = await db_module.create_watch(1, "atlasbus", "mg_mnsk", "2026-05-24", "11:00", "23:00", 120)
    await db_module.mark_watch_check_error(wid, "HTTP 429: Too Many Requests")
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("list_watches", {}, ctx)
    data = json.loads(result)
    assert len(data["watches"]) == 1
    assert data["watches"][0]["provider"] == "atlasbus"
    assert data["watches"][0]["execution"]["status"] == "error"
    assert data["watches"][0]["execution"]["consecutive_errors"] == 1
    assert "429" in data["watches"][0]["execution"]["last_error"]


@pytest.mark.asyncio
async def test_dispatch_create_watch_creates_and_starts(tmp_db, fake_scheduler):
    ctx = ToolContext(user_id=1)
    args = {
        "providers": ["atlasbus", "mogilevminsk"],
        "direction": "mg_mnsk",
        "date": "2026-05-24",
        "time_from": "11:00",
        "time_to": "23:00",
        "interval_sec": 120,
    }
    result = await dispatch_tool("create_watch", args, ctx)
    data = json.loads(result)
    assert len(data["created_ids"]) == 2
    assert len(fake_scheduler.started) == 2
    watches = await db_module.get_user_watches(1)
    assert len(watches) == 2


@pytest.mark.asyncio
async def test_dispatch_create_watch_supports_bobruisk_providers(tmp_db, fake_scheduler):
    ctx = ToolContext(user_id=1)
    args = {
        "providers": ["atlasbus", "magnitplus"],
        "direction": "mg_bobr",
        "date": "2026-05-24",
        "time_from": "11:00",
        "time_to": "23:00",
        "interval_sec": 120,
    }
    result = await dispatch_tool("create_watch", args, ctx)
    data = json.loads(result)
    assert len(data["created_ids"]) == 2
    assert len(fake_scheduler.started) == 2
    watches = await db_module.get_user_watches(1)
    assert {w["provider"] for w in watches} == {"atlasbus", "magnitplus"}
    assert {w["direction"] for w in watches} == {"mg_bobr"}


@pytest.mark.asyncio
async def test_dispatch_create_watch_rejects_unsupported_direction(tmp_db, fake_scheduler):
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("create_watch", {
        "providers": ["buspro"], "direction": "mg_bobr",
        "date": "2026-05-24", "time_from": "11:00", "time_to": "23:00",
        "interval_sec": 120,
    }, ctx)
    data = json.loads(result)
    assert "error" in data
    assert "buspro" in data["error"]
    assert len(await db_module.get_user_watches(1)) == 0


@pytest.mark.asyncio
async def test_dispatch_create_watch_invalid_provider(tmp_db, fake_scheduler):
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("create_watch", {
        "providers": ["unknown"], "direction": "mg_mnsk",
        "date": "2026-05-24", "time_from": "11:00", "time_to": "23:00",
        "interval_sec": 120,
    }, ctx)
    data = json.loads(result)
    assert "error" in data
    assert "unknown" in data["error"].lower()


@pytest.mark.asyncio
async def test_dispatch_create_watch_invalid_date(tmp_db, fake_scheduler):
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("create_watch", {
        "providers": ["atlasbus"], "direction": "mg_mnsk",
        "date": "tomorrow", "time_from": "11:00", "time_to": "23:00",
        "interval_sec": 120,
    }, ctx)
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_dispatch_create_watch_invalid_interval(tmp_db, fake_scheduler):
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("create_watch", {
        "providers": ["atlasbus"], "direction": "mg_mnsk",
        "date": "2026-05-24", "time_from": "11:00", "time_to": "23:00",
        "interval_sec": 30,
    }, ctx)
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_dispatch_stop_watch_success(tmp_db, fake_scheduler):
    wid = await db_module.create_watch(1, "atlasbus", "mg_mnsk", "2026-05-24", "11:00", "23:00", 120)
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("stop_watch", {"watch_id": wid}, ctx)
    data = json.loads(result)
    assert data["ok"] is True
    assert wid in fake_scheduler.cancelled
    assert len(await db_module.get_user_watches(1)) == 0


@pytest.mark.asyncio
async def test_dispatch_stop_watch_not_owned(tmp_db, fake_scheduler):
    wid = await db_module.create_watch(2, "atlasbus", "mg_mnsk", "2026-05-24", "11:00", "23:00", 120)
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("stop_watch", {"watch_id": wid}, ctx)
    data = json.loads(result)
    assert data["ok"] is False


@pytest.mark.asyncio
async def test_dispatch_stop_all_watches(tmp_db, fake_scheduler):
    await db_module.create_watch(1, "atlasbus", "mg_mnsk", "2026-05-24", "11:00", "23:00", 120)
    await db_module.create_watch(1, "buspro", "mg_mnsk", "2026-05-24", "11:00", "23:00", 120)
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("stop_all_watches", {}, ctx)
    data = json.loads(result)
    assert data["stopped"] == 2
    assert len(await db_module.get_user_watches(1)) == 0


@pytest.mark.asyncio
async def test_dispatch_check_trips_now(monkeypatch, tmp_db):
    from providers.base import Trip

    async def fake_get_trips(client, date, direction):
        return [
            Trip(trip_id="t1", provider="atlasbus", route="r", date=date,
                 departure_time="12:00", free_seats=5, price=20, currency="BYN"),
            Trip(trip_id="t2", provider="atlasbus", route="r", date=date,
                 departure_time="14:00", free_seats=0, price=20, currency="BYN"),
        ]
    from providers import PROVIDERS
    monkeypatch.setattr(PROVIDERS["atlasbus"], "get_trips", fake_get_trips)

    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("check_trips_now", {
        "provider": "atlasbus", "direction": "mg_mnsk",
        "date": "2026-05-24", "time_from": "11:00", "time_to": "15:00",
    }, ctx)
    data = json.loads(result)
    assert len(data["trips"]) == 2
    assert data["trips"][0]["departure_time"] == "12:00"


@pytest.mark.asyncio
async def test_dispatch_check_trips_now_rejects_unsupported_direction(tmp_db):
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("check_trips_now", {
        "provider": "buspro", "direction": "mg_bobr",
        "date": "2026-05-24", "time_from": "11:00", "time_to": "15:00",
    }, ctx)
    data = json.loads(result)
    assert "error" in data
    assert "buspro" in data["error"]


@pytest.mark.asyncio
async def test_dispatch_atlas_proxy_tools(tmp_db, fake_scheduler, monkeypatch):
    monkeypatch.setattr(
        "providers.atlas_proxy.ATLAS_PROXY",
        "http://login__cr.pl:secret@gw.dataimpulse.com:823",
    )

    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("set_atlas_proxy_target", {
        "country": "at",
        "asn": "8412",
    }, ctx)
    data = json.loads(result)
    assert data["configured"] is True
    assert data["country"] == "at"
    assert data["asn"] == "8412"
    assert data["host"] == "gw.dataimpulse.com"
    assert "secret" not in result
    assert "login" not in result

    status = json.loads(await dispatch_tool("get_atlas_proxy_status", {}, ctx))
    assert status["source"] == "runtime_override"
    assert status["country"] == "at"


@pytest.mark.asyncio
async def test_dispatch_atlas_proxy_rejects_blocked_country(tmp_db, fake_scheduler, monkeypatch):
    monkeypatch.setattr(
        "providers.atlas_proxy.ATLAS_PROXY",
        "http://login__cr.pl:secret@gw.dataimpulse.com:823",
    )
    ctx = ToolContext(user_id=1)
    data = json.loads(await dispatch_tool("set_atlas_proxy_target", {"country": "ru"}, ctx))
    assert "error" in data


@pytest.mark.asyncio
async def test_dispatch_schedule_self_callback(tmp_db, fake_scheduler):
    calls = []

    async def schedule(user_id, run_at, prompt):
        calls.append((user_id, run_at, prompt))
        return 7

    ctx = ToolContext(user_id=1, schedule_self_callback=schedule)
    result = await dispatch_tool("schedule_self_callback", {
        "delay_seconds": 10,
        "prompt": "проверь atlas proxy",
    }, ctx)
    data = json.loads(result)
    assert data["callback_id"] == 7
    assert calls[0][0] == 1
    assert calls[0][2] == "проверь atlas proxy"


@pytest.mark.asyncio
async def test_dispatch_list_self_callbacks(tmp_db, fake_scheduler):
    await db_module.create_agent_callback(1, 1900000000.0, "проверь позже")
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("list_self_callbacks", {}, ctx)
    data = json.loads(result)
    assert len(data["callbacks"]) == 1
    assert data["callbacks"][0]["prompt"] == "проверь позже"


@pytest.mark.asyncio
async def test_dispatch_unknown_tool(tmp_db, fake_scheduler):
    ctx = ToolContext(user_id=1)
    result = await dispatch_tool("nonexistent_xyz", {}, ctx)
    data = json.loads(result)
    assert "error" in data
