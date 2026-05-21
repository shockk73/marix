import pytest_asyncio

import db as db_module


@pytest_asyncio.fixture
async def tmp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    await db_module.init_db()
    yield db_path
