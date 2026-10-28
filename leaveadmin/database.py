"""
公休余额管理后台 - 数据库模块
独立SQLite: admin.db
"""
import aiosqlite
import json
import math
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from leaveadmin.env import load_dotenv, project_root

load_dotenv()
PROJECT_ROOT = project_root()
DB_PATH = Path(os.getenv("LEAVEADMIN_DB_PATH", str(PROJECT_ROOT / "admin.db"))).resolve()


async def init_db():
    from leaveadmin.attendance_effects import (
        SCHEMA as attendance_effect_schema, SEND_SCHEMA as attendance_effect_send_schema,
    )

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
        for alter_sql in (
            "ALTER TABLE employees ADD COLUMN is_disabled INTEGER DEFAULT 0",
            "ALTER TABLE employees ADD COLUMN disabled_reason TEXT DEFAULT ''",
            "ALTER TABLE employees ADD COLUMN hired_date TEXT DEFAULT ''",
        ):
            try:
                await db.execute(alter_sql)
            except Exception as e:
                if "duplicate column name" not in str(e).lower():
                    raise

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
                balance_x100 REAL,
                has_record INTEGER DEFAULT 0,
                period TEXT,
                version TEXT DEFAULT '',
                snapshot_type TEXT,
                created_at TEXT,
                expires_at TEXT
            )
        """)
        # 兼容已有表: 补 expires_at 列及 v2 迁移列
        for alter_sql in (
            "ALTER TABLE balance_snapshots ADD COLUMN expires_at TEXT",
            "ALTER TABLE balance_snapshots ADD COLUMN balance_x100 REAL",
            "ALTER TABLE balance_snapshots ADD COLUMN has_record INTEGER DEFAULT 0",
            "ALTER TABLE balance_snapshots ADD COLUMN period TEXT",
            "ALTER TABLE balance_snapshots ADD COLUMN version TEXT DEFAULT ''",
        ):
            try:
                await db.execute(alter_sql)
            except Exception as e:
                if "duplicate column name" not in str(e).lower():
                    raise

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
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_bal_tx_userid ON balance_transactions(userid)",
            "CREATE INDEX IF NOT EXISTS idx_bal_tx_refid ON balance_transactions(ref_id, action)",
        ):
            try:
                await db.execute(idx_sql)
            except Exception as e:
                if "already exists" not in str(e).lower():
                    raise

        # 月份级批次头保证即使首次名单为空，重跑也不会吸收月初后的新员工。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS hourly_monthly_reset_runs (
                period TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                created_at TEXT
            )
        """)

        # 小时工月初重置批次明细：同一月份同一员工只执行一次，失败可重试。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS hourly_monthly_resets (
                period TEXT NOT NULL,
                userid TEXT NOT NULL,
                name TEXT,
                batch_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                old_balance REAL,
                new_balance REAL,
                error_msg TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT,
                PRIMARY KEY (period, userid)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_hourly_reset_period_status "
            "ON hourly_monthly_resets(period, status)"
        )

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
        # Only new atomic applications receive a proven ledger link; do not infer
        # historical links from timestamps or matching amounts.
        cursor = await db.execute("PRAGMA table_info(future_leave_reservations)")
        future_columns = {row[1] for row in await cursor.fetchall()}
        if "applied_transaction_id" not in future_columns:
            await db.execute("ALTER TABLE future_leave_reservations ADD COLUMN applied_transaction_id INTEGER")
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

        await db.execute(attendance_effect_schema)
        await db.execute(attendance_effect_send_schema)
        await db.commit()


# ========== 余额快照(危险操作前自动备份) ==========

SNAPSHOT_VERSION = "v2"


