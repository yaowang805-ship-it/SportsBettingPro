#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
source venv/bin/activate 2>/dev/null || true
echo "🚀 启动 SportsBettingPro 仪表盘..."
echo "   浏览器打开: http://localhost:8501"
exec streamlit run src/dashboard/app.py --server.port 8501
