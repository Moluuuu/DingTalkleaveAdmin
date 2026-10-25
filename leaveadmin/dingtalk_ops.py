"""
公休余额管理后台 - 钉钉API + 通讯录同步 + 余额操作
"""
import httpx
import asyncio
import json
import os
import time
import aiosqlite
import yaml
from pathlib import Path
from leaveadmin.env import load_dotenv, project_root
from datetime import datetime
from leaveadmin.database import (
    upsert_employee, get_all_employees, update_employee_balance, set_leave_balance_abs, get_leave_balance,
    get_employee_by_userid, add_quota_log, update_quota_log_status,
    add_cron_run, update_cron_run, get_rule_days,
    create_balance_snapshot, get_snapshot_detail, set_department_disabled,
    create_departed_candidates, get_departed_candidates, confirm_departed_candidate,
    check_leave_balance, deduct_leave_balance, refund_leave_balance,
    parse_leave_start_date, sum_future_reservations, create_future_reservation,
    get_future_reservation_by_ref, cancel_future_reservation,
    list_pending_future_reservations, mark_future_reservation_applied, mark_future_reservation_failed,
    update_employee_hired_date, _add_month, _current_period, needs_hourly_initialization,
    prepare_hourly_monthly_resets, apply_hourly_monthly_reset, mark_hourly_monthly_reset_failed,
    DB_PATH,
)

# 加载配置
load_dotenv()
PROJECT_ROOT = project_root()
CONFIG_PATH = Path(os.getenv("LEAVEADMIN_CONFIG", str(PROJECT_ROOT / "config.json"))).resolve()
CONSTANTS_PATH = Path(os.getenv("LEAVEADMIN_CONSTANTS", str(PROJECT_ROOT / "constants.yaml"))).resolve()
# 缺失真实规则时应阻止启动，不能用示例额度规则接管生产。
with open(CONSTANTS_PATH, "r", encoding="utf-8") as f:
    CONSTANTS = yaml.safe_load(f)

DINGTALK = CONSTANTS["dingtalk"]
LEAVE_CODE = CONSTANTS["leave_code"]
SIX_DAY_DEPT_IDS = set(CONSTANTS["six_day_dept_ids"])
MY_USERID = CONSTANTS["my_userid"]
USER_CATEGORY_OVERRIDES = CONSTANTS.get("user_category_overrides", {}) or {}
OP_USERID = CONSTANTS["op_userid"]
HOURLY_KEYWORDS = CONSTANTS["sync"]["hourly_keywords"]
ROOT_DEPT_ID = CONSTANTS["sync"]["root_dept_id"]
SPECIAL_MONTHS = set(CONSTANTS["special_months"])
REQUEST_INTERVAL = CONSTANTS["rate_limit"]["request_interval"]

BIANWAI_DEPT = "编外"
UNLIMITED_BALANCE_DEPTS = {"国网超市部", "骑手部"}
ANNUAL_LUMP_SUM_DEPT_DAYS = {"蔬果品类部": 52}
CRON_ADJUST_EXCLUDED_DEPTS = UNLIMITED_BALANCE_DEPTS | set(ANNUAL_LUMP_SUM_DEPT_DAYS.keys())


def _dept_name(emp: dict) -> str:
    return (emp or {}).get("dept_name") or ""


def is_unlimited_balance_employee(emp: dict) -> bool:
    return _dept_name(emp) in UNLIMITED_BALANCE_DEPTS


def is_annual_lump_sum_employee(emp: dict) -> bool:
    return _dept_name(emp) in ANNUAL_LUMP_SUM_DEPT_DAYS


def is_cron_adjust_excluded(emp: dict) -> bool:
    """不参加月初/特殊月份/dept_adjust 等自动额度调整的部门。"""
    return _dept_name(emp) in CRON_ADJUST_EXCLUDED_DEPTS


def annual_reset_days_for_employee(emp: dict):
    if is_unlimited_balance_employee(emp):
        return 99
    return ANNUAL_LUMP_SUM_DEPT_DAYS.get(_dept_name(emp))


async def carryover_balance_if_missing(userid: str, name: str = "", period: str = None, reason_prefix: str = "月度结转"):
    """若指定 period 无 leave_balances 行，从最近月份继承余额建当月行（惰性结转）。

    特殊政策部门(国网/骑手/蔬果)不参与月初发放，月度结转保证当月有行，
    否则 deduct/check 会看到余额 0，误报"余额不足: 当前0天"。
    返回 True=已结转，False=当月已有行或完全无历史（无历史时建 0 行避免空行）。
    """
    period = period or _current_period()
    existing = await get_leave_balance(userid, period)
    if existing.get("name"):
        return False  # 当月已有行，不覆盖

    carry = 0.0
    carry_from = None
    for back in range(1, 13):
        prev_period = _add_month(period, -back)
        prev = await get_leave_balance(userid, prev_period)
        if prev.get("name"):
            carry = prev["balance"]
            carry_from = prev_period
            break

    nm = name or existing.get("name") or userid
    if carry_from:
        await set_leave_balance_abs(userid, carry, nm, f"特殊部门{reason_prefix}({carry_from})", "carryover", period=period)
    else:
        # 完全无历史：建 0 行，保证后续 deduct/check 有行可查
        await set_leave_balance_abs(userid, 0.0, nm, f"特殊部门{reason_prefix}(无历史)", "carryover", period=period)
    await update_employee_balance(userid, round(carry / 100, 2))
    return True


# ========== Token ==========

