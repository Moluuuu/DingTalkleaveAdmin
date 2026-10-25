# 当前生产环境的发布适配

本目录是待验证的部署配置，不代表已部署。仅适用于当前 `/opt/yida/leaveAdmin` 实例；不改余额规则、数据库结构或任务配置，不处理额度恢复工具。

## 目录与配置

- 代码：每个固定Git提交放在独立发布目录，`/opt/leaveadmin/current` 指向选定目录；该目录内创建独立 `.venv`。保留旧生产目录和旧服务配置以便回退。
- 数据：继续使用 `/opt/yida/leaveAdmin/admin.db` 和同目录的现有配置、日志、备份，不复制空数据库或示例配置覆盖生产。
- `LEAVEADMIN_HOME` 只定位运行数据；API及调度器从自身代码所在目录启动 `scripts/run_dingpan_backup_cron.py`。备份runner的 `--db`、`--app-config` 默认遵循对应环境变量。
- 安全填写 `leaveadmin.env.example` 为 `/etc/leaveadmin/leaveadmin.env`，权限0600；密码、通知接收人和机器人编码应与原生产值逐项核对，不能留空。实际凭据不入库。
- 两个service模板共用此环境文件；保留现有端口18001、2个worker、120秒超时、日志路径及root运行用户，不在本次顺带更改权限模型。
- API缺少真实constants或有效密码会拒绝启动。service启动前检查既有数据库和配置文件，避免误建空库；文件存在不等于内容已验证。

## 依赖

独立虚拟环境只使用 `requirements-production.txt`，不与开发用 `requirements.txt` 混装。清单来自2026-10-25生产包清单，不含pip与requests，保留urllib客户端路径。先核对生产Python版本、Linux平台及软件包源可获取性；本次未安装或验证依赖解析。不要直接在旧生产虚拟环境安装/升级。

## 切换前后（需另获运行与部署授权）

1. 隔离验证使用数据库副本和独立端口，不启动scheduler，不调用真实扣返、通知或外部写入。
   注意：现有API启动会执行 `init_db()`（含WAL设置及按需补列）和过期余额快照清理；这些逻辑本次未改，启动不等于只读。首次验证必须用副本，生产首次启动前必须完成一致性备份。
2. 对照原配置核对全部10项定时任务的时间和启停状态，不重新建任务或启用已停用任务；同时确认主机时区保持原值。
3. 维护窗口先停止旧scheduler、暂停业务写入并制作一致性备份；停止旧API后切换同名service配置和代码目录。不能并行启动新旧scheduler，也不能让隔离服务连生产库。
4. 先验收API、登录、查询和日志，再恢复scheduler。任何运行验收单独授权，不把真实扣返作为默认探测。
   保留原服务的启动顺序（After）、Restart=always及5秒重启间隔，不新增Requires强依赖；scheduler仍追加到原scheduler.log。切换时显式先停scheduler并确认在途任务，API验收后再启动scheduler。
5. 原root crontab的03:00备份应替换而非新增第二项：

   ```cron
   0 3 * * * LEAVEADMIN_HOME=/opt/yida/leaveAdmin LEAVEADMIN_DB_PATH=/opt/yida/leaveAdmin/admin.db /bin/bash /opt/leaveadmin/current/scripts/backup_db.sh
   ```

   shell备份不读取systemd环境文件；因此路径在此显式给出。数据库缺失或备份失败返回非零，保留原30天保留期。
6. 回退先停新scheduler/API，恢复原service及cron入口后启动旧服务。代码回退不自动回滚数据库；已有真实写入时禁止直接覆盖旧数据库备份。

尚未验收：真实配置值迁移、依赖安装、systemd运行、隔离业务验证、备份产物、生产切换。上线前仍需核对当时的线上版本与在途任务。
