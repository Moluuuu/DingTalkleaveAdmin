# leaveAdmin - 企业公休余额管理后台

![](https://img.shields.io/badge/GPT%20Assisted-100%25-00a67d?logo=openai)
![](https://img.shields.io/badge/DeepSeek%20Assisted-100%25-00a67d)

基于 FastAPI + SQLite 的企业假期余额管理后台，支持余额个性化管理、钉钉镜像同步、灾备恢复。

## 架构概览

| 组件 | 说明 |
|---|---|
| **FastAPI 主服务** (`main.py`) | REST API，端口 18001，密码保护 |
| **SQLite 数据库** | `leave_balances` 为权威余额，`employees` 为员工镜像 |
| **APScheduler 调度器** (`scheduler.py`) | 独立进程，取代 crontab，30 秒热加载 |
| **单页管理后台** (`templates/admin.html`) | 纯前端，密码登录，ES5 无框架 |
| **钉钉 API 集成** (`dingtalk_ops.py`) | 通讯录同步、余额推送、通知 |
| **钉盘备份** (`dingpan_backup.py`) | 小时级在线备份，保留最近 24 份 |
| **Windows 恢复工具** (`公休余额紧急恢复工具.pyw`) | GUI，dry-run + 三重确认，从钉盘/本地恢复余额 |

## 功能

- 员工/部门通讯录同步（递归，只增不减）
- 余额查询、扣减、退回、重置（全链路 ×100 单位）
- 幂等 `ref_id`（同一审批单不会重复扣减/退回）
- **月初额度发放**（按分类规则自动发放）
- **下月公休预占** `future_leave_reservations`（月底提下月假先占位，月初自动扣减）
- 特殊部门年度额度（国网/骑手/蔬果品类）
- 离职候选扫描（人工审核，不自动屏蔽）
- 新员工入职自动初始化余额
- 自管定时任务（后台 UI 增删改，无需 SSH）
- 每日 03:00 推钉钉镜像（唯一钉钉写入口，非实时双写）
- 钉盘 24 小时滚动备份（可见共享空间，钉钉客户端可下载）
- Windows 紧急恢复工具（三重确认 + dry-run）

## 数据权威说明

> `leave_balances` 是唯一权威。余额写入不调钉钉 API，钉钉每日 03:00 由 cron 单次镜像推送。
>
> 这避免了实时双写带来的 truncation / 覆盖冲突 —— 这是 DingTalk `quota/update` 绝对值 API 的已知限制。

所有余额单位为 **×100**：1 天 = 100，0.5 天 = 50。

## 安装

### 环境要求

- Python 3.10+
- SQLite3
- 钉钉开放平台应用（需假期管理、通讯录、钉盘权限）

### 快速启动

```bash
# 1. 克隆
git clone https://github.com/Moluuuu/leaveAdmin.git
cd leaveAdmin

# 2. 虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置
cp config.example.json config.json
cp constants.example.yaml constants.yaml
cp dingpan_backup_config.example.json dingpan_backup_config.json
cp .env.example .env

# 5. 编辑配置文件填入钉钉应用凭据，编辑 .env 设置管理后台密码

# 6. 启动服务
python3 -m uvicorn main:app --host 0.0.0.0 --port 18001

# 7. 独立启动调度器
python3 scheduler.py
```

## 配置

### config.json
```json
{
  "appKey": "钉钉应用 AppKey",
  "appSecret": "钉钉应用 AppSecret"
}
```

### constants.yaml
```yaml
dingtalk:
  token_url: "https://oapi.dingtalk.com/gettoken"
  # ... 其他 API 端点
leave_code: "钉钉假期类型 leave_code（如 2574891a-...）"
quota_rules:
  normal:    # 正常月(3-11月)
    hourly: 5     # 小时工
    six_day: 6    # 职能部门
    other: 4      # 门店/其他
    me: 8         # 管理员
```

### 环境变量 (.env)

```bash
LEAVEADMIN_AUTH_PASSWORD="your-password"         # 管理后台登录密码
DINGTALK_NOTIFY_USERID="dingtalk-user-id"        # 通知推送接收人
DINGTALK_ROBOT_CODE="dingtalk-app-key"           # 机器人应用 AppKey
```

## API

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/leave/check` | GET | 余额校验（支持 `leave_start_date` 跨月） |
| `/api/leave/deduct` | POST | 余额扣减（当前月直接扣，下月写预占） |
| `/api/leave/refund` | POST | 余额退回 |
| `/api/leave/reset` | POST | 月初重置（绝对值） |
| `/api/leave/all` | GET | 全员余额查询 |
| `/api/future-reservations` | GET | 下月预占情况查询 |
| `/api/apply-future-reservations` | POST | 月初应用预占扣减 |
| `/api/push-dingtalk` | POST | 全量推送余额到钉钉 |
| `/api/employees` | GET | 员工列表 |
| `/api/sync` | POST | 触发通讯录同步 |

API 需要 `X-Auth` header 鉴权（值等于 `LEAVEADMIN_AUTH_PASSWORD` 环境变量）。

## 测试

```bash
pytest tests/ -q
```

测试使用临时 SQLite 数据库，不连接生产环境。当前全量测试通过。

## 安全说明

- `admin.db` 不得提交到 Git（已在 .gitignore 排除）
- 配置文件（`config.json`、`constants.yaml`、`dingpan_backup_config.json`）已排除
- 管理后台密码通过环境变量设置，不硬编码
- 余额是伪货币数据，建议部署不可公网访问的内网环境

## 技术栈

Python / FastAPI / SQLite / APScheduler / httpx / Pydantic / ES5 单页前端 / DingTalk OpenAPI

## AI 协作声明

本项目由 **GPT (gpt-5.5)** 和 **DeepSeek (deepseek-v4-pro)** 协作开发。

- GPT：系统架构设计、余额权威翻转、未来月份预占、员工余额初始化、下月预占后台 UI、宜搭页面 JS 改造
- DeepSeek：脱敏发布包构建、README 编写、GitHub 仓库部署

---
*Made with ❤️ by AI and humans. 公休不公休的，代码说了算。*
