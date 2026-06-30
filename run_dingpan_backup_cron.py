#!/usr/bin/env python3
"""Scheduled DingPan backup runner for leaveAdmin.

This is the bridge used by the leaveadmin-scheduler `dingpan_backup` task.
It keeps the actual backup logic in dingpan_backup.py and adds the production
defaults plus failure notification.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import List, Optional

import dingpan_backup


BASE_DIR = Path(__file__).resolve().parent


def _notify(title: str, content: str) -> None:
    """Best-effort DingTalk admin notification for scheduled backup failures."""
    try:
        from dingtalk_ops import send_notify

        asyncio.run(send_notify(title, content))
    except Exception as exc:  # pragma: no cover - notification must never mask backup result
        print(f"[DingPanBackup] 通知发送失败: {exc}", file=sys.stderr)


def run_once(args: argparse.Namespace) -> dict:
    client, cfg = dingpan_backup.build_client_from_configs(args.app_config, args.dingpan_config)
    keep = args.keep if args.keep is not None else cfg["keep"]
    return dingpan_backup.run_backup(
        db_path=args.db,
        out_dir=args.out_dir,
        client=client,
        space_name=cfg["space_name"],
        keep=keep,
        dry_run=args.dry_run,
        notify=_notify,
        union_id=cfg["union_id"],
        parent_id=cfg["parent_id"],
        viewer_user_id=cfg.get("viewer_user_id"),
        corp_id=cfg.get("corp_id"),
        log_path=cfg.get("log_path"),
        source_host=args.source_host,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="leaveAdmin 钉盘定时备份 runner")
    parser.add_argument("--db", default=str(BASE_DIR / "admin.db"))
    parser.add_argument("--out-dir", default=str(BASE_DIR / "backups" / "dingpan_hourly"))
    parser.add_argument("--app-config", default=str(BASE_DIR / "config.json"))
    parser.add_argument("--dingpan-config", default=str(BASE_DIR / "dingpan_backup_config.json"))
    parser.add_argument("--keep", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source-host")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = run_once(args)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:
        _notify("钉盘备份失败", f"### 钉盘小时备份失败\n- 错误: {exc}")
        print(f"[DingPanBackup] 失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