class TokenManager:
    def __init__(self):
        self._token = None
        self._expires_at = 0
        self._lock = asyncio.Lock()

    def is_valid(self):
        return self._token is not None and time.time() < (self._expires_at - 300)

    async def get_token(self, app_key, app_secret):
        if self.is_valid():
            return self._token
        async with self._lock:
            if self.is_valid():
                return self._token
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(DINGTALK["token_url"], params={"appkey": app_key, "appsecret": app_secret})
                result = r.json()
            if result.get("errcode") == 0:
                self._token = result["access_token"]
                self._expires_at = time.time() + result.get("expires_in", 7200)
                return self._token
            raise Exception(f"获取token失败: {result.get('errmsg')}")


token_manager = TokenManager()


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ========== 通讯录同步 ==========

DINGTALK_TRANSIENT_ERRCODES = {90018, 88, 40014, 40001}


def _is_transient_dingtalk_error(data: dict) -> bool:
    errcode = data.get("errcode", 0)
    errmsg = (data.get("errmsg") or "").lower()
    return (
        errcode in DINGTALK_TRANSIENT_ERRCODES
        or "rate" in errmsg
        or "limit" in errmsg
        or "too many" in errmsg
        or "频" in errmsg
        or "限流" in errmsg
    )


async def _dingtalk_post_with_retry(url: str, token: str, body: dict, op_name: str, timeout: int = 15, max_attempts: int = 4):
    """钉钉POST请求，遇到限流/瞬时错误退避重试；最终仍返回业务JSON，由调用方决定是否硬失败。"""
    last_data = None
    for attempt in range(1, max_attempts + 1):
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(url + "?access_token=" + token, json=body)
        data = r.json()
        last_data = data
        if data.get("errcode", 0) == 0:
            return data
        if attempt >= max_attempts or not _is_transient_dingtalk_error(data):
            return data
        delay = min(2 ** (attempt - 1), 8)
        print(f"[DingTalkRetry] {op_name} 第{attempt}次失败, {delay}s后重试: {data}")
        await asyncio.sleep(delay)
    return last_data or {}


def _normalize_hired_date(value):
    """把钉钉 hired_date/create_time 统一成 YYYY-MM-DD。hired_date 可能是毫秒戳数字。"""
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        # 钉钉 hired_date 是当天 00:00:00 的毫秒戳，按本地日期解释。
        return datetime.fromtimestamp(float(value) / 1000).strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text:
        return ""
    if text.isdigit():
        n = int(text)
        if n > 100000000000:
            return datetime.fromtimestamp(n / 1000).strftime("%Y-%m-%d")
    return text[:10] if "T" in text else text[:10]


async def _get_all_depts(token, parent_id=1, depth=0):
    """递归获取所有子部门。任何部门树接口异常都必须硬失败，禁止返回残缺树。"""
    all_depts = []
    data = await _dingtalk_post_with_retry(
        DINGTALK["dept_list_url"], token, {"dept_id": parent_id}, f"拉取子部门 parent={parent_id}"
    )
    if data.get("errcode", 0) != 0:
        raise RuntimeError(f"拉取子部门失败 parent={parent_id}: {data}")
    subs = data.get("result", [])
    if not isinstance(subs, list):
        raise RuntimeError(f"拉取子部门返回格式异常 parent={parent_id}: {data}")
    for sub in subs:
        sub_id = sub["dept_id"]
        sub_name = sub.get("name", "")
        print(f"[Sync]{'  '*depth} {sub_name} (id={sub_id})")
        all_depts.append({"dept_id": sub_id, "name": sub_name})
        await asyncio.sleep(0.15)
        children = await _get_all_depts(token, sub_id, depth + 1)
        all_depts.extend(children)
    return all_depts


async def fetch_user_detail(token: str, userid: str):
    """调钉钉 /topapi/v2/user/get 获取员工入职日期。

    返回 hired_date 字符串或 None。
    优先级: hired_date > create_time。
    """
    data = await _dingtalk_post_with_retry(
        DINGTALK["user_detail_url"], token, {"userid": userid}, f"获取用户详情 {userid}"
    )
    if data.get("errcode", 0) != 0:
        print(f"[UserDetail] {userid} 获取失败: {data.get('errmsg', '?')}")
        return None
    result = data.get("result", {})
    hired = _normalize_hired_date(result.get("hired_date"))
    created = _normalize_hired_date(result.get("create_time"))
    if hired:
        print(f"[UserDetail] {userid} hired_date={hired}")
        return hired
    if created:
        print(f"[UserDetail] {userid} hired_date缺失, 用create_time={created}")
        return created
    print(f"[UserDetail] {userid} hired_date和create_time均缺失")
    return None


async def init_employee_balance(userid: str, category: str, default_quota: float, hired_date: str = None, name: str = ""):
    """为新入职员工初始化当月公休额度。

    规则:
    - 小时工: default_quota 天（通常5天）
    - 其他员工:
      - 当月10号前入职: default_quota / 2 天
      - 当月10号及之后入职: 0 天
    - hired_date 为空时按0天处理（安全兜底）
    """
    period = _current_period_sync()
    hired_date = _normalize_hired_date(hired_date)
    if hired_date:
        try:
            hire_dt = datetime.fromisoformat(hired_date)
        except (ValueError, TypeError):
            hire_dt = datetime.now()
        day_of_month = hire_dt.day
    else:
        day_of_month = 99  # 未知日期，不给额度

    if category == "hourly":
        days = default_quota  # 小时工始终给足
    elif day_of_month < 10:
        days = default_quota / 2.0
    else:
        days = 0.0

    balance_x100 = round(days * 100, 2)
    nm = name or userid
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO leave_balances (userid, name, balance, period, month_type, updated_at, created_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(userid, period) DO UPDATE SET "
            "balance=excluded.balance, name=excluded.name, month_type=excluded.month_type, updated_at=excluded.updated_at "
            "WHERE leave_balances.balance=0 AND NOT EXISTS ("
            "SELECT 1 FROM balance_transactions "
            "WHERE balance_transactions.userid=excluded.userid "
            "AND balance_transactions.created_at >= COALESCE(leave_balances.created_at, '') "
            "AND balance_transactions.action NOT IN ('carryover')"
            ")",
            (userid, nm, balance_x100, period, _target_month_type(period), now, now)
        )
        await db.commit()

    # 初始化写入后必须回读真实行。旧逻辑使用 DO NOTHING 后仍无条件把员工镜像
    # 改成5天，造成 employees=5 但 leave_balances=0 的分裂。
    actual = await get_leave_balance(userid, period)
    actual_balance_x100 = actual["balance"]
    actual_days = round(actual_balance_x100 / 100, 2)
    await update_employee_balance(userid, actual_days)
    print(
        f"[NewEmployee] {userid} {nm} category={category} hire_date={hired_date} "
        f"day={day_of_month} → init {days}天 ({balance_x100}×100), "
        f"actual {actual_days}天 ({actual_balance_x100}×100)"
    )
    return actual_days


