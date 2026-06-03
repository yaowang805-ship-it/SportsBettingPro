#!/bin/bash
set -e
source venv/bin/activate
export PYTHONPATH="$PWD"

echo "=========================================="
echo "  SportsBettingPro 全自动部署"
echo "=========================================="

echo ""
echo "🏁 训练集成模型（4模型Stacking + Optuna + 校准）..."
python src/models/ensemble_trainer.py bb
python src/models/ensemble_trainer.py fb

echo ""
echo "📊 运行回测评估..."
python src/backtest/backtest_runner.py

echo ""
echo "📈 生成每日预测 + 全球Top5 + CLV追踪..."
python main.py

echo ""
echo "🏆 Power Rating 战力评级..."
python -c "from src.core.power_rating import print_ratings_report; print_ratings_report()"

echo ""
echo "📸 盘口基线快照..."
python -c "from src.monitor.line_movement import take_snapshot; take_snapshot()"

echo ""
echo "=========================================="
echo "  🎉 部署完成！"
echo "=========================================="
echo ""
echo "📋 后续命令:"
echo "  bash run_daily.sh     # 每日运行"
echo "  python src/monitor/clv_tracker.py     # 查看CLV报告"
echo "  python -c 'from src.core.prediction_logger import print_performance; print_performance()'  # 查看绩效"
