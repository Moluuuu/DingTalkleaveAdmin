"""windows_restore_leave_balance 测试：dry-run 读取 leave_balances 生成 quota/update payload、
真实执行参数保护、从备份包恢复。所有钉钉调用走 fake client，绝不访问真实 API。"""
import pytest

from leaveadmin import windows_restore_leave_balance as wr
from leaveadmin import dingpan_backup as db_mod


LEAVE_CODE = "TEST_LEAVE_CODE"


class FakeQuotaClient:
    """假的钉钉额度客户端，记录被推送的 quota/update payload。"""

    def __init__(self):
        self.updates = []

    def quota_update(self, payload):
        self.updates.append(payload)
        return {"errcode": 0, "errmsg": "ok"}


# ---------- payload 构造（单位 ×100，不换算成天） ----------

def test_build_quota_payloads_from_db(sample_db):
    payloads = wr.build_quota_payloads(
        db_path=sample_db, leave_code=LEAVE_CODE, op_userid="op001"
    )
    by_uid = {p["userid"]: p for p in payloads}

    # 屏蔽员工 u003 不应进入
    assert "u003" not in by_uid
    assert set(by_uid) == {"u001", "u002"}

    # quota_num_per_day 直接用 leave_balances.balance(×100)，不除以 100
    assert by_uid["u001"]["quota_num_per_day"] == 500
    assert by_uid["u002"]["quota_num_per_day"] == 425

    p = by_uid["u001"]
    assert p["leave_code"] == LEAVE_CODE
    assert p["op_userid"] == "op001"
    assert p["userid"] == "u001"


def test_build_quota_payloads_uses_x100_not_days(sample_db):
    """显式守住单位：425 必须是 425，不能被换算成 4.25。"""
    payloads = wr.build_quota_payloads(
        db_path=sample_db, leave_code=LEAVE_CODE, op_userid="op001"
    )
    vals = {p["userid"]: p["quota_num_per_day"] for p in payloads}
    assert vals["u002"] == 425
    assert all(isinstance(v, int) for v in vals.values())


def test_build_quota_payloads_includes_active_employee_without_balance_as_zero(sample_db):
    """灾备恢复要覆盖所有活跃员工；新员工缺余额行时按 0 推送，不能跳过。"""
    import sqlite3

    conn = sqlite3.connect(str(sample_db))
    conn.execute(
        "INSERT INTO employees (userid, name, dept_name, current_balance, is_disabled) VALUES (?,?,?,?,?)",
        ("u004", "赵六", "No.02店", 0.0, 0),
    )
    conn.commit()
    conn.close()

    payloads = wr.build_quota_payloads(
        db_path=sample_db, leave_code=LEAVE_CODE, op_userid="op001"
    )
    vals = {p["userid"]: p["quota_num_per_day"] for p in payloads}

    assert vals["u004"] == 0
    assert set(vals) == {"u001", "u002", "u004"}


# ---------- dry-run ----------

def test_restore_dry_run_does_not_push(sample_db):
    client = FakeQuotaClient()
    report = wr.restore(
        db_path=sample_db,
        client=client,
        leave_code=LEAVE_CODE,
        op_userid="op001",
        execute=False,
        confirm_overwrite=False,
    )
    assert report["dry_run"] is True
    assert report["executed"] is False
    assert report["target_count"] == 2
    # dry-run 绝不推钉钉
    assert client.updates == []
    # 仍给出摘要供人工核对
    assert len(report["preview"]) == 2


# ---------- 真实执行参数保护 ----------

def test_restore_execute_without_confirm_refuses(sample_db):
    client = FakeQuotaClient()
    with pytest.raises(wr.ConfirmationRequired):
        wr.restore(
            db_path=sample_db,
            client=client,
            leave_code=LEAVE_CODE,
            op_userid="op001",
            execute=True,
            confirm_overwrite=False,  # 缺少显式确认
        )
    # 没有任何推送发生
    assert client.updates == []


def test_restore_execute_with_confirm_pushes(sample_db):
    client = FakeQuotaClient()
    report = wr.restore(
        db_path=sample_db,
        client=client,
        leave_code=LEAVE_CODE,
        op_userid="op001",
        execute=True,
        confirm_overwrite=True,
    )
    assert report["executed"] is True
    assert report["dry_run"] is False
    assert len(client.updates) == 2
    pushed = {u["userid"]: u["quota_num_per_day"] for u in client.updates}
    assert pushed == {"u001": 500, "u002": 425}


# ---------- 从备份包恢复 ----------

def test_restore_from_backup_file(sample_db, tmp_path):
    """模式 B：从钉盘下载的 tar.gz 备份包解出 db 后再读余额。"""
    pkg = db_mod.create_backup_package(db_path=sample_db, out_dir=tmp_path / "out")
    tar_path = pkg["path"]

    client = FakeQuotaClient()
    report = wr.restore_from_backup_file(
        backup_path=tar_path,
        work_dir=tmp_path / "ex",
        client=client,
        leave_code=LEAVE_CODE,
        op_userid="op001",
        execute=False,
        confirm_overwrite=False,
    )
    assert report["dry_run"] is True
    assert report["target_count"] == 2
    assert client.updates == []


def test_restore_from_corrupt_backup_file_rejected(sample_db, tmp_path):
    """损坏的备份包应在恢复前被 sha256 校验拦下。"""
    import tarfile
    pkg = db_mod.create_backup_package(db_path=sample_db, out_dir=tmp_path / "out")
    extracted = db_mod.extract_backup_package(pkg["path"], tmp_path / "ex")
    with open(extracted["db_path"], "ab") as f:
        f.write(b"corruption")
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        tar.add(extracted["db_path"], arcname="admin.db")
        tar.add(extracted["manifest_path"], arcname="manifest.json")

    client = FakeQuotaClient()
    with pytest.raises(db_mod.BackupError):
        wr.restore_from_backup_file(
            backup_path=bad,
            work_dir=tmp_path / "ex2",
            client=client,
            leave_code=LEAVE_CODE,
            op_userid="op001",
            execute=False,
            confirm_overwrite=False,
        )
