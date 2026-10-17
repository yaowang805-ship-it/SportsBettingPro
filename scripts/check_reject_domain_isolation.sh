#!/bin/bash
# 拒绝域观察库隔离检查(2026-10-17 预注册配套, 提交前跑, 非人肉)
# 硬约束: 观察库物理隔离只写, 任何聚合脚本(dashboard/REAL_ROI_GATE/释放判据/日报)显式 exclude。
# 观察库数据绝不反馈进 edge 估计器。
set -e
cd "$(dirname "$0")/.."

echo "=== 1. reject_domain 的读者清单(应只有写模块 + 埋点调用) ==="
grep -rn "reject_domain" --include="*.py" src/ scripts/ | grep -vE "src/monitor/reject_domain.py" || echo "(无)"

echo ""
echo "=== 2. 聚合脚本是否误读 reject_domain(应为空=✅) ==="
if grep -rn "reject_domain\|reject_domain.jsonl" --include="*.py" \
    src/monitor/clv_collector.py src/monitor/performance.py src/report/ \
    scripts/compute_market_release.py scripts/observe_analysis.py src/dashboard/ 2>/dev/null; then
    echo "❌ 发现聚合脚本读 reject_domain — 违反硬约束, 禁止提交"
    exit 1
else
    echo "✅ 无聚合脚本读 reject_domain"
fi

echo ""
echo "=== 3. 观察库文件状态 ==="
if [ -f data/storage/reject_domain.jsonl ]; then
    echo "✅ reject_domain.jsonl 存在, $(wc -l < data/storage/reject_domain.jsonl) 行"
else
    echo "⚠️ 尚未创建(首次埋点触发后生成)"
fi