def _current_period_sync():
    return datetime.now().strftime("%Y-%m")


async def _pull_active_contacts(upsert: bool = True):
    """拉取钉钉通讯录活跃人员。

    upsert=True 时写入/更新 employees；upsert=False 时只返回 seen_userids，供离职候选扫描使用。
    注意：本函数只负责拉取和可选 upsert，绝不做缺席屏蔽。
    """
    cfg = load_config()
    token = await token_manager.get_token(cfg["appKey"], cfg["appSecret"])

    print("[Sync] 递归拉取全部部门...")
    all_depts = await _get_all_depts(token, ROOT_DEPT_ID)
    all_depts.insert(0, {"dept_id": ROOT_DEPT_ID, "name": "全公司"})
    print(f"[Sync] 总部门数: {len(all_depts)}")

    seen_userids = set()
    total = 0
    dept_user_counts = {}
    new_employees = []  # 非小时工仍需获取入职日期后初始化
    new_employee_count = 0
    init_target_count = 0
    init_count = 0
    init_zero_count = 0
    init_failures = []

    for dept in all_depts:
        dept_id = dept["dept_id"]
        dept_name = dept.get("name", "")
        cursor = 0
        has_more = True
        dept_count = 0

        while has_more:
            user_result = await _dingtalk_post_with_retry(
                DINGTALK["user_list_url"], token, {"dept_id": dept_id, "cursor": cursor, "size": 100},
                f"拉取部门人员 dept={dept_id}/{dept_name}"
            )
            if user_result.get("errcode", 0) != 0:
                raise RuntimeError(f"拉取部门人员失败 dept={dept_id}/{dept_name}: {user_result}")

            result = user_result.get("result", {})
            users = result.get("list", [])
            has_more = result.get("has_more", False)
            cursor = result.get("next_cursor", 0)

            for user in users:
                if user.get("active") is False:
                    continue

                userid = user.get("userid", "")
                name = user.get("name", "")
                job_title = user.get("title", "") or ""

                if not userid:
                    continue

                if userid in seen_userids:
                    continue
                seen_userids.add(userid)
                dept_count += 1

                is_hourly = 0
                is_six_day = 0
                is_me = 0
                category = "other"

                for kw in HOURLY_KEYWORDS:
                    if kw in job_title or kw in name:
                        is_hourly = 1
                        category = "hourly"
                        break

                if str(dept_id) in SIX_DAY_DEPT_IDS:
                    is_six_day = 1
                    if category == "other":
                        category = "six_day"

                if userid == MY_USERID:
                    is_me = 1
                    category = "me"

                if userid in USER_CATEGORY_OVERRIDES:
                    category = USER_CATEGORY_OVERRIDES[userid]

                month_type = "special" if datetime.now().month in SPECIAL_MONTHS else "normal"
                default_quota = await get_rule_days(month_type, category)

                if upsert:
                    force_disabled = 1 if dept_name == BIANWAI_DEPT else 0
                    emp = {
                        "userid": userid,
                        "name": name,
                        "dept_id": str(dept_id),
                        "dept_name": dept_name,
                        "job_title": job_title,
                        "is_hourly": is_hourly,
                        "is_six_day": is_six_day,
                        "is_me": is_me,
                        "category": category,
                        "default_quota": default_quota,
                        "is_disabled": force_disabled,
                        "disabled_reason": "dept:编外" if force_disabled else "",
                    }
                    is_new, _ = await upsert_employee(emp)
                    if is_new and not force_disabled:
                        new_employee_count += 1

                    if not force_disabled and category == "hourly":
                        # 新员工直接初始化；若之前初始化异常或被0占位行挡住，
                        # 后续通讯录同步也会按需重试。已经有真实业务流水的0余额不会重发。
                        should_init = is_new or await needs_hourly_initialization(userid)
                        if should_init:
                            init_target_count += 1
                            try:
                                initialized_days = await init_employee_balance(
                                    userid, category,
                                    5 if not is_cron_adjust_excluded(emp) else default_quota,
                                    None, name
                                )
                                if initialized_days > 0:
                                    init_count += 1
                                else:
                                    init_zero_count += 1
                                    init_failures.append(
                                        f"{name}({userid}): 小时工实际余额为{initialized_days}天"
                                    )
                            except Exception as e:
                                init_failures.append(f"{name}({userid}): {e}")
                                print(f"[Sync] 新员工 {userid} 额度初始化失败: {e}")
                    elif is_new and not force_disabled:
                        # 非小时工仍需详情接口提供入职日期后按入职日规则计算。
                        new_employees.append((userid, name, category, default_quota))
                total += 1

            await asyncio.sleep(REQUEST_INTERVAL)
        dept_user_counts[str(dept_id)] = dept_count

    # 为非小时工新员工获取入职日期并初始化额度。
    if new_employees:
        print(f"[Sync] 发现 {len(new_employees)} 名非小时工新员工，正在获取入职日期并初始化额度...")
        for userid, name, category, default_quota in new_employees:
            try:
                hired_date = await fetch_user_detail(token, userid)
                if hired_date:
                    await update_employee_hired_date(userid, hired_date)
                initialized_days = await init_employee_balance(userid, category, default_quota, hired_date, name)
                if initialized_days > 0:
                    init_count += 1
                else:
                    init_failures.append(
                        f"{name}({userid}): 实际余额为{initialized_days}天"
                    )
                await asyncio.sleep(0.5)  # 避免接口限流
            except Exception as e:
                init_failures.append(f"{name}({userid}): {e}")
                print(f"[Sync] 新员工 {userid} 额度初始化失败: {e}")

    if new_employee_count or init_target_count:
        print(
            f"[Sync] 新员工额度初始化完成: {init_count}/{init_target_count} 人实际获得额度"
            + (f"，失败 {len(init_failures)} 人" if init_failures else "")
        )

    return {
        "total": total,
        "seen_userids": seen_userids,
        "dept_count": len(all_depts),
        "dept_user_counts": dept_user_counts,
        "complete": True,
        "new_employees": new_employee_count,
        "init_targets": init_target_count,
        "init_count": init_count,
        "init_zero": init_zero_count,
        "init_failed": len(init_failures),
        "init_failure_details": init_failures[:20],
    }


