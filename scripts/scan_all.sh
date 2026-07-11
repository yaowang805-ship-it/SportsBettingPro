#!/bin/bash
# 一键扫描：足球 + 篮球 + 棒球 + 网球 → 统一推送
cd "$(dirname "$0")/.." || exit 1

echo "=== 足球扫描 ==="
python3 -c "
import sys; sys.path.insert(0, '.')
from src.betting.line_shopping import run_line_shopping
opps = run_line_shopping()
print(f'足球: {len(opps)} 条')
" 2>&1

echo ""
echo "=== 篮球扫描 ==="
python3 -c "
import sys; sys.path.insert(0, '.')
from src.betting.bb_line_shopping import scan_and_notify
n = scan_and_notify()
print(f'篮球: {n} 条')
" 2>&1

echo ""
echo "=== 棒球扫描 ==="
python3 -c "
import sys; sys.path.insert(0, '.')
from src.betting.mlb_line_shopping import scan_and_notify
n = scan_and_notify()
print(f'棒球: {n} 条')
" 2>&1

echo ""
echo "=== 网球扫描 ==="
python3 -c "
import sys; sys.path.insert(0, '.')
from src.betting.tennis_line_shopping import scan_and_notify
n = scan_and_notify()
print(f'网球: {n} 条')
" 2>&1

echo ""
echo "=== 推送结果 ==="
python3 -c "
import sys; sys.path.insert(0, '.')
from src.betting.line_shopping import push_cached_recommendations
push_cached_recommendations()
" 2>&1
