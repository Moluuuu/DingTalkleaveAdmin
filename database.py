"""
公休余额管理后台 - 数据库模块
独立SQLite: admin.db
"""
import aiosqlite
import json
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "admin.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # WAL模式:读写不互斥,提升并发(持久化,设一次即生效)
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        # 人员表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS employees (
                userid TEXT PRIMARY KEY,
                name TEXT,
                dept_id TEXT,
                dept_name TEXT,
                job_title TEXT,
                is_hourly INTEGER DEFAULT 0,
                is_six_day INTEGER DEFAULT 0,
                is_me INTEGER DEFAULT 0,
                category TEXT DEFAULT 'other',
                default_quota REAL DEFAULT 4,
                current_balance REAL DEFAULT 0,
                is_disabled INTEGER DEFAULT 0,
                disabled_reason TEXT DEFAULT '',
                synced_at TEXT,
                updated_at TEXT
            )
        """)
        # 兼容已有表: 补 is_disabled 列
        try:
            await db.execute("ALTER TABLE employees ADD COLUMN is_disabled INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE employees ADD COLUMN disabled_reason TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE employees ADD COLUMN hired_date TEXT DEFAULT ''")
        except Exception:
            pass

        # 额度操作日志
        await db.execute("""
            CREATE TABLE IF NOT EXISTS quota_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,
                userid TEXT,
                name TEXT,
                old_value REAL,
                new_value REAL,
                delta REAL,
                reason TEXT,
                operator TEXT,
                status TEXT DEFAULT 'pending',
                error_msg TEXT,
                created_at TEXT
            )
        """)

        # 余额快照(危险操作前自动备份,12小时后自动清理)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS balance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,
                userid TEXT,
                name TEXT,
                balance REAL,
                snapshot_type TEXT,
                created_at TEXT,
                expires_at TEXT
            )
        """)
        # 兼容已有表: 补 expires_at 列
        try:
            await db.execute("ALTER TABLE balance_snapshots ADD COLUMN expires_at TEXT")
        except Exception:
            pass

        # cron执行记录
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cron_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT,
                status TEXT,
                detail TEXT,
                total INTEGER DEFAULT 0,
                success INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)

        # 额度规则(可后台改)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS quota_rules (
                month_type TEXT,
                category TEXT,
                days INTEGER,
                updated_at TEXT,
                PRIMARY KEY (month_type, category)
            )
        """)

        # 初始化默认规则
        defaults = [
            ("normal", "hourly", 5),
            ("normal", "six_day", 6),
            ("normal", "other", 4),
            ("normal", "me", 8),
            ("special", "hourly", 20),
            ("special", "six_day", 18),
            ("special", "other", 0),
            ("special", "me", 0),
        ]
        for mt, cat, days in defaults:
            await db.execute(
                "INSERT OR IGNORE INTO quota_rules (month_type, category, days, updated_at) VALUES (?,?,?,?)",
                (mt, cat, days, datetime.now().isoformat())
            )

        # 公休余额主表(自建,替代钉钉额度)
        # balance=剩余天数(支持0.5), period=所属月份(YYYY-MM), month_type=适用规则
        await db.execute("""
            CREATE TABLE IF NOT EXISTS leave_balances (
                userid TEXT,
                name TEXT,
                balance REAL DEFAULT 0,
                period TEXT,
                month_type TEXT,
                updated_at TEXT,
                created_at TEXT,
                PRIMARY KEY (userid, period)
            )
        """)
        # 余额变更流水(扣减/回退/重置,每次一条,审计用)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS balance_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                userid TEXT,
                name TEXT,
                action TEXT,
                days REAL,
                balance_before REAL,
                balance_after REAL,
                reason TEXT,
                ref_id TEXT,
                created_at TEXT
            )
        """)
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS idx_bal_tx_userid ON balance_transactions(userid)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_bal_tx_refid ON balance_transactions(ref_id, action)")
        except Exception:
            pass

        # 未来月份公休预占：月底提交下月公休时先占用下月额度，月初初始化后再扣减
        await db.execute("""
            CREATE TABLE IF NOT EXISTS future_leave_reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                userid TEXT NOT NULL,
                name TEXT,
                days REAL NOT NULL,
                leave_start_date TEXT NOT NULL,
                target_period TEXT NOT NULL,
                ref_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                reason TEXT DEFAULT '',
                error_msg TEXT DEFAULT '',
                created_at TEXT,
                applied_at TEXT,
                cancelled_at TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_future_leave_period_status ON future_leave_reservations(target_period, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_future_leave_user_period_status ON future_leave_reservations(userid, target_period, status)")

        # 离职/缺席候选表：通讯录扫描只生成候选，不直接屏蔽员工
        await db.execute("""
            CREATE TABLE IF NOT EXISTS departed_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                userid TEXT UNIQUE,
                name TEXT,
                dept_id TEXT,
                dept_name TEXT,
                job_title TEXT,
                category TEXT,
                status TEXT DEFAULT 'pending',
                source_run_id TEXT,
                reason TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT,
                confirmed_at TEXT,
                confirmed_by TEXT,
                action_note TEXT,
                raw_json TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS departed_candidate_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER,
                userid TEXT,
                action TEXT,
                operator TEXT,
                note TEXT,
                before_status TEXT,
                after_status TEXT,
                created_at TEXT
            )
        """)

        # 定时任务配置(用户在UI自管理,scheduler进程读取执行)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                task_type TEXT NOT NULL,
                cron_expr TEXT NOT NULL,
                config TEXT,
                enabled INTEGER DEFAULT 1,
                last_run TEXT,
                last_status TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        # 预置默认任务(幂等:按name+task_type判断,已存在则跳过)
        defaults_tasks = [
            ("全员公休余额刷新", "refresh_all", "20 1 * * *", "{}"),
            ("过期快照清理", "cleanup_snapshots", "0 * * * *", "{}"),
            ("月初公休额度发放", "monthly_assign", "0 2 1 * *", '{"month_type":"auto"}'),
            ("下月公休预占扣减", "apply_future_reservations", "20 2 1 * *", '{}'),
            ("特殊部门年度额度重置", "annual_dept_reset", "0 2 1 3 *", '{}'),
            ("离职候选扫描", "departed_scan", "35 1 * * *", '{"auto_confirm":false}'),
            ("钉盘小时备份", "dingpan_backup", "7 * * * *", '{"keep":24}'),
        ]
        for name, ttype, cron, cfg in defaults_tasks:
            existing = await db.execute_fetchall(
                "SELECT id FROM scheduled_tasks WHERE name=? AND task_type=?", (name, ttype)
            )
            if not existing:
                await db.execute(
                    "INSERT INTO scheduled_tasks (name, task_type, cron_expr, config, enabled, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
                    (name, ttype, cron, cfg, datetime.now().isoformat(), datetime.now().isoformat())
                )

        await db.commit()


# ========== 余额快照(危险操作前自动备份) ==========

async def create_balance_snapshot(batch_id: str, snapshot_type: str = "pre_operate", userids: list = None):
    """操作前快照: 把涉及人员的余额存一份到balance_snapshots(12小时后自动清理)
    userids=None 时快照全员; 传 list 时只快照涉及的人(精准备份)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        if userids:
            placeholders = ",".join("?" * len(userids))
            rows = await db.execute_fetchall(
                "SELECT userid, name, current_balance FROM employees WHERE userid IN ({})".format(placeholders),
                userids
            )
        else:
            rows = await db.execute_fetchall("SELECT userid, name, current_balance FROM employees")
        ts = datetime.now().isoformat()
        expires = (datetime.now() + timedelta(hours=12)).isoformat()
        for userid, name, balance in rows:
            await db.execute(
                "INSERT INTO balance_snapshots (batch_id, userid, name, balance, snapshot_type, created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
                (batch_id, userid, name or "", balance or 0, snapshot_type, ts, expires)
            )
        await db.commit()
        return len(rows)


