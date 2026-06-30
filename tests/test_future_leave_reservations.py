"""月底提交下月公休预占测试。"""
import asyncio
from datetime import datetime


def _next_month_ts_july_1_2026():
    # 服务器本地时间语义下的 2026-07-01 00:00:00，例子同用户给的 1782835200000
    return 1782835200000


async def _seed_employee(db, userid="u001", name="张三", category="six_day", dept_name="财务信息部"):
    await db.execute(
        """
        INSERT INTO employees (userid, name, dept_id, dept_name, job_title, category, current_balance, is_disabled, updated_at)
        VALUES (?, ?, 'd1', ?, '员工', ?, 600, 0, '2026-06-30T00:00:00')
        """,
        (userid, name, dept_name, category),
    )
    await db.execute(
        """
        INSERT INTO leave_balances (userid, name, balance, period, month_type, updated_at, created_at)
        VALUES (?, ?, 600, '2026-07', 'normal', '2026-07-01T02:00:00', '2026-07-01T02:00:00')
        """,
        (userid, name),
    )
    await db.commit()


def test_parse_leave_start_date_local_timestamp():
    import database

    parsed = database.parse_leave_start_date(_next_month_ts_july_1_2026(), now=datetime(2026, 6, 30, 12, 0, 0))
    assert parsed["mode"] == "next"
    assert parsed["date"] == "2026-07-01"
    assert parsed["target_period"] == "2026-07"


def test_parse_leave_start_date_rejects_far_future():
    import database

    parsed = database.parse_leave_start_date("2026-08-01", now=datetime(2026, 6, 30, 12, 0, 0))
    assert parsed["mode"] == "unsupported"
    assert parsed["target_period"] == "2026-08"


def test_future_check_ok_and_reject(monkeypatch, tmp_path):
    import database
    import dingtalk_ops

    db_path = tmp_path / "future_check.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(dingtalk_ops, "DB_PATH", db_path)

    async def scenario():
        await database.init_db()
        async with database.aiosqlite.connect(db_path) as db:
            await _seed_employee(db, category="six_day")
            # 已预占2天
            await db.execute(
                """
                INSERT INTO future_leave_reservations (userid, name, days, leave_start_date, target_period, ref_id, status, reason, created_at)
                VALUES ('u001', '张三', 200, '2026-07-01', '2026-07', 'ref-old', 'pending', 'old', '2026-06-30')
                """
            )
            await db.commit()

        ok = await dingtalk_ops.check_leave_balance_with_future("u001", 300, leave_start_date=_next_month_ts_july_1_2026())
        assert ok["ok"] is True
        assert ok["mode"] == "future"
        assert ok["limit"] == 600
        assert ok["reserved"] == 200
        assert ok["available"] == 400

        reject = await dingtalk_ops.check_leave_balance_with_future("u001", 500, leave_start_date=_next_month_ts_july_1_2026())
        assert reject["ok"] is False
        assert reject["message"] == "下月公休额度不足"

    asyncio.run(scenario())


def test_future_check_rejects_far_future(monkeypatch, tmp_path):
    import database
    import dingtalk_ops

    db_path = tmp_path / "future_far.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(dingtalk_ops, "DB_PATH", db_path)

    async def scenario():
        await database.init_db()
        async with database.aiosqlite.connect(db_path) as db:
            await _seed_employee(db)
        result = await dingtalk_ops.check_leave_balance_with_future("u001", 100, leave_start_date="2026-08-01")
        assert result["ok"] is False
        assert result["mode"] == "unsupported"

    asyncio.run(scenario())


def test_future_deduct_creates_pending_and_is_idempotent(monkeypatch, tmp_path):
    import database
    import dingtalk_ops

    db_path = tmp_path / "future_deduct.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(dingtalk_ops, "DB_PATH", db_path)

    async def scenario():
        await database.init_db()
        async with database.aiosqlite.connect(db_path) as db:
            await _seed_employee(db, category="six_day")

        first = await dingtalk_ops.deduct_leave_balance_with_future("u001", 300, "下月请假", "ref-001", _next_month_ts_july_1_2026())
        assert first["ok"] is True
        assert first["mode"] == "future"
        assert first["idempotent"] is False

        second = await dingtalk_ops.deduct_leave_balance_with_future("u001", 300, "下月请假", "ref-001", _next_month_ts_july_1_2026())
        assert second["ok"] is True
        assert second["idempotent"] is True

        async with database.aiosqlite.connect(db_path) as db:
            row = await (await db.execute("SELECT COUNT(*), SUM(days) FROM future_leave_reservations WHERE ref_id='ref-001'")).fetchone()
            assert row[0] == 1
            assert row[1] == 300
            bal = await (await db.execute("SELECT balance FROM leave_balances WHERE userid='u001' AND period='2026-06'")).fetchone()
            assert bal is None  # 没扣当前月

    asyncio.run(scenario())


def test_future_refund_pending_cancels(monkeypatch, tmp_path):
    import database
    import dingtalk_ops

    db_path = tmp_path / "future_refund.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(dingtalk_ops, "DB_PATH", db_path)

    async def scenario():
        await database.init_db()
        async with database.aiosqlite.connect(db_path) as db:
            await _seed_employee(db)
        await dingtalk_ops.deduct_leave_balance_with_future("u001", 200, "下月请假", "ref-cancel", _next_month_ts_july_1_2026())

        result = await dingtalk_ops.refund_leave_balance_with_future("u001", 200, "撤销", "ref-cancel")
        assert result["ok"] is True
        assert result["status"] == "cancelled"

        again = await dingtalk_ops.refund_leave_balance_with_future("u001", 200, "撤销", "ref-cancel")
        assert again["ok"] is True
        assert again["idempotent"] is True

    asyncio.run(scenario())


def test_apply_future_reservations_deducts_next_month_once(monkeypatch, tmp_path):
    import database
    import dingtalk_ops

    db_path = tmp_path / "future_apply.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(dingtalk_ops, "DB_PATH", db_path)

    async def scenario():
        await database.init_db()
        async with database.aiosqlite.connect(db_path) as db:
            await _seed_employee(db, category="six_day")
        await dingtalk_ops.deduct_leave_balance_with_future("u001", 200, "下月请假", "ref-apply", _next_month_ts_july_1_2026())

        result = await dingtalk_ops.apply_future_reservations("2026-07")
        assert result["total"] == 1
        assert result["success"] == 1
        assert result["failed"] == 0

        async with database.aiosqlite.connect(db_path) as db:
            bal = await (await db.execute("SELECT balance FROM leave_balances WHERE userid='u001' AND period='2026-07'")).fetchone()
            assert bal[0] == 400
            status = await (await db.execute("SELECT status FROM future_leave_reservations WHERE ref_id='ref-apply'")).fetchone()
            assert status[0] == "applied"

        second = await dingtalk_ops.apply_future_reservations("2026-07")
        assert second["total"] == 0
        async with database.aiosqlite.connect(db_path) as db:
            bal2 = await (await db.execute("SELECT balance FROM leave_balances WHERE userid='u001' AND period='2026-07'")).fetchone()
            assert bal2[0] == 400

    asyncio.run(scenario())
