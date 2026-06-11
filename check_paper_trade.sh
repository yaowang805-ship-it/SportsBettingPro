#!/usr/bin/env bash
# 模拟交易状态查看 — 随时运行，不消耗 API 配额
cd "$(dirname "$0")"
source venv/bin/activate 2>/dev/null
python3 -c "
from src.monitor.auto_settle import auto_settle
from src.monitor.performance import update_performance
from src.betting.paper_trader import PaperTrader

# 自动结算已结束的比赛
n = auto_settle()
if n:
    print(f'✅ 自动结算了 {n} 笔投注')
update_performance()
PaperTrader().print_report()
"
