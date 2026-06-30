"""CLI/config helper tests for DingPan backup."""
import json

import dingpan_backup as db_mod


def test_build_client_from_configs_reads_app_and_dingpan_config(tmp_path):
    app_config = tmp_path / "config.json"
    dingpan_config = tmp_path / "dingpan_backup_config.json"
    app_config.write_text(json.dumps({"appKey": "ak", "appSecret": "sk"}), encoding="utf-8")
    dingpan_config.write_text(json.dumps({
        "unionId": "union",
        "spaceId": "space",
        "spaceName": "公休余额灾备",
        "parentId": "0",
        "viewerUserId": "userId001",
        "corpId": "dingxxx",
        "keep": 24,
    }), encoding="utf-8")

    client, cfg = db_mod.build_client_from_configs(app_config, dingpan_config, session=object())
    assert client.app_key == "ak"
    assert client.app_secret == "sk"
    assert client.union_id == "union"
    assert client.space_id == "space"
    assert cfg["space_name"] == "公休余额灾备"
    assert cfg["parent_id"] == "0"
    assert cfg["keep"] == 24


def test_build_client_from_configs_requires_union_id(tmp_path):
    app_config = tmp_path / "config.json"
    dingpan_config = tmp_path / "dingpan_backup_config.json"
    app_config.write_text(json.dumps({"appKey": "ak", "appSecret": "sk"}), encoding="utf-8")
    dingpan_config.write_text(json.dumps({}), encoding="utf-8")

    try:
        db_mod.build_client_from_configs(app_config, dingpan_config, session=object())
    except db_mod.BackupError as exc:
        assert "unionId" in str(exc)
    else:
        raise AssertionError("expected BackupError")
