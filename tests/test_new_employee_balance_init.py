"""新员工入职额度初始化测试"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── init_employee_balance 单元测试 ──

def test_init_hourly_employee_always_full_quota(monkeypatch, tmp_path):
    """小时工无论哪天入职都给满 default_quota 天。"""
    from leaveadmin import dingtalk_ops
    from leaveadmin import database as db

    db_path = tmp_path / "test_hourly.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(dingtalk_ops, "DB_PATH", db_path)
    monkeypatch.setattr(dingtalk_ops, "_current_period_sync", lambda: "2026-06")

    async def scenario():
        await db.init_db()
        days = await dingtalk_ops.init_employee_balance("u001", "hourly", 5, "2026-06-05T00:00:00Z")
        assert days == 5

        async with db.aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute("SELECT balance FROM leave_balances WHERE userid='u001' AND period='2026-06'")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 500  # 5 × 100

    asyncio.run(scenario())


def test_init_employee_before_10th_gets_half_quota(monkeypatch, tmp_path):
    """10 号前入职的非小时工，给 default_quota / 2。"""
    from leaveadmin import dingtalk_ops
    from leaveadmin import database as db

    db_path = tmp_path / "test_half.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(dingtalk_ops, "DB_PATH", db_path)
    monkeypatch.setattr(dingtalk_ops, "_current_period_sync", lambda: "2026-06")

    async def scenario():
        await db.init_db()
        days = await dingtalk_ops.init_employee_balance("u002", "other", 4, "2026-06-08T00:00:00Z")
        assert days == 2.0

        async with db.aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute("SELECT balance FROM leave_balances WHERE userid='u002' AND period='2026-06'")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 200  # 2 × 100

    asyncio.run(scenario())


def test_init_employee_on_or_after_10th_gets_zero(monkeypatch, tmp_path):
    """10 号及之后入职的非小时工，不给额度。"""
    from leaveadmin import dingtalk_ops
    from leaveadmin import database as db

    db_path = tmp_path / "test_zero.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(dingtalk_ops, "DB_PATH", db_path)
    monkeypatch.setattr(dingtalk_ops, "_current_period_sync", lambda: "2026-06")

    async def scenario():
        await db.init_db()
        days = await dingtalk_ops.init_employee_balance("u003", "six_day", 6, "2026-06-10T00:00:00Z")
        assert days == 0.0

        async with db.aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute("SELECT balance FROM leave_balances WHERE userid='u003' AND period='2026-06'")
            row = await cursor.fetchone()
            assert row is None  # 0天不建行

    asyncio.run(scenario())


def test_init_employee_without_hired_date_gets_zero(monkeypatch, tmp_path):
    """入职日期缺失时安全兜底，不给额度。"""
    from leaveadmin import dingtalk_ops
    from leaveadmin import database as db

    db_path = tmp_path / "test_nodate.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(dingtalk_ops, "DB_PATH", db_path)
    monkeypatch.setattr(dingtalk_ops, "_current_period_sync", lambda: "2026-06")

    async def scenario():
        await db.init_db()
        days = await dingtalk_ops.init_employee_balance("u004", "other", 4, None)
        assert days == 0.0

        days2 = await dingtalk_ops.init_employee_balance("u005", "other", 4, "")
        assert days2 == 0.0

    asyncio.run(scenario())


# ── fetch_user_detail 单元测试 ──

def test_fetch_user_detail_prioritizes_hired_date(monkeypatch):
    """hired_date 存在时优先返回 hired_date。"""
    from leaveadmin import dingtalk_ops
    import httpx

    class FakeResponse:
        def json(self):
            return {
                "errcode": 0,
                "result": {
                    "hired_date": "2026-06-03",
                    "create_time": "2025-09-22T01:16:03.000Z",
                },
            }

    class FakeClient:
        def __init__(self, timeout=None):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(dingtalk_ops.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(dingtalk_ops.fetch_user_detail("token", "u001"))
    assert result == "2026-06-03"


def test_fetch_user_detail_falls_back_to_create_time(monkeypatch):
    """hired_date 缺失时回退到 create_time。"""
    from leaveadmin import dingtalk_ops

    class FakeResponse:
        def json(self):
            return {
                "errcode": 0,
                "result": {
                    "hired_date": "",
                    "create_time": "2025-09-22T01:16:03.000Z",
                },
            }

    class FakeClient:
        def __init__(self, timeout=None):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(dingtalk_ops.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(dingtalk_ops.fetch_user_detail("token", "u002"))
    assert result == "2025-09-22"


# ── upsert_employee 返回 is_new ──

def test_upsert_employee_returns_is_new(monkeypatch, tmp_path):
    """首次插入返回 (True, userid)，重复返回 (False, userid)。"""
    from leaveadmin import database as db

    db_path = tmp_path / "test_upsert.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    async def scenario():
        await db.init_db()
        emp = {"userid": "u001", "name": "新员工", "category": "hourly", "default_quota": 5}

        is_new, uid = await db.upsert_employee(emp)
        assert is_new is True
        assert uid == "u001"

        is_new2, uid2 = await db.upsert_employee(emp)
        assert is_new2 is False
        assert uid2 == "u001"

    asyncio.run(scenario())


def test_upsert_employee_stores_hired_date(monkeypatch, tmp_path):
    """hired_date 写入并持久化。"""
    from leaveadmin import database as db

    db_path = tmp_path / "test_hired.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    async def scenario():
        await db.init_db()
        emp = {"userid": "u001", "name": "新员工", "category": "other", "default_quota": 4, "hired_date": "2026-06-03"}

        await db.upsert_employee(emp)
        async with db.aiosqlite.connect(db_path) as conn:
            db.aiosqlite.Row = lambda cursor, row: dict(zip([d[0] for d in cursor.description], row))
            cursor = await conn.execute("SELECT hired_date FROM employees WHERE userid='u001'")
            row = await cursor.fetchone()
            assert row[0] == "2026-06-03"

    asyncio.run(scenario())