async def create_balance_snapshot(batch_id: str, snapshot_type: str = "pre_operate", userids: list = None, period: str = None):
    """操作前快照: 从权威表 leave_balances 读取当期余额，存一份到 balance_snapshots (12小时后自动清理)。
    明确原始x100 (balance_x100) 与镜像天单位 (balance)，保留原始数值精度，不截断。
    未找到当期余额按真实查询语义（0余额），保留是否有当期行语义(has_record)，不猜成镜像余额。
    包含可验证来源版本 version='v2' 和期间信息 period。
    userids=None 时快照全员; 传 list 时只快照涉及的人(精准备份); 显式空列表 [] 保持无操作。
    """
    if userids is not None and len(userids) == 0:
        return 0

    if not batch_id or not str(batch_id).strip():
        raise ValueError("快照批次 batch_id 不能为空")

    batch_id = str(batch_id).strip()
    period = period or _current_period()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            # 拒绝已存在的 batch_id，防止快照追加导致污染
            check_cur = await db.execute("SELECT 1 FROM balance_snapshots WHERE batch_id=? LIMIT 1", (batch_id,))
            if await check_cur.fetchone():
                await db.rollback()
                raise ValueError(f"快照批次 {batch_id} 已存在，拒绝重复写入")

            # 1. 获取目标人员名单 (userid, name)
            emp_rows = []
            if userids is not None:
                dedup_userids = list(dict.fromkeys(userids))
                placeholders = ",".join("?" * len(dedup_userids))
                cursor = await db.execute(
                    f"SELECT userid, name FROM employees WHERE userid IN ({placeholders})",
                    dedup_userids
                )
                emp_rows = [dict(r) for r in await cursor.fetchall()]
                # 与批量调整一致：不存在的员工不执行，也不进入该批恢复名单。
            else:
                cursor = await db.execute("SELECT userid, name FROM employees")
                emp_rows = [dict(r) for r in await cursor.fetchall()]

            # 2. 从权威 leave_balances 读取指定 period 的余额
            target_userids = [r["userid"] for r in emp_rows]
            leave_bal_map = {}
            if target_userids:
                lb_placeholders = ",".join("?" * len(target_userids))
                lb_cursor = await db.execute(
                    f"SELECT userid, balance, name FROM leave_balances WHERE period=? AND userid IN ({lb_placeholders})",
                    [period] + target_userids
                )
                lb_rows = await lb_cursor.fetchall()
                for r in lb_rows:
                    bal_val = float(r["balance"] or 0)
                    if not math.isfinite(bal_val):
                        bal_val = 0.0
                    leave_bal_map[r["userid"]] = {
                        "balance_x100": bal_val,
                        "name": r["name"] or ""
                    }

            now_dt = datetime.now()
            ts = now_dt.isoformat()
            expires = (now_dt + timedelta(hours=12)).isoformat()
            insert_params = []
            for emp in emp_rows:
                uid = emp["userid"]
                emp_name = emp.get("name") or uid
                if uid in leave_bal_map:
                    lb_info = leave_bal_map[uid]
                    has_record = 1
                    bal_x100 = float(lb_info["balance_x100"])
                    bal_days = bal_x100 / 100
                    display_name = lb_info["name"] or emp_name
                else:
                    # 真实查询语义: 无当期记录返回0，不猜镜像余额，保留无记录标记
                    has_record = 0
                    bal_x100 = 0.0
                    bal_days = 0.0
                    display_name = emp_name

                insert_params.append((
                    batch_id, uid, display_name, bal_days, bal_x100,
                    has_record, period, SNAPSHOT_VERSION, snapshot_type, ts, expires
                ))

            if insert_params:
                await db.executemany(
                    """
                    INSERT INTO balance_snapshots
                    (batch_id, userid, name, balance, balance_x100, has_record, period, version, snapshot_type, created_at, expires_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    insert_params
                )
            await db.commit()
            return len(insert_params)
        except Exception:
            await db.rollback()
            raise


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
                   MIN(created_at) as created_at, MIN(expires_at) as expires_at,
                   MIN(period) as period, MIN(version) as version
            FROM balance_snapshots GROUP BY batch_id ORDER BY MIN(created_at) DESC LIMIT ?
        """, (limit,))
        rows = await cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("expires_at") is None:
                d["expires_at"] = ""
            if d.get("period") is None:
                d["period"] = ""
            if d.get("version") is None:
                d["version"] = ""
            result.append(d)
        return result


