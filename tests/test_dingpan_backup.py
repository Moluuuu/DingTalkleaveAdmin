"""dingpan_backup 模块测试：manifest 生成/校验、打包解包、24 份保留策略、上传流程（fake client）。"""
import hashlib
import json
import tarfile

import pytest

from leaveadmin import dingpan_backup as db_mod
from leaveadmin.dingpan_backup import FakeDingpanClient


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- manifest 与打包 ----------

def test_create_package_produces_tar_with_db_and_manifest(sample_db, tmp_path):
    out = tmp_path / "out"
    result = db_mod.create_backup_package(
        db_path=sample_db, out_dir=out, source_host="testhost"
    )
    tar_path = result["path"]
    assert tar_path.exists()
    assert tar_path.suffix == ".gz"

    with tarfile.open(tar_path, "r:gz") as tar:
        names = tar.getnames()
    assert "admin.db" in names
    assert "manifest.json" in names


def test_manifest_fields_and_counts(sample_db, tmp_path):
    result = db_mod.create_backup_package(
        db_path=sample_db, out_dir=tmp_path / "out", source_host="testhost"
    )
    m = result["manifest"]
    for key in (
        "backup_time", "period", "db_size", "db_sha256",
        "employees_active", "leave_balance_rows", "latest_transaction_id",
        "source_host", "version",
    ):
        assert key in m, f"manifest 缺字段 {key}"

    # employees_active 只数未屏蔽的（3 人里 1 人 is_disabled=1）
    assert m["employees_active"] == 2
    # leave_balances 共 3 行
    assert m["leave_balance_rows"] == 3
    # 最新 transaction id = 2
    assert m["latest_transaction_id"] == 2
    assert m["source_host"] == "testhost"
    assert len(m["db_sha256"]) == 64
    assert m["db_size"] > 0


def test_manifest_sha256_matches_db_inside_tar(sample_db, tmp_path):
    out = tmp_path / "out"
    result = db_mod.create_backup_package(db_path=sample_db, out_dir=out)
    tar_path = result["path"]
    extract_dir = tmp_path / "ex"
    extracted = db_mod.extract_backup_package(tar_path, extract_dir)

    real_sha = _sha256(extracted["db_path"])
    manifest = extracted["manifest"]
    assert manifest["db_sha256"] == real_sha


def test_verify_backup_package_ok(sample_db, tmp_path):
    result = db_mod.create_backup_package(db_path=sample_db, out_dir=tmp_path / "out")
    verified = db_mod.verify_backup_package(result["path"])
    assert verified["ok"] is True
    assert verified["manifest"]["db_sha256"] == result["manifest"]["db_sha256"]


def test_verify_backup_package_detects_corruption(sample_db, tmp_path):
    """篡改包内 db 后，校验应失败。"""
    result = db_mod.create_backup_package(db_path=sample_db, out_dir=tmp_path / "out")
    tar_path = result["path"]

    # 解包 -> 改 db -> 用原 manifest 重新打包
    extract_dir = tmp_path / "ex"
    extracted = db_mod.extract_backup_package(tar_path, extract_dir)
    with open(extracted["db_path"], "ab") as f:
        f.write(b"corruption")

    bad_tar = tmp_path / "bad.tar.gz"
    with tarfile.open(bad_tar, "w:gz") as tar:
        tar.add(extracted["db_path"], arcname="admin.db")
        tar.add(extracted["manifest_path"], arcname="manifest.json")

    verified = db_mod.verify_backup_package(bad_tar)
    assert verified["ok"] is False


# ---------- 24 份保留策略 ----------

def test_select_files_to_delete_keeps_latest_24():
    # 构造 30 份，时间戳从旧到新
    files = [
        {"name": f"admin_backup_{i:03d}.tar.gz", "modified_time": 1000 + i, "file_id": f"f{i}"}
        for i in range(30)
    ]
    to_delete = db_mod.select_files_to_delete(files, keep=24)
    # 删 6 份最旧的
    assert len(to_delete) == 6
    deleted_ids = {f["file_id"] for f in to_delete}
    assert deleted_ids == {f"f{i}" for i in range(6)}


def test_select_files_to_delete_under_limit_keeps_all():
    files = [
        {"name": f"b{i}.tar.gz", "modified_time": i, "file_id": f"f{i}"}
        for i in range(10)
    ]
    assert db_mod.select_files_to_delete(files, keep=24) == []


def test_select_files_to_delete_only_targets_backup_files():
    """非备份命名的文件不应被纳入删除候选。"""
    files = [
        {"name": "admin_backup_001.tar.gz", "modified_time": 1, "file_id": "a"},
        {"name": "unrelated.txt", "modified_time": 2, "file_id": "b"},
        {"name": "admin_backup_002.tar.gz", "modified_time": 3, "file_id": "c"},
    ]
    to_delete = db_mod.select_files_to_delete(files, keep=1, name_prefix="admin_backup_")
    deleted_ids = {f["file_id"] for f in to_delete}
    # 只在两个 backup 里删最旧的 a，绝不碰 unrelated.txt
    assert deleted_ids == {"a"}


