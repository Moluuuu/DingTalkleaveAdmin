"""
公休余额管理后台 - FastAPI 主服务
端口 18001
"""
import json
import os
import sys
import asyncio
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Any
from leaveadmin.env import load_dotenv, project_root

from leaveadmin.database import (
    init_db, get_all_employees, get_quota_logs, get_cron_runs,
    get_quota_rules, update_quota_rule, get_employee_by_userid,
    get_snapshots, rollback_snapshot, get_snapshot_detail,
    get_depts, get_jobs, get_employees_by_filter, set_disabled,
    get_leave_balance, reset_leave_balance, get_all_leave_balances,
    get_balance_transactions, get_rule_days, cleanup_expired_snapshots,
    get_scheduled_tasks, add_scheduled_task, update_scheduled_task,
    delete_scheduled_task, get_departed_candidates,
    confirm_departed_candidate, ignore_departed_candidate,
    list_future_reservations
)
from leaveadmin.dingtalk_ops import (
    sync_contacts, refresh_all_balances, refresh_all_balances_async,
    get_task_progress, adjust_balance,
    batch_monthly_assign, token_manager, load_config, CONSTANTS,
    query_balance_realtime, refresh_balances_by_userids,
    fetch_balances, adjust_balance_batch, rollback_adjustment,
    dept_adjust_batch, annual_dept_reset, push_balances_to_dingtalk,
    scan_departed_candidates,
    check_leave_balance_with_future, deduct_leave_balance_with_future,
    refund_leave_balance_with_future, apply_future_reservations
)
import uuid

load_dotenv()
PROJECT_ROOT = project_root()
RUNNER_SCRIPT = PROJECT_ROOT / "scripts" / "run_dingpan_backup_cron.py"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    n = await cleanup_expired_snapshots()
    print(f"[App] 公休余额管理后台启动完成,清理过期快照{n}条")
    yield