async def sync_contacts():
    """同步钉钉通讯录到 SQLite：只增/只更新/恢复出现人员，不再自动屏蔽缺席人员。"""
    result = await _pull_active_contacts(upsert=True)
    bianwai_count = await set_department_disabled([BIANWAI_DEPT], True, "dept:编外")
    print(f"[Sync] 同步完成，共 {result['total']} 人；离职缺席不在本任务处理；编外屏蔽规则覆盖 {bianwai_count} 人")
    return result["total"]


async def scan_departed_candidates(config: dict = None, operator: str = "cron"):
    """扫描离职/缺席候选。

    该任务只生成候选，不直接屏蔽。config.auto_confirm=True 时才自动确认，并且逐条写审计。
    """
    config = config or {}
    auto_confirm = bool(config.get("auto_confirm", False))
    source_run_id = config.get("source_run_id") or f"departed_scan_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    result = await _pull_active_contacts(upsert=False)

    if not result.get("complete") or not result.get("seen_userids"):
        return {"ok": False, "message": "通讯录拉取不完整，已跳过", "created": 0, "updated": 0, "auto_confirmed": 0}

    candidate_result = await create_departed_candidates(
        result["seen_userids"],
        source_run_id=source_run_id,
        reason="离职候选扫描：本次完整通讯录未出现",
        raw_meta={"dept_count": result.get("dept_count"), "total": result.get("total")},
    )

    auto_confirmed = 0
    if auto_confirm:
        candidates = await get_departed_candidates(status="pending", limit=1000)
        for cand in candidates:
            confirm = await confirm_departed_candidate(
                cand["id"],
                operator=operator or "cron-auto",
                note="自动屏蔽离职候选人(开关开启)",
                auto=True,
            )
            if confirm.get("ok"):
                auto_confirmed += 1

    return {
        "ok": True,
        "message": "离职候选扫描完成",
        "source_run_id": source_run_id,
        "created": candidate_result.get("created", 0),
        "updated": candidate_result.get("updated", 0),
        "total_candidates": candidate_result.get("total", 0),
        "auto_confirmed": auto_confirmed,
        "scanned": result.get("total", 0),
    }


# ========== 余额查询 ==========

async def query_balance(token, userid):
    """查单人余额，返回可用天数。从 leave_balances 读(权威源)，不再调钉钉"""
    bal = await get_leave_balance(userid)
    return bal["balance"] / 100


async def query_balance_realtime(userid):
    """单人查余额(读leave_balances同步employees),返回 {balance, err}。不再调钉钉"""
    try:
        balance = await query_balance(None, userid)
        await update_employee_balance(userid, balance)
        return {"balance": balance, "err": None}
    except Exception as e:
        return {"balance": None, "err": str(e)}


def _target_month_type(target_period: str) -> str:
    try:
        month = int(target_period.split("-")[1])
    except Exception:
        month = datetime.now().month
    return "special" if month in SPECIAL_MONTHS else "normal"


def _is_special_policy_employee(emp: dict) -> bool:
    return is_cron_adjust_excluded(emp or {})


async def check_leave_balance_with_future(userid: str, days: float, period: str = None, leave_start_date=None):
    """公休余额校验：当前月走现有余额，下月走预占额度校验。"""
    parsed = parse_leave_start_date(leave_start_date)
    if parsed["mode"] in ("missing", "current"):
        result = await check_leave_balance(userid, days, period)
        result["mode"] = "current"
        return result
    if parsed["mode"] != "next":
        return {"ok": False, "mode": "unsupported", "message": "仅支持提交当前月或下个月开始的公休", "target_period": parsed.get("target_period")}

    emp = await get_employee_by_userid(userid)
    if not emp:
        return {"ok": False, "mode": "future", "message": "员工不存在", "target_period": parsed["target_period"]}
    if _is_special_policy_employee(emp):
        # 特殊部门额度管够/年度发放，不进入未来预占；保持现有余额校验逻辑。
        result = await check_leave_balance(userid, days, period)
        result["mode"] = "current"
        result["special_policy"] = True
        return result

    month_type = _target_month_type(parsed["target_period"])
    limit_days = await get_rule_days(month_type, emp.get("category") or "other")
    limit = round(float(limit_days or 0) * 100, 2)
    reserved = round(float(await sum_future_reservations(userid, parsed["target_period"])), 2)
    need = round(float(days or 0), 2)
    available = round(limit - reserved, 2)
    ok = reserved + need <= limit
    return {
        "ok": ok,
        "mode": "future",
        "target_period": parsed["target_period"],
        "leave_start_date": parsed["date"],
        "limit": limit,
        "reserved": reserved,
        "available": max(available, 0),
        "need": need,
        "message": "下月公休额度充足" if ok else "下月公休额度不足",
    }


