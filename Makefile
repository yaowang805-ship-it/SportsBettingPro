.PHONY: help run run-nba run-football dashboard backtest health test install clean deploy deploy-remove

help:
	@echo ""
	@echo "SportsBettingPro 命令菜单"
	@echo "────────────────────────────────────"
	@echo "  make run          每日全量预测（篮球+足球）"
	@echo "  make run-nba      仅篮球预测"
	@echo "  make run-football 仅足球预测"
	@echo "  make dashboard    启动 Streamlit 监控仪表盘"
	@echo "  make backtest     运行回测"
	@echo "  make health       系统健康检查"
	@echo "  make test         运行单元测试（85+ 项）"
	@echo "  make install      安装依赖"
	@echo "  make clean        清理临时文件"
	@echo "  make deploy       安装 crontab 定时任务（每天09:30）"
	@echo "  make deploy-remove 移除 crontab"
	@echo ""

run:
	python src/predict/run_all.py --sport all

run-nba:
	python src/predict/run_all.py --sport nba

run-football:
	python src/predict/run_all.py --sport football

dashboard:
	streamlit run src/dashboard/app.py --server.port 8501

backtest:
	python src/backtest/backtest_runner.py

health:
	python health_check.py

test:
	python3 -m pytest tests/ -v

install:
	pip install -r requirements.txt

deploy:
	bash deploy/install_cron.sh

deploy-remove:
	bash deploy/install_cron.sh --remove

clean:
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@echo "清理完成"
