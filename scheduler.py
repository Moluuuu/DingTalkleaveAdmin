"""
LeaveAdmin 定时任务调度器(独立进程)
读取数据库 scheduled_tasks 配置,按cron表达式触发,调本机API执行
systemd 管理: leaveadmin-scheduler.service
"""
import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# 确保print立即输出到日志
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

DB_PATH = Path(__file__).parent / "admin.db"
BASE_DIR = Path(__file__).parent
API_BASE = "http://localhost:18001"
AUTH = __import__("os").getenv("LEAVEADMIN_AUTH_PASSWORD", "change-me")


def get_tasks():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM scheduled_tasks WHERE enabled=1").fetchall()
    db.close()
    return [dict(r) for r in rows]


def call_api(path, method="POST", body=None):
    """同步调API(scheduler是asyncio但API调用用subprocess避免阻塞)"""
    try:
        cmd = ["curl", "-s", "-X", method, f"{API_BASE}{path}", "-H", f"X-Auth:{AUTH}"]
        if body is not None:
            cmd += ["-H", "Content-Type:application/json", "-d", json.dumps(body, ensure_ascii=False)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return result.stdout
    except Exception as e:
        print(f"[Scheduler] 调API失败 {path}: {e}")
        return None


def run_task(task_id, task_type, task_name, config):
    """任务执行函数"""
    print(f"[Scheduler] {datetime.now().strftime('%H:%M:%S')} 执行: {task_name}({task_type})")
    db = sqlite3.connect(str(DB_PATH))
    db.execute("UPDATE scheduled_tasks SET last_run=?, last_status='running' WHERE id=?",
        (datetime.now().isoformat(), task_id))
    db.commit()
    db.close()

    try:
        if task_type == "refresh_all":
            call_api("/api/refresh-balances", "POST")
        elif task_type == "cleanup_snapshots":
            call_api("/api/cleanup-snapshots", "POST")
        elif task_type == "monthly_assign":
            cfg = json.loads(config or "{}")
            month_type = cfg.get("month_type", "auto")
            if month_type == "auto":
                month = datetime.now().month
                month_type = "special" if month in (12, 1, 2) else "normal"
            call_api(f"/api/batch-monthly?month_type={month_type}", "POST")
        elif task_type == "apply_future_reservations":
            call_api("/api/apply-future-reservations", "POST")
        elif task_type == "dept_adjust":
            # 按部门/分类/人员批量调整,config直接透传给后端
            call_api("/api/dept-adjust", "POST", json.loads(config or "{}"))
        elif task_type == "push_dingtalk":
            call_api("/api/push-dingtalk", "POST")
        elif task_type == "annual_dept_reset":
            call_api("/api/annual-dept-reset", "POST", json.loads(config or "{}"))
        elif task_type == "dingpan_backup":
            result = subprocess.run(
                [str(BASE_DIR / "run_dingpan_backup_cron.py")],
                cwd=str(BASE_DIR), capture_output=True, text=True, timeout=900,
            )
            if result.stdout:
                print(result.stdout.strip())
            if result.stderr:
                print(result.stderr.strip())
            if result.returncode != 0:
                raise RuntimeError(f"钉盘备份失败 exit={result.returncode}")
        elif task_type == "sync_contacts":
            call_api("/api/sync", "POST")
        elif task_type == "departed_scan":
            call_api("/api/departed-candidates/scan", "POST", json.loads(config or "{}"))
        else:
            print(f"[Scheduler] 未知任务类型: {task_type}")

        db = sqlite3.connect(str(DB_PATH))
        db.execute("UPDATE scheduled_tasks SET last_status='success' WHERE id=?", (task_id,))
        db.commit()
        db.close()
        print(f"[Scheduler] {task_name} 完成")
    except Exception as e:
        print(f"[Scheduler] {task_name} 失败: {e}")
        db = sqlite3.connect(str(DB_PATH))
        db.execute("UPDATE scheduled_tasks SET last_status='failed' WHERE id=?", (task_id,))
        db.commit()
        db.close()


def parse_cron(expr):
    """标准5段cron表达式转CronTrigger参数"""
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"cron表达式必须是5段: {expr}")
    return CronTrigger(minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4])


# 已注册任务的指纹缓存(用于检测变化)
_job_fingerprints = {}


async def reload_jobs(scheduler):
    """重新加载任务:对比内存中已注册任务与数据库,新增/修改的重新注册,删除的移除"""
    try:
        tasks = get_tasks()
        db_ids = {str(t["id"]) for t in tasks}
        existing_jobs = {j.id for j in scheduler.get_jobs()}
        # 移除已删除/禁用的任务
        for jid in existing_jobs - db_ids:
            scheduler.remove_job(jid)
            _job_fingerprints.pop(jid, None)
            print(f"[Scheduler] 已移除任务 id={jid}")
        for t in tasks:
            jid = str(t["id"])
            fingerprint = f"{t['cron_expr']}|{t['config']}|{t['name']}"
            existing = scheduler.get_job(jid)
            need_add = False
            if existing is None:
                need_add = True
            elif _job_fingerprints.get(jid) != fingerprint:
                need_add = True
            if need_add:
                try:
                    trigger = parse_cron(t["cron_expr"])
                    scheduler.add_job(run_task, trigger,
                        args=[t["id"], t["task_type"], t["name"], t["config"]],
                        id=jid, name=t["name"], replace_existing=True)
                    _job_fingerprints[jid] = fingerprint
                    if existing is None:
                        print(f"[Scheduler] 新增任务: {t['name']} cron={t['cron_expr']}")
                    else:
                        print(f"[Scheduler] 更新任务: {t['name']} cron={t['cron_expr']}")
                except Exception as e:
                    print(f"[Scheduler] 注册失败 {t['name']}: {e}")
        return len(tasks)
    except Exception as e:
        print(f"[Scheduler] reload异常: {e}")
        return -1


async def main():
    scheduler = AsyncIOScheduler()
    count = await reload_jobs(scheduler)
    scheduler.start()
    print(f"[Scheduler] 启动完成,共{count}个任务")

    # 每30秒检查任务变化,自动重新加载(新增/修改/删除的任务无需重启即可生效)
    try:
        while True:
            await asyncio.sleep(30)
            await reload_jobs(scheduler)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
