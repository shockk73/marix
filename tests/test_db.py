import pytest
import pytest_asyncio

import db as db_module


@pytest_asyncio.fixture
async def tmp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    await db_module.init_db()
    yield db_path


@pytest.mark.asyncio
async def test_create_and_get_watch(tmp_db):
    watch_id = await db_module.create_watch(
        user_id=123,
        provider="mogilevminsk",
        direction="mg_mnsk",
        date="2026-05-24",
        time_from="11:00",
        time_to="23:00",
        interval_sec=120,
    )
    assert watch_id == 1
    watches = await db_module.get_active_watches()
    assert len(watches) == 1
    w = watches[0]
    assert w["user_id"] == 123
    assert w["provider"] == "mogilevminsk"
    assert w["direction"] == "mg_mnsk"
    assert w["notified_trips"] == "[]"


@pytest.mark.asyncio
async def test_stop_watch(tmp_db):
    watch_id = await db_module.create_watch(
        user_id=456, provider="buspro", direction="mg_mnsk",
        date="2026-05-24", time_from="10:00", time_to="20:00", interval_sec=60,
    )
    result = await db_module.stop_watch(watch_id, 456)
    assert result is True
    watches = await db_module.get_active_watches()
    assert len(watches) == 0


@pytest.mark.asyncio
async def test_stop_watch_wrong_user(tmp_db):
    watch_id = await db_module.create_watch(
        user_id=789, provider="atlasbus", direction="mg_mnsk",
        date="2026-05-24", time_from="10:00", time_to="20:00", interval_sec=60,
    )
    result = await db_module.stop_watch(watch_id, 999)
    assert result is False
    watches = await db_module.get_active_watches()
    assert len(watches) == 1


@pytest.mark.asyncio
async def test_update_notified_trips(tmp_db):
    watch_id = await db_module.create_watch(
        user_id=123, provider="mogilevminsk", direction="mg_mnsk",
        date="2026-05-24", time_from="11:00", time_to="23:00", interval_sec=120,
    )
    await db_module.update_notified_trips(watch_id, ["trip_1", "trip_2"])
    watches = await db_module.get_active_watches()
    import json
    assert json.loads(watches[0]["notified_trips"]) == ["trip_1", "trip_2"]


@pytest.mark.asyncio
async def test_get_user_watches(tmp_db):
    await db_module.create_watch(123, "mogilevminsk", "mg_mnsk", "2026-05-24", "11:00", "23:00", 120)
    await db_module.create_watch(123, "atlasbus", "mg_mnsk", "2026-05-24", "11:00", "23:00", 60)
    await db_module.create_watch(456, "buspro", "mg_mnsk", "2026-05-24", "11:00", "23:00", 60)
    watches = await db_module.get_user_watches(123)
    assert len(watches) == 2
    assert all(w["user_id"] == 123 for w in watches)


@pytest.mark.asyncio
async def test_unauthorized_by_default(tmp_db):
    assert await db_module.is_authorized(111) is False


@pytest.mark.asyncio
async def test_authorize_and_check(tmp_db):
    await db_module.authorize_user(222)
    assert await db_module.is_authorized(222) is True
    assert await db_module.is_authorized(223) is False


@pytest.mark.asyncio
async def test_authorize_user_idempotent(tmp_db):
    await db_module.authorize_user(333)
    await db_module.authorize_user(333)
    assert await db_module.is_authorized(333) is True


@pytest.mark.asyncio
async def test_failed_attempts_default_zero(tmp_db):
    assert await db_module.get_failed_attempts(444) == 0
    assert await db_module.is_banned(444) is False


@pytest.mark.asyncio
async def test_increment_failed_attempts(tmp_db):
    assert await db_module.increment_failed_attempts(555) == 1
    assert await db_module.increment_failed_attempts(555) == 2
    assert await db_module.is_banned(555) is False
    assert await db_module.increment_failed_attempts(555) == 3
    assert await db_module.is_banned(555) is True


@pytest.mark.asyncio
async def test_failed_attempts_isolated_per_user(tmp_db):
    await db_module.increment_failed_attempts(666)
    await db_module.increment_failed_attempts(666)
    await db_module.increment_failed_attempts(777)
    assert await db_module.get_failed_attempts(666) == 2
    assert await db_module.get_failed_attempts(777) == 1