def test_select_files_to_delete_uses_filename_when_storage_time_is_zero():
    """Storage search can return modified_time=0; then retention must parse backup filename."""
    files = [
        {"name": f"leaveAdmin-backup-20260628-00{minute:02d}00.tar.gz", "modified_time": 0, "file_id": f"f{minute}"}
        for minute in range(30)
    ]
    to_delete = db_mod.select_files_to_delete(files, keep=24, name_prefix="leaveAdmin-backup-")
    deleted_ids = {f["file_id"] for f in to_delete}
    assert deleted_ids == {f"f{i}" for i in range(6)}


# ---------- 上传流程（fake client） ----------

def test_run_backup_dry_run_does_not_upload(sample_db, tmp_path):
    client = FakeDingpanClient()
    report = db_mod.run_backup(
        db_path=sample_db,
        out_dir=tmp_path / "out",
        client=client,
        space_name="leaveadmin-backup",
        keep=24,
        dry_run=True,
    )
    assert report["dry_run"] is True
    assert report["uploaded"] is False
    # dry-run 不应调用任何上传 API
    assert client.calls["get_upload_info"] == 0
    assert client.calls["add_file"] == 0
    # 但本地包应已生成（便于人工检查）
    assert report["package_path"] is not None


def test_run_backup_uploads_and_records_metadata(sample_db, tmp_path):
    client = FakeDingpanClient()
    report = db_mod.run_backup(
        db_path=sample_db,
        out_dir=tmp_path / "out",
        client=client,
        space_name="leaveadmin-backup",
        keep=24,
        dry_run=False,
    )
    assert report["uploaded"] is True
    rec = report["record"]
    for key in ("file_id", "space_id", "file_name", "sha256", "size", "time"):
        assert key in rec and rec[key], f"记录缺字段 {key}"
    # 完整流程都被调用
    assert client.calls["get_upload_info"] == 1
    assert client.calls["oss_upload"] == 1
    assert client.calls["add_file"] == 1
    assert client.calls["add_permission"] == 1


def test_run_backup_prunes_old_after_upload(sample_db, tmp_path):
    """上传后保留 24 份，更旧的被删。"""
    client = FakeDingpanClient()
    # 预置 24 份已有备份
    for i in range(24):
        client.add_existing_file(f"admin_backup_{i:03d}.tar.gz", modified_time=i)

    report = db_mod.run_backup(
        db_path=sample_db,
        out_dir=tmp_path / "out",
        client=client,
        space_name="leaveadmin-backup",
        keep=24,
        dry_run=False,
    )
    # 上传 1 份后共 25 份，应删 1 份最旧的
    assert report["pruned"] == 1
    assert client.calls["delete_file"] == 1


def test_download_latest_backup_selects_newest_matching_file(tmp_path):
    """GUI 自动拉取依赖这个能力：只下载最新 leaveAdmin 备份包。"""
    client = FakeDingpanClient()
    client.add_existing_file("leaveAdmin-backup-20260628-010000.tar.gz", modified_time=0, file_id="old", content=b"old")
    client.add_existing_file("unrelated-20260629.tar.gz", modified_time=999999, file_id="noise", content=b"noise")
    client.add_existing_file("leaveAdmin-backup-20260629-120000.tar.gz", modified_time=0, file_id="new", content=b"new")

    result = client.download_latest_backup("fake_space", tmp_path, name_prefix="leaveAdmin-backup-")

    assert result["name"] == "leaveAdmin-backup-20260629-120000.tar.gz"
    assert result["file_id"] == "new"
    assert result["path"].read_bytes() == b"new"
    assert client.calls["list_files"] == 1
    assert client.calls["download_file"] == 1


def test_download_latest_backup_errors_when_no_matching_backup(tmp_path):
    client = FakeDingpanClient()
    client.add_existing_file("unrelated.tar.gz", modified_time=1, file_id="x", content=b"x")

    with pytest.raises(db_mod.BackupError):
        client.download_latest_backup("fake_space", tmp_path, name_prefix="leaveAdmin-backup-")


def test_run_backup_failure_sends_notify(sample_db, tmp_path):
    """上传失败应触发通知回调。"""
    client = FakeDingpanClient(fail_on="add_file")
    notes = []

    def notify(title, content):
        notes.append((title, content))

    with pytest.raises(db_mod.BackupError):
        db_mod.run_backup(
            db_path=sample_db,
            out_dir=tmp_path / "out",
            client=client,
            space_name="leaveadmin-backup",
            keep=24,
            dry_run=False,
            notify=notify,
        )
    assert len(notes) == 1
    assert "备份" in notes[0][0]
