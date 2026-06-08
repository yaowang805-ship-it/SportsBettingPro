FROM python:3.9-slim

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖清单
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 数据目录挂载点
VOLUME ["/app/data/storage", "/app/models", "/app/logs"]

# 默认入口
CMD ["python", "src/predict/run_all.py", "--sport", "all"]