async def deduct_leave_balance_with_future(userid: str, days: float, reason: str, ref_id: str = "", leave_start_date=None):
    """公休扣减：当前月直接扣，下月写预占 reservation。"""
    parsed = parse_leave_start_date(leave_start_date)
    if parsed["mode"] in ("missing", "current"):
        return await deduct_leave_balance(userid, days, reason, ref_id)
    if parsed["mode"] != "next":
        return {"ok": False, "mode": "unsupported", "message": "仅支持提交当前月或下个月开始的公休", "target_period": parsed.get("target_period")}

    emp = await get_employee_by_userid(userid)
    if not emp:
        return {"ok": False, "mode": "future", "message": "员工不存在", "target_period": parsed["target_period"]}
    if _is_special_policy_employee(emp):
        return await deduct_leave_balance(userid, days, reason, ref_id)

    check = await check_leave_balance_with_future(userid, days, None, leave_start_date)
    if not check.get("ok"):
        return check
    if not ref_id or not str(ref_id).strip():
        return {"ok": False, "mode": "future", "message": "下月公休预占必须提供 ref_id 用于幂等"}

    result = await create_future_reservation(
        userid=userid,
        name=emp.get("name") or userid,
        days=round(float(days or 0), 2),
        leave_start_date=parsed["date"],
        target_period=parsed["target_period"],
        ref_id=ref_id,
        reason=reason or "下月公休预占",
    )
    result.update({"mode": "future", "target_period": parsed["target_period"], "leave_start_date": parsed["date"]})
    return result


async def refund_leave_balance_with_future(userid: str, days: float, reason: str, ref_id: str = "", leave_start_date=None):
    """公休回退：优先处理未来预占，再退回当前月普通扣减。"""
    reservation = await get_future_reservation_by_ref(ref_id)
    if reservation:
        status = reservation.get("status")
        if status in ("pending", "failed"):
            result = await cancel_future_reservation(ref_id, reason or "撤销/拒绝下月公休预占")
            result["idempotent"] = False
            return result
        if status == "cancelled":
            return {"ok": True, "mode": "future", "idempotent": True, "message": "下月公休预占已取消(幂等)"}
        if status == "applied":
            refund_ref = f"future_refund:{ref_id}"
            existing_refund = await get_future_reservation_by_ref(refund_ref)
            # 不复用 future 表记录 refund，直接依赖 balance_transactions 的 refund 幂等会被“必须有 deduct”挡住；这里用空ref_id执行一次并标cancelled。
            result = await refund_leave_balance(userid, reservation["days"], reason or "撤销已应用的下月公休预占", "", period=reservation["target_period"])
            if result.get("ok"):
                await cancel_future_reservation(ref_id, reason or "撤销已应用的下月公休预占")
                result.update({"mode": "future", "status": "cancelled", "target_period": reservation["target_period"]})
            return result
    return await refund_leave_balance(userid, days, reason, ref_id)


async def apply_future_reservations(period: str = None):
    """月初额度初始化后，应用当月 pending 的下月预占扣减。"""
    period = period or datetime.now().strftime("%Y-%m")
    reservations = await list_pending_future_reservations(period)
    total = len(reservations)
    success = 0
    failed = 0
    details = []
    for r in reservations:
        ref_id = r.get("ref_id") or ""
        apply_ref = f"future_apply:{ref_id}"
        try:
            result = await deduct_leave_balance(
                r["userid"],
                r["days"],
                "月底提交下月公休自动扣减",
                apply_ref,
                period=r["target_period"],
            )
            if result.get("ok"):
                await mark_future_reservation_applied(ref_id)
                success += 1
                details.append({"userid": r["userid"], "name": r.get("name"), "ok": True, "days": r["days"]})
            else:
                failed += 1
                msg = result.get("message") or "扣减失败"
                await mark_future_reservation_failed(ref_id, msg)
                details.append({"userid": r["userid"], "name": r.get("name"), "ok": False, "error": msg})
        except Exception as e:
            failed += 1
            msg = str(e)
            await mark_future_reservation_failed(ref_id, msg)
            details.append({"userid": r.get("userid"), "name": r.get("name"), "ok": False, "error": msg})
    if failed > 0:
        await send_notify(
            "下月公休预占扣减异常",
            f"### 下月公休预占扣减结果\n- 月份: {period}\n- 总数: {total}\n- 成功: {success}\n- 失败: {failed}\n- 失败详情(前5): {'; '.join((d.get('name') or d.get('userid') or '?') + ':' + d.get('error','') for d in details if not d.get('ok'))[:200]}"
        )
    return {"total": total, "success": success, "failed": failed, "period": period, "details": details}


async def refresh_balances_by_userids(userids):
    """刷新指定人员余额(从leave_balances同步到employees)。不再调钉钉"""
    success = 0
    failed = 0
    for uid in userids:
        try:
            balance = await query_balance(None, uid)
            await update_employee_balance(uid, balance)
            success += 1
        except Exception as e:
            print(f"[RefreshFilter] {uid} 失败: {e}")
            failed += 1
    return {"total": len(userids), "success": success, "failed": failed}