async def cleanup_expired_snapshots():
    """清理已过期的快照(12小时前),返回清理条数
    expires_at为NULL的旧数据按创建时间+12小时兜底处理
    """
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()
        # 清理有expires_at且已过期的
        cursor = await db.execute(
            "DELETE FROM balance_snapshots WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,)
        )
        # 清理expires_at为NULL但创建已超12小时的旧数据兜底
        cutoff = (datetime.now() - timedelta(hours=12)).isoformat()
        cursor2 = await db.execute(
            "DELETE FROM balance_snapshots WHERE expires_at IS NULL AND created_at < ?",
            (cutoff,)
        )
        await db.commit()
        return cursor.rowcount + cursor2.rowcount


async def get_snapshots(limit: int = 20):
    """查询快照批次列表(按batch_id聚合)"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT batch_id, snapshot_type, COUNT(*) as cnt,
                   MIN(created_at) as created_at, MIN(expires_at) as expires_at
            FROM balance_snapshots GROUP BY batch_id ORDER BY MIN(created_at) DESC LIMIT ?
        """, (limit,))
        rows = await cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("expires_at") is None:
                d["expires_at"] = ""
            result.append(d)
        return result


async def rollback_snapshot(batch_id: str):
    """回滚某次快照: 把employees的余额恢复到快照时的值(只恢复本地DB,钉钉侧需另调接口)"""
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await db.execute_fetchall(
            "SELECT userid, balance FROM balance_snapshots WHERE batch_id=?", (batch_id,)
        )
        count = 0
        for userid, balance in rows:
            await db.execute(
                "UPDATE employees SET current_balance=?, updated_at=? WHERE userid=?",
                (balance, datetime.now().isoformat(), userid)
            )
            count += 1
        await db.commit()
        return count


