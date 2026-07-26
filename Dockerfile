# Hermes Suite — 容器化部署
# 用法：
#   docker build -t hermes .
#   docker run -d -p 9865:9865 --env-file .env -e PORT=9865 --name hermes hermes
# 云平台（Railway / Render / Koyeb 等）会通过 PORT 环境变量注入端口，无需手动 -p。

FROM python:3.11-slim

WORKDIR /app

# 编译依赖（numpy/jieba 等预编译 wheel 通常不需，保留 gcc 以防万一）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码（含 hermes_rag / hermes_caspian 本地包与 report_html.py）
COPY . .

# 端口由平台通过 PORT 环境变量注入；本地默认 9865
ENV PORT=9865
EXPOSE 9865

CMD ["python", "server.py"]
