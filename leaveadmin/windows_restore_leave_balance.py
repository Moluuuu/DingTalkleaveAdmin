"""Windows/local emergency restore helper for leaveAdmin balances.

Default mode is dry-run. Real DingTalk overwrite must pass explicit execution
and confirmation flags.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from leaveadmin import dingpan_backup
from leaveadmin.env import load_dotenv

load_dotenv()
DEFAULT_LEAVE_CODE = os.getenv("DINGTALK_LEAVE_CODE", "")
DEFAULT_OP_USERID = os.getenv("DINGTALK_OP_USERID", "")
TOKEN_URL = "https://oapi.dingtalk.com/gettoken"
QUOTA_UPDATE_URL = "https://oapi.dingtalk.com/topapi/attendance/vacation/quota/update"


class ConfirmationRequired(RuntimeError):
    """Raised when a destructive restore lacks explicit confirmation."""


class QuotaClient:
    """Abstract quota-update client."""

    def quota_update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class DingTalkQuotaClient(QuotaClient):
    """Minimal DingTalk quota/update client for emergency restore.

    Uses only stdlib urllib so the Windows emergency tool does not need extra
    packages. Payload balances are already in DingTalk's ×100 unit.
    """

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        token_url: str = TOKEN_URL,
        quota_update_url: str = QUOTA_UPDATE_URL,
        timeout: int = 30,
    ):
        self.app_key = app_key
        self.app_secret = app_secret
        self.token_url = token_url
        self.quota_update_url = quota_update_url
        self.timeout = timeout
        self._token: str | None = None
        self._expires_at = 0.0

    @classmethod
    def from_config(cls, config_path: str | Path) -> "DingTalkQuotaClient":
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        app_key = config.get("appKey") or config.get("app_key")
        app_secret = config.get("appSecret") or config.get("app_secret")
        if not app_key or not app_secret:
            raise ValueError("config.json 缺少 appKey/appSecret")
        return cls(app_key=app_key, app_secret=app_secret)

    def _request_json(self, url: str, method: str = "GET", body: Dict[str, Any] | None = None) -> Dict[str, Any]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {raw[:500]}") from exc
        return json.loads(raw or "{}")

    def get_token(self) -> str:
        if self._token and time.time() < self._expires_at - 300:
            return self._token
        query = urllib.parse.urlencode({"appkey": self.app_key, "appsecret": self.app_secret})
        data = self._request_json(f"{self.token_url}?{query}")
        if str(data.get("errcode", 0)) not in ("0", "None"):
            raise RuntimeError(f"获取 access_token 失败: {data}")
        token = data.get("access_token") or data.get("accessToken")
        if not token:
            raise RuntimeError(f"access_token 缺失: {data}")
        self._token = token
        self._expires_at = time.time() + int(data.get("expires_in") or 7200)
        return token

    def quota_update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        start_ms, end_ms = period_bounds_ms(payload["period"])
        body = {
            "op_userid": payload.get("op_userid") or DEFAULT_OP_USERID,
            "leave_quotas": {
                "start_time": start_ms,
                "end_time": end_ms,
                "reason": payload.get("reason") or "公休余额灾备恢复",
                "quota_num_per_day": int(payload["quota_num_per_day"]),
                "quota_cycle": payload["quota_cycle"],
                "leave_code": payload["leave_code"],
                "quota_num_per_hour": int(payload.get("quota_num_per_hour") or 0),
                "userid": payload["userid"],
            },
        }
        token = self.get_token()
        url = f"{self.quota_update_url}?{urllib.parse.urlencode({'access_token': token})}"
        return self._request_json(url, method="POST", body=body)


def _current_period() -> str:
    return dt.datetime.now().strftime("%Y-%m")


def build_quota_payloads(
    db_path: str | Path,
    leave_code: str = DEFAULT_LEAVE_CODE,
    op_userid: str = "",
    period: str | None = None,
) -> List[Dict[str, Any]]:
    """Read leave_balances and build quota/update payloads.

    leave_balances.balance is already in DingTalk's ×100 unit. Do not divide by
    100 here.
    """
    db_path = Path(db_path)
    period = period or _detect_period(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                e.userid,
                COALESCE(lb.name, e.name, e.userid) AS name,
                COALESCE(lb.balance, ROUND(COALESCE(e.current_balance, 0) * 100), 0) AS balance
            FROM employees e
            LEFT JOIN leave_balances lb ON lb.userid = e.userid AND lb.period = ?
            WHERE COALESCE(e.is_disabled, 0) = 0
            ORDER BY e.userid
            """,
            (period,),
        ).fetchall()
    finally:
        conn.close()
    payloads: List[Dict[str, Any]] = []
    for row in rows:
        balance = int(round(float(row["balance"])))
        payloads.append(
            {
                "userid": row["userid"],
                "name": row["name"],
                "leave_code": leave_code,
                "quota_num_per_day": balance,
                "quota_num_per_hour": 0,
                "quota_cycle": period_to_quota_cycle(period),
                "period": period,
                "op_userid": op_userid,
            }
        )
    return payloads


