"""公休余额紧急恢复 GUI。

放在 Windows 桌面使用。默认只做 dry-run 预览；真实覆盖钉钉余额必须选择
config.json、勾选确认，并输入“覆盖钉钉”。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import queue
import threading
import traceback
import webbrowser
from pathlib import Path
import sys
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, BooleanVar, StringVar, Tk, filedialog, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from leaveadmin import dingpan_backup
from leaveadmin import windows_restore_leave_balance as restore_core

APP_TITLE = "公休余额紧急恢复工具"
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
REPORT_DIR = BASE_DIR / "reports"
WORK_DIR = BASE_DIR / "restore_work"
DOWNLOAD_DIR = BASE_DIR / "downloads"
SETTINGS_FILE = BASE_DIR / "restore_gui_settings.json"
MAX_PREVIEW_ROWS = 300


class DryRunClient(restore_core.QuotaClient):
    def quota_update(self, payload):
        return {"errcode": 0, "errmsg": "dry-run"}


class RestoreGui:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1120x760")
        self.root.minsize(980, 640)
        self.queue: queue.Queue = queue.Queue()
        self.last_report = None
        self.last_payloads = []
        self.running = False

        self.source_type = StringVar(value="backup")
        self.backup_path = StringVar()
        self.db_path = StringVar()
        self.config_path = StringVar()
        self.dingpan_config_path = StringVar(value=str(BASE_DIR / "dingpan_backup_config.json"))
        self.period = StringVar()
        self.leave_code = StringVar(value=restore_core.DEFAULT_LEAVE_CODE)
        self.op_userid = StringVar(value=restore_core.DEFAULT_OP_USERID)
        self.confirm_check = BooleanVar(value=False)
        self.confirm_text = StringVar()
        self.status_text = StringVar(value="待命。先选择备份包或 admin.db，然后点 Dry-run 预览。")
        self.progress_text = StringVar(value="0/0")

        self._load_settings()
        self._build_ui()
        self.root.after(120, self._poll_queue)

    def _load_settings(self):
        try:
            if SETTINGS_FILE.exists():
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                self.backup_path.set(data.get("backup_path", ""))
                self.db_path.set(data.get("db_path", ""))
                self.config_path.set(data.get("config_path", ""))
                self.dingpan_config_path.set(data.get("dingpan_config_path", str(BASE_DIR / "dingpan_backup_config.json")))
                self.op_userid.set(data.get("op_userid", restore_core.DEFAULT_OP_USERID))
                self.leave_code.set(data.get("leave_code", restore_core.DEFAULT_LEAVE_CODE))
        except Exception:
            pass

    def _save_settings(self):
        data = {
            "backup_path": self.backup_path.get(),
            "db_path": self.db_path.get(),
            "config_path": self.config_path.get(),
            "dingpan_config_path": self.dingpan_config_path.get(),
            "op_userid": self.op_userid.get(),
            "leave_code": self.leave_code.get(),
        }
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Danger.TButton", foreground="#b91c1c")
        style.configure("Primary.TButton", foreground="#1d4ed8")

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=X)
        ttk.Label(header, text=APP_TITLE, font=("Microsoft YaHei UI", 18, "bold")).pack(side=LEFT)
        ttk.Label(header, text="默认 dry-run，不会改钉钉。真实执行需要三重确认。", foreground="#64748b").pack(side=LEFT, padx=18)

        main = ttk.PanedWindow(outer, orient="horizontal")
        main.pack(fill=BOTH, expand=True, pady=(12, 8))

        left = ttk.Frame(main, padding=10)
        right = ttk.Frame(main, padding=10)
        main.add(left, weight=1)
        main.add(right, weight=2)

        self._build_controls(left)
        self._build_preview(right)
        self._build_log(outer)

        bottom = ttk.Frame(outer)
        bottom.pack(fill=X)
        ttk.Label(bottom, textvariable=self.status_text).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(bottom, text="打开日志目录", command=lambda: self._open_path(LOG_DIR)).pack(side=RIGHT, padx=(8, 0))
        ttk.Button(bottom, text="打开工具目录", command=lambda: self._open_path(BASE_DIR)).pack(side=RIGHT)

    def _build_controls(self, parent):
        box = ttk.LabelFrame(parent, text="1. 数据来源", padding=10)
        box.pack(fill=X)
        ttk.Radiobutton(box, text="从钉盘自动下载最新备份后恢复", variable=self.source_type, value="dingpan").pack(anchor="w")
        ttk.Label(box, text="会读取下方 config.json + dingpan_backup_config.json，下载最新 leaveAdmin-backup-*.tar.gz 到 downloads 目录。", foreground="#64748b", wraplength=360).pack(anchor="w", pady=(0, 8))

        ttk.Radiobutton(box, text="从钉盘下载的 .tar.gz 备份包恢复", variable=self.source_type, value="backup").pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(fill=X, pady=4)
        ttk.Entry(row, textvariable=self.backup_path).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(row, text="选择备份包", command=self._choose_backup).pack(side=RIGHT, padx=(6, 0))

        ttk.Radiobutton(box, text="从 admin.db 直接恢复", variable=self.source_type, value="db").pack(anchor="w", pady=(8, 0))
        row = ttk.Frame(box)
        row.pack(fill=X, pady=4)
        ttk.Entry(row, textvariable=self.db_path).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(row, text="选择 DB", command=self._choose_db).pack(side=RIGHT, padx=(6, 0))

        cfg = ttk.LabelFrame(parent, text="2. 恢复参数", padding=10)
        cfg.pack(fill=X, pady=(10, 0))
        self._entry_row(cfg, "月份 period", self.period, "留空自动识别，如 2026-06")
        self._entry_row(cfg, "假期 leaveCode", self.leave_code)
        self._entry_row(cfg, "操作人 userid", self.op_userid)
        row = ttk.Frame(cfg)
        row.pack(fill=X, pady=4)
        ttk.Label(row, text="钉钉 config", width=13).pack(side=LEFT)
        ttk.Entry(row, textvariable=self.config_path).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(row, text="选择", command=self._choose_config).pack(side=RIGHT, padx=(6, 0))
        ttk.Label(cfg, text="真实执行才需要 config.json；如果选择“从钉盘自动下载”，Dry-run 也需要它用于下载。", foreground="#64748b", wraplength=360).pack(anchor="w", pady=(2, 0))
        row = ttk.Frame(cfg)
        row.pack(fill=X, pady=4)
        ttk.Label(row, text="钉盘配置", width=13).pack(side=LEFT)
        ttk.Entry(row, textvariable=self.dingpan_config_path).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(row, text="选择", command=self._choose_dingpan_config).pack(side=RIGHT, padx=(6, 0))
        ttk.Label(cfg, text="默认读取工具目录下的 dingpan_backup_config.json。", foreground="#64748b").pack(anchor="w", pady=(2, 0))
        ttk.Button(cfg, text="测试/下载最新钉盘备份", command=self._download_latest_backup_only).pack(fill=X, pady=(8, 0))
        ttk.Label(cfg, text="只下载并校验备份包，不会执行恢复，也不会写钉钉。下载成功后会自动填入备份包路径。", foreground="#64748b", wraplength=360).pack(anchor="w", pady=(2, 0))

        safety = ttk.LabelFrame(parent, text="3. 安全确认", padding=10)
        safety.pack(fill=X, pady=(10, 0))
        ttk.Checkbutton(safety, text="我确认这是紧急恢复，会覆盖钉钉公休余额", variable=self.confirm_check).pack(anchor="w")
        row = ttk.Frame(safety)
        row.pack(fill=X, pady=4)
        ttk.Label(row, text="输入确认词", width=13).pack(side=LEFT)
        ttk.Entry(row, textvariable=self.confirm_text).pack(side=LEFT, fill=X, expand=True)
        ttk.Label(safety, text="真实执行前必须输入：覆盖钉钉", foreground="#b91c1c").pack(anchor="w")

        actions = ttk.Frame(parent)
        actions.pack(fill=X, pady=(14, 0))
        ttk.Button(actions, text="Dry-run 预览", style="Primary.TButton", command=lambda: self._run(False)).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(actions, text="真实覆盖钉钉", style="Danger.TButton", command=lambda: self._run(True)).pack(side=LEFT, fill=X, expand=True, padx=(8, 0))

        ttk.Button(parent, text="导出上次预览 JSON", command=self._export_preview).pack(fill=X, pady=(10, 0))
        ttk.Button(parent, text="清空日志窗口", command=lambda: self.log_text.delete("1.0", END)).pack(fill=X, pady=(6, 0))

        prog_box = ttk.LabelFrame(parent, text="执行进度", padding=10)
        prog_box.pack(fill=X, pady=(10, 0))
        self.progress = ttk.Progressbar(prog_box, mode="determinate")
        self.progress.pack(fill=X)
        ttk.Label(prog_box, textvariable=self.progress_text).pack(anchor="e", pady=(4, 0))

    def _entry_row(self, parent, label, var, placeholder=""):
        row = ttk.Frame(parent)
        row.pack(fill=X, pady=4)
        ttk.Label(row, text=label, width=13).pack(side=LEFT)
        entry = ttk.Entry(row, textvariable=var)
        entry.pack(side=LEFT, fill=X, expand=True)
        if placeholder:
            ttk.Label(row, text=placeholder, foreground="#94a3b8").pack(side=RIGHT, padx=(8, 0))

    def _build_preview(self, parent):
        summary = ttk.LabelFrame(parent, text="摘要", padding=10)
        summary.pack(fill=X)
        self.summary_text = ScrolledText(summary, height=7, wrap="word")
        self.summary_text.pack(fill=X)

        table_box = ttk.LabelFrame(parent, text="预览明细，最多显示前 300 人", padding=10)
        table_box.pack(fill=BOTH, expand=True, pady=(10, 0))
        columns = ("name", "userid", "balance_x100", "days", "period")
        self.tree = ttk.Treeview(table_box, columns=columns, show="headings", height=14)
        headers = {
            "name": "姓名",
            "userid": "userid",
            "balance_x100": "余额×100",
            "days": "天数",
            "period": "月份",
        }
        widths = {"name": 120, "userid": 190, "balance_x100": 90, "days": 80, "period": 90}
        for col in columns:
            self.tree.heading(col, text=headers[col])
            self.tree.column(col, width=widths[col], anchor="center")
        ybar = ttk.Scrollbar(table_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ybar.set)
        self.tree.pack(side=LEFT, fill=BOTH, expand=True)
        ybar.pack(side=RIGHT, fill=Y)

    def _build_log(self, parent):
        log_box = ttk.LabelFrame(parent, text="执行日志", padding=8)
        log_box.pack(fill=BOTH, expand=False)
        self.log_text = ScrolledText(log_box, height=10, wrap="word")
        self.log_text.pack(fill=BOTH, expand=True)

    def _choose_backup(self):
        path = filedialog.askopenfilename(title="选择备份包", filetypes=[("Backup package", "*.tar.gz"), ("All files", "*.*")])
        if path:
            self.backup_path.set(path)
            self.source_type.set("backup")
            self._save_settings()

    def _choose_db(self):
        path = filedialog.askopenfilename(title="选择 admin.db", filetypes=[("SQLite DB", "*.db"), ("All files", "*.*")])
        if path:
            self.db_path.set(path)
            self.source_type.set("db")
            self._save_settings()

    def _choose_config(self):
        path = filedialog.askopenfilename(title="选择 config.json", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if path:
            self.config_path.set(path)
            self._save_settings()

    def _choose_dingpan_config(self):
        path = filedialog.askopenfilename(title="选择 dingpan_backup_config.json", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if path:
            self.dingpan_config_path.set(path)
            self._save_settings()

    def _validate_dingpan_download_inputs(self):
        if self.running:
            raise RuntimeError("当前已有任务在执行")
        if not self.config_path.get() or not Path(self.config_path.get()).exists():
            raise RuntimeError("测试钉盘下载必须选择包含 appKey/appSecret 的 config.json")
        if not self.dingpan_config_path.get() or not Path(self.dingpan_config_path.get()).exists():
            raise RuntimeError("测试钉盘下载必须选择 dingpan_backup_config.json")

    def _download_latest_backup_only(self):
        try:
            self._validate_dingpan_download_inputs()
        except Exception as exc:
            messagebox.showerror("不能下载", str(exc))
            return
        self._save_settings()
        self.running = True
        self.progress.configure(value=0, maximum=100)
        self.progress_text.set("0/0")
        self.status_text.set("正在从钉盘下载最新备份...")
        self._log("=" * 80)
        self._log("开始测试/下载最新钉盘备份。该操作不会恢复，也不会写钉钉。")
        threading.Thread(target=self._download_latest_backup_worker, daemon=True).start()

    def _download_latest_backup_worker(self):
        try:
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            dest_dir = DOWNLOAD_DIR / stamp
            self.queue.put(("log", "正在读取钉盘配置并获取空间..."))
            dp_client, dp_cfg = dingpan_backup.build_client_from_configs(self.config_path.get(), self.dingpan_config_path.get())
            space_id = dp_client.ensure_space(dp_cfg["space_name"], union_id=dp_cfg["union_id"])
            self.queue.put(("log", "正在查找并下载最新 leaveAdmin 备份..."))
            downloaded = dp_client.download_latest_backup(space_id, dest_dir, name_prefix="leaveAdmin-backup-")
            self.queue.put(("log", f"已下载: {downloaded['name']} -> {downloaded['path']}"))
            verification = dingpan_backup.verify_backup_package(downloaded["path"])
            if not verification.get("ok"):
                raise RuntimeError(verification.get("error") or "备份包校验失败")
            manifest = verification.get("manifest") or {}
            self.queue.put(("download_done", str(downloaded["path"]), downloaded.get("name"), downloaded.get("file_id"), manifest))
        except Exception as exc:
            tb = traceback.format_exc()
            self.queue.put(("error", str(exc), tb))

    def _validate(self, execute: bool):
        if self.running:
            raise RuntimeError("当前已有任务在执行")
        source = self.source_type.get()
        if source == "dingpan":
            if not self.config_path.get() or not Path(self.config_path.get()).exists():
                raise RuntimeError("从钉盘自动下载必须选择包含 appKey/appSecret 的 config.json")
            if not self.dingpan_config_path.get() or not Path(self.dingpan_config_path.get()).exists():
                raise RuntimeError("从钉盘自动下载必须选择 dingpan_backup_config.json")
        elif source == "backup":
            if not self.backup_path.get() or not Path(self.backup_path.get()).exists():
                raise RuntimeError("请选择有效的 .tar.gz 备份包")
        else:
            if not self.db_path.get() or not Path(self.db_path.get()).exists():
                raise RuntimeError("请选择有效的 admin.db")
        if execute:
            if not self.config_path.get() or not Path(self.config_path.get()).exists():
                raise RuntimeError("真实执行必须选择包含 appKey/appSecret 的 config.json")
            if not self.confirm_check.get() or self.confirm_text.get().strip() != "覆盖钉钉":
                raise RuntimeError("真实执行需要勾选确认，并输入：覆盖钉钉")
            if not messagebox.askyesno("最后确认", "这会把备份里的公休余额覆盖写回钉钉。确定继续？"):
                raise RuntimeError("用户取消真实执行")

    def _run(self, execute: bool):
        try:
            self._validate(execute)
        except Exception as exc:
            messagebox.showerror("不能执行", str(exc))
            return
        self._save_settings()
        self.running = True
        self.progress.configure(value=0, maximum=100)
        self.progress_text.set("0/0")
        self.status_text.set("执行中..." if execute else "Dry-run 预览中...")
        self._log("=" * 80)
        self._log(f"开始 {'真实覆盖钉钉' if execute else 'Dry-run 预览'}")
        worker = threading.Thread(target=self._worker, args=(execute,), daemon=True)
        worker.start()

    def _worker(self, execute: bool):
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            WORK_DIR.mkdir(parents=True, exist_ok=True)
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            log_path = LOG_DIR / f"restore-{stamp}.log"
            report_path = REPORT_DIR / f"restore-report-{stamp}.json"

            def emit(msg):
                self.queue.put(("log", msg))
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(msg + "\n")

            def progress(index, total, payload, result):
                name = payload.get("name") or payload.get("userid")
                ok = result.get("errcode", 0) in (0, "0")
                emit(f"[{index}/{total}] {'OK' if ok else '失败'} {name} {payload.get('quota_num_per_day')}×100 -> {result}")
                self.queue.put(("progress", index, total))

            client = restore_core.DingTalkQuotaClient.from_config(self.config_path.get()) if execute else DryRunClient()
            source = self.source_type.get()
            if source == "dingpan":
                emit("正在从钉盘查找最新备份...")
                dp_client, dp_cfg = dingpan_backup.build_client_from_configs(self.config_path.get(), self.dingpan_config_path.get())
                space_id = dp_client.ensure_space(dp_cfg["space_name"], union_id=dp_cfg["union_id"])
                downloaded = dp_client.download_latest_backup(space_id, DOWNLOAD_DIR / stamp)
                backup_path = downloaded["path"]
                emit(f"已下载钉盘最新备份: {downloaded['name']} -> {backup_path}")
                report = restore_core.restore_from_backup_file(
                    backup_path,
                    WORK_DIR / stamp,
                    client,
                    leave_code=self.leave_code.get().strip() or restore_core.DEFAULT_LEAVE_CODE,
                    op_userid=self.op_userid.get().strip() or restore_core.DEFAULT_OP_USERID,
                    execute=execute,
                    confirm_overwrite=execute,
                    period=self.period.get().strip() or None,
                    progress=progress,
                    request_interval=0.3 if execute else 0.0,
                )
                report["dingpan_download"] = {
                    "name": downloaded["name"],
                    "file_id": downloaded.get("file_id"),
                    "path": str(backup_path),
                }
            elif source == "backup":
                emit(f"备份包: {self.backup_path.get()}")
                report = restore_core.restore_from_backup_file(
                    self.backup_path.get(),
                    WORK_DIR / stamp,
                    client,
                    leave_code=self.leave_code.get().strip() or restore_core.DEFAULT_LEAVE_CODE,
                    op_userid=self.op_userid.get().strip() or restore_core.DEFAULT_OP_USERID,
                    execute=execute,
                    confirm_overwrite=execute,
                    period=self.period.get().strip() or None,
                    progress=progress,
                    request_interval=0.3 if execute else 0.0,
                )
            else:
                emit(f"数据库: {self.db_path.get()}")
                report = restore_core.restore(
                    self.db_path.get(),
                    client,
                    leave_code=self.leave_code.get().strip() or restore_core.DEFAULT_LEAVE_CODE,
                    op_userid=self.op_userid.get().strip() or restore_core.DEFAULT_OP_USERID,
                    execute=execute,
                    confirm_overwrite=execute,
                    period=self.period.get().strip() or None,
                    progress=progress,
                    request_interval=0.3 if execute else 0.0,
                )
            report["log_path"] = str(log_path)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            emit(f"报告已保存: {report_path}")
            self.queue.put(("done", report, str(report_path)))
        except Exception as exc:
            tb = traceback.format_exc()
            self.queue.put(("error", str(exc), tb))

    def _poll_queue(self):
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._log(item[1])
                elif kind == "progress":
                    index, total = item[1], item[2]
                    self.progress.configure(maximum=max(total, 1), value=index)
                    self.progress_text.set(f"{index}/{total}")
                elif kind == "done":
                    self.running = False
                    self.last_report = item[1]
                    self._render_report(item[1])
                    self.status_text.set(f"完成。报告: {item[2]}")
                    self._log("完成。")
                elif kind == "download_done":
                    self.running = False
                    path, name, file_id, manifest = item[1], item[2], item[3], item[4]
                    self.backup_path.set(path)
                    self.source_type.set("backup")
                    self._save_settings()
                    self.status_text.set("钉盘最新备份已下载并校验通过，可直接 Dry-run 预览。")
                    self._log("钉盘下载校验通过。")
                    self._log(f"备份文件: {name}")
                    self._log(f"file_id: {file_id}")
                    self._log(f"backup_time: {manifest.get('backup_time')}")
                    self._log(f"employees_active: {manifest.get('employees_active')} / leave_balance_rows: {manifest.get('leave_balance_rows')}")
                    self.summary_text.delete("1.0", END)
                    self.summary_text.insert(END, "\n".join([
                        "钉盘最新备份已下载并校验通过。",
                        f"文件: {name}",
                        f"本地路径: {path}",
                        f"file_id: {file_id}",
                        "",
                        "备份 manifest:",
                        f"  backup_time: {manifest.get('backup_time')}",
                        f"  period: {manifest.get('period')}",
                        f"  employees_active: {manifest.get('employees_active')}",
                        f"  leave_balance_rows: {manifest.get('leave_balance_rows')}",
                        f"  latest_transaction_id: {manifest.get('latest_transaction_id')}",
                        f"  db_sha256: {manifest.get('db_sha256')}",
                    ]))
                elif kind == "error":
                    self.running = False
                    self.status_text.set("失败。查看日志窗口。")
                    self._log("失败: " + item[1])
                    self._log(item[2])
                    messagebox.showerror("执行失败", item[1])
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    def _render_report(self, report):
        self.summary_text.delete("1.0", END)
        manifest = report.get("manifest") or {}
        lines = [
            f"模式: {'Dry-run 预览' if report.get('dry_run') else '真实执行'}",
            f"月份: {report.get('period')}",
            f"目标人数: {report.get('target_count')}",
            f"成功/失败: {report.get('success', 0)} / {report.get('failed', 0)}",
        ]
        if report.get("dingpan_download"):
            dl = report["dingpan_download"]
            lines += [
                "",
                "钉盘下载:",
                f"  file: {dl.get('name')}",
                f"  file_id: {dl.get('file_id')}",
                f"  local_path: {dl.get('path')}",
            ]
        if manifest:
            lines += [
                "",
                "备份 manifest:",
                f"  backup_time: {manifest.get('backup_time')}",
                f"  employees_active: {manifest.get('employees_active')}",
                f"  leave_balance_rows: {manifest.get('leave_balance_rows')}",
                f"  latest_transaction_id: {manifest.get('latest_transaction_id')}",
                f"  db_sha256: {manifest.get('db_sha256')}",
            ]
        if report.get("errors"):
            lines += ["", "错误前 10 条:"] + [json.dumps(e, ensure_ascii=False) for e in report["errors"][:10]]
        self.summary_text.insert(END, "\n".join(lines))

        self.last_payloads = report.get("preview") or []
        for row in self.tree.get_children():
            self.tree.delete(row)
        for payload in self.last_payloads[:MAX_PREVIEW_ROWS]:
            x100 = int(payload.get("quota_num_per_day") or 0)
            self.tree.insert("", END, values=(
                payload.get("name"),
                payload.get("userid"),
                x100,
                f"{x100 / 100:.2f}",
                payload.get("period"),
            ))

    def _export_preview(self):
        if not self.last_report:
            messagebox.showinfo("没有可导出内容", "先执行一次 Dry-run。")
            return
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(
            title="保存预览 JSON",
            initialdir=str(REPORT_DIR),
            initialfile="restore-preview.json",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if path:
            Path(path).write_text(json.dumps(self.last_report, ensure_ascii=False, indent=2), encoding="utf-8")
            self._log(f"已导出: {path}")

    def _log(self, msg):
        now = dt.datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(END, f"[{now}] {msg}\n")
        self.log_text.see(END)

    def _open_path(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(path))
        else:
            webbrowser.open(path.as_uri())


def main():
    root = Tk()
    RestoreGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
