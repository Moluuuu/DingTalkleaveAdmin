"""Tests for the DingPan scheduled-task runner wrapper."""
import json
from pathlib import Path

import run_dingpan_backup_cron as runner


def test_cron_runner_dry_run_uses_configs_without_upload(sample_db, tmp_path, capsys):
    app_config = tmp_path / "config.json"
    dingpan_config = tmp_path / "dingpan_backup_config.json"
    out_dir = tmp_path / "out"
    app_config.write_text(json.dumps({"appKey": "ak", "appSecret": "sk"}), encoding="utf-8")
    dingpan_config.write_text(json.dumps({
        "unionId": "union",
        "spaceName": "公休余额灾备",
        "spaceId": "space",
        "parentId": "0",
        "keep": 24,
        "logPath": str(tmp_path / "dingpan.log"),
    }), encoding="utf-8")

    code = runner.main([
        "--dry-run",
        "--db", str(sample_db),
        "--out-dir", str(out_dir),
        "--app-config", str(app_config),
        "--dingpan-config", str(dingpan_config),
    ])

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert report["uploaded"] is False
    assert Path(report["package_path"]).exists()
    assert report["manifest"]["service"] == "leaveAdmin"