def _detect_period(db_path: Path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT MAX(period) FROM leave_balances").fetchone()
        return row[0] if row and row[0] else _current_period()
    finally:
        conn.close()


def period_to_quota_cycle(period: str) -> str:
    year, month = period.split("-", 1)
    return f"{year}-M-{int(month)}"


def period_bounds_ms(period: str) -> tuple[int, int]:
    """Return local-month start/end milliseconds for DingTalk quota/update."""
    year_s, month_s = period.split("-", 1)
    year = int(year_s)
    month = int(month_s)
    start = dt.datetime(year, month, 1)
    if month == 12:
        next_month = dt.datetime(year + 1, 1, 1)
    else:
        next_month = dt.datetime(year, month + 1, 1)
    return int(start.timestamp() * 1000), int(next_month.timestamp() * 1000) - 1


def restore(
    db_path: str | Path,
    client: QuotaClient,
    leave_code: str = DEFAULT_LEAVE_CODE,
    op_userid: str = "",
    execute: bool = False,
    confirm_overwrite: bool = False,
    period: str | None = None,
    progress: Optional[Callable[[int, int, Dict[str, Any], Dict[str, Any]], None]] = None,
    request_interval: float = 0.0,
) -> Dict[str, Any]:
    payloads = build_quota_payloads(db_path, leave_code=leave_code, op_userid=op_userid, period=period)
    if execute and not confirm_overwrite:
        raise ConfirmationRequired("真实覆盖钉钉假期余额必须显式确认")
    if execute and not leave_code:
        raise ConfirmationRequired("真实执行必须提供 --leave-code 或 DINGTALK_LEAVE_CODE")
    if execute and not op_userid:
        raise ConfirmationRequired("真实执行必须提供 --op-userid 或 DINGTALK_OP_USERID")
    report = {
        "dry_run": not execute,
        "executed": False,
        "target_count": len(payloads),
        "period": period or (_detect_period(Path(db_path)) if Path(db_path).exists() else None),
        "preview": payloads[:20],
        "success": 0,
        "failed": 0,
        "errors": [],
    }
    if not execute:
        return report
    total = len(payloads)
    for index, payload in enumerate(payloads, start=1):
        result: Dict[str, Any]
        try:
            result = client.quota_update(payload)
            if result.get("errcode", 0) not in (0, "0"):
                report["failed"] += 1
                report["errors"].append({"userid": payload["userid"], "result": result})
            else:
                report["success"] += 1
        except Exception as exc:
            result = {"error": str(exc)}
            report["failed"] += 1
            report["errors"].append({"userid": payload["userid"], "error": str(exc)})
        if progress:
            progress(index, total, payload, result)
        if request_interval and index < total:
            time.sleep(request_interval)
    report["executed"] = True
    report["dry_run"] = False
    return report


def restore_from_backup_file(
    backup_path: str | Path,
    work_dir: str | Path,
    client: QuotaClient,
    leave_code: str = DEFAULT_LEAVE_CODE,
    op_userid: str = "",
    execute: bool = False,
    confirm_overwrite: bool = False,
    period: str | None = None,
    progress: Optional[Callable[[int, int, Dict[str, Any], Dict[str, Any]], None]] = None,
    request_interval: float = 0.0,
) -> Dict[str, Any]:
    verification = dingpan_backup.verify_backup_package(backup_path)
    if not verification.get("ok"):
        raise dingpan_backup.BackupError(verification.get("error") or "backup package verification failed")
    extracted = dingpan_backup.extract_backup_package(backup_path, work_dir)
    report = restore(
        db_path=extracted["db_path"],
        client=client,
        leave_code=leave_code,
        op_userid=op_userid,
        execute=execute,
        confirm_overwrite=confirm_overwrite,
        period=period or extracted["manifest"].get("period"),
        progress=progress,
        request_interval=request_interval,
    )
    report["manifest"] = extracted["manifest"]
    return report


def create_remote_backup_via_ssh(
    ssh_target: str,
    remote_dir: str = "/opt/leaveadmin",
    identity_file: str | None = None,
    local_dir: str | Path | None = None,
) -> Path:
    """Mode A helper: ask remote host to create a consistent DB backup and pull it.

    This is intentionally a thin wrapper; production use should review the
    generated command before execution.
    """
    local_dir = Path(local_dir or tempfile.mkdtemp(prefix="leaveadmin-remote-db-"))
    local_dir.mkdir(parents=True, exist_ok=True)
    remote_file = f"/tmp/leaveadmin-admin-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    ssh_cmd = ["ssh"]
    scp_cmd = ["scp"]
    if identity_file:
        ssh_cmd += ["-i", identity_file]
        scp_cmd += ["-i", identity_file]
    ssh_cmd += [ssh_target, f"cd {remote_dir} && sqlite3 admin.db \".backup '{remote_file}'\""]
    subprocess.run(ssh_cmd, check=True)
    local_path = local_dir / "admin.db"
    scp_cmd += [f"{ssh_target}:{remote_file}", str(local_path)]
    subprocess.run(scp_cmd, check=True)
    return local_path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="公休余额紧急恢复工具，默认 dry-run")
    parser.add_argument("--db")
    parser.add_argument("--backup-file")
    parser.add_argument("--work-dir", default="restore_work")
    parser.add_argument("--leave-code", default=DEFAULT_LEAVE_CODE)
    parser.add_argument("--op-userid", default=DEFAULT_OP_USERID)
    parser.add_argument("--config", help="包含 appKey/appSecret 的钉钉应用配置；真实执行必填")
    parser.add_argument("--period", help="指定恢复月份，例如 2026-06；默认从备份/数据库识别")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--i-understand-this-overwrites-dingtalk", action="store_true", dest="confirm")
    args = parser.parse_args(argv)
    if not args.db and not args.backup_file:
        raise SystemExit("必须提供 --db 或 --backup-file")
    if args.execute and not args.config:
        raise SystemExit("真实执行必须提供 --config")
    client: QuotaClient = DingTalkQuotaClient.from_config(args.config) if args.config else QuotaClient()
    if not args.config:
        class DryRunClient(QuotaClient):
            def quota_update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
                return {"errcode": 0, "errmsg": "dry-run"}
        client = DryRunClient()
    if args.backup_file:
        report = restore_from_backup_file(
            args.backup_file,
            args.work_dir,
            client,
            leave_code=args.leave_code,
            op_userid=args.op_userid,
            execute=args.execute,
            confirm_overwrite=args.confirm,
            period=args.period,
            request_interval=0.3 if args.execute else 0.0,
        )
    else:
        report = restore(
            args.db,
            client,
            leave_code=args.leave_code,
            op_userid=args.op_userid,
            execute=args.execute,
            confirm_overwrite=args.confirm,
            period=args.period,
            request_interval=0.3 if args.execute else 0.0,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