async def rollback_snapshot(batch_id: str, operator: str = "rollback"):
    """回滚某次快照: 数据库层恢复逻辑（两个回滚入口共用）。
    同一连接同一原子事务(BEGIN IMMEDIATE)内完成：
    1. 校验快照存在性、完整性、版本、月份、有效期、无重复、员工存在性及数值有限性(fail closed)；
    2. 恢复前为本次目标记录当下权威状态的可信v2 pre_rollback快照(批次ID与quota_logs一致，不丢精度，保留无行语义)；
    3. 恢复权威余额(leave_balances)、镜像(employees)及必要流水(balance_transactions + quota_logs)；
    4. 任意校验不通过或异常整批回滚，零新增备份、流水、余额写入。
    返回 {"ok": bool, "total": int, "success": int, "failed": int, "message": str, "details": list}
    """
    if not batch_id or not str(batch_id).strip():
        return {
            "ok": False,
            "total": 0,
            "success": 0,
            "failed": 0,
            "message": "快照批次ID不能为空",
            "details": [],
        }

    batch_id = str(batch_id).strip()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async def _fail(message: str, count: int = 0):
            await db.rollback()
            return {
                "ok": False,
                "total": count,
                "success": 0,
                "failed": count,
                "message": message,
                "details": [],
            }

        try:
            await db.execute("BEGIN IMMEDIATE")
            # 1. 事务内读取快照行
            cursor = await db.execute(
                """
                SELECT id, batch_id, userid, name, balance, balance_x100,
                       has_record, period, version, snapshot_type, created_at, expires_at
                FROM balance_snapshots WHERE batch_id=? ORDER BY id
                """,
                (batch_id,)
            )
            rows = [dict(r) for r in await cursor.fetchall()]

            # 批次存在性校验
            if not rows:
                return await _fail("找不到该批次的备份(可能已过期清理)", 0)

            total = len(rows)
            now = datetime.now()
            now_iso = now.isoformat()
            current_period = _current_period()

            # 2. 校验工号有效性及重复人员
            userids = [r.get("userid") for r in rows]
            if any(not uid for uid in userids) or len(userids) != len(set(userids)):
                return await _fail(f"快照批次 {batch_id} 包含重复人员或空工号记录，拒绝回滚", total)

            # 3. 校验不可信旧快照、缺少版本或期间、混合版本或混合月份 (fail closed)
            versions = {r.get("version") for r in rows}
            periods = {r.get("period") for r in rows}
            if len(versions) > 1 or len(periods) > 1:
                return await _fail(f"快照批次 {batch_id} 包含混合版本或混合月份数据，拒绝回滚", total)

            snapshot_version = next(iter(versions))
            snapshot_period = next(iter(periods))

            if not snapshot_version or snapshot_version != SNAPSHOT_VERSION:
                return await _fail(
                    f"快照批次 {batch_id} 为不可信旧版本快照(缺少权威来源版本或期间信息)，为防止脏数据覆盖已拒绝回滚",
                    total
                )

            if not snapshot_period:
                return await _fail(f"快照批次 {batch_id} 缺少期间信息，拒绝回滚", total)

            # 4. 校验跨月回滚 (限定所属月份，不能跨月恢复)
            if snapshot_period != current_period:
                return await _fail(
                    f"跨月回滚已拒绝：快照月份({snapshot_period})与当前月份({current_period})不一致",
                    total
                )

            # 5. 校验 has_record, balance_x100, expires_at, created_at (fail closed: 不得默认为0或except pass)
            for r in rows:
                has_rec = r.get("has_record")
                if has_rec is None or has_rec not in (0, 1):
                    return await _fail(f"快照批次 {batch_id} 存在无效或缺失的 has_record 标记，拒绝回滚", total)

                bal_x100 = r.get("balance_x100")
                if bal_x100 is None:
                    return await _fail(f"快照批次 {batch_id} 存在缺失 balance_x100 的记录，拒绝回滚", total)
                try:
                    bal_x100_float = float(bal_x100)
                except (ValueError, TypeError):
                    return await _fail(f"快照批次 {batch_id} 存在非数值 balance_x100 记录，拒绝回滚", total)
                if not math.isfinite(bal_x100_float):
                    return await _fail(f"快照批次 {batch_id} 存在非有限数值(NaN/Inf) balance_x100，拒绝回滚", total)

                # expires_at: v2快照必须有效且未过期
                exp = r.get("expires_at")
                if not exp or not isinstance(exp, str) or not exp.strip():
                    return await _fail(f"快照批次 {batch_id} 缺少有效过期时间 expires_at，拒绝回滚", total)
                try:
                    exp_dt = datetime.fromisoformat(exp.strip())
                except Exception:
                    return await _fail(f"快照批次 {batch_id} 过期时间 expires_at 格式无效，拒绝回滚", total)
                if exp_dt < now:
                    return await _fail(f"快照批次 {batch_id} 已过期(失效时间: {exp})，拒绝回滚", total)

                # created_at: 若存在且解析超12小时拒绝，解析异常也 fail closed
                created = r.get("created_at")
                if created and isinstance(created, str) and created.strip():
                    try:
                        created_dt = datetime.fromisoformat(created.strip())
                        if (now - created_dt) > timedelta(hours=12):
                            return await _fail(f"快照批次 {batch_id} 创建时间已超过12小时，已过期拒绝回滚", total)
                    except Exception:
                        return await _fail(f"快照批次 {batch_id} 创建时间 created_at 格式无效，拒绝回滚", total)

            # 6. 校验目标员工存在性 (fail closed: 目标员工已缺失时明确拒绝整批，不能成功计数但镜像不存在)
            placeholders = ",".join("?" * len(userids))
            emp_cursor = await db.execute(
                f"SELECT userid, name, current_balance FROM employees WHERE userid IN ({placeholders})",
                userids
            )
            emp_rows = await emp_cursor.fetchall()
            found_emp_map = {row["userid"]: dict(row) for row in emp_rows}
            missing_uids = [uid for uid in userids if uid not in found_emp_map]
            if missing_uids:
                return await _fail(
                    f"快照批次 {batch_id} 目标员工已缺失({','.join(missing_uids[:5])})，拒绝回滚",
                    total
                )

            # 7. 在同一事务内记录可信 v2 pre_rollback 快照：
            # 必须记录当下权威状态(leave_balances)，批次ID与quota_logs一致，保留无行/月份/单位语义，不丢精度
            rollback_batch_id = f"rollback_{uuid.uuid4().hex}"
            pre_rollback_expires = (now + timedelta(hours=12)).isoformat()

            lb_cursor = await db.execute(
                f"SELECT userid, balance, name FROM leave_balances WHERE period=? AND userid IN ({placeholders})",
                [snapshot_period] + userids
            )
            lb_rows = await lb_cursor.fetchall()
            current_lb_map = {row["userid"]: dict(row) for row in lb_rows}

            pre_rollback_params = []
            for uid in userids:
                if uid in current_lb_map:
                    cur_lb = current_lb_map[uid]
                    cur_has_rec = 1
                    cur_lb_x100 = float(cur_lb["balance"] or 0)
                    cur_name = cur_lb["name"] or found_emp_map[uid].get("name") or uid
                else:
                    cur_has_rec = 0
                    cur_lb_x100 = 0.0
                    cur_name = found_emp_map[uid].get("name") or uid
                cur_days = (cur_lb_x100 / 100) if cur_has_rec else 0.0

                pre_rollback_params.append((
                    rollback_batch_id, uid, cur_name, cur_days, cur_lb_x100,
                    cur_has_rec, snapshot_period, SNAPSHOT_VERSION, "pre_rollback",
                    now_iso, pre_rollback_expires
                ))

            await db.executemany(
                """
                INSERT INTO balance_snapshots
                (batch_id, userid, name, balance, balance_x100, has_record, period, version, snapshot_type, created_at, expires_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                pre_rollback_params
            )

            # 8. 执行恢复：权威表(leave_balances) + 镜像(employees) + balance_transactions + quota_logs
            details = []
            for r in rows:
                uid = r["userid"]
                emp_info = found_emp_map[uid]
                name = r.get("name") or emp_info.get("name") or uid
                has_rec = int(r["has_record"])
                target_bal_x100 = float(r["balance_x100"])
                target_days = (target_bal_x100 / 100) if has_rec else 0.0

                # 回滚前当下权威余额
                if uid in current_lb_map:
                    cur_lb = current_lb_map[uid]
                    old_lb_has_rec = 1
                    old_lb_x100 = float(cur_lb["balance"] or 0)
                else:
                    old_lb_has_rec = 0
                    old_lb_x100 = 0.0
                old_days = (old_lb_x100 / 100) if old_lb_has_rec else 0.0

                # 恢复权威余额 leave_balances
                if has_rec:
                    await db.execute("""
                        INSERT INTO leave_balances (userid, name, balance, period, updated_at, created_at)
                        VALUES (?,?,?,?,?,?)
                        ON CONFLICT(userid, period) DO UPDATE SET
                            balance=excluded.balance, name=excluded.name, updated_at=excluded.updated_at
                    """, (uid, name, target_bal_x100, snapshot_period, now_iso, now_iso))
                else:
                    # 快照时无当期行：删除可能被新建的当期行，保留无行语义，避免破坏惰性结转
                    await db.execute(
                        "DELETE FROM leave_balances WHERE userid=? AND period=?",
                        (uid, snapshot_period)
                    )

                # 恢复员工镜像 employees (不丢精度)
                await db.execute(
                    "UPDATE employees SET current_balance=?, updated_at=? WHERE userid=?",
                    (target_days, now_iso, uid)
                )

                # 写入 balance_transactions 流水 (不丢精度)
                delta_x100 = target_bal_x100 - old_lb_x100
                reason = f"快照回滚({batch_id})"
                ref_id = f"rollback:{batch_id}:{uid}"
                await db.execute("""
                    INSERT INTO balance_transactions
                    (userid, name, action, days, balance_before, balance_after, reason, ref_id, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (
                    uid, name, "rollback", delta_x100, old_lb_x100, target_bal_x100,
                    reason, ref_id, now_iso
                ))

                # 写入 quota_logs 审计：old_value 必须来自权威余额/100，不能取脏镜像 (不丢精度)
                delta_days = target_days - old_days
                await db.execute("""
                    INSERT INTO quota_logs
                    (batch_id, userid, name, old_value, new_value, delta, reason, operator, status, error_msg, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    rollback_batch_id, uid, name, old_days, target_days,
                    delta_days, reason, operator, "success", "", now_iso
                ))

                details.append({
                    "userid": uid,
                    "name": name,
                    "success": True,
                    "restored_to": target_days,
                    "restored_x100": target_bal_x100,
                    "has_record": has_rec
                })

            await db.commit()
            return {
                "ok": True,
                "total": total,
                "success": total,
                "failed": 0,
                "message": f"成功回滚批次 {batch_id}，共恢复 {total} 人",
                "details": details,
            }
        except Exception as exc:
            await db.rollback()
            return {
                "ok": False,
                "total": total if "total" in locals() else 0,
                "success": 0,
                "failed": total if "total" in locals() else 0,
                "message": f"回滚事务执行异常，已全量回滚: {str(exc)}",
                "details": [],
            }


async def get_snapshot_detail(batch_id: str):
    """查看某次快照的明细"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT userid, name, balance, balance_x100, has_record, period, version, created_at, expires_at
            FROM balance_snapshots WHERE batch_id=? ORDER BY name
            """,
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



async def update_employee_hired_date(userid: str, hired_date: str):
    """回写员工入职日期。通讯录列表接口没有该字段，详情接口补取后调用。"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE employees SET hired_date=?, updated_at=? WHERE userid=?",
            (hired_date or "", datetime.now().isoformat(), userid)
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

async def prepare_hourly_monthly_resets(period: str, batch_id: str, employees: list):
    """首次执行时固定当月小时工名单；重跑只返回原名单。"""
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT batch_id FROM hourly_monthly_reset_runs WHERE period=?",
            (period,),
        )
        run = await cursor.fetchone()
        created = run is None
        if created:
            await db.execute(
                "INSERT INTO hourly_monthly_reset_runs (period, batch_id, created_at) VALUES (?,?,?)",
                (period, batch_id, now),
            )
            if employees:
                await db.executemany(
                    "INSERT INTO hourly_monthly_resets "
                    "(period, userid, name, batch_id, status, created_at, updated_at) "
                    "VALUES (?,?,?,?, 'pending', ?, ?)",
                    [
                        (period, emp["userid"], emp.get("name") or emp["userid"], batch_id, now, now)
                        for emp in employees
                    ],
                )
        cursor = await db.execute(
            "SELECT period, userid, name, batch_id, status, old_balance, new_balance, error_msg "
            "FROM hourly_monthly_resets WHERE period=? ORDER BY userid",
            (period,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await db.commit()
    return {"created": created, "rows": rows}


async def apply_hourly_monthly_reset(period: str, userid: str, days: float, month_type: str):
    """原子地把小时工当月额度重置为固定天数；成功记录再次调用时直接跳过。"""
    target_x100 = round(days * 100, 2)
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT * FROM hourly_monthly_resets WHERE period=? AND userid=?",
            (period, userid),
        )
        reset = await cursor.fetchone()
        if reset is None:
            await db.rollback()
            raise RuntimeError(f"小时工月初重置名单不存在: {period}/{userid}")
        if reset["status"] == "success":
            await db.commit()
            return {"idempotent": True, "old_x100": reset["old_balance"], "new_x100": reset["new_balance"]}

        cursor = await db.execute(
            "SELECT balance, name FROM leave_balances WHERE userid=? AND period=?",
            (userid, period),
        )
        balance_row = await cursor.fetchone()
        old_x100 = float(balance_row["balance"] or 0) if balance_row else 0.0
        name = reset["name"] or (balance_row["name"] if balance_row else "") or userid
        delta_x100 = round(target_x100 - old_x100, 2)
        reason = f"小时工月初重置({month_type})为{days:g}天"
        ref_id = f"hourly_monthly_reset:{period}:{userid}"

        await db.execute(
            "INSERT INTO leave_balances "
            "(userid, name, balance, period, month_type, updated_at, created_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(userid, period) DO UPDATE SET "
            "balance=excluded.balance, name=excluded.name, month_type=excluded.month_type, updated_at=excluded.updated_at",
            (userid, name, target_x100, period, month_type, now, now),
        )
        await db.execute(
            "UPDATE employees SET current_balance=?, updated_at=? WHERE userid=?",
            (round(target_x100 / 100, 2), now, userid),
        )
        await db.execute(
            "INSERT INTO balance_transactions "
            "(userid, name, action, days, balance_before, balance_after, reason, ref_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (userid, name, "monthly_reset", delta_x100, old_x100, target_x100, reason, ref_id, now),
        )
        await db.execute(
            "INSERT INTO quota_logs "
            "(batch_id, userid, name, old_value, new_value, delta, reason, operator, status, error_msg, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                reset["batch_id"], userid, name, round(old_x100 / 100, 2), days,
                round(delta_x100 / 100, 2), reason, "cron", "success", "", now,
            ),
        )
        await db.execute(
            "UPDATE hourly_monthly_resets "
            "SET status='success', old_balance=?, new_balance=?, error_msg='', updated_at=? "
            "WHERE period=? AND userid=?",
            (old_x100, target_x100, now, period, userid),
        )
        await db.commit()
    return {"idempotent": False, "old_x100": old_x100, "new_x100": target_x100}


async def mark_hourly_monthly_reset_failed(period: str, userid: str, error_msg: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE hourly_monthly_resets SET status='failed', error_msg=?, updated_at=? "
            "WHERE period=? AND userid=? AND status!='success'",
            (error_msg[:500], datetime.now().isoformat(), period, userid),
        )
        await db.commit()


async def needs_hourly_initialization(userid: str, period: str = None) -> bool:
    """判断小时工当前周期是否需要补发首次5天额度。

    新员工由调用方直接初始化。既有小时工在月初重置名单建立前即使尚无当月行，
    也不抢先发放；名单建立后若重置失败或缺行，通讯录同步才会补发。0余额占位
    仍沿用原重试规则，已经真实用完5天的员工不会重新发放。
    """
    period = period or _current_period()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT balance, created_at FROM leave_balances WHERE userid=? AND period=?",
            (userid, period),
        )
        row = await cursor.fetchone()
        if row is None:
            cursor = await db.execute(
                "SELECT 1 FROM hourly_monthly_resets WHERE period=? AND userid=? LIMIT 1",
                (period, userid),
            )
            return await cursor.fetchone() is not None
        if float(row[0] or 0) != 0:
            return False
        cursor = await db.execute(
            "SELECT 1 FROM balance_transactions "
            "WHERE userid=? AND created_at >= COALESCE(?, '') "
            "AND action NOT IN ('carryover') LIMIT 1",
            (userid, row[1]),
        )
        return await cursor.fetchone() is None


