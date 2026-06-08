#!/bin/bash
set -e
source venv/bin/activate
export PYTHONPATH="$PWD"

echo "=========================================="
echo "  SportsBettingPro 全自动部署 v2"
echo "=========================================="

echo ""
echo "🏁 自动重训所有模型（含 Optuna 调优 + Stacking + 校准）..."
python src/models/auto_retrain.py

echo ""
echo "🏆 Power Rating 战力评级..."
python -c "from src.core.power_rating import print_ratings_report; print_ratings_report()"

echo ""
echo "📊 运行回测评估..."
python src/backtest/backtest_runner.py

echo ""
echo "📈 运行全运动每日预测..."
python src/predict/run_all.py --sport all

echo ""
echo "🏆 全体育统一排名..."
python -c "from src.predict.rank_recommendations import rank_recommendations; rank_recommendations()"

echo ""
echo "📸 盘口基线快照..."
python -c "from src.monitor.line_movement import take_snapshot; take_snapshot()"

echo ""
echo "✅ 系统健康检查..."
python health_check.py

echo ""
echo "=========================================="
echo "  🎉 部署完成！"
echo "=========================================="
echo ""
echo "📋 附加命令:"
echo "  python src/predict/run_all.py --sport all     # 每日预测"
echo "  python src/monitor/clv_tracker.py              # CLV报告"
echo "  python src/predict/global_top5.py              # 全球Top5"
echo "  python -c 'from src.core.prediction_logger import print_performance; print_performance()'  # 绩效"