app = FastAPI(title="公休余额管理后台", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ========== 密码校验 ==========

AUTH_PASSWORD = __import__("os").getenv("LEAVEADMIN_AUTH_PASSWORD", "change-me")


class AuthRequest(BaseModel):
    password: str


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # 放行: 页面、登录接口
    if path == "/" or path == "/api/auth":
        return await call_next(request)
    # 保护所有其他 API
    if path.startswith("/api/"):
        auth = request.headers.get("X-Auth", "")
        if auth != AUTH_PASSWORD:
            return JSONResponse(status_code=401, content={"code": 401, "message": "未授权"})
    return await call_next(request)


# ========== 页面 ==========

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "templates" / "admin.html"
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


@app.post("/api/auth")
async def api_auth(req: AuthRequest):
    if req.password == AUTH_PASSWORD:
        return {"code": 200, "message": "认证成功"}
    return JSONResponse(status_code=401, content={"code": 401, "message": "密码错误"})


# ========== 人员 ==========

@app.get("/api/employees")
async def api_employees(dept: str = None, job: str = None):
    if dept or job:
        employees = await get_employees_by_filter(dept, job)
    else:
        employees = await get_all_employees()
    return {"code": 200, "data": employees, "total": len(employees)}


@app.get("/api/depts")
async def api_depts():
    return {"code": 200, "data": await get_depts()}


@app.get("/api/jobs")
async def api_jobs():
    return {"code": 200, "data": await get_jobs()}


@app.get("/api/balance/{userid}")
async def api_balance_realtime(userid: str):
    """单人实时查余额"""
    result = await query_balance_realtime(userid)
    return {"code": 200, "data": result}


class RefreshFilterRequest(BaseModel):
    userids: list


@app.post("/api/refresh-filtered")
async def api_refresh_filtered(req: RefreshFilterRequest):
    """刷新筛选结果的余额"""
    result = await refresh_balances_by_userids(req.userids)
    return {"code": 200, "data": result}


@app.post("/api/fetch-balances")
async def api_fetch_balances(req: RefreshFilterRequest):
    """查多人余额(预览用),返回{userid:balance}"""
    result = await fetch_balances(req.userids)
    return {"code": 200, "data": result}


class AdjustBatchRequest(BaseModel):
    userids: list
    delta: float
    reason: str = ""


@app.post("/api/adjust-batch")
async def api_adjust_batch(req: AdjustBatchRequest):
    """批量调整余额"""
    result = await adjust_balance_batch(req.userids, req.delta, req.reason)
    await cleanup_expired_snapshots()
    return {"code": 200, "data": result}


class DisableRequest(BaseModel):
    userid: str
    disabled: bool = True


@app.post("/api/disable")
async def api_disable(req: DisableRequest):
    """屏蔽/取消屏蔽人员(不删除,同步后保留状态)"""
    await set_disabled(req.userid, req.disabled)
    return {"code": 200, "message": "已屏蔽" if req.disabled else "已取消屏蔽"}


@app.post("/api/sync")
async def api_sync():
    """手动同步通讯录：只增/只更新，不做离职屏蔽。"""
    try:
        total = await sync_contacts()
        return {"code": 200, "message": f"同步完成，共 {total} 人；离职缺席已改为单独候选扫描，不在同步时自动屏蔽"}
    except Exception as e:
        return {"code": 500, "message": f"同步失败: {str(e)}"}


class DepartedScanRequest(BaseModel):
    auto_confirm: bool = False
    operator: str = "manual"


class CandidateActionRequest(BaseModel):
    operator: str = "manual"
    note: str = ""


@app.get("/api/departed-candidates")
async def api_departed_candidates(status: str = "pending", limit: int = 200):
    """离职/缺席候选列表。默认只看待确认。"""
    rows = await get_departed_candidates(status=status or None, limit=limit)
    return {"code": 200, "data": rows, "total": len(rows)}


@app.post("/api/departed-candidates/scan")
async def api_departed_candidates_scan(req: Request):
    """扫描离职/缺席候选。默认只生成候选，不屏蔽。"""
    body = {}
    try:
        body = await req.json()
    except Exception:
        body = {}
    result = await scan_departed_candidates(body, operator=body.get("operator", "manual"))
    return {"code": 200 if result.get("ok", True) else 500, "data": result, "message": result.get("message", "扫描完成")}


@app.post("/api/departed-candidates/{candidate_id}/confirm")
async def api_departed_candidate_confirm(candidate_id: int, req: CandidateActionRequest):
    """人工确认离职候选，确认后才真正屏蔽 employees，并写审计。"""
    result = await confirm_departed_candidate(candidate_id, operator=req.operator or "manual", note=req.note or "人工确认离职")
    return {"code": 200 if result.get("ok") else 404, "data": result, "message": result.get("message", "")}


@app.post("/api/departed-candidates/{candidate_id}/ignore")
async def api_departed_candidate_ignore(candidate_id: int, req: CandidateActionRequest):
    """忽略离职候选，不修改 employees，但写审计。"""
    result = await ignore_departed_candidate(candidate_id, operator=req.operator or "manual", note=req.note or "人工忽略")
    return {"code": 200 if result.get("ok") else 404, "data": result, "message": result.get("message", "")}


@app.post("/api/refresh-balances")
async def api_refresh():
    """启动异步刷新余额任务"""
    task_id = str(uuid.uuid4())[:8]
    asyncio.create_task(refresh_all_balances_async(task_id))
    return {"code": 200, "data": {"task_id": task_id}}


@app.get("/api/refresh-progress")
async def api_refresh_progress(task_id: str):
    """查询刷新进度"""
    return {"code": 200, "data": get_task_progress(task_id)}


# ========== 余额调整 ==========

class AdjustRequest(BaseModel):
    userid: str
    delta: float
    reason: str = ""


@app.post("/api/adjust")
async def api_adjust(req: AdjustRequest):
    """调整单人余额(累加delta)"""
    result = await adjust_balance(req.userid, req.delta, req.reason or "手动调整")
    return {"code": 200 if result.get("success") else 500, "data": result}


class BatchMonthlyRequest(BaseModel):
    month_type: str = "normal"  # normal / special


@app.post("/api/batch-monthly")
async def api_batch_monthly(req: Request):
    """月初批量发放。兼容 JSON body 和 scheduler 的 query 参数。"""
    body = {}
    try:
        body = await req.json()
    except Exception:
        body = {}
    month_type = body.get("month_type") or req.query_params.get("month_type") or "normal"
    result = await batch_monthly_assign(month_type)
    return {"code": 200, "data": result}


@app.post("/api/annual-dept-reset")
async def api_annual_dept_reset(req: Request):
    """每年三月：国网/骑手重置99天，蔬果品类重置52天"""
    body = {}
    try:
        body = await req.json()
    except Exception:
        body = {}
    result = await annual_dept_reset(body)
    return {"code": 200, "data": result}


@app.post("/api/push-dingtalk")
async def api_push_dingtalk():
    """全量推送 leave_balances 到钉钉侧(凌晨3点cron调用,唯一推钉钉入口)"""
    result = await push_balances_to_dingtalk()
    return {"code": 200, "data": result}


@app.post("/api/dept-adjust")
async def api_dept_adjust(req: Request):
    """按部门/分类/人员批量调整额度(cron触发)"""
    body = await req.json()
    # 安全校验: 必须有筛选条件+delta
    if not any(body.get(k) for k in ("depts", "categories", "userids")):
        return {"code": 400, "message": "必须指定 depts/categories/userids 至少一项"}
    if not body.get("delta"):
        return {"code": 400, "message": "delta 不能为0"}
    result = await dept_adjust_batch(body)
    return {"code": 200, "data": result}


# ========== 规则 ==========

@app.get("/api/rules")
async def api_rules():
    rules = await get_quota_rules()
    return {"code": 200, "data": rules}


class RuleUpdateRequest(BaseModel):
    month_type: str
    category: str
    days: int


@app.post("/api/rules")
async def api_update_rule(req: RuleUpdateRequest):
    await update_quota_rule(req.month_type, req.category, req.days)
    return {"code": 200, "message": "规则已更新"}


# ========== 日志 ==========

@app.get("/api/quota-logs")
async def api_quota_logs(limit: int = 50):
    logs = await get_quota_logs(limit)
    return {"code": 200, "data": logs}


@app.get("/api/cron-logs")
async def api_cron_logs(limit: int = 30):
    logs = await get_cron_runs(limit)
    return {"code": 200, "data": logs}


# ========== token状态 ==========

@app.get("/api/token-status")
async def api_token_status():
    return token_manager.get_status() if hasattr(token_manager, "get_status") else {"has_token": token_manager._token is not None}


# ========== 余额快照 ==========

@app.get("/api/snapshots")
async def api_snapshots(limit: int = 20):
    snaps = await get_snapshots(limit)
    return {"code": 200, "data": snaps}


@app.get("/api/snapshots/{batch_id}")
async def api_snapshot_detail(batch_id: str):
    detail = await get_snapshot_detail(batch_id)
    return {"code": 200, "data": detail}


class RollbackRequest(BaseModel):
    batch_id: str


@app.post("/api/rollback")
async def api_rollback(req: RollbackRequest):
    """回滚某次快照(仅恢复本地DB余额,钉钉侧需手动另调)"""
    count = await rollback_snapshot(req.batch_id)
    return {"code": 200, "message": f"已回滚 {count} 人(本地DB),钉钉侧需手动恢复"}


@app.post("/api/cleanup-snapshots")
async def api_cleanup_snapshots():
    """清理过期快照(12小时前),cron每小时调"""
    n = await cleanup_expired_snapshots()
    return {"code": 200, "message": f"已清理{n}条过期快照"}


class RollbackAdjustRequest(BaseModel):
    batch_id: str


@app.post("/api/rollback-adjust")
async def api_rollback_adjust(req: RollbackAdjustRequest):
    """回滚某次调整: 把涉及人员余额恢复到快照值(真正撤销钉钉侧)"""
    result = await rollback_adjustment(req.batch_id)
    await cleanup_expired_snapshots()
    return {"code": 200, "data": result}


# ========== 自建余额API(宜搭将来对接) ==========
# 这套接口独立于钉钉额度,基于本地leave_balances表
# 宜搭改造时:didMount查余额调GET /api/leave/balance,审批通过扣减调POST /api/leave/deduct

@app.get("/api/leave/balance")
async def api_leave_balance(userid: str, period: str = None):
    """查单人余额(宜搭didMount调用)"""
    bal = await get_leave_balance(userid, period)
    return {"code": 200, "data": bal}


@app.get("/api/leave/check")
async def api_leave_check(userid: str, days: float, period: str = None, leave_start_date: Any = None):
    """校验余额是否足够(宜搭beforeSubmit调用)。leave_start_date落在下月时走预占校验。"""
    result = await check_leave_balance_with_future(userid, days, period, leave_start_date)
    return {"code": 200 if result.get("ok") else 400, "data": result}


class DeductRequest(BaseModel):
    userid: str
    days: float
    reason: str = ""
    ref_id: str = ""
    leave_start_date: Any = None


@app.post("/api/leave/deduct")
async def api_leave_deduct(req: DeductRequest):
    """扣减余额(审批通过时调)。下月开始的公休写入预占，不扣当前月。"""
    result = await deduct_leave_balance_with_future(req.userid, req.days, req.reason, req.ref_id, req.leave_start_date)
    return {"code": 200 if result["ok"] else 400, "data": result}


@app.post("/api/leave/refund")
async def api_leave_refund(req: DeductRequest):
    """回退余额(撤销/拒绝时调)。优先取消下月预占。"""
    result = await refund_leave_balance_with_future(req.userid, req.days, req.reason, req.ref_id, req.leave_start_date)
    return {"code": 200 if result.get("ok") else 400, "data": result}


class ResetRequest(BaseModel):
    userid: str
    days: float
    month_type: str = "normal"


@app.post("/api/leave/reset")
async def api_leave_reset(req: ResetRequest):
    """重置单人余额(月初发放)"""
    result = await reset_leave_balance(req.userid, req.days, req.month_type)
    return {"code": 200, "data": result}


@app.post("/api/apply-future-reservations")
async def api_apply_future_reservations(period: str = None):
    """月初额度初始化后，应用下月公休预占扣减。"""
    result = await apply_future_reservations(period)
    return {"code": 200 if result.get("failed", 0) == 0 else 500, "data": result}


@app.get("/api/future-reservations")
async def api_future_reservations(period: str = None, status: str = None, limit: int = 500):
    """查看次月/指定月份公休预占明细。默认展示下个月全部状态。"""
    data = await list_future_reservations(period, status, limit)
    return {"code": 200, "data": data}


@app.get("/api/leave/all")
async def api_leave_all(period: str = None):
    """查全员余额"""
    data = await get_all_leave_balances(period)
    return {"code": 200, "data": data, "total": len(data)}


@app.get("/api/leave/transactions")
async def api_leave_transactions(userid: str, limit: int = 50):
    """查某人余额变更流水"""
    data = await get_balance_transactions(userid, limit)
    return {"code": 200, "data": data}


# ========== 定时任务管理 ==========

@app.get("/api/tasks")
async def api_get_tasks():
    tasks = await get_scheduled_tasks()
    return {"code": 200, "data": tasks}


class TaskRequest(BaseModel):
    name: str
    task_type: str
    cron_expr: str
    config: str = "{}"


@app.post("/api/tasks")
async def api_add_task(req: TaskRequest):
    task_id = await add_scheduled_task(req.name, req.task_type, req.cron_expr, req.config)
    return {"code": 200, "message": "已添加,约30秒后自动生效"}


@app.put("/api/tasks/{task_id}")
async def api_update_task(task_id: int, req: TaskRequest):
    await update_scheduled_task(task_id, name=req.name, cron_expr=req.cron_expr, config=req.config)
    return {"code": 200, "message": "已更新,约30秒后自动生效"}


class TaskToggleRequest(BaseModel):
    enabled: bool


@app.post("/api/tasks/{task_id}/toggle")
async def api_toggle_task(task_id: int, req: TaskToggleRequest):
    await update_scheduled_task(task_id, enabled=req.enabled)
    return {"code": 200, "message": "已" + ("启用" if req.enabled else "禁用")}


@app.delete("/api/tasks/{task_id}")
async def api_delete_task(task_id: int):
    await delete_scheduled_task(task_id)
    return {"code": 200, "message": "已删除"}


class TaskRunRequest(BaseModel):
    task_id: int


@app.post("/api/tasks/run")
async def api_run_task(req: TaskRunRequest):
    """手动触发定时任务(立即执行)"""
    tasks = await get_scheduled_tasks()
    task = next((t for t in tasks if t["id"] == req.task_id), None)
    if not task:
        return {"code": 404, "message": "任务不存在"}
    ttype = task["task_type"]
    cmd = ["curl", "-s", "-X", "POST", "-H", f"X-Auth:{AUTH_PASSWORD}"]
    if ttype == "refresh_all":
        path = "/api/refresh-balances"
    elif ttype == "cleanup_snapshots":
        path = "/api/cleanup-snapshots"
    elif ttype == "monthly_assign":
        path = "/api/batch-monthly"
    elif ttype == "apply_future_reservations":
        path = "/api/apply-future-reservations"
    elif ttype == "sync_contacts":
        path = "/api/sync"
    elif ttype == "departed_scan":
        path = "/api/departed-candidates/scan"
        cmd += ["-H", "Content-Type:application/json", "-d", task.get("config") or "{}"]
    elif ttype == "push_dingtalk":
        path = "/api/push-dingtalk"
    elif ttype == "annual_dept_reset":
        path = "/api/annual-dept-reset"
    elif ttype == "dingpan_backup":
        import subprocess
        subprocess.Popen(
            [sys.executable, str(RUNNER_SCRIPT)],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"code": 200, "message": "已触发钉盘备份"}
    elif ttype == "dept_adjust":
        path = "/api/dept-adjust"
        cmd += ["-H", "Content-Type:application/json", "-d", task.get("config") or "{}"]
    else:
        return {"code": 400, "message": "不支持手动触发此类型"}
    import subprocess
    cmd += [f"http://localhost:18001{path}"]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"code": 200, "message": "已触发"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("leaveadmin.main:app", host="0.0.0.0", port=18001, reload=False)
