import asyncio
import sqlite3
from pathlib import Path


def test_departed_candidate_requires_confirmation_before_disabling(tmp_path, monkeypatch):
    import database

    db_path = tmp_path / "admin.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)

    async def scenario():
        await database.init_db()
        async with database.aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO employees (userid, name, dept_id, dept_name, job_title, category, is_disabled, disabled_reason, updated_at)
                VALUES
                ('u_seen', '仍在职', 'd1', '门店', '员工', 'other', 0, '', '2026-06-29T00:00:00'),
                ('u_missing', '待确认离职', 'd2', '财务信息部', '会计', 'six_day', 0, '', '2026-06-29T00:00:00'),
                ('u_disabled', '已屏蔽', 'd3', '编外', '', 'other', 1, 'manual', '2026-06-29T00:00:00')
                """
            )
            await db.commit()

        result = await database.create_departed_candidates(
            active_userids={"u_seen"},
            source_run_id="run-001",
            reason="通讯录缺席候选",
        )
        assert result["created"] == 1
        assert result["updated"] == 0

        async with database.aiosqlite.connect(database.DB_PATH) as db:
            db.row_factory = database.aiosqlite.Row
            emp = await (await db.execute("SELECT is_disabled, disabled_reason FROM employees WHERE userid='u_missing'")).fetchone()
            assert dict(emp) == {"is_disabled": 0, "disabled_reason": ""}

        candidates = await database.get_departed_candidates(status="pending")
        assert len(candidates) == 1
        assert candidates[0]["userid"] == "u_missing"
        assert candidates[0]["dept_name"] == "财务信息部"

        confirm = await database.confirm_departed_candidate(
            candidates[0]["id"],
            operator="moluu",
            note="人工确认离职",
        )
        assert confirm["ok"] is True

        async with database.aiosqlite.connect(database.DB_PATH) as db:
            db.row_factory = database.aiosqlite.Row
            emp = await (await db.execute("SELECT is_disabled, disabled_reason FROM employees WHERE userid='u_missing'")).fetchone()
            assert dict(emp) == {"is_disabled": 1, "disabled_reason": "sync_absent"}
            audit = await (await db.execute("SELECT action, operator, note, before_status, after_status FROM departed_candidate_audit WHERE userid='u_missing'")).fetchone()
            assert dict(audit) == {
                "action": "confirm",
                "operator": "moluu",
                "note": "人工确认离职",
                "before_status": "pending",
                "after_status": "confirmed",
            }

    asyncio.run(scenario())


def test_scan_departed_candidates_default_pending_and_auto_confirm_leaves_audit(tmp_path, monkeypatch):
    import database
    import dingtalk_ops

    db_path = tmp_path / "admin.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)

    async def fake_pull_active_contacts(upsert=True):
        assert upsert is False
        return {
            "total": 1,
            "seen_userids": {"u_seen"},
            "dept_count": 2,
            "dept_user_counts": {"1": 1},
            "complete": True,
        }

    notify_calls = []

    async def fake_send_notify(title, content):
        notify_calls.append((title, content))
        raise AssertionError("departed scan should not push DingTalk notification on normal scan")

    monkeypatch.setattr(dingtalk_ops, "_pull_active_contacts", fake_pull_active_contacts)
    monkeypatch.setattr(dingtalk_ops, "send_notify", fake_send_notify)

    async def scenario():
        await database.init_db()
        async with database.aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO employees (userid, name, dept_id, dept_name, job_title, category, is_disabled, disabled_reason, updated_at)
                VALUES
                ('u_seen', '仍在职', 'd1', '门店', '员工', 'other', 0, '', '2026-06-29T00:00:00'),
                ('u_missing', '待确认离职', 'd2', '财务信息部', '会计', 'six_day', 0, '', '2026-06-29T00:00:00')
                """
            )
            await db.commit()

        result = await dingtalk_ops.scan_departed_candidates({"source_run_id": "scan-001"}, operator="cron")
        assert result["ok"] is True
        assert result["created"] == 1
        assert result["auto_confirmed"] == 0

        async with database.aiosqlite.connect(database.DB_PATH) as db:
            db.row_factory = database.aiosqlite.Row
            emp = await (await db.execute("SELECT is_disabled, disabled_reason FROM employees WHERE userid='u_missing'")).fetchone()
            assert dict(emp) == {"is_disabled": 0, "disabled_reason": ""}

        result2 = await dingtalk_ops.scan_departed_candidates(
            {"source_run_id": "scan-002", "auto_confirm": True},
            operator="cron-auto",
        )
        assert result2["ok"] is True
        assert result2["auto_confirmed"] == 1

        async with database.aiosqlite.connect(database.DB_PATH) as db:
            db.row_factory = database.aiosqlite.Row
            emp = await (await db.execute("SELECT is_disabled, disabled_reason FROM employees WHERE userid='u_missing'")).fetchone()
            assert dict(emp) == {"is_disabled": 1, "disabled_reason": "sync_absent"}
            audit = await (await db.execute("SELECT action, operator, before_status, after_status FROM departed_candidate_audit WHERE userid='u_missing' ORDER BY id DESC LIMIT 1")).fetchone()
            assert dict(audit) == {
                "action": "auto_confirm",
                "operator": "cron-auto",
                "before_status": "pending",
                "after_status": "auto_confirmed",
            }

    asyncio.run(scenario())


def test_sync_contacts_only_upserts_and_never_auto_disables(monkeypatch):
    import dingtalk_ops

    class FakeTokenManager:
        async def get_token(self, app_key, app_secret):
            return "token"

    class FakeResponse:
        def json(self):
            return {
                "errcode": 0,
                "result": {
                    "list": [
                        {
                            "userid": "u_seen",
                            "name": "仍在职",
                            "title": "员工",
                            "active": True,
                        }
                    ],
                    "has_more": False,
                    "next_cursor": 0,
                },
            }

    class FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    calls = {"upsert": [], "set_department_disabled": []}

    async def fake_upsert(emp):
        calls["upsert"].append(emp)
        return (False, emp.get("userid", ""))  # all existing, not new

    async def fake_get_rule_days(month_type, category):
        return 4

    async def fake_get_all_depts(token, parent_id=1, depth=0):
        return [{"dept_id": 2, "name": "门店"}]

    async def forbidden_mark_absent(active_userids):
        raise AssertionError("sync_contacts must not auto-disable absent employees")

    async def fake_set_department_disabled(depts, disabled=True, reason="dept_rule"):
        calls["set_department_disabled"].append((depts, disabled, reason))
        return 0

    monkeypatch.setattr(dingtalk_ops, "token_manager", FakeTokenManager())
    monkeypatch.setattr(dingtalk_ops, "load_config", lambda: {"appKey": "ak", "appSecret": "as"})
    monkeypatch.setattr(dingtalk_ops, "_get_all_depts", fake_get_all_depts)
    monkeypatch.setattr(dingtalk_ops.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(dingtalk_ops, "upsert_employee", fake_upsert)
    monkeypatch.setattr(dingtalk_ops, "get_rule_days", fake_get_rule_days)
    assert not hasattr(dingtalk_ops, "mark_absent_employees_disabled")
    monkeypatch.setattr(dingtalk_ops, "set_department_disabled", fake_set_department_disabled)

    total = asyncio.run(dingtalk_ops.sync_contacts())

    assert total == 1
    assert [emp["userid"] for emp in calls["upsert"]] == ["u_seen"]