async def _carryover_in_transaction(db, userid: str, period: str):
    # Recheck under the caller's write lock so a stale missing-row read cannot
    # overwrite a balance created by another request.
    cursor = await db.execute(
        "SELECT balance, name FROM leave_balances WHERE userid=? AND period=?",
        (userid, period),
    )
    row = await cursor.fetchone()
    if row:
        return {"balance": row[0], "name": row[1], "period": period}
    carry, name, carry_from = 0.0, userid, None
    for back in range(1, 13):
        prev_period = _add_month(period, -back)
        cursor = await db.execute(
            "SELECT balance, name FROM leave_balances WHERE userid=? AND period=?",
            (userid, prev_period),
        )
        row = await cursor.fetchone()
        if row and row[1]:
            carry, name, carry_from = row[0], row[1], prev_period
            break
    now = datetime.now().isoformat()
    await db.execute(
        "INSERT INTO leave_balances (userid, name, balance, period, updated_at, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (userid, name, carry, period, now, now),
    )
    await _insert_transaction(
        db, userid, name, "carryover", carry, 0.0, carry,
        f"月度结转({carry_from})" if carry_from else "月度结转(无历史,建0行)",
    )
    return {"balance": carry, "name": name, "period": period}


async def _lazy_carryover(userid: str, period: str = None):
    """缺失月份的余额及结转流水一起提交，不覆盖并发创建的余额。"""
    period = period or _current_period()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        balance = await _carryover_in_transaction(db, userid, period)
        await _sync_current_balance(db, userid, period, balance["balance"])
        await db.commit()
    return balance


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


