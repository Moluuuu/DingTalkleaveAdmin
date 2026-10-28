"""公休生效请求的不可变准备记录；准备成功不代表获得发送授权。"""

import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

import aiosqlite

from leaveadmin import database


SCHEMA = """
CREATE TABLE IF NOT EXISTS attendance_effect_requests (
    corp_id TEXT NOT NULL,
    application_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    predebit_ref TEXT NOT NULL,
    approve_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    canonicalization_version INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'prepared' CHECK (state = 'prepared'),
    created_at TEXT NOT NULL,
    PRIMARY KEY (corp_id, application_id, revision),
    UNIQUE (corp_id, approve_id),
    UNIQUE (corp_id, predebit_ref)
)
"""


def _required_text(value, name):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name}必须是无首尾空白的非空字符串")
    return value


def _half_day(value):
    _required_text(value, "半天时间")
    # 不依赖操作系统locale的AM/PM解析。
    if len(value) != 13 or value[10:] not in (" AM", " PM"):
        raise ValueError("时间须为YYYY-MM-DD AM或YYYY-MM-DD PM")
    parsed = datetime.strptime(value[:10], "%Y-%m-%d").date()
    if parsed.isoformat() != value[:10]:
        raise ValueError("日期格式不规范")
    return parsed.toordinal() * 2 + (value[11:] == "PM")


def public_leave_payload(*, userid, approve_id, from_time, to_time, jump_url):
    """构建已确认的公休请求；不生成原单身份，也不调用钉钉。"""
    _required_text(userid, "userid")
    _required_text(approve_id, "approve_id")
    _required_text(jump_url, "jump_url")
    if len(approve_id) > 100 or len(jump_url) > 200:
        raise ValueError("approve_id或jump_url超过接口长度限制")
    url = urlsplit(jump_url)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("jump_url须为无凭据的HTTPS地址")
    start, end = _half_day(from_time), _half_day(to_time)
    if end < start or end // 2 - start // 2 > 31:
        raise ValueError("公休区间顺序错误或跨度超过31天")
    return {
        "userid": userid, "approve_id": approve_id,
        "from_time": from_time, "to_time": to_time, "jump_url": jump_url,
        "biz_type": 3, "tag_name": "请假", "sub_type": "公休##halfDay",
        "duration_unit": "halfDay", "calculate_model": 0,
    }


