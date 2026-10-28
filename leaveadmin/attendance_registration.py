"""单张人工登记：服务端读取OA事实，原扣由发送事务复查；不修改余额。"""
import json
from datetime import date

from leaveadmin.attendance_effects import prepare_public_leave, send_public_leave_once, send_attendance_payload

# 与当前公休模板的固定组件对应，使用ID而非可改名的显示标签。
FIELDS = {
    'person': ('InnerContactField_W4DIK477K8W0', 'InnerContactField'),
    'start': ('DDDateField_145R7CIBLKOW0', 'DDDateField'),
    'start_half': ('DDSelectField_203F9NMJG7XC0', 'DDSelectField'),
    'end': ('DDDateField_1E4048PI5JUO0', 'DDDateField'),
    'end_half': ('DDSelectField_Q6PSWKGW3SW0', 'DDSelectField'),
    'ref': ('SeqNumberField_SIPXEIKJ9VK0', 'SeqNumberField'),
    'days': ('TextField-days', 'TextField'),
    'start_helper': ('TextField-leave_start_date', 'TextField'),
    'userid': ('TextField_1DVSFHTYEYAO0', 'TextField'),
}


async def fetch_approval(instance_id):
    import httpx
    from leaveadmin.dingtalk_ops import load_config, token_manager
    config = load_config()
    token = await token_manager.get_token(config['appKey'], config['appSecret'])
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
        response = await client.get(
            'https://api.dingtalk.com/v1.0/workflow/processInstances',
            params={'processInstanceId': instance_id},
            headers={'x-acs-dingtalk-access-token': token},
        )
        response.raise_for_status()
        return response.json()


def registration_args(response, *, corp_id, instance_id, business_id, ref_id, jump_url):
    """仅适配已实读的企业应用响应；实例归属由单单配置限制，不伪造processCode。"""
    if not isinstance(response, dict) or response.get('success') is not True:
        raise ValueError('审批查询未成功')
    detail = response.get('result')
    if (not isinstance(detail, dict) or detail.get('businessId') != business_id
            or detail.get('status') != 'COMPLETED' or detail.get('result') != 'agree'
            or detail.get('bizAction') not in (None, '', 'NONE')
            or detail.get('attachedProcessInstanceIds') != []
            or detail.get('mainProcessInstanceId')):
        raise ValueError('审批未通过、编号不符或存在修改撤销关联')
    components = detail.get('formComponentValues')
    if not isinstance(components, list):
        raise ValueError('审批表单缺失')
    fields = {}
    for name, (identifier, kind) in FIELDS.items():
        matches = [c for c in components if isinstance(c, dict) and c.get('id') == identifier]
        if len(matches) != 1 or matches[0].get('componentType') != kind:
            raise ValueError('公休组件缺失或变更')
        fields[name] = matches[0]
    people = json.loads(fields['person']['extValue'])
    if not isinstance(people, list) or len(people) != 1 or not isinstance(people[0], dict):
        raise ValueError('休假人必须唯一')
    userid = people[0].get('emplId')
    if not isinstance(userid, str) or not userid or fields['userid'].get('value') != userid:
        raise ValueError('休假人与辅助人员字段不一致')
    if fields['ref'].get('value') != ref_id:
        raise ValueError('原扣编号不匹配')
    start_text, end_text = fields['start']['value'], fields['end']['value']
    start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
    halves = {'上午': (0, 'AM'), '下午': (1, 'PM')}
    first, last = halves[fields['start_half']['value']], halves[fields['end_half']['value']]
    units = ((end - start).days * 2 + last[0] - first[0] + 1) * 50
    if (units <= 0 or (start.year, start.month) != (end.year, end.month)
            or start.isoformat() != start_text or end.isoformat() != end_text
            or fields['days'].get('value') != str(units)
            or fields['start_helper'].get('value') != start_text):
        raise ValueError('公休日期或天数不一致')
    # 使用详情中真实返回的任务链接，不接受任意跳转地址。
    urls = {t.get(k) for t in detail.get('tasks', []) if isinstance(t, dict)
            for k in ('mobileUrl', 'pcUrl') if isinstance(t.get(k), str)}
    if jump_url not in urls:
        raise ValueError('跳转地址不是该审批返回的任务链接')
    return dict(corp_id=corp_id, application_id=instance_id, revision=1,
                predebit_ref=ref_id, userid=userid, approve_id=instance_id,
                from_time=f'{start_text} {first[1]}', to_time=f'{end_text} {last[1]}',
                jump_url=jump_url)


async def register_manual_leave(*, corp_id, instance_id, business_id, ref_id, jump_url):
    response = await fetch_approval(instance_id)
    args = registration_args(response, corp_id=corp_id, instance_id=instance_id,
                             business_id=business_id, ref_id=ref_id, jump_url=jump_url)
    await prepare_public_leave(**args)
    return await send_public_leave_once(corp_id=corp_id, application_id=instance_id,
        revision=1, send=send_attendance_payload, require_original_debit=True)