async def create_future_reservation(userid: str, name: str, days: float, leave_start_date: str, target_period: str, ref_id: str, reason: str = "", *, month_type: str):
    """按目标月规则锁内复核额度，再创建预占；预检查不能代替此处。"""
    if not ref_id or not ref_id.strip():
        return {"ok": False, "message": "下月公休预占必须提供 ref_id 用于幂等"}
    if not math.isfinite(days) or days <= 0:
        return {"ok": False, "message": "预占额度必须是大于0的有效数值"}
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        db.row_factory = aiosqlite.Row
        # 预占与普通扣减必须争用同一写锁，不能先扣当月再以同号占下月。
        cursor = await db.execute(
            "SELECT 1 FROM balance_transactions WHERE userid=? AND ref_id=? AND action='deduct' LIMIT 1",
            (userid, ref_id.strip()),
        )
        if await cursor.fetchone():
            return {"ok": False, "message": "原单已有普通扣减记录，请核对原单，未创建预占"}
        cursor = await db.execute(
            "SELECT * FROM future_leave_reservations WHERE ref_id=?", (ref_id.strip(),),
        )
        row = await cursor.fetchone()
        if row:
            existing = dict(row)
            if any(existing[key] != value for key, value in (
                ("userid", userid), ("days", days),
                ("leave_start_date", leave_start_date), ("target_period", target_period),
            )):
                return {"ok": False, "message": "同一原单的预占人员、额度或日期与已有记录不一致"}
            if existing["status"] not in ("pending", "applied"):
                return {"ok": False, "message": "原预占已取消或需核对，不能重新放行"}
            return {"ok": True, "idempotent": True, "reservation": existing, "message": "重复预占已忽略(幂等)"}
        cursor = await db.execute(
            "SELECT category FROM employees WHERE userid=?", (userid,),
        )
        employee = await cursor.fetchone()
        if employee is None:
            return {"ok": False, "message": "员工不存在"}
        cursor = await db.execute(
            "SELECT days FROM quota_rules WHERE month_type=? AND category=?",
            (month_type, employee["category"] or "other"),
        )
        rule = await cursor.fetchone()
        limit = round(float(rule["days"] or 0) * 100, 2) if rule else 0
        cursor = await db.execute(
            "SELECT COALESCE(SUM(days), 0) FROM future_leave_reservations "
            "WHERE userid=? AND target_period=? AND status='pending'",
            (userid, target_period),
        )
        reserved = round(float((await cursor.fetchone())[0]), 2)
        if reserved + days > limit:
            return {
                "ok": False, "message": "下月公休额度不足", "target_period": target_period,
                "limit": limit, "reserved": reserved, "available": max(limit - reserved, 0), "need": days,
            }
        now = datetime.now().isoformat()
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