async def prepare_public_leave(*, corp_id, application_id, revision,
                               predebit_ref, userid, approve_id,
                               from_time, to_time, jump_url):
    """冻结整份请求。调用者仍须另行核实审批、原扣与发送前置条件。

    本模块不提供发送/领取入口；prepared不能被解释为ready或已生效。
    原扣引用暂不允许跨修订复用，修订处理须先完成协调层设计。
    """
    for name, value in (("corp_id", corp_id), ("application_id", application_id),
                        ("predebit_ref", predebit_ref)):
        _required_text(value, name)
    if type(revision) is not int or revision < 1:
        raise ValueError("revision须为正整数")
    payload = public_leave_payload(userid=userid, approve_id=approve_id,
                                  from_time=from_time, to_time=to_time,
                                  jump_url=jump_url)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    async with aiosqlite.connect(database.DB_PATH, timeout=5) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT * FROM attendance_effect_requests WHERE corp_id=? AND "
            "((application_id=? AND revision=?) OR approve_id=? OR predebit_ref=?)",
            (corp_id, application_id, revision, approve_id, predebit_ref),
        ) as cursor:
            rows = await cursor.fetchall()
        if rows:
            row = rows[0]
            if (len(rows) != 1 or row["application_id"] != application_id
                    or row["revision"] != revision or row["approve_id"] != approve_id
                    or row["predebit_ref"] != predebit_ref
                    or row["canonicalization_version"] != 1
                    or row["payload_json"] != canonical):
                raise ValueError("生效请求身份或内容冲突")
            await db.commit()
            return {"state": row["state"], "reused": True}
        await db.execute(
            "INSERT INTO attendance_effect_requests "
            "(corp_id,application_id,revision,predebit_ref,approve_id,payload_json,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (corp_id, application_id, revision, predebit_ref, approve_id, canonical,
             datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    return {"state": "prepared", "reused": False}


async def send_attendance_payload(payload_json):
    """内部旧版finish适配；仅供完成业务授权后的单次发送核心调用。"""
    import httpx
    # 延迟导入，准备记录与数据库初始化不应加载钉钉配置。
    from leaveadmin.dingtalk_ops import load_config, token_manager

    if not isinstance(payload_json, str):
        raise TypeError("payload_json须为冻结的JSON字符串")
    cfg = load_config()
    token = await token_manager.get_token(cfg['appKey'], cfg['appSecret'])
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
        response = await client.post(
            'https://oapi.dingtalk.com/topapi/attendance/approve/finish',
            params={'access_token': token},
            content=payload_json.encode('utf-8'),
            headers={'Content-Type': 'application/json'},
        )
        response.raise_for_status()
    return response.json()


SEND_SCHEMA = """
CREATE TABLE IF NOT EXISTS attendance_effect_sends (
    corp_id TEXT NOT NULL,
    application_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    approve_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('sending', 'confirmed', 'unknown')),
    error_type TEXT NOT NULL DEFAULT '',
    result_json TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY (corp_id, application_id, revision),
    UNIQUE (corp_id, approve_id)
 )
"""


async def send_public_leave_once(*, corp_id, application_id, revision, send,
                                 require_original_debit=False):
    """内部单次发送核心，无默认网络适配器或公开入口。

    send接收冻结的JSON字符串，返回旧版finish响应字典。调用方必须先完成
    审批及原扣授权；prepared本身不授权。任何已有发送记录均不重发。
    取消或写回失败会留下sending，表示需要核查，不能当作失败重试。
    """
    if not callable(send):
        raise ValueError("send须为异步发送函数")
    for name, value in (("corp_id", corp_id), ("application_id", application_id)):
        _required_text(value, name)
    if type(revision) is not int or revision < 1:
        raise ValueError("revision须为正整数")
    key = (corp_id, application_id, revision)
    async with aiosqlite.connect(database.DB_PATH, timeout=5) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT approve_id,payload_json,predebit_ref FROM attendance_effect_requests "
            "WHERE corp_id=? AND application_id=? AND revision=?", key,
        ) as cursor:
            prepared = await cursor.fetchone()
        if prepared is None:
            raise ValueError("生效请求尚未准备")
        async with db.execute(
            "SELECT * FROM attendance_effect_sends "
            "WHERE corp_id=? AND application_id=? AND revision=?", key,
        ) as cursor:
            existing = await cursor.fetchone()
        if existing is not None:
            if (existing['approve_id'] != prepared['approve_id']
                    or existing['payload_json'] != prepared['payload_json']):
                raise ValueError("发送记录与准备内容冲突")
            await db.commit()
            return {"state": existing['state'], "reused": True,
                    "error_type": existing['error_type']}
        if require_original_debit:
            payload = json.loads(prepared['payload_json'])
            ref = prepared['predebit_ref']
            async with db.execute(
                "SELECT 1 FROM future_leave_reservations WHERE TRIM(ref_id)=?", (ref,),
            ) as cursor:
                if await cursor.fetchone():
                    raise ValueError('预占单暂不支持此人工登记入口')
            async with db.execute(
                "SELECT id,userid,ref_id,action,days FROM balance_transactions "
                "WHERE TRIM(ref_id) IN (?,?,?)",
                (ref, 'future_apply:' + ref, 'future_refund:' + ref),
            ) as cursor:
                debits = await cursor.fetchall()
            units = (_half_day(payload['to_time']) - _half_day(payload['from_time']) + 1) * 50
            if (len(debits) != 1 or debits[0]['action'] != 'deduct'
                    or debits[0]['ref_id'] != ref
                    or debits[0]['userid'] != payload['userid']
                    or debits[0]['days'] != -units):
                raise ValueError('原扣不唯一、已返还或人员金额不匹配')
        await db.execute(
            "INSERT INTO attendance_effect_sends "
            "(corp_id,application_id,revision,approve_id,payload_json,state,created_at) "
            "VALUES (?,?,?,?,?,'sending',?)",
            (*key, prepared['approve_id'], prepared['payload_json'],
             datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()

    state, error_type, result_json = 'unknown', '', None
    try:
        response = await send(prepared['payload_json'])
        if (isinstance(response, dict) and type(response.get('errcode')) is int
                and response['errcode'] == 0 and isinstance(response.get('result'), dict)):
            result_json = json.dumps(response['result'], ensure_ascii=False, allow_nan=False)
            state = 'confirmed'
        else:
            error_type = 'UnconfirmedResponse'
    except Exception as exc:
        # 异常文本可能含带token的URL，仅保留类别，不保存原始异常。
        error_type = type(exc).__name__

    async with aiosqlite.connect(database.DB_PATH, timeout=5) as db:
        await db.execute(
            "UPDATE attendance_effect_sends SET state=?,error_type=?,result_json=?,finished_at=? "
            "WHERE corp_id=? AND application_id=? AND revision=? AND state='sending'",
            (state, error_type, result_json, datetime.now(timezone.utc).isoformat(), *key),
        )
        await db.commit()
    return {"state": state, "reused": False, "error_type": error_type}
