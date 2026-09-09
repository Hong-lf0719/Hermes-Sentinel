# Hermes Sentinel — 容器化部署
# 用法：
#   docker build -t hermes .
#   docker run -d -p 9866:9866 --env-file .env -e PORT=9866 --name hermes hermes
# 云平台（Railway / Render / Koyeb 等）会通过 PORT 环境变量注入端口，无需手动 -p。
#
# 注意：HOST 必须为 0.0.0.0 —— 容器内绑 127.0.0.1 会导致 -p 端口映射永远连不上。
# 公网部署前请先完成 execute_command 沙箱化（见 未解决问题具体方案.md ④）。

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

# 端口由平台通过 PORT 环境变量注入；与 server.py 默认 9866 保持一致
ENV PORT=9866
ENV HOST=0.0.0.0
EXPOSE 9866

CMD ["python", "server.py"]
