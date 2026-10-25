#!/bin/bash
# 公休余额管理后台 - 数据库每日备份
# 保留最近30天备份

# 默认沿用现有生产数据目录；代码所在发布目录不决定数据位置。
DATA_DIR="${LEAVEADMIN_HOME:-/opt/yida/leaveAdmin}"
BACKUP_DIR="$DATA_DIR/backups"
DB_FILE="${LEAVEADMIN_DB_PATH:-$DATA_DIR/admin.db}"
KEEP_DAYS=30
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR" || exit 1

if [ ! -f "$DB_FILE" ]; then
  echo "[$DATE] DB不存在，备份失败" >> "$BACKUP_DIR/backup.log"
  exit 1
fi

# SQLite在线备份(不锁库)
sqlite3 "$DB_FILE" ".backup '$BACKUP_DIR/admin.db.$DATE'" 2>>"$BACKUP_DIR/backup.log"

if [ $? -eq 0 ]; then
  gzip -f "$BACKUP_DIR/admin.db.$DATE" || exit 1
  echo "[$DATE] 备份成功: admin.db.$DATE.gz ($(du -sh $BACKUP_DIR/admin.db.$DATE.gz | cut -f1))" >> "$BACKUP_DIR/backup.log"
else
  echo "[$DATE] 备份失败" >> "$BACKUP_DIR/backup.log"
  exit 1
fi

# 清理过期备份
if ! find "$BACKUP_DIR" -name "admin.db.*.gz" -mtime +$KEEP_DAYS -delete 2>>"$BACKUP_DIR/backup.log"; then
  echo "[$DATE] 清理过期备份失败" >> "$BACKUP_DIR/backup.log"
  exit 1
fi
echo "[$DATE] 清理30天前备份完成" >> "$BACKUP_DIR/backup.log"
