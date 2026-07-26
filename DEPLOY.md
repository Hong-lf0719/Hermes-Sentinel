# Hermes Sentinel 部署指南

Hermes 是一个**常驻进程**服务：FastAPI 提供 Web UI，Caspian-SDK 长轮询邮箱实现「喊一声就推日报」。
因此部署目标是 **24 小时在线的容器/主机**，不要选会自动休眠的免费层（如 Render 免费层）。

## 一、部署前必读

### 1. 端口
`server.py` 的 `PORT` 优先读环境变量 `PORT`，本地默认 `9865`。
- 本地 / Docker 手动跑：保持 `9865` 即可。
- 云平台（Railway / Render / Koyeb / Fly.io）：平台会自动注入 `PORT`，无需改动。

### 2. 环境变量（必须配置）
在平台后台或 `.env` 文件中设置：

| 变量 | 说明 |
|------|------|
| `CASPIAN_API_KEY` | Caspian 邮箱渠道密钥（必填，否则无邮件推送） |
| `DEEPSEEK_API_KEY` | DeepSeek API Key（若走官方网关；当前 server.py 内已硬编码代理网关 Key，可后置） |
| `HERMES_EMBEDDING` | 可选，`tfidf`（默认）或 `dense`（稠密向量，需另装 sentence-transformers） |

> `.env` 含密钥，**切勿提交进 Git / 镜像**。`.dockerignore` 已排除它。

### 3. 依赖注意
- `caspian-sdk`：若不在 PyPI 公开源，请在部署环境手动 `pip install caspian-sdk`（或放入私有源）。
- `numpy`：已加入 `requirements.txt`。
- 本地包 `hermes_rag/`、`hermes_caspian/`（若存在）会与 `server.py` 一同复制进容器，无需额外安装。

### 4. 持久化
- `hermes.db`（SQLite 对话记忆）与 `kb/`（RAG 知识库）在容器内会随容器销毁丢失。
- 生产部署建议挂载卷：`docker run -v hermes_data:/app ...`，或改用外部数据库。

## 二、方式 A：Docker（国内云 / 任意 Linux 主机）

```bash
# 构建
docker build -t hermes .

# 运行（挂载数据卷 + 注入密钥）
docker run -d \
  --name hermes \
  -p 9865:9865 \
  -e PORT=9865 \
  --env-file .env \
  -v hermes_data:/app \
  hermes
```

国内云（阿里云/腾讯云轻量服务器）学生机：装 Docker → 上述命令 → 防火墙放通 9865 → 手机浏览器 `http://<公网IP>:9865`。

## 三、方式 B：Railway（现代 PaaS）

1. 把 `hermes-web` 推到 GitHub 私有仓库。
2. Railway 新建 Project → 关联仓库 → 选 `hermes-web`。
3. 在 Variables 里添加 `CASPIAN_API_KEY` 等（不填 `PORT`，平台自动注入）。
4. 部署完成得到 `*.railway.app` 域名。
5. 验证：手机发邮件「发日报」→ 收到 HTML 日报（邮件渠道不受地域影响）。

## 四、验收

- Web UI：`curl http://localhost:9865/` 返回页面。
- 邮件渠道：发「发日报」→ 手机邮箱收到当日 HTML 情报日报。
- 安全：尝试 `del /f` 类命令应被白名单拦截（返回 ⛔ 已拦截）。