async def fetch_balances(userids):
    """查多人余额并返回{userid:balance},同步employees。从leave_balances读,不再调钉钉"""
    balances = {}
    failed = []
    for uid in userids:
        try:
            balance = await query_balance(None, uid)
            await update_employee_balance(uid, balance)
            balances[uid] = balance
        except Exception as e:
            print(f"[FetchBalance] {uid} 失败: {e}")
            failed.append(uid)
    return {"balances": balances, "failed": failed}


async def adjust_balance_batch(userids, delta_days, reason, operator="admin"):
    """批量调整余额(累加delta),以leave_balances为权威,不推钉钉(凌晨cron统一推)"""
    batch_id = f"batch_adjust_{int(time.time())}"
    await create_balance_snapshot(batch_id, "pre_batch_adjust", userids)

    success = 0
    failed = 0
    details = []
    for uid in userids:
        emp = await get_employee_by_userid(uid)
        if not emp:
            failed += 1
            details.append({"userid": uid, "name": "?", "success": False, "message": "员工不存在"})
            continue
        current = await query_balance(None, uid)
        new_balance = current + delta_days
        if new_balance < 0:
            new_balance = 0
        log_id = await add_quota_log({
            "batch_id": batch_id, "userid": uid, "name": emp["name"],
            "old_value": current, "new_value": new_balance, "delta": delta_days,
            "reason": reason, "operator": operator, "status": "pending"
        })
        await set_leave_balance_abs(uid, round(new_balance * 100, 2), emp["name"], reason, "adjust")
        await update_employee_balance(uid, new_balance)
        await update_quota_log_status(log_id, "success")
        success += 1
        details.append({"userid": uid, "name": emp["name"], "success": True, "old": current, "new": new_balance})
    if failed > 0:
        fail_names = [d["name"] for d in details if not d.get("success")]
        await send_notify(
            "批量调整异常",
            f"### 批量余额调整结果\n- 总数: {len(userids)}\n- 成功: {success}\n- 失败: {failed}\n- 失败人员(前5): {', '.join(fail_names[:5])}\n- 操作人: admin"
        )
    return {"total": len(userids), "success": success, "failed": failed, "details": details}


async def dept_adjust_batch(config: dict):
    """按部门/分类/指定人员批量调整额度(cron用)
    config字段:
      depts: [部门名列表]  如 ["No.01店","后勤部"]
      categories: [分类列表] 如 ["hourly","other"]
      userids: [userId列表] 精确指定
      delta: 调整天数(正增负减)
      reason: 原因
    筛选逻辑: depts/categories/userids 取并集(任一命中即纳入)
    """
    depts = config.get("depts", [])
    categories = config.get("categories", [])
    userids_cfg = config.get("userids", [])
    delta = config.get("delta", 0)
    reason = config.get("reason", "定时批量调整")

    employees = await get_all_employees()
    targets = []
    for emp in employees:
        if emp.get("is_disabled") or is_cron_adjust_excluded(emp):
            continue
        hit = False
        if depts and emp.get("dept_name") in depts:
            hit = True
        if categories and emp.get("category") in categories:
            hit = True
        if userids_cfg and emp.get("userid") in userids_cfg:
            hit = True
        # 如果三个条件都为空,不调整任何人(安全)
        if hit and (depts or categories or userids_cfg):
            targets.append(emp["userid"])

    if not targets:
        await send_notify("按部门调整-无目标", f"### 批量调整跳过\n筛选条件: depts={depts} categories={categories} userids={userids_cfg}\n未匹配到任何人员,本次不执行调整")
        return {"total": 0, "success": 0, "failed": 0, "skipped": True, "message": "无匹配人员"}

    # 复用已有的批量调整逻辑
    return await adjust_balance_batch(targets, delta, reason, "cron")


async def rollback_adjustment(batch_id: str):
    """回滚某次调整: 把涉及人员余额恢复到快照值。以leave_balances为权威,不推钉钉(凌晨cron统一推)"""
    snapshots = await get_snapshot_detail(batch_id)
    if not snapshots:
        return {"success": 0, "failed": 0, "total": 0, "message": "找不到该批次的备份(可能已过期清理)"}

    userids = [s["userid"] for s in snapshots]
    rollback_batch_id = f"rollback_{int(time.time())}"
    await create_balance_snapshot(rollback_batch_id, "pre_rollback", userids)

    success = 0
    failed = 0
    details = []
    for snap in snapshots:
        uid = snap["userid"]
        name = snap["name"]
        target_balance = snap["balance"]
        current = await query_balance(None, uid)
        await set_leave_balance_abs(uid, round(target_balance * 100, 2), name, f"回滚{batch_id}", "rollback")
        await update_employee_balance(uid, target_balance)
        log_id = await add_quota_log({
            "batch_id": rollback_batch_id, "userid": uid, "name": name,
            "old_value": current, "new_value": target_balance, "delta": round(target_balance - current, 2),
            "reason": f"回滚操作{batch_id[:20]}", "operator": "rollback", "status": "success"
        })
        success += 1
        details.append({"userid": uid, "name": name, "success": True, "restored_to": target_balance})
    return {"success": success, "failed": failed, "total": len(snapshots), "details": details}


# 全局任务进度(进程内)
_task_progress = {}

def get_task_progress(task_id):
    return _task_progress.get(task_id, {"status": "unknown"})


