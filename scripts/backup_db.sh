#!/bin/bash
# 公休余额管理后台 - 数据库每日备份
# 保留最近30天备份

BACKUP_DIR="/opt/leaveadmin/backups"
DB_FILE="/opt/leaveadmin/admin.db"
KEEP_DAYS=30
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB_FILE" ]; then
  echo "[$DATE] DB不存在，跳过" >> "$BACKUP_DIR/backup.log"
  exit 0
fi

# SQLite在线备份(不锁库)
sqlite3 "$DB_FILE" ".backup '$BACKUP_DIR/admin.db.$DATE'" 2>>"$BACKUP_DIR/backup.log"

if [ $? -eq 0 ]; then
  gzip -f "$BACKUP_DIR/admin.db.$DATE"
  echo "[$DATE] 备份成功: admin.db.$DATE.gz ($(du -sh $BACKUP_DIR/admin.db.$DATE.gz | cut -f1))" >> "$BACKUP_DIR/backup.log"
else
  echo "[$DATE] 备份失败" >> "$BACKUP_DIR/backup.log"
fi

# 清理过期备份
find "$BACKUP_DIR" -name "admin.db.*.gz" -mtime +$KEEP_DAYS -delete 2>/dev/null
echo "[$DATE] 清理30天前备份完成" >> "$BACKUP_DIR/backup.log"
