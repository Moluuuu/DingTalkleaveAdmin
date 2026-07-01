# DingTalkleaveAdmin - 企业公休余额管理后台

![](https://img.shields.io/badge/GPT%20Assisted-100%25-00a67d?logo=openai)
![](https://img.shields.io/badge/DeepSeek%20Assisted-100%25-00a67d)

基于 FastAPI + SQLite 的企业假期余额管理后台，支持余额个性化管理、钉钉镜像同步、钉盘备份和 Windows 灾备恢复。

## 项目结构

```text
.
├── leaveadmin/                 # 应用源码包
│   ├── main.py                 # FastAPI 主服务
│   ├── database.py             # SQLite 数据层
│   ├── dingtalk_ops.py         # 钉钉通讯录、假期额度、通知集成
│   ├── scheduler.py            # APScheduler 独立调度进程
│   ├── dingpan_backup.py       # 钉盘备份/校验/保留策略
│   ├── windows_restore_leave_balance.py  # 灾备恢复核心逻辑
│   └── templates/admin.html    # 单页管理后台
├── scripts/                    # 运维入口脚本，不混在源码根目录
│   ├── backup_db.sh
│   ├── run_dingpan_backup_cron.py
│   └── windows_restore_gui.pyw
├── tests/                      # pytest 测试
├── config.example.json
├── constants.example.yaml
├── dingpan_backup_config.example.json
├── .env.example
└── requirements.txt
```

## 功能

- 员工/部门通讯录递归同步，只增不减，离职候选走人工审核
- 余额查询、扣减、退回、重置，全链路使用 ×100 单位：1 天 = 100，0.5 天 = 50
- `ref_id` 幂等，防止同一审批单重复扣减或重复退回
- 月初额度发放、下月公休预占、特殊部门年度额度
- 自管定时任务，后台 UI 增删改，无需 SSH 改 crontab
- 每日定时推送钉钉镜像，钉钉不是实时双写源
- 钉盘 24 小时滚动备份
- Windows 紧急恢复工具，默认 dry-run，真实覆盖需显式确认

## 数据权威说明

`leave_balances` 是唯一权威余额表。余额写入先落本地 SQLite，钉钉只作为定时镜像目标。

这样避免实时双写带来的覆盖冲突，也绕开钉钉部分接口对小数天处理不一致的问题。

## 环境要求

- Python 3.10+
- SQLite3
- 钉钉开放平台应用，需要通讯录、假期管理、钉盘等相应权限

## 快速启动

```bash
git clone https://github.com/Moluuuu/DingTalkleaveAdmin.git
cd DingTalkleaveAdmin

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.json config.json
cp constants.example.yaml constants.yaml
cp dingpan_backup_config.example.json dingpan_backup_config.json
cp .env.example .env
```

编辑 `config.json`、`constants.yaml`、`dingpan_backup_config.json` 和 `.env` 后启动：

```bash
# API 服务
python -m uvicorn leaveadmin.main:app --host 0.0.0.0 --port 18001

# 另开一个进程启动调度器
python -m leaveadmin.scheduler
```

## 配置

### `.env`

```bash
LEAVEADMIN_AUTH_PASSWORD=change-me

# 可选：运行目录和配置路径。部署到 /opt/leaveadmin 时建议显式设置。
LEAVEADMIN_HOME=/opt/leaveadmin
LEAVEADMIN_DB_PATH=/opt/leaveadmin/admin.db
LEAVEADMIN_CONFIG=/opt/leaveadmin/config.json
LEAVEADMIN_CONSTANTS=/opt/leaveadmin/constants.yaml

# 可选：钉钉机器人通知。为空则不推送。
DINGTALK_NOTIFY_USERID=
DINGTALK_ROBOT_CODE=

# 可选：灾备恢复默认值。也可以在 GUI/CLI 中手动填写。
DINGTALK_LEAVE_CODE=
DINGTALK_OP_USERID=
```

### `config.json`

```json
{
  "appKey": "钉钉应用 AppKey",
  "appSecret": "钉钉应用 AppSecret"
}
```

### `constants.yaml`

```yaml
leave_code: "YOUR_DINGTALK_LEAVE_CODE"
quota_rules:
  normal:
    hourly: 5
    six_day: 6
    other: 4
    me: 8
```

## 常用入口

```bash
# API 服务
python -m uvicorn leaveadmin.main:app --host 0.0.0.0 --port 18001

# 调度器
python -m leaveadmin.scheduler

# 钉盘备份 runner
python scripts/run_dingpan_backup_cron.py

# Windows GUI 恢复工具
python scripts/windows_restore_gui.pyw
```

## API 概览

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/leave/check` | GET | 余额校验，支持 `leave_start_date` 跨月 |
| `/api/leave/deduct` | POST | 余额扣减，当前月直接扣，下月写预占 |
| `/api/leave/refund` | POST | 余额退回 |
| `/api/leave/reset` | POST | 月初重置，绝对值 |
| `/api/leave/all` | GET | 全员余额查询 |
| `/api/future-reservations` | GET | 下月预占查询 |
| `/api/apply-future-reservations` | POST | 月初应用预占扣减 |
| `/api/push-dingtalk` | POST | 全量推送余额到钉钉 |
| `/api/employees` | GET | 员工列表 |
| `/api/sync` | POST | 触发通讯录同步 |

除 `/` 和 `/api/auth` 外，API 需要 `X-Auth` header，值为 `LEAVEADMIN_AUTH_PASSWORD`。

## 测试

```bash
python -m pytest tests -q
```

测试使用临时 SQLite 数据库和 fake client，不连接生产环境。

## 安全说明

- 不提交 `admin.db`、日志、备份包或真实配置文件
- `config.json`、`constants.yaml`、`dingpan_backup_config.json`、`.env` 均在 `.gitignore` 中
- 公开仓库只保留 example 配置和占位符
- 管理后台密码、通知接收人、灾备恢复 leave code / operator userid 均通过环境变量或运行时输入提供

## AI 协作声明

本项目由 GPT 和 DeepSeek 协作整理发布。

- GPT：系统架构设计、余额权威翻转、未来月份预占、员工余额初始化、验收与发布整改
- DeepSeek：脱敏发布包初版构建、README 初稿、GitHub 仓库部署

---

公休不公休的，代码说了算。
