#!/bin/bash
# 每日备份 sportsbetting.db — 保留最近 7 份。
# 用 sqlite3 .backup 做一致性备份(避免 WAL 模式下直接 cp 导致损坏)。
set -u
DB="/Users/wangyao/SportsBettingPro/data/storage/sportsbetting.db"
BACKUP_DIR="/Users/wangyao/SportsBettingPro/data/storage/backups"
TODAY=$(date +%Y%m%d)

mkdir -p "$BACKUP_DIR"
if [ ! -f "$DB" ]; then
  echo "[backup] DB 不存在, 跳过"
  exit 0
fi

# 一致性备份
sqlite3 "$DB" ".backup '$BACKUP_DIR/sportsbetting_$TODAY.db'"
echo "[backup] ✅ 已备份 → sportsbetting_$TODAY.db"

# 只保留最近 7 份
ls -1t "$BACKUP_DIR"/sportsbetting_*.db 2>/dev/null | tail -n +8 | while read -r f; do
  rm -f "$f"
  echo "[backup] 清理旧备份: $(basename "$f")"
done