async def refresh_all_balances_async(task_id="default"):
    """异步刷新所有人余额:从leave_balances同步到employees。不再调钉钉"""
    _task_progress[task_id] = {"status": "running", "total": 0, "done": 0, "success": 0, "failed": 0, "current": ""}
    employees = await get_all_employees()
    _task_progress[task_id]["total"] = len(employees)

    success = 0
    failed = 0
    for i, emp in enumerate(employees):
        _task_progress[task_id]["current"] = f"{emp.get('name','')} ({i+1}/{len(employees)})"
        try:
            balance = await query_balance(None, emp["userid"])
            await update_employee_balance(emp["userid"], balance)
            success += 1
        except Exception as e:
            print(f"[Refresh] {emp.get('name')} 失败: {e}")
            failed += 1
        _task_progress[task_id]["done"] = i + 1
        _task_progress[task_id]["success"] = success
        _task_progress[task_id]["failed"] = failed

    _task_progress[task_id]["status"] = "done"
    print(f"[Refresh] 完成 {len(employees)}人 成功{success} 失败{failed}")
    return {"total": len(employees), "success": success, "failed": failed}


async def refresh_all_balances():
    """刷新所有人余额(同步,保留兼容)"""
    return await refresh_all_balances_async("sync")


# ========== 余额调整 ==========

async def set_balance(token, userid, new_balance_days, reason, operator, batch_id):
    """设绝对值余额"""
    now = datetime.now()
    start_ms = int(datetime(now.year, now.month, 1).timestamp() * 1000)
    end_ms = int(datetime(now.year, now.month + 1, 1).timestamp() * 1000) - 1
    cycle = f"{now.year}-M-{now.month}"
    quota_per_day = int(round(new_balance_days * 100))

    body = {
        "op_userid": OP_USERID,
        "leave_quotas": {
            "start_time": start_ms,
            "reason": reason,
            "quota_num_per_day": quota_per_day,
            "quota_cycle": cycle,
            "end_time": end_ms,
            "leave_code": LEAVE_CODE,
            "quota_num_per_hour": 0,
            "userid": userid
        }
    }
    d = await _dingtalk_post_with_retry(
        DINGTALK["quota_update_url"], token, body, f"更新公休额度 {userid}", timeout=15
    )
    return d.get("errcode") == 0, d


async def adjust_balance(userid, delta_days, reason, operator="admin"):
    """调整余额(累加delta)，以leave_balances为权威，不推钉钉(凌晨cron统一推)"""
    emp = await get_employee_by_userid(userid)
    if not emp:
        return {"success": False, "message": "员工不存在"}

    batch_id = f"adjust_{int(time.time())}"
    await create_balance_snapshot(batch_id, "pre_adjust", [userid])

    current = await query_balance(None, userid)
    new_balance = current + delta_days
    if new_balance < 0:
        new_balance = 0

    log_id = await add_quota_log({
        "batch_id": batch_id, "userid": userid, "name": emp["name"],
        "old_value": current, "new_value": new_balance, "delta": delta_days,
        "reason": reason, "operator": operator, "status": "pending"
    })

    # 写 leave_balances(权威) + employees(镜像), 不推钉钉
    await set_leave_balance_abs(userid, round(new_balance * 100, 2), emp["name"], reason, "adjust")
    await update_employee_balance(userid, new_balance)
    await update_quota_log_status(log_id, "success")
    return {"success": True, "message": f"调整成功: {current} → {new_balance}天", "old": current, "new": new_balance}


# 通知接收人(管理员)
NOTIFY_USERID = __import__("os").getenv("DINGTALK_NOTIFY_USERID", "")
ROBOT_CODE = __import__("os").getenv("DINGTALK_ROBOT_CODE", "")


