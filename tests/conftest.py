"""测试公共夹具：构造临时 SQLite 数据库，绝不触碰生产 admin.db。"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

# 让测试能 import leaveadmin 包和 scripts 工具
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _create_schema(db_path: Path):
    """建出备份/恢复用到的最小表结构，字段与 database.py 保持一致。"""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE employees (
            userid TEXT PRIMARY KEY,
            name TEXT,
            dept_name TEXT,
            is_hourly INTEGER DEFAULT 0,
            category TEXT DEFAULT 'other',
            default_quota REAL DEFAULT 4,
            current_balance REAL DEFAULT 0,
            is_disabled INTEGER DEFAULT 0,
            hired_date TEXT DEFAULT ''
        );
        CREATE TABLE leave_balances (
            userid TEXT,
            name TEXT,
            balance REAL DEFAULT 0,
            period TEXT,
            month_type TEXT,
            updated_at TEXT,
            created_at TEXT,
            PRIMARY KEY (userid, period)
        );
        CREATE TABLE balance_transactions (
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
        );
        """
    )
    conn.commit()
    return conn


@pytest.fixture
def sample_db(tmp_path):
    """一个填了样本数据的临时 admin.db，返回路径。

    leave_balances.balance 单位是 ×100（与生产一致）。
    """
    db_path = tmp_path / "admin.db"
    conn = _create_schema(db_path)
    now = datetime.now().isoformat()
    period = datetime.now().strftime("%Y-%m")

    employees = [
        ("u001", "张三", "No.01店", 5.0, 0),
        ("u002", "李四", "后勤部", 4.0, 0),
        ("u003", "王五", "编外", 0.0, 1),   # 已屏蔽
    ]
    conn.executemany(
        "INSERT INTO employees (userid, name, dept_name, current_balance, is_disabled) VALUES (?,?,?,?,?)",
        employees,
    )

    # leave_balances 存 ×100
    balances = [
        ("u001", "张三", 500.0, period, "normal", now, now),
        ("u002", "李四", 425.0, period, "normal", now, now),  # 4.25 天
        ("u003", "王五", 0.0, period, "normal", now, now),
    ]
    conn.executemany(
        "INSERT INTO leave_balances (userid, name, balance, period, month_type, updated_at, created_at) VALUES (?,?,?,?,?,?,?)",
        balances,
    )

    conn.executemany(
        "INSERT INTO balance_transactions (userid, name, action, days, balance_before, balance_after, reason, ref_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("u001", "张三", "deduct", -100, 600, 500, "请假", "", now),
            ("u002", "李四", "adjust", 25, 400, 425, "补", "", now),
        ],
    )
    conn.commit()
    conn.close()
    return db_path
