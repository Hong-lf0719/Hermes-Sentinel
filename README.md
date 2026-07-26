# Hermes Sentinel — 多渠道 AI 情报助手

> **让信息找人，而不是人找信息。**
> 在 Email / 手机浏览器上，像聊天一样让 AI 帮你搜信息、生成情报日报、管理长期记忆、按需操控电脑。

Hermes Sentinel 是一个面向**求职 AI/Agent 岗位**的个人信息情报助手：多渠道接入、可插拔的异步适配器、RAG 长期记忆、主动情报推送、以及一个可演示的 Web 后台。强调**工程成熟度**而非功能堆砌——异步 SDK 接入层、结构化渲染、记忆系统、模块化可发布包，都是可直接写进简历的硬资产。

## 🎯 核心能力

| 能力 | 说明 | 触发方式 |
|------|------|---------|
| 💬 智能对话 | DeepSeek 推理，带结构化输出与思维链回传 | 直接发消息 |
| 🛰️ 联网情报 | Web 搜索（Bing + DuckDuckGo 容错）、GitHub、HackerNews、arXiv | 「搜一下 MCP agent」「最新论文」 |
| 📊 AI 日报 | 结构化 AI 情报日报（HTML 卡片，手机友好） | 「发日报」「推日报」 |
| 💼 求职专题 | 大厂动态 / 招聘信号 / 可投递方向专题报告 | 「求职报告」「岗位报告」 |
| 🧠 RAG 长期记忆 | TF-IDF 向量库持久化，每次对话**自动注入**上下文 | 「记住我的简历…」 |
| 💻 本机操控 | 执行命令（白名单安全限制）、读写文件 | 「桌面有什么文件」 |
| 📡 按需推送 | 在邮件里喊一声，HTML 情报邮件推到手机 | 邮件发「发日报」 |
| 📈 可视化后台 | 对话统计 / 知识库 / 工具调用 / 日报存档 | 浏览器开 `/dashboard` |

## 🏗️ 架构

```
Email (Caspian) ──┐
                  ├──→ hermes_caspian (异步适配器) ──┐
手机浏览器 ───────┤                                    ├──→ Agent Core ──→ DeepSeek API
  (FastAPI)       └──→ static/index.html, dashboard ──┘        │
                                                              │   12 个工具 (web / 报告 / RAG / 操控)
RAG 长期记忆 ───── hermes_rag (自动注入) ─────────────────────┘
                                                              └── reports 表 (日报存档)
```

- **接入层**：`hermes_caspian`（可发布 pip 包，异步 Email 接入）+ FastAPI Web UI
- **核心**：DeepSeek（`deepseek-v4-flash`，走代理网关）
- **工具（12 个）**：`web_search` / `search_github` / `hackernews_top` / `arxiv_latest` / `ai_daily_report` / `ai_job_report` / `kb_add` / `kb_search` / `kb_stats` / `execute_command` / `read_file` / `write_file`
- **记忆双轨**：SQLite 短期对话历史 + `hermes_rag` 长期可检索知识库
- **渲染**：`report_html.py` 数据驱动，输出手机 QQ 邮箱兼容的 table 布局 + 内联 CSS

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置密钥

在项目根目录创建 `.env`，填入：

```
CASPIAN_API_KEY=你的_caspian_key
DEEPSEEK_API_KEY=你的_deepseek_key
```

> `.env` 已被 `.gitignore` 排除，不会进版本库。

### 3. 启动

```bash
python server.py
```

然后：
- 电脑浏览器打开 http://localhost:9866
- 手机浏览器打开 http://电脑IP:9866
- 邮件渠道：对 Caspian 邮箱发「发日报」，HTML 情报邮件推到手机

### 4. （可选）RAG 长期记忆

```
对助手说：「记住我的简历：我是 2027 届数据科学本科…」
之后任意对话，助手自动带着你的简历/项目经历作答
```

升级稠密向量（语义检索更准，需联网下载模型）：

```bash
pip install sentence-transformers
set HERMES_EMBEDDING=dense
```

## 📁 项目结构

```
hermes-sentinel/
├── server.py            # 核心服务 (FastAPI + Caspian + DeepSeek + 12 工具)
├── report_html.py       # 数据驱动 HTML 渲染（日报/求职报告）
├── static/
│   ├── index.html       # Web 聊天 UI
│   └── dashboard.html   # 可视化后台（对话/知识库/工具/日报存档）
├── hermes_caspian/      # 可发布异步 Email 接入层（pip 包）
├── hermes_rag/          # 可插拔 RAG 引擎（TF-IDF / 可选稠密，持久化）
├── requirements.txt
├── Dockerfile           # 通用容器化部署
├── .dockerignore
├── .env.example         # 环境变量模板
├── DEPLOY.md            # 部署指南（Railway / 国内云）
├── 启动.bat             # Windows 一键启动
└── README.md
```

> 敏感与运行时文件（`.env`、`hermes.db`、`kb/`、`cloudflared.exe`、`__pycache__`）不进版本库，也不随项目分发。

## 🔒 安全

- `execute_command` 受**命令白名单**约束：仅放行常用只读/开发命令，拦截 `rm`/`del`/`format`/`shutdown`/`powershell` 及 fork bomb 等危险指令。
- 所有密钥走环境变量 / `.env`，不硬编码。

## 🧪 工程化

- `hermes_caspian`：异步接入层，15 个 pytest 用例 + GitHub Actions CI（Python 3.10–3.12）
- `hermes_rag`：12 个 pytest 用例覆盖灌库 / 检索 / 持久化重载 / 清空
- 通用 Docker 部署配置，端口读 `PORT` 环境变量，适配云端动态端口