async def send_notify(title: str, content: str):
    """发送钉钉机器人单聊通知给管理员,失败只记日志不抛异常"""
    try:
        cfg = load_config()
        token = await token_manager.get_token(cfg["appKey"], cfg["appSecret"])
        body = {
            "robotCode": ROBOT_CODE,
            "userIds": [NOTIFY_USERID],
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({"title": title, "text": content}, ensure_ascii=False)
        }
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post("https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                json=body, headers={"x-acs-dingtalk-access-token": token})
        d = r.json()
        print(f"[Notify] {title} => {d.get('processQueryKey','')[:20] or d}")
        return True
    except Exception as e:
        print(f"[Notify] 发送失败 {title}: {e}")
        return False


async def batch_monthly_assign(month_type="normal"):
    """月初批量处理：小时工绝对重置为5天且按月幂等，其他人员沿用原累加规则。"""
    employees = await get_all_employees()
    period = _current_period()

    batch_id = f"monthly_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    snap_count = await create_balance_snapshot(batch_id, "pre_monthly")
    print(f"[Batch] 已生成操作前快照: {snap_count}人")
    cron_id = await add_cron_run("monthly_assign", "running", f"批量发放 {month_type}")

    hourly_employees = [
        emp for emp in employees
        if emp.get("category") == "hourly" and not is_cron_adjust_excluded(emp)
    ]
    try:
        hourly_plan = await prepare_hourly_monthly_resets(period, batch_id, hourly_employees)
    except Exception as e:
        await update_cron_run(cron_id, "failed", f"小时工重置名单准备失败: {e}", len(employees), 0, 1, 0)
        raise
    hourly_resets = hourly_plan["rows"]
    hourly_reset_userids = {row["userid"] for row in hourly_resets}

    total = len({emp["userid"] for emp in employees} | hourly_reset_userids)
    success = 0
    failed = 0
    skipped = 0
    fail_detail = []

    for reset in hourly_resets:
        if reset["status"] == "success":
            skipped += 1
            continue
        try:
            result = await apply_hourly_monthly_reset(period, reset["userid"], 5, month_type)
            if result["idempotent"]:
                skipped += 1
            else:
                success += 1
        except Exception as e:
            failed += 1
            fail_detail.append(f"{reset['name']}: 小时工重置失败 {e}")
            await mark_hourly_monthly_reset_failed(period, reset["userid"], str(e))

    for emp in employees:
        if is_cron_adjust_excluded(emp):
            # 特殊政策部门不发放，但必须结转上月余额建当月行，否则当月无行
            # 导致 deduct/check 误报"余额不足: 当前0天"。
            try:
                await carryover_balance_if_missing(emp["userid"], emp.get("name") or "", reason_prefix="月度结转")
                skipped += 1
            except Exception as e:
                failed += 1
                fail_detail.append(f"{emp['name']}: 结转失败 {e}")
            continue
        category = emp["category"]
        if category == "hourly":
            # 首次运行后入职的小时工已在入职同步中获得5天，不加入重跑名单。
            if emp["userid"] not in hourly_reset_userids:
                skipped += 1
            continue
        delta = await get_rule_days(month_type, category)
        if delta == 0:
            skipped += 1
            continue

        try:
            current = await query_balance(None, emp["userid"])
            new_balance = current + delta

            log_id = await add_quota_log({
                "batch_id": batch_id, "userid": emp["userid"], "name": emp["name"],
                "old_value": current, "new_value": new_balance, "delta": delta,
                "reason": f"月初发放({month_type})+{delta}天", "operator": "cron", "status": "pending"
            })

            await set_leave_balance_abs(emp["userid"], round(new_balance * 100, 2), emp["name"], f"月初发放({month_type})+{delta}天", "monthly")
            await update_employee_balance(emp["userid"], new_balance)
            await update_quota_log_status(log_id, "success")
            success += 1
        except Exception as e:
            failed += 1
            fail_detail.append(f"{emp['name']}: {str(e)}")

    status = "success" if failed == 0 else ("partial" if success > 0 else "failed")
    await update_cron_run(cron_id, status, "; ".join(fail_detail[:5]), total, success, failed, skipped)
    if failed > 0:
        await send_notify(
            f"月初发放异常({status})",
            f"### 月初额度发放结果\n- 状态: **{status}**\n- 总数: {total}\n- 成功: {success}\n- 失败: {failed}\n- 跳过: {skipped}\n- 失败详情(前5): {'; '.join(fail_detail[:5]) or '无'}\n- 批次: {batch_id}"
        )
    return {"total": total, "success": success, "failed": failed, "skipped": skipped, "batch_id": batch_id}


async def annual_dept_reset(config: dict = None):
    """每年三月重置特殊部门额度：国网/骑手=99天，蔬果品类=52天。

    这是绝对重置，不是累加；写 leave_balances(×100) + employees.current_balance(天)。
    """
    employees = await get_all_employees()
    targets = []
    for emp in employees:
        if emp.get("is_disabled"):
            continue
        days = annual_reset_days_for_employee(emp)
        if days is not None:
            targets.append((emp, days))

    batch_id = f"annual_reset_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    if targets:
        await create_balance_snapshot(batch_id, "pre_annual_reset", [emp["userid"] for emp, _ in targets])
    cron_id = await add_cron_run("annual_dept_reset", "running", "特殊部门年度额度重置")

    success = 0
    failed = 0
    fail_detail = []
    for emp, days in targets:
        try:
            await set_leave_balance_abs(emp["userid"], days * 100, emp["name"], f"年度额度重置({days}天)", "annual_reset")
            await update_employee_balance(emp["userid"], days)
            success += 1
        except Exception as e:
            failed += 1
            fail_detail.append(f"{emp.get('name')}: {e}")

    skipped = len(employees) - len(targets)
    status = "success" if failed == 0 else ("partial" if success > 0 else "failed")
    await update_cron_run(cron_id, status, "; ".join(fail_detail[:5]), len(employees), success, failed, skipped)
    if failed > 0:
        await send_notify("特殊部门年度额度重置异常", f"### 年度额度重置结果\n- 状态: {status}\n- 总数: {len(employees)}\n- 成功: {success}\n- 失败: {failed}\n- 跳过: {skipped}\n- 失败详情(前5): {'; '.join(fail_detail[:5]) or '无'}")
    return {"total": len(employees), "success": success, "failed": failed, "skipped": skipped, "batch_id": batch_id}


async def push_balances_to_dingtalk():
    """凌晨定时:把 leave_balances 真实额度全量推到钉钉侧(唯一与钉钉交互点)"""
    cfg = load_config()
    token = await token_manager.get_token(cfg["appKey"], cfg["appSecret"])
    employees = await get_all_employees()
    cron_id = await add_cron_run("push_dingtalk", "running", "全量推钉钉")

    total = len(employees)
    success = 0
    failed = 0
    fail_detail = []
    for emp in employees:
        uid = emp["userid"]
        try:
            bal = await get_leave_balance(uid)
            new_balance_days = bal["balance"] / 100
            ok, resp = await set_balance(token, uid, new_balance_days, "凌晨同步推钉钉", "cron_push", f"push_{int(time.time())}")
            if ok:
                success += 1
            else:
                failed += 1
                fail_detail.append(f"{emp['name']}: {resp}")
        except Exception as e:
            failed += 1
            fail_detail.append(f"{emp['name']}: {str(e)}")
        await asyncio.sleep(REQUEST_INTERVAL)

    status = "success" if failed == 0 else ("partial" if success > 0 else "failed")
    await update_cron_run(cron_id, status, "; ".join(fail_detail[:5]), total, success, failed, 0)
    print(f"[PushDingtalk] 完成 {total}人 成功{success} 失败{failed}")
    if failed > 0:
        await send_notify("凌晨推钉钉异常", f"### 推钉钉结果\n- 状态: {status}\n- 总数: {total}\n- 成功: {success}\n- 失败: {failed}\n- 失败详情(前5): {'; '.join(fail_detail[:5]) or '无'}")
    return {"total": total, "success": success, "failed": failed}
