#!/bin/bash
# 全量重抓 OddsPortal: 等 NHL 验证完成 → 备份旧CSV → 清空 → 跑修复后批处理
# 后台长任务, 可反复运行(已存在且 >100 字节的 CSV 会跳过, 断点续传)。
set -u
cd /Users/wangyao/SportsBettingPro || exit 1
LOG=data/logs/op_rescrape.log

echo "=== 全量重抓开始 $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "$LOG"

# 1) 等 NHL 验证进程结束(最多 25 分钟)
for i in $(seq 1 250); do
  if ! ps -p 58003 >/dev/null 2>&1; then break; fi
  sleep 6
done

# 2) 备份旧 CSV(截断数据先保存, 不直接删)
TS=$(date +%Y%m%d_%H%M%S)
if [ -d data/oddsportal ]; then
  mv data/oddsportal "data/oddsportal_backup_$TS"
  echo "已备份 → data/oddsportal_backup_$TS" | tee -a "$LOG"
fi
mkdir -p data/oddsportal

# 3) 全量批处理(修复后 scraper, 逐联赛点 Next 翻页)
.venv312/bin/python data/pinnacle_historical/op_batch_run.py >> "$LOG" 2>&1

echo "=== 全量重抓结束 $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "$LOG"