async def get_snapshot_detail(batch_id: str):
    """查看某次快照的明细"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT userid, name, balance, created_at FROM balance_snapshots WHERE batch_id=? ORDER BY name",
            (batch_id,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ========== 人员 ==========

async def upsert_employee(emp: dict):
    """新增/更新员工。

    is_disabled 的规则：
    - emp 显式传 is_disabled=1（如编外部门）时，强制屏蔽并写 disabled_reason。
    - 因上次通讯录缺席(sync_absent)被屏蔽的人，如果这次又出现在通讯录，自动恢复。
    - 人工屏蔽(manual)保持屏蔽，避免同步把手工处理的人洗回来。

    返回 (is_new, userid) — is_new=True 表示首次入库。
    """
    now = datetime.now().isoformat()
    forced_disabled = 1 if emp.get("is_disabled") else 0
    disabled_reason = emp.get("disabled_reason", "") if forced_disabled else ""
    hired_date = emp.get("hired_date", "") or ""
    async with aiosqlite.connect(DB_PATH) as db:
        # 检测是否新员工
        cursor = await db.execute("SELECT 1 FROM employees WHERE userid=?", (emp["userid"],))
        existing = await cursor.fetchone()
        is_new = existing is None

        await db.execute("""
            INSERT INTO employees (userid, name, dept_id, dept_name, job_title, is_hourly, is_six_day, is_me, category, default_quota, is_disabled, disabled_reason, hired_date, synced_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(userid) DO UPDATE SET
                name=excluded.name, dept_id=excluded.dept_id, dept_name=excluded.dept_name,
                job_title=excluded.job_title, is_hourly=excluded.is_hourly, is_six_day=excluded.is_six_day,
                is_me=excluded.is_me, category=excluded.category, default_quota=excluded.default_quota,
                is_disabled=CASE
                    WHEN excluded.is_disabled=1 THEN 1
                    WHEN employees.disabled_reason='sync_absent' THEN 0
                    ELSE employees.is_disabled
                END,
                disabled_reason=CASE
                    WHEN excluded.is_disabled=1 THEN excluded.disabled_reason
                    WHEN employees.disabled_reason='sync_absent' THEN ''
                    ELSE COALESCE(employees.disabled_reason, '')
                END,
                hired_date=CASE
                    WHEN excluded.hired_date != '' THEN excluded.hired_date
                    ELSE COALESCE(employees.hired_date, '')
                END,
                synced_at=excluded.synced_at, updated_at=excluded.updated_at
        """, (
            emp["userid"], emp["name"], emp.get("dept_id",""), emp.get("dept_name",""),
            emp.get("job_title",""), emp.get("is_hourly",0), emp.get("is_six_day",0),
            emp.get("is_me",0), emp.get("category","other"), emp.get("default_quota",4),
            forced_disabled, disabled_reason, hired_date, now, now
        ))
        await db.commit()
    return (is_new, emp["userid"])


async def set_disabled(userid: str, disabled: bool, reason: str = "manual"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE employees SET is_disabled=?, disabled_reason=?, updated_at=? WHERE userid=?",
            (1 if disabled else 0, reason if disabled else "", datetime.now().isoformat(), userid))
        await db.commit()


async def mark_absent_employees_disabled(active_userids: set):
    """完整通讯录同步后，把本地有、钉钉本次没有的人标为离职/缺席屏蔽。

    只做软屏蔽，不物理删除，保留历史审计。active_userids 为空时直接跳过，避免上游异常导致全员误屏蔽。
    """
    if not active_userids:
        return {"disabled": 0, "guarded": True}
    userids = list(active_userids)
    placeholders = ",".join("?" * len(userids))
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"""
            UPDATE employees
            SET is_disabled=1, disabled_reason='sync_absent', updated_at=?
            WHERE is_disabled=0 AND userid NOT IN ({placeholders})
            """,
            [datetime.now().isoformat()] + userids
        )
        await db.commit()
        return {"disabled": cursor.rowcount, "guarded": False}


async def set_department_disabled(dept_names: list, disabled: bool = True, reason: str = "dept_rule"):
    if not dept_names:
        return 0
    placeholders = ",".join("?" * len(dept_names))
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"UPDATE employees SET is_disabled=?, disabled_reason=?, updated_at=? WHERE dept_name IN ({placeholders})",
            [1 if disabled else 0, reason if disabled else "", datetime.now().isoformat()] + list(dept_names)
        )
        await db.commit()
        return cursor.rowcount


async def get_all_employees(include_disabled=False):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = "SELECT * FROM employees"
        if not include_disabled:
            sql += " WHERE is_disabled=0"
        sql += " ORDER BY dept_name, name"
        cursor = await db.execute(sql)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_depts():
    """获取所有部门(去重,排除已屏蔽)"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT DISTINCT dept_name FROM employees WHERE dept_name != '' AND is_disabled=0 ORDER BY dept_name"
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def get_jobs():
    """获取所有职位(去重,排除已屏蔽)"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT DISTINCT job_title FROM employees WHERE job_title != '' AND is_disabled=0 ORDER BY job_title"
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def get_employees_by_filter(dept=None, job=None, include_disabled=False):
    """按部门/职位筛选人员"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = "SELECT * FROM employees WHERE 1=1"
        params = []
        if not include_disabled:
            sql += " AND is_disabled=0"
        if dept:
            sql += " AND dept_name=?"
            params.append(dept)
        if job:
            sql += " AND job_title=?"
            params.append(job)
        sql += " ORDER BY dept_name, name"
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_employee_balance(userid: str, balance: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE employees SET current_balance=?, updated_at=? WHERE userid=?",
            (balance, datetime.now().isoformat(), userid)
        )
        await db.commit()



async def set_leave_balance_abs(userid: str, balance_x100: float, name: str = "", reason: str = "", action: str = "adjust", period: str = None):
    """设 leave_balances 绝对值(×100)，同时写 balance_transactions 审计。供后台 adjust/rollback/monthly 用。
    返回 {old_x100, new_x100, name}"""
    period = period or _current_period()
    old = await get_leave_balance(userid, period)
    old_x100 = old["balance"]
    nm = name or old["name"] or userid
    new_x100 = round(balance_x100, 2)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO leave_balances (userid, name, balance, period, updated_at, created_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(userid, period) DO UPDATE SET
                balance=excluded.balance, name=excluded.name, updated_at=excluded.updated_at
        """, (userid, nm, new_x100, period, datetime.now().isoformat(), datetime.now().isoformat()))
        await db.commit()
    await _add_transaction(userid, nm, action, round(new_x100 - old_x100, 2), old_x100, new_x100, reason or "后台调整", "")
    return {"old_x100": old_x100, "new_x100": new_x100, "name": nm}


async def get_employee_by_userid(userid: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM employees WHERE userid=?", (userid,))
        row = await cursor.fetchone()
        return dict(row) if row else None


# ========== 离职/缺席候选审核 ==========

async def create_departed_candidates(active_userids: set, source_run_id: str = "", reason: str = "通讯录缺席候选", raw_meta: dict = None):
    """根据本次完整通讯录 active_userids 生成离职/缺席候选。

    只写 departed_candidates，不直接修改 employees。active_userids 为空时直接保护跳过。
    """
    if not active_userids:
        return {"created": 0, "updated": 0, "guarded": True, "total": 0}

    now = datetime.now().isoformat()
    userids = list(active_userids)
    placeholders = ",".join("?" * len(userids))
    raw_json = json.dumps(raw_meta or {}, ensure_ascii=False)
    created = 0
    updated = 0

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT userid, name, dept_id, dept_name, job_title, category
            FROM employees
            WHERE is_disabled=0 AND userid NOT IN ({placeholders})
            ORDER BY dept_name, name
            """,
            userids
        )
        rows = await cursor.fetchall()
        for row in rows:
            emp = dict(row)
            existing_cur = await db.execute("SELECT id, status FROM departed_candidates WHERE userid=?", (emp["userid"],))
            existing = await existing_cur.fetchone()
            if existing:
                await db.execute(
                    """
                    UPDATE departed_candidates
                    SET name=?, dept_id=?, dept_name=?, job_title=?, category=?,
                        source_run_id=?, reason=?, last_seen_at=?, raw_json=?
                    WHERE userid=?
                    """,
                    (
                        emp.get("name") or "", emp.get("dept_id") or "", emp.get("dept_name") or "",
                        emp.get("job_title") or "", emp.get("category") or "other",
                        source_run_id, reason, now, raw_json, emp["userid"]
                    )
                )
                updated += 1
            else:
                await db.execute(
                    """
                    INSERT INTO departed_candidates
                    (userid, name, dept_id, dept_name, job_title, category, status, source_run_id, reason, first_seen_at, last_seen_at, raw_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        emp["userid"], emp.get("name") or "", emp.get("dept_id") or "",
                        emp.get("dept_name") or "", emp.get("job_title") or "", emp.get("category") or "other",
                        "pending", source_run_id, reason, now, now, raw_json
                    )
                )
                created += 1
        await db.commit()
    return {"created": created, "updated": updated, "guarded": False, "total": created + updated}


async def get_departed_candidates(status: str = None, limit: int = 200):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = "SELECT * FROM departed_candidates"
        params = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY last_seen_at DESC, dept_name, name LIMIT ?"
        params.append(limit)
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def _add_departed_candidate_audit(db, candidate_id: int, userid: str, action: str, operator: str, note: str, before_status: str, after_status: str):
    await db.execute(
        """
        INSERT INTO departed_candidate_audit
        (candidate_id, userid, action, operator, note, before_status, after_status, created_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (candidate_id, userid, action, operator, note or "", before_status or "", after_status or "", datetime.now().isoformat())
    )


async def confirm_departed_candidate(candidate_id: int, operator: str = "manual", note: str = "", auto: bool = False):
    """确认候选为缺席/离职：此时才真正屏蔽 employees，并写审计。"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM departed_candidates WHERE id=?", (candidate_id,))
        row = await cursor.fetchone()
        if not row:
            return {"ok": False, "message": "候选不存在"}
        cand = dict(row)
        before_status = cand.get("status") or "pending"
        after_status = "auto_confirmed" if auto else "confirmed"
        action = "auto_confirm" if auto else "confirm"
        now = datetime.now().isoformat()
        await db.execute(
            "UPDATE employees SET is_disabled=1, disabled_reason='sync_absent', updated_at=? WHERE userid=?",
            (now, cand["userid"])
        )
        await db.execute(
            """
            UPDATE departed_candidates
            SET status=?, confirmed_at=?, confirmed_by=?, action_note=?, last_seen_at=?
            WHERE id=?
            """,
            (after_status, now, operator, note or "", now, candidate_id)
        )
        await _add_departed_candidate_audit(db, candidate_id, cand["userid"], action, operator, note, before_status, after_status)
        await db.commit()
        return {"ok": True, "message": "已确认并屏蔽", "userid": cand["userid"], "status": after_status}


async def ignore_departed_candidate(candidate_id: int, operator: str = "manual", note: str = ""):
    """忽略候选：不修改 employees，仅记录审核痕迹。"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM departed_candidates WHERE id=?", (candidate_id,))
        row = await cursor.fetchone()
        if not row:
            return {"ok": False, "message": "候选不存在"}
        cand = dict(row)
        before_status = cand.get("status") or "pending"
        after_status = "ignored"
        await db.execute(
            "UPDATE departed_candidates SET status=?, confirmed_at=?, confirmed_by=?, action_note=? WHERE id=?",
            (after_status, datetime.now().isoformat(), operator, note or "", candidate_id)
        )
        await _add_departed_candidate_audit(db, candidate_id, cand["userid"], "ignore", operator, note, before_status, after_status)
        await db.commit()
        return {"ok": True, "message": "已忽略候选", "userid": cand["userid"], "status": after_status}


# ========== 日志 ==========

async def add_quota_log(log: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO quota_logs (batch_id, userid, name, old_value, new_value, delta, reason, operator, status, error_msg, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            log.get("batch_id",""), log.get("userid",""), log.get("name",""),
            log.get("old_value",0), log.get("new_value",0), log.get("delta",0),
            log.get("reason",""), log.get("operator",""), log.get("status","pending"),
            log.get("error_msg",""), datetime.now().isoformat()
        ))
        await db.commit()
        return cursor.lastrowid


async def update_quota_log_status(log_id: int, status: str, error_msg: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE quota_logs SET status=?, error_msg=? WHERE id=?",
            (status, error_msg, log_id)
        )
        await db.commit()


async def get_quota_logs(limit: int = 50):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM quota_logs ORDER BY id DESC LIMIT ?", (limit,))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ========== cron记录 ==========

async def add_cron_run(task_type: str, status: str, detail: str = "", total: int = 0, success: int = 0, failed: int = 0, skipped: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO cron_runs (task_type, status, detail, total, success, failed, skipped, created_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (task_type, status, detail, total, success, failed, skipped, datetime.now().isoformat()))
        await db.commit()
        return cursor.lastrowid


async def update_cron_run(cron_id: int, status: str, detail: str = "", total: int = 0, success: int = 0, failed: int = 0, skipped: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE cron_runs SET status=?, detail=?, total=?, success=?, failed=?, skipped=? WHERE id=?",
            (status, detail, total, success, failed, skipped, cron_id)
        )
        await db.commit()


async def get_cron_runs(limit: int = 30):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM cron_runs ORDER BY id DESC LIMIT ?", (limit,))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ========== 规则 ==========

async def get_quota_rules():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM quota_rules")
        rows = await cursor.fetchall()
        result = {"normal": {}, "special": {}}
        for r in rows:
            result[r["month_type"]][r["category"]] = r["days"]
        return result


async def update_quota_rule(month_type: str, category: str, days: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE quota_rules SET days=?, updated_at=? WHERE month_type=? AND category=?",
            (days, datetime.now().isoformat(), month_type, category)
        )
        await db.commit()


async def get_rule_days(month_type: str, category: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT days FROM quota_rules WHERE month_type=? AND category=?",
            (month_type, category)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


# ========== 自建余额(leave_balances) ==========
# 替代钉钉额度的本地余额管控,宜搭将来直接调这套
# 余额单位=天,支持0.5天精度,不取整

def _current_period():
    """当前月份 YYYY-MM"""
    return datetime.now().strftime("%Y-%m")


async def get_leave_balance(userid: str, period: str = None):
    """查单人某月余额,不存在则返回0"""
    period = period or _current_period()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT balance, name FROM leave_balances WHERE userid=? AND period=?",
            (userid, period)
        )
        row = await cursor.fetchone()
        if row:
            return {"balance": row[0], "name": row[1], "period": period}
        # 没记录,补一条0余额
        return {"balance": 0, "name": "", "period": period}


def _period_from_date(d: datetime) -> str:
    return d.strftime("%Y-%m")


def _add_month(period: str, months: int = 1) -> str:
    year, month = map(int, period.split("-"))
    month += months
    while month > 12:
        year += 1
        month -= 12
    while month < 1:
        year -= 1
        month += 12
    return f"{year:04d}-{month:02d}"


def parse_leave_start_date(value, now: datetime = None):
    """解析宜搭传入的休假开始日期。

    value 可以是毫秒时间戳(如 1782835200000)或 YYYY-MM-DD 字符串。
    按服务器本地日期语义解析，只取日期，不用 UTC 日期，避免日期被转成前一天。
    返回 {date, target_period, mode}: mode=current/next/unsupported/missing。
    """
    if value is None or value == "":
        return {"mode": "missing", "date": None, "target_period": _current_period()}
    if now is None:
        now = datetime.now()
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return {"mode": "missing", "date": None, "target_period": _current_period()}
        if s.isdigit():
            ts = int(s) / 1000
            dt = datetime.fromtimestamp(ts)
        else:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00").split("T")[0])
    else:
        dt = datetime.fromtimestamp(float(value) / 1000)
    leave_date = dt.date().isoformat()
    target_period = dt.strftime("%Y-%m")
    current_period = now.strftime("%Y-%m")
    next_period = _add_month(current_period, 1)
    if target_period == current_period:
        mode = "current"
    elif target_period == next_period:
        mode = "next"
    else:
        mode = "unsupported"
    return {"mode": mode, "date": leave_date, "target_period": target_period, "current_period": current_period, "next_period": next_period}


async def sum_future_reservations(userid: str, target_period: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(days), 0) FROM future_leave_reservations WHERE userid=? AND target_period=? AND status='pending'",
            (userid, target_period)
        )
        row = await cursor.fetchone()
        return row[0] or 0


async def get_future_reservation_by_ref(ref_id: str):
    if not ref_id or not ref_id.strip():
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM future_leave_reservations WHERE ref_id=?", (ref_id.strip(),))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def create_future_reservation(userid: str, name: str, days: float, leave_start_date: str, target_period: str, ref_id: str, reason: str = ""):
    """创建未来月份公休预占。ref_id 唯一幂等。"""
    existing = await get_future_reservation_by_ref(ref_id)
    if existing:
        return {"ok": True, "idempotent": True, "reservation": existing, "message": "重复预占已忽略(幂等)"}
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO future_leave_reservations
            (userid, name, days, leave_start_date, target_period, ref_id, status, reason, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (userid, name or userid, days, leave_start_date, target_period, ref_id.strip(), "pending", reason or "下月公休预占", now)
        )
        await db.commit()
    return {"ok": True, "idempotent": False, "target_period": target_period, "reserved": days, "message": "已记录为下月公休预占"}


async def cancel_future_reservation(ref_id: str, reason: str = ""):
    existing = await get_future_reservation_by_ref(ref_id)
    if not existing:
        return None
    status = existing.get("status")
    if status == "cancelled":
        return {"ok": True, "idempotent": True, "mode": "future", "status": "cancelled", "message": "预占已取消(幂等)"}
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE future_leave_reservations SET status='cancelled', cancelled_at=?, reason=COALESCE(NULLIF(reason,''), ?) WHERE ref_id=?",
            (now, reason or existing.get("reason") or "撤销下月公休预占", ref_id.strip())
        )
        await db.commit()
    return {"ok": True, "mode": "future", "status": "cancelled", "message": "已取消下月公休预占", "reservation": existing}


