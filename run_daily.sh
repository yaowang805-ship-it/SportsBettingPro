#!/bin/bash
# SportsBettingPro 每日自动化运行脚本

set -e  # 遇到错误立即退出

echo "🏁 SportsBettingPro 每日自动化开始 - $(date)"

# 检查虚拟环境
if [ ! -d "venv" ]; then
    echo "❌ 找不到虚拟环境，请先运行: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

# 激活虚拟环境
source venv/bin/activate

# 设置Python路径
export PYTHONPATH="$PWD"

# 检查.env文件
if [ ! -f ".env" ]; then
    echo "⚠️ 未找到.env文件，正在从.env.example复制..."
    cp .env.example .env
    echo "请编辑.env文件并填入真实API密钥，然后重新运行"
    exit 1
fi

# 运行主程序（内部处理数据同步）
echo "🚀 启动主程序..."
python main.py

# 检查退出码
if [ $? -eq 0 ]; then
    echo "✅ 每日流程执行成功 - $(date)"
else
    echo "❌ 每日流程执行失败 - $(date)"
    # 可以在这里添加告警逻辑
    exit 1
fi