async def cancel_future_reservation(ref_id: str, reason: str = "", *, expected_status: str = None, userid: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM future_leave_reservations WHERE ref_id=?", (ref_id.strip(),))
        row = await cursor.fetchone()
        if not row:
            return None
        existing = dict(row)
        if userid is not None and existing["userid"] != userid:
            return {"ok": False, "message": "预占人员与请求不一致"}
        status = existing["status"]
        if status == "cancelled":
            return {"ok": True, "idempotent": True, "mode": "future", "status": "cancelled", "message": "预占已取消(幂等)"}
        if expected_status is not None and status != expected_status:
            return {"ok": False, "message": "预占状态已变化，请重新核对后重试"}
        if status in ("pending", "failed"):
            cursor = await db.execute(
                "SELECT 1 FROM balance_transactions WHERE userid=? AND ref_id=? AND action='deduct' LIMIT 1",
                (existing["userid"], f"future_apply:{ref_id.strip()}"),
            )
            if await cursor.fetchone():
                return {"ok": False, "message": "预占存在实扣记录，需核对原扣后处理"}
        elif status == "applied":
            transaction_id = existing.get("applied_transaction_id")
            if transaction_id is None:
                return {"ok": False, "message": "历史预占缺少可靠实扣关联，需核实后退款"}
            apply_ref = f"future_apply:{ref_id.strip()}"
            cursor = await db.execute(
                "SELECT userid, action, days, ref_id FROM balance_transactions WHERE id=?",
                (transaction_id,),
            )
            original = await cursor.fetchone()
            if (not original or original["userid"] != existing["userid"]
                    or original["action"] != "deduct" or original["ref_id"] != apply_ref
                    or original["days"] >= 0 or -original["days"] != existing["days"]):
                return {"ok": False, "message": "预占与原扣流水不一致，未退款"}
            # Legacy/manual refund paths must not be combined with a second refund.
            cursor = await db.execute(
                "SELECT 1 FROM balance_transactions WHERE userid=? AND action='refund' "
                "AND ref_id IN (?, ?, ?) LIMIT 1",
                (existing["userid"], apply_ref, ref_id.strip(), f"future_refund:{ref_id.strip()}"),
            )
            if await cursor.fetchone():
                return {"ok": False, "message": "预占已有退款流水但状态未取消，需核对后处理"}
            period = existing["target_period"]
            cursor = await db.execute(
                "SELECT balance, name FROM leave_balances WHERE userid=? AND period=?",
                (existing["userid"], period),
            )
            balance_row = await cursor.fetchone()
            if not balance_row:
                return {"ok": False, "message": "原扣账期余额记录缺失，未退款"}
            current = balance_row["balance"]
            amount = -original["days"]
            new_balance = round(current + amount, 2)
            await db.execute(
                "UPDATE leave_balances SET balance=?, updated_at=? WHERE userid=? AND period=?",
                (new_balance, datetime.now().isoformat(), existing["userid"], period),
            )
            await _insert_transaction(
                db, existing["userid"], balance_row["name"] or existing["userid"],
                "refund", amount, current, new_balance,
                reason or "撤销已应用的下月公休预占", apply_ref,
            )
            await _sync_current_balance(db, existing["userid"], period, new_balance)
        else:
            return {"ok": False, "message": "预占状态不支持自动取消"}
        now = datetime.now().isoformat()
        await db.execute(
            "UPDATE future_leave_reservations SET status='cancelled', cancelled_at=?, reason=COALESCE(NULLIF(reason,''), ?) WHERE ref_id=?",
            (now, reason or existing.get("reason") or "撤销下月公休预占", ref_id.strip()),
        )
        await db.commit()
    result = {"ok": True, "mode": "future", "status": "cancelled", "message": "已取消下月公休预占", "reservation": existing}
    if status == "applied":
        result["balance"] = new_balance
    return result


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


async def apply_future_reservation(ref_id: str, target_period: str):
    """锁内重查预占，实扣与applied状态一次提交。"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM future_leave_reservations WHERE ref_id=?", (ref_id.strip(),))
        row = await cursor.fetchone()
        if not row:
            return {"ok": False, "message": "预占不存在"}
        r = dict(row)
        if r["target_period"] != target_period:
            return {"ok": False, "message": "预占目标月份不一致"}
        if r["status"] in ("cancelled", "applied"):
            return {"ok": True, "skipped": True, "status": r["status"], "message": "预占已处理，跳过"}
        if r["status"] != "pending":
            return {"ok": False, "message": "预占非待处理状态，需核对后处理"}
        apply_ref = f"future_apply:{ref_id.strip()}"
        if await _find_transaction(db, r["userid"], apply_ref, "deduct"):
            return {"ok": False, "message": "待处理预占已有实扣流水，需核对原扣，未再次扣减"}
        bal = await _carryover_in_transaction(db, r["userid"], target_period)
        current = bal["balance"]
        if current < r["days"]:
            return {"ok": False, "message": f"余额不足: 当前{current} 需要{r['days']}"}
        new_balance = round(current - r["days"], 2)
        now = datetime.now().isoformat()
        await db.execute(
            "UPDATE leave_balances SET balance=?, updated_at=? WHERE userid=? AND period=?",
            (new_balance, now, r["userid"], target_period),
        )
        transaction_id = await _insert_transaction(
            db, r["userid"], bal["name"] or r["userid"], "deduct", -r["days"],
            current, new_balance, "月底提交下月公休自动扣减", apply_ref,
        )
        await _sync_current_balance(db, r["userid"], target_period, new_balance)
        await db.execute(
            "UPDATE future_leave_reservations SET status='applied', applied_at=?, error_msg='', applied_transaction_id=? WHERE ref_id=?",
            (now, transaction_id, ref_id.strip()),
        )
        await db.commit()
    return {"ok": True, "status": "applied", "balance": new_balance}


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
    if not bal.get("name"):
        # 当月无行: 惰性结转, 避免误报"余额不足: 当前0天"
        bal = await _lazy_carryover(userid, period)
    current = bal["balance"]
    ok = current >= days
    return {"ok": ok, "balance": current, "need": days}


async def _find_transaction(db, userid: str, ref_id: str, action: str):
    if not ref_id or not ref_id.strip():
        return None
    cursor = await db.execute(
        "SELECT balance_after, days, created_at FROM balance_transactions "
        "WHERE userid=? AND ref_id=? AND action=? ORDER BY id DESC LIMIT 1",
        (userid, ref_id.strip(), action),
    )
    row = await cursor.fetchone()
    return {"balance_after": row[0], "days": row[1], "created_at": row[2]} if row else None


async def _check_idempotent(userid: str, ref_id: str, action: str):
    async with aiosqlite.connect(DB_PATH) as db:
        return await _find_transaction(db, userid, ref_id, action)


async def _insert_transaction(db, userid, name, action, days, before, after, reason, ref_id=""):
    cursor = await db.execute(
        "INSERT INTO balance_transactions "
        "(userid, name, action, days, balance_before, balance_after, reason, ref_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (userid, name, action, days, before, after, reason, ref_id, datetime.now().isoformat()),
    )
    return cursor.lastrowid


async def _add_transaction(userid, name, action, days, before, after, reason, ref_id=""):
    async with aiosqlite.connect(DB_PATH) as db:
        await _insert_transaction(db, userid, name, action, days, before, after, reason, ref_id)
        await db.commit()


async def _sync_current_balance(db, userid: str, period: str, balance):
    # Updating the mirror within the write transaction avoids publishing an older
    # balance after a newer request has already committed.
    if period == _current_period():
        await db.execute(
            "UPDATE employees SET current_balance=?, updated_at=? WHERE userid=?",
            (round(balance / 100, 2), datetime.now().isoformat(), userid),
        )


async def deduct_leave_balance(userid: str, days: float, reason: str, ref_id: str = "", period: str = None):
    """余额、扣减流水及惰性结转在同一写事务内提交。"""
    if not math.isfinite(days) or days <= 0:
        return {"ok": False, "message": "扣减额度必须是大于0的有效数值", "balance": None}
    if ref_id and ref_id != ref_id.strip():
        return {"ok": False, "message": "原单标识不能带首尾空白，请核对原单", "balance": None}
    period = period or _current_period()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        if ref_id:
            # 日期跨月后不能把已登记的预占当作一笔新的当月扣减。
            # 自动回放须等原单身份与日期契约明确；此处只阻断二次写账。
            cursor = await db.execute(
                "SELECT 1 FROM future_leave_reservations WHERE ref_id=?", (ref_id,),
            )
            if await cursor.fetchone():
                return {"ok": False, "message": "原单已有预占记录，请按原预占核对处理，未再次扣减", "balance": None}
        existing = await _find_transaction(db, userid, ref_id, "deduct")
        if existing:
            if existing["days"] != -days:
                return {"ok": False, "message": "同一原单的扣减额度与已处理记录不一致", "balance": None}
            return {"ok": True, "balance": existing["balance_after"], "message": "重复扣减已忽略(幂等)", "idempotent": True}
        bal = await _carryover_in_transaction(db, userid, period)
        current = bal["balance"]
        if current < days:
            return {"ok": False, "message": f"余额不足: 当前{current}天 需要{days}天", "balance": current}
        new_balance = round(current - days, 2)
        name = bal["name"] or userid
        await db.execute(
            "UPDATE leave_balances SET balance=?, name=?, updated_at=? WHERE userid=? AND period=?",
            (new_balance, name, datetime.now().isoformat(), userid, period),
        )
        await _insert_transaction(db, userid, name, "deduct", -days, current, new_balance, reason, ref_id)
        await _sync_current_balance(db, userid, period, new_balance)
        await db.commit()
    return {"ok": True, "balance": new_balance, "message": f"扣减{days}(×100),余额{current}->{new_balance}"}


async def refund_leave_balance(userid: str, days: float, reason: str, ref_id: str = "", period: str = None):
    """原扣检查、退款上限、余额与流水共用写事务。"""
    if not math.isfinite(days) or days <= 0:
        return {"ok": False, "message": "返还额度必须是大于0的有效数值", "balance": None}
    # 旧查询会strip而流水保留原值；拒绝这类新退款，避免重试漏查已返记录。
    if ref_id and ref_id != ref_id.strip():
        return {"ok": False, "message": "原单标识不能带首尾空白，请核对原单", "balance": None}
    period = period or _current_period()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        existing = await _find_transaction(db, userid, ref_id, "refund")
        if existing:
            if existing["days"] != days:
                return {"ok": False, "message": "同一原单的返还额度与已处理记录不一致", "balance": None}
            return {"ok": True, "balance": existing["balance_after"], "message": "重复回退已忽略(幂等)", "idempotent": True}
        if ref_id and ref_id.strip():
            cursor = await db.execute(
                "SELECT days FROM balance_transactions WHERE userid=? AND ref_id=? AND action='deduct'",
                (userid, ref_id.strip()),
            )
            deductions = await cursor.fetchall()
            if not deductions:
                return {"ok": False, "message": "未找到对应的请假扣减记录,无法回退", "balance": None}
            # 历史同号多笔不能靠时间或最新一笔猜原扣；保留原账交人工核实。
            if len(deductions) != 1:
                return {"ok": False, "message": "原单存在多笔扣减记录，请核对后处理", "balance": None}
            deducted = deductions[0][0]
            if deducted is None or not math.isfinite(deducted) or deducted >= 0:
                return {"ok": False, "message": "原扣减额度无法核实，请核对后处理", "balance": None}
            if days > -deducted:
                return {"ok": False, "message": "返还额度不能超过原扣减额度", "balance": None}
        cursor = await db.execute(
            "SELECT balance, name FROM leave_balances WHERE userid=? AND period=?",
            (userid, period),
        )
        row = await cursor.fetchone()
        current, name = (row[0], row[1] or userid) if row else (0, userid)
        new_balance = round(current + days, 2)
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT INTO leave_balances (userid, name, balance, period, updated_at, created_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(userid, period) DO UPDATE SET "
            "balance=excluded.balance, updated_at=excluded.updated_at",
            (userid, name, new_balance, period, now, now),
        )
        await _insert_transaction(db, userid, name, "refund", days, current, new_balance, reason, ref_id)
        await _sync_current_balance(db, userid, period, new_balance)
        await db.commit()
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


async def get_leave_operation_evidence(ref_id: str):
    """Return one consistent ledger snapshot, without choosing a historical debit."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        cursor = await db.execute(
            "SELECT id, userid, days, leave_start_date, target_period, ref_id, status, "
            "applied_transaction_id, created_at, applied_at, cancelled_at "
            "FROM future_leave_reservations WHERE ref_id=?", (ref_id,),
        )
        row = await cursor.fetchone()
        reservation = dict(row) if row else None
        # Include all people/actions so a duplicate or conflicting row cannot be hidden.
        cursor = await db.execute(
            "SELECT id, userid, action, days, balance_before, balance_after, ref_id, created_at "
            "FROM balance_transactions WHERE ref_id IN (?, ?, ?) OR id=? ORDER BY id",
            (ref_id, f"future_apply:{ref_id}", f"future_refund:{ref_id}",
             reservation["applied_transaction_id"] if reservation else None),
        )
        transactions = [dict(row) for row in await cursor.fetchall()]
        return {
            "ref_id": ref_id, "unit": "DAY_X100",
            "transactions": transactions, "reservation": reservation,
            "verification_state": "unverified",
        }


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