async def list_pending_future_reservations(target_period: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM future_leave_reservations WHERE target_period=? AND status='pending' ORDER BY id",
            (target_period,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def list_future_reservations(target_period: str = None, status: str = None, limit: int = 500):
    """查询未来月份公休预占明细，默认查下个月全部状态。"""
    if not target_period:
        target_period = _add_month(_current_period(), 1)
    where = ["r.target_period=?"]
    params = [target_period]
    if status:
        where.append("r.status=?")
        params.append(status)
    sql = f"""
        SELECT r.*, e.dept_name, e.job_title, e.category
        FROM future_leave_reservations r
        LEFT JOIN employees e ON e.userid = r.userid
        WHERE {' AND '.join(where)}
        ORDER BY
            CASE r.status WHEN 'pending' THEN 0 WHEN 'failed' THEN 1 WHEN 'applied' THEN 2 WHEN 'cancelled' THEN 3 ELSE 9 END,
            r.leave_start_date,
            r.created_at DESC
        LIMIT ?
    """
    params.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        rows = [dict(r) for r in await cursor.fetchall()]
        summary_cursor = await db.execute(
            """
            SELECT status, COUNT(*) AS count, COALESCE(SUM(days), 0) AS days
            FROM future_leave_reservations
            WHERE target_period=?
            GROUP BY status
            """,
            (target_period,)
        )
        summary_rows = await summary_cursor.fetchall()
    summary = {"target_period": target_period, "total_count": 0, "total_days": 0}
    for r in summary_rows:
        st = r["status"]
        cnt = r["count"] or 0
        days = r["days"] or 0
        summary[f"{st}_count"] = cnt
        summary[f"{st}_days"] = days
        summary["total_count"] += cnt
        summary["total_days"] += days
    return {"target_period": target_period, "summary": summary, "rows": rows}


async def mark_future_reservation_applied(ref_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE future_leave_reservations SET status='applied', applied_at=?, error_msg='' WHERE ref_id=? AND status='pending'",
            (datetime.now().isoformat(), ref_id.strip())
        )
        await db.commit()


async def mark_future_reservation_failed(ref_id: str, error_msg: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE future_leave_reservations SET status='failed', error_msg=? WHERE ref_id=? AND status='pending'",
            (error_msg[:500], ref_id.strip())
        )
        await db.commit()


async def check_leave_balance(userid: str, days: float, period: str = None):
    """校验余额是否足够扣减days天,返回{ok, balance, need}"""
    bal = await get_leave_balance(userid, period)
    current = bal["balance"]
    ok = current >= days
    return {"ok": ok, "balance": current, "need": days}


async def _check_idempotent(userid: str, ref_id: str, action: str):
    """幂等检查:查询某uuid是否已有指定类型的交易记录.返回最近一条{balance_after,days,created_at}或None.
    ref_id为空时不做幂等(兼容测试/无uuid场景)."""
    if not ref_id or not ref_id.strip():
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT balance_after, days, created_at FROM balance_transactions WHERE userid=? AND ref_id=? AND action=? ORDER BY id DESC LIMIT 1",
            (userid, ref_id.strip(), action)
        )
        row = await cursor.fetchone()
        if row:
            return {"balance_after": row[0], "days": row[1], "created_at": row[2]}
        return None


async def _add_transaction(userid, name, action, days, before, after, reason, ref_id=""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO balance_transactions (userid, name, action, days, balance_before, balance_after, reason, ref_id, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (userid, name, action, days, before, after, reason, ref_id, datetime.now().isoformat()))
        await db.commit()


async def deduct_leave_balance(userid: str, days: float, reason: str, ref_id: str = "", period: str = None):
    """扣减余额(审批通过时调),不足则失败.幂等:同一ref_id(uuid)只扣一次"""
    # 幂等检查:同一uuid已扣过则直接返回,不重复扣减
    existing = await _check_idempotent(userid, ref_id, "deduct")
    if existing:
        return {"ok": True, "balance": existing["balance_after"], "message": "重复扣减已忽略(幂等)", "idempotent": True}
    period = period or _current_period()
    bal = await get_leave_balance(userid, period)
    current = bal["balance"]
    if current < days:
        return {"ok": False, "message": f"余额不足: 当前{current}天 需要{days}天", "balance": current}
    new_balance = round(current - days, 2)
    name = bal["name"] or userid
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO leave_balances (userid, name, balance, period, updated_at, created_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(userid, period) DO UPDATE SET
                balance=excluded.balance, name=excluded.name, updated_at=excluded.updated_at
        """, (userid, name, new_balance, period, datetime.now().isoformat(), datetime.now().isoformat()))
        await db.commit()
    await _add_transaction(userid, name, "deduct", -days, current, new_balance, reason, ref_id)
    # 同步 employees.current_balance(天数) 供后台表格显示, leave_balances 存×100 需 ÷100
    try:
        await update_employee_balance(userid, round(new_balance / 100, 2))
    except Exception as e:
        print(f"[sync] deduct 同步employees失败 userid={userid}: {e}", flush=True)
    return {"ok": True, "balance": new_balance, "message": f"扣减{days}(×100),余额{current}->{new_balance}"}


async def refund_leave_balance(userid: str, days: float, reason: str, ref_id: str = "", period: str = None):
    """回退余额(撤销/拒绝时调),累加回去.幂等:同一ref_id(uuid)只退一次.前置校验:必须先有扣减记录"""
    # 幂等检查:同一uuid已退过则直接返回
    existing = await _check_idempotent(userid, ref_id, "refund")
    if existing:
        return {"ok": True, "balance": existing["balance_after"], "message": "重复回退已忽略(幂等)", "idempotent": True}
    # 前置校验:回退前确认确实有对应的扣减记录(防止凭空退款)
    if ref_id and ref_id.strip():
        deduct_rec = await _check_idempotent(userid, ref_id, "deduct")
        if not deduct_rec:
            return {"ok": False, "message": "未找到对应的请假扣减记录,无法回退", "balance": None}
    period = period or _current_period()
    bal = await get_leave_balance(userid, period)
    current = bal["balance"]
    new_balance = round(current + days, 2)
    name = bal["name"] or userid
    async with aiosqlite.connect(DB_PATH) as db:
        # 如果该周期没记录,创建一条
        if current == 0 and not bal["name"]:
            await db.execute(
                "INSERT INTO leave_balances (userid, name, balance, period, updated_at, created_at) VALUES (?,?,?,?,?,?)",
                (userid, name, new_balance, period, datetime.now().isoformat(), datetime.now().isoformat())
            )
        else:
            await db.execute(
                "UPDATE leave_balances SET balance=?, updated_at=? WHERE userid=? AND period=?",
                (new_balance, datetime.now().isoformat(), userid, period)
            )
        await db.commit()
    await _add_transaction(userid, name, "refund", days, current, new_balance, reason, ref_id)
    # 同步 employees.current_balance(天数) 供后台表格显示, leave_balances 存×100 需 ÷100
    try:
        await update_employee_balance(userid, round(new_balance / 100, 2))
    except Exception as e:
        print(f"[sync] refund 同步employees失败 userid={userid}: {e}", flush=True)
    return {"ok": True, "balance": new_balance, "message": f"回退{days}(×100),余额{current}->{new_balance}"}


async def reset_leave_balance(userid: str, days: float, month_type: str = "normal", period: str = None):
    """重置单人余额(月初发放,直接设绝对值,丢弃剩余)"""
    period = period or _current_period()
    bal = await get_leave_balance(userid, period)
    current = bal["balance"]
    name = bal["name"] or userid
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO leave_balances (userid, name, balance, period, month_type, updated_at, created_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(userid, period) DO UPDATE SET
                balance=excluded.balance, month_type=excluded.month_type, updated_at=excluded.updated_at
        """, (userid, name, days, period, month_type, datetime.now().isoformat(), datetime.now().isoformat()))
        await db.commit()
    await _add_transaction(userid, name, "reset", days, current, days, f"月初重置({month_type})", "")
    return {"ok": True, "balance": days, "message": f"重置为{days}天(原{current}天)"}


async def get_all_leave_balances(period: str = None):
    """查全员某月余额"""
    period = period or _current_period()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM leave_balances WHERE period=? ORDER BY name", (period,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_balance_transactions(userid: str, limit: int = 50):
    """查某人余额变更流水"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM balance_transactions WHERE userid=? ORDER BY id DESC LIMIT ?",
            (userid, limit)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ========== 定时任务配置 ==========

async def get_scheduled_tasks():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM scheduled_tasks ORDER BY id")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def add_scheduled_task(name, task_type, cron_expr, config="{}"):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO scheduled_tasks (name, task_type, cron_expr, config, enabled, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
            (name, task_type, cron_expr, config, datetime.now().isoformat(), datetime.now().isoformat())
        )
        await db.commit()
        return cursor.lastrowid


async def update_scheduled_task(task_id, name=None, cron_expr=None, config=None, enabled=None):
    async with aiosqlite.connect(DB_PATH) as db:
        sets = []
        params = []
        if name is not None: sets.append("name=?"); params.append(name)
        if cron_expr is not None: sets.append("cron_expr=?"); params.append(cron_expr)
        if config is not None: sets.append("config=?"); params.append(config)
        if enabled is not None: sets.append("enabled=?"); params.append(1 if enabled else 0)
        sets.append("updated_at=?"); params.append(datetime.now().isoformat())
        params.append(task_id)
        await db.execute("UPDATE scheduled_tasks SET "+",".join(sets)+" WHERE id=?", params)
        await db.commit()


async def delete_scheduled_task(task_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM scheduled_tasks WHERE id=?", (task_id,))
        await db.commit()


async def update_task_last_run(task_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE scheduled_tasks SET last_run=?, last_status=? WHERE id=?",
            (datetime.now().isoformat(), status, task_id))
        await db.commit()
