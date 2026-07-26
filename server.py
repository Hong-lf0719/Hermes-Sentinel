"""
Hermes Sentinel — 多渠道 AI 助手 + 电脑控制 + AI 情报收集

入口：
  - Email (via Caspian-SDK)
  - 手机浏览器 (via FastAPI)

核心能力：
  - DeepSeek 对话（带 SQLite 持久记忆）
  - 电脑操控（执行命令、读写文件）
  - AI 日报（GitHub + HackerNews + arXiv 汇总）
  - GitHub 项目搜索
"""

import os
import json
import ssl
import asyncio
import logging
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# RAG 长期知识库（hermes_rag 包，需与 server.py 同目录）
from hermes_rag import retrieve_context, get_store

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logger = logging.getLogger("hermes")

# 加载 .env（Caspian 密钥等）
load_dotenv(Path(__file__).parent / ".env")

# 修复 Windows SSL
ssl._create_default_https_context = ssl._create_unverified_context

# ============================================================
# 配置
# ============================================================
API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
BASE_URL = "https://api.deepseek.com/v1"
MODEL = "deepseek-v4-flash"
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
# HOST="0.0.0.0" 纯 IPv4 监听：本机 127.0.0.1 / localhost(需代理例外) 均可访问。
# 注：本机 "::" 为纯 IPv6 绑定（未开双栈），会导致 127.0.0.1 连不上，故用 IPv4。
HOST = "0.0.0.0"
# 端口优先读环境变量 PORT（云端平台动态分配端口），本地默认 9865
PORT = int(os.getenv("PORT", 9866))

CASPIAN_API_KEY = os.getenv("CASPIAN_API_KEY", "")
DB_PATH = Path(__file__).parent / "hermes.db"
KB_PATH = Path(__file__).parent / "kb"           # 长期知识库（RAG）持久化目录

# ============================================================
# SQLite 记忆系统
# ============================================================
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tool_name TEXT,
                tool_result TEXT
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_session ON conversations(session_id, created_at)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_type TEXT NOT NULL,
                data_json TEXT NOT NULL,
                html TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()
    print("🗄️ SQLite 记忆已就绪")

def load_history(session_id: str, max_messages: int = 30) -> list[dict]:
    """加载对话历史"""
    with get_db() as db:
        rows = db.execute(
            "SELECT role, content FROM conversations "
            "WHERE session_id = ? AND role != 'tool' "
            "ORDER BY created_at DESC LIMIT ?",
            (session_id, max_messages)
        ).fetchall()
    messages = []
    for row in reversed(rows):
        if row["role"] in ("system", "user", "assistant"):
            messages.append({"role": row["role"], "content": row["content"]})
    return messages

def save_message(session_id: str, role: str, content: str, tool_name: str = None, tool_result: str = None):
    """保存单条消息"""
    with get_db() as db:
        db.execute(
            "INSERT INTO conversations (session_id, role, content, tool_name, tool_result) VALUES (?,?,?,?,?)",
            (session_id, role, content, tool_name, tool_result)
        )

def save_conversation(session_id: str, messages: list[dict]):
    """全量替换保存对话"""
    with get_db() as db:
        db.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))
        for msg in messages:
            db.execute(
                "INSERT INTO conversations (session_id, role, content) VALUES (?,?,?)",
                (session_id, msg["role"], msg.get("content", ""))
            )
        db.commit()

def _save_report(report_type: str, data: dict, html: str = None):
    """把生成的日报/求职报告存档到 reports 表（结构化 + HTML），供 Dashboard 回溯。"""
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO reports (report_type, data_json, html) VALUES (?,?,?)",
                (report_type, json.dumps(data, ensure_ascii=False), html or "")
            )
    except Exception as e:
        logger.warning(f"报告存档失败（不影响推送）: {e}")

# ============================================================
# FastAPI
# ============================================================
app = FastAPI(title="Hermes Sentinel")
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Caspian 邮件渠道作为后台协程启动（用 uvicorn.run 标准入口时，
# 靠 startup 事件拉起，避免 Windows 下 asyncio.run + Server.serve 的信号冲突）
@app.on_event("startup")
async def _startup_caspian():
    asyncio.create_task(start_caspian())

# ============================================================
# System Prompt
# ============================================================
SYSTEM_PROMPT = """你是 Hermes，用户的专属 AI 情报官。

用户画像：{profile}

规则：中文回答，简洁务实。信息要筛选——只告诉用户对他求职和学习有用的东西，不用什么都报。提供可操作的建议。
**不要预设固定的区块/表格结构。如果某个方面没有足够信息，直接不写，不要留空标题。**
**必须使用 content 字段输出回答，不要只输出 reasoning_content。**"""

# 方向 → 用户画像映射
DIRECTION_PROFILES = {
    "数据科学": "2027 届本科，数据科学与大数据技术专业，找 AI/Agent 方向岗位。技能 Python、数据分析、机器学习基础。关注 AI Agent 框架、开源项目、大厂动态、实习/校招机会。",
    "前端开发": "2027 届本科，前端开发方向，找前端/全栈岗位。技能 HTML/CSS/JavaScript/TypeScript、React/Vue、Node.js 基础。关注前端框架趋势、UI/UX、AI 前端工具、大厂前端面试题、开源前端项目。",
    "后端开发": "2027 届本科，后端开发方向，找后端/全栈岗位。技能 Python/Java/Go、数据库、Linux、分布式基础。关注后端架构、微服务、AI 后端集成、系统设计面试、开源后端项目。",
    "AI/LLM": "2027 届本科，AI/LLM 方向，找 AI 研发/算法岗位。关注大模型训练/推理、Agent 框架、RAG、Prompt Engineering、AI 开源项目、顶会论文、AI 创业动态。",
    "产品经理": "2027 届本科，产品方向，找 AI PM 岗位。关注 AI 产品设计、用户研究、竞品分析、AI 商业化、产品面试题。",
    "全栈开发": "2027 届本科，全栈开发方向，前后端都做。技能 JS/TS + Python，React/Next.js + FastAPI，数据库 + DevOps。关注 AI 驱动的全栈开发、开源项目、系统设计。",
    "测试开发": "2027 届本科，测试开发方向，找 QA/测试开发岗位。技能 Python/Java、自动化测试框架(Selenium/Playwright/Appium)、CI/CD。关注 AI 测试、质量工程、大厂测试面试题。",
    "运维/DevOps": "2027 届本科，运维/DevOps/SRE 方向。技能 Linux、Docker/K8s、CI/CD(Jenkins/GitHub Actions)、监控(Prometheus/Grafana)。关注云原生、AIOps、容器编排、自动化运维。",
    "移动开发": "2027 届本科，移动端开发方向，找 iOS/Android/跨端岗位。技能 Swift/Kotlin、Flutter/React Native。关注移动端 AI 集成、跨端框架趋势、性能优化、大厂移动端面试题。",
    "安全工程师": "2027 届本科，网络安全方向，找安全研发/攻防岗位。技能 Web 安全、渗透测试、逆向、密码学基础。关注 AI 安全、零信任、开源安全工具、CTF、安全厂商动态。",
    "算法工程师": "2027 届本科，算法方向，找 ML/CV/NLP 研发岗位。技能 Python/PyTorch/TensorFlow，扎实的数学与机器学习理论。关注顶会论文(NeurIPS/ICML/CVPR)、前沿模型架构、开源算法项目。",
    "数据分析": "2027 届本科，数据分析方向，找 BA/DA 岗位。技能 SQL/Excel/Python、BI 工具(Tableau/PowerBI)、统计学。关注业务分析案例、AB 测试、数据产品、大厂数分面试题。",
}

def _build_system_prompt(direction: str = "数据科学") -> str:
    """根据方向构建动态 SYSTEM_PROMPT"""
    profile = DIRECTION_PROFILES.get(direction, DIRECTION_PROFILES["数据科学"])
    return SYSTEM_PROMPT.format(profile=profile)

# ============================================================
# 工具定义
# ============================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "在用户 Windows 电脑上执行命令",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容或列出目录",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入内容到文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_github",
            "description": "搜索 GitHub 开源项目",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "max_results": {"type": "integer", "default": 5}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "hackernews_top",
            "description": "获取 HackerNews 首页热门文章（全球开发者社区头条）",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "default": 10, "description": "获取几条"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "arxiv_latest",
            "description": "搜索 arXiv 最新 AI 论文",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "default": "AI agent", "description": "搜索关键词"},
                    "max_results": {"type": "integer", "default": 5}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索网页与新闻（多源容错，免密钥默认可用）。问最新资讯、大厂动态、招聘新闻时触发。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "max_results": {"type": "integer", "default": 5}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ai_daily_report",
            "description": "生成今日 AI 行业日报（GitHub + HackerNews + arXiv 三源汇总）。说「日报」「速报」「今天有什么」时触发。",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ai_job_report",
            "description": "生成求职专题情报：聚焦大厂 AI 动态、招聘信号、开源项目与可投递方向。问「岗位/招聘/求职/大厂/实习」时触发。",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "kb_add",
            "description": "把知识存入 Hermes 长期记忆库（RAG）。action=text 存一段文本；action=file 存本地文件（支持 .txt/.md/.py/.json/.csv 等）；action=url 抓取网页正文存档。说「记住这个/存下来/这是我的简历/我的项目经历」时触发。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["text", "file", "url"], "description": "知识来源类型"},
                    "value": {"type": "string", "description": "文本内容 / 文件路径 / 网页 URL"},
                    "source": {"type": "string", "description": "可选标签，如「简历」「项目经历」「学习笔记」"}
                },
                "required": ["action", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "kb_search",
            "description": "从长期记忆库检索与问题相关的内容。系统会在每次对话时自动调用以增强个性化；用户也可手动问「我的记忆里有没有…」。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                    "top_k": {"type": "integer", "default": 3}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "kb_stats",
            "description": "查看长期记忆库统计（文档数 / 片段数 / 嵌入模型维度）。问「记忆库有多少内容」时触发。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
]


# ============================================================
# 工具实现
# ============================================================
# ── execute_command 安全白名单 ──────────────────────────────
# 只允许下列程序名（basename，忽略 .exe/路径/扩展名）执行；其余一律拒绝。
# 同时黑名单拦截破坏性 / 可绕过命令。两条规则兼顾「有用」与「安全」。
COMMAND_ALLOWLIST = {
    "dir", "ls", "echo", "type", "cat", "python", "python3", "pip", "pip3",
    "git", "ipconfig", "systeminfo", "tasklist", "date", "time", "pwd", "cd",
    "where", "ver", "hostname", "ping", "cls", "tree", "find", "findstr",
    "nvidia-smi", "jupyter", "node", "npm", "code", "explorer",
}
COMMAND_DENYLIST = {  # 独立危险命令（单词边界匹配，防误伤 git/format 等）
    "rm", "del", "rmdir", "rd", "format", "shutdown", "mkfs", "dd", "reg",
    "netsh", "diskpart", "wmic", "takeown", "icacls", "fsutil", "powercfg",
    "taskkill", "powershell", "pwsh", "cmd", "certutil", "curl", "wget",
    "bitsadmin", "schtasks", "at", "sc", "net",
}
COMMAND_DENY_SUBSTR = {  # 特殊符号串（子串匹配，防 shell 绕过）
    "></dev/sd", ":|:", "|sh", "&&sh",
}
import re as _re


def _command_allowed(command: str):
    """返回 (allowed, reason)。白名单放行 + 黑名单兜底拒绝。"""
    c = (command or "").strip()
    low = c.lower()
    if not low:
        return False, "命令为空"
    # 1) 特殊符号串子串匹配（防 shell 绕过，如 fork bomb、写设备）
    for d in COMMAND_DENY_SUBSTR:
        if d in low:
            return False, f"命中危险指令拦截: '{d}'"
    # 2) 危险命令词边界匹配（避免误伤 git status 中的 'at'、format 中的 'rm'）
    for d in COMMAND_DENYLIST:
        if _re.search(r"(?<![a-z0-9])" + _re.escape(d) + r"(?![a-z0-9])", low):
            return False, f"命中危险指令拦截: '{d}'"
    # 3) 提取首个 token 的 basename（去路径、去引号、去扩展名）
    first = low.split()[0].replace('"', "").replace("'", "")
    base = first.split("\\")[-1].split("/")[-1]
    for ext in (".exe", ".bat", ".cmd", ".ps1"):
        if base.endswith(ext):
            base = base[: -len(ext)]
    if base in COMMAND_ALLOWLIST:
        return True, ""
    return False, f"'{base}' 不在执行白名单（允许: {', '.join(sorted(COMMAND_ALLOWLIST))}）"


async def execute_command(command: str) -> str:
    # 安全白名单检查（先于任何执行）
    ok, why = _command_allowed(command)
    if not ok:
        return f"⛔ 已拦截: {why}"
    try:
        proc = await asyncio.create_subprocess_shell(
            f'cmd /c "{command}"',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        out = (stdout or b"").decode("utf-8", errors="replace").strip()
        err = (stderr or b"").decode("utf-8", errors="replace").strip()
        r = f"[exit={proc.returncode}]\n{out}"
        if err: r += f"\n[stderr]\n{err}"
        return r[:3000]
    except asyncio.TimeoutError:
        return "超时 (30s)"
    except Exception as e:
        return f"错误: {e}"


async def read_file(path: str) -> str:
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"不存在: {path}"
        if p.is_dir():
            items = list(p.iterdir())
            return f"📁 {p}\n" + "\n".join(
                f"{'📁' if x.is_dir() else '📄'} {x.name}" for x in items[:50]
            )
        text = p.read_text("utf-8", errors="replace")
        if len(text) > 3000:
            text = text[:3000] + "\n...(截断)"
        return text
    except Exception as e:
        return f"错误: {e}"


async def write_file(path: str, content: str) -> str:
    try:
        p = Path(path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, "utf-8")
        return f"✅ 已写入: {p} ({len(content)} 字符)"
    except Exception as e:
        return f"错误: {e}"


async def search_github(keyword: str, max_results: int = 5) -> str:
    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as cli:
            r = await cli.get("https://api.github.com/search/repositories", params={
                "q": keyword, "sort": "stars", "order": "desc", "per_page": max_results
            })
            if r.status_code != 200:
                return f"GitHub API {r.status_code}"
            data = r.json()
            items = data.get("items", [])
            if not items:
                return f"🔍 没找到「{keyword}」"
            lines = [f"🔍 **GitHub: 「{keyword}」** ({data['total_count']} 个结果)\n"]
            for i, repo in enumerate(items, 1):
                lines.append(f"{i}. **{repo['full_name']}** ⭐{repo['stargazers_count']} | {repo.get('language','?')}")
                lines.append(f"   {(repo.get('description') or '')[:100]}")
                lines.append(f"   🔗 {repo['html_url']}\n")
            return "\n".join(lines)
    except Exception as e:
        return f"GitHub 搜索失败: {e}"


async def hackernews_top(count: int = 10) -> str:
    """获取 HackerNews 热门"""
    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as cli:
            # 获取 top story IDs
            r = await cli.get("https://hacker-news.firebaseio.com/v0/topstories.json")
            ids = r.json()[:count]

            # 逐条获取详情
            stories = []
            for sid in ids[:count]:
                sr = await cli.get(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json")
                if sr.status_code == 200:
                    s = sr.json()
                    stories.append({
                        "title": s.get("title", ""),
                        "url": s.get("url", f"https://news.ycombinator.com/item?id={sid}"),
                        "score": s.get("score", 0),
                        "comments": s.get("descendants", 0),
                    })

            lines = [f"🔥 **HackerNews 热门 Top {len(stories)}**\n"]
            for i, s in enumerate(stories, 1):
                lines.append(f"{i}. **{s['title']}** (↑{s['score']} 💬{s['comments']})")
                lines.append(f"   🔗 {s['url']}\n")
            return "\n".join(lines)
    except Exception as e:
        return f"HackerNews 获取失败: {e}"


async def arxiv_latest(keyword: str = "AI agent", max_results: int = 5) -> str:
    """搜索 arXiv 最新论文"""
    try:
        async with httpx.AsyncClient(timeout=20, verify=False) as cli:
            r = await cli.get("https://export.arxiv.org/api/query", params={
                "search_query": f"all:{keyword}",
                "start": 0,
                "max_results": max_results,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            })
            if r.status_code != 200:
                return f"arXiv API {r.status_code}"

            # 简单解析 XML
            import xml.etree.ElementTree as ET
            ns = {"a": "http://www.w3.org/2005/Atom"}
            root = ET.fromstring(r.text)
            entries = root.findall("a:entry", ns)

            lines = [f"📚 **arXiv 最新: 「{keyword}」** ({len(entries)} 篇)\n"]
            for i, e in enumerate(entries, 1):
                title = (e.find("a:title", ns).text or "").strip().replace("\n", " ")
                url = e.find("a:id", ns).text or ""
                authors = [a.find("a:name", ns).text for a in e.findall("a:author", ns) if a.find("a:name", ns) is not None]
                published = (e.find("a:published", ns).text or "")[:10]
                lines.append(f"{i}. **{title}**")
                lines.append(f"   👤 {', '.join(authors[:3])}{'...' if len(authors)>3 else ''} | 📅 {published}")
                lines.append(f"   🔗 {url}\n")
            return "\n".join(lines)
    except Exception as e:
        return f"arXiv 搜索失败: {e}"


# ============================================================
# 网页搜索（多源容错：Bing 可选 → DuckDuckGo 免密钥 → 优雅降级）
# ============================================================
def _parse_ddg_html(html_text: str, max_results: int) -> str:
    """从 DuckDuckGo HTML 结果页解析 标题/链接/摘要（无外部依赖）"""
    import re
    titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html_text, re.DOTALL)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html_text, re.DOTALL)
    links = re.findall(r'class="result__a"[^>]*href="([^"]+)"', html_text)
    if not titles:
        return ""
    lines = []
    for i in range(min(len(titles), max_results)):
        title = re.sub(r"<[^>]+>", "", titles[i]).strip()
        snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
        url = links[i] if i < len(links) else ""
        lines.append(f"{i+1}. **{title}**")
        if snippet:
            lines.append(f"   {snippet[:140]}")
        if url:
            lines.append(f"   🔗 {url}")
    return "\n".join(lines)


async def web_search(query: str, max_results: int = 5) -> str:
    """
    多源容错的网页搜索。

    数据源（按优先级自动降级）：
      1. Bing Web Search v7 —— 若环境变量 BING_SEARCH_KEY 已配置
      2. DuckDuckGo HTML —— 免密钥，默认启用
      3. 全部失败 → 返回友好提示（不抛异常、不中断主流程）

    设计目标：任一搜索源抖动都不应导致 Agent 整体失败 —— 这是
    “多源容错降级”工程能力的体现。
    """
    # 数据源 1：Bing（可选）
    bing_key = os.getenv("BING_SEARCH_KEY")
    if bing_key:
        try:
            async with httpx.AsyncClient(timeout=15, verify=False) as cli:
                r = await cli.get(
                    "https://api.bing.microsoft.com/v7.0/search",
                    headers={"Ocp-Apim-Subscription-Key": bing_key},
                    params={"q": query, "count": max_results, "mkt": "zh-CN"},
                )
                if r.status_code == 200:
                    items = r.json().get("webPages", {}).get("value", [])
                    if items:
                        lines = [f"🔎 **Bing 搜索: 「{query}」**\n"]
                        for i, it in enumerate(items[:max_results], 1):
                            lines.append(f"{i}. **{it.get('name', '')}**")
                            lines.append(f"   {(it.get('snippet', '') or '')[:140]}")
                            lines.append(f"   🔗 {it.get('url', '')}\n")
                        return "\n".join(lines)
        except Exception as e:
            logger.warning(f"Bing 搜索失败，降级 DuckDuckGo: {e}")

    # 数据源 2：DuckDuckGo HTML（免密钥）
    try:
        async with httpx.AsyncClient(timeout=15, verify=False, headers={"User-Agent": "Mozilla/5.0"}) as cli:
            r = await cli.post("https://html.duckduckgo.com/html/", data={"q": query})
            if r.status_code == 200:
                parsed = _parse_ddg_html(r.text, max_results)
                if parsed:
                    return f"🔎 **DuckDuckGo 搜索: 「{query}」**\n{parsed}"
    except Exception as e:
        logger.warning(f"DuckDuckGo 搜索失败: {e}")

    # 数据源 3：优雅降级
    return f"⚠️ 网页搜索暂时不可用（Bing/DuckDuckGo 均失败），已跳过联网检索。"


async def fetch_news(query: str, max_results: int = 5) -> str:
    """新闻检索便捷封装，report 函数共用"""
    return await web_search(query, max_results)


# ============================================================
# 长期知识库（RAG）—— 让 Hermes 拥有可检索的「长期记忆」
# ============================================================
def _strip_html_simple(html_text: str) -> str:
    """极简 HTML → 纯文本（去 script/style + 标签 + 折叠空白）。"""
    import re
    html_text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
    html_text = re.sub(r"<[^>]+>", " ", html_text)
    html_text = re.sub(r"&nbsp;", " ", html_text)
    html_text = re.sub(r"\s+", " ", html_text)
    return html_text.strip()


async def kb_add(action: str, value: str, source: str = "") -> str:
    """把知识灌入长期记忆：action=text/file/url。"""
    store = get_store(str(KB_PATH))
    try:
        if action == "text":
            if not value or not value.strip():
                return "⚠️ 请提供要保存的文本内容"
            res = store.add_text(value, source or "文本")
        elif action == "file":
            p = Path(value).expanduser().resolve()
            if not p.exists():
                return f"⚠️ 文件不存在: {value}"
            if p.is_dir():
                return f"⚠️ 不支持目录: {value}，请指定具体文件"
            txt = p.read_text("utf-8", errors="replace")
            md = p.suffix.lower() in (".md", ".markdown")
            res = store.add_text(txt, source or p.name, markdown=md)
        elif action == "url":
            async with httpx.AsyncClient(timeout=20, verify=False, headers={"User-Agent": "Mozilla/5.0"}) as c:
                r = await c.get(value)
                r.raise_for_status()
            txt = _strip_html_simple(r.text)
            res = store.add_text(txt, source or value)
        else:
            return "⚠️ action 必须是 text / file / url 之一"
        return (f"✅ 已存入长期记忆（文档 {res['doc_id']}，新增 {res['added']} 个片段）"
                f"｜当前库：{store.stats()['docs']} 篇 / {store.stats()['chunks']} 片段")
    except Exception as e:
        return f"⚠️ 知识库写入失败: {e}"


async def kb_search(query: str, top_k: int = 3) -> str:
    """检索长期记忆中的相关内容（系统每次对话也会自动调用）。"""
    try:
        ctx = retrieve_context(query, top_k=top_k, path=str(KB_PATH))
        return ctx or "📭 长期记忆中暂无相关内容。"
    except Exception as e:
        return f"⚠️ 知识库检索失败: {e}"


async def kb_stats() -> str:
    """查看长期记忆库统计。"""
    try:
        s = get_store(str(KB_PATH)).stats()
        return f"📚 长期记忆库：{s['docs']} 篇文档 / {s['chunks']} 个片段，嵌入模型 {s['embedding']}（维度 {s['dim']}）"
    except Exception as e:
        return f"⚠️ 无法读取知识库: {e}"


async def ai_daily_report(direction: str = "数据科学") -> str:
    """三源日报：GitHub + HN + arXiv"""
    today = datetime.now().strftime("%Y-%m-%d")
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    # Step 1: 并行获取三个数据源
    async def fetch_github():
        async with httpx.AsyncClient(timeout=15, verify=False) as cli:
            r = await cli.get("https://api.github.com/search/repositories", params={
                "q": f"ai agent created:>{since}",
                "sort": "stars", "order": "desc", "per_page": 10
            })
            if r.status_code != 200:
                return []
            return r.json().get("items", [])

    async def fetch_hn():
        return await hackernews_top(8)

    async def fetch_arxiv():
        return await arxiv_latest("AI agent LLM", 5)

    async def fetch_news_web():
        return await web_search("AI Agent 行业动态 本周", 5)

    gh_items, hn_text, arxiv_text, news_text = await asyncio.gather(
        fetch_github(), fetch_hn(), fetch_arxiv(), fetch_news_web(),
        return_exceptions=True
    )

    if isinstance(gh_items, Exception):
        gh_items = []
    if isinstance(hn_text, Exception):
        hn_text = f"HN 失败: {hn_text}"
    if isinstance(arxiv_text, Exception):
        arxiv_text = f"arXiv 失败: {arxiv_text}"
    if isinstance(news_text, Exception):
        news_text = f"网页搜索失败: {news_text}"

    # Step 2: 用 LLM 生成结构化日报
    gh_summary = json.dumps([{
        "name": r["full_name"],
        "stars": r["stargazers_count"],
        "lang": r.get("language") or "?",
        "desc": (r.get("description") or "")[:100]
    } for r in gh_items[:10]], ensure_ascii=False)

    # 方向描述从 DIRECTION_PROFILES 取，避免维护两份数据
    _profile = DIRECTION_PROFILES.get(direction, DIRECTION_PROFILES["数据科学"])

    prompt = f"""你是 AI 行业分析师，为一位 {_profile} 生成个性化日报。

📅 {today}

📦 GitHub 7天新项目：
{gh_summary}

🔥 HackerNews：
{hn_text[:1200]}

📚 arXiv：
{arxiv_text[:1000]}

🔎 网页搜索（行业动态）：
{news_text[:1000]}

    要求——只输出一个 JSON 对象，不要任何 markdown 代码块或额外文字。结构严格如下：
    {{
      "intro": "一段 2-3 句的日报引言/概览，概括本周最值得关注的信号",
      "top3": [
        {{"title": "标题", "stars": "数字或字符串", "desc": "2-3 句为什么对他重要", "value": "求职价值(10字内)", "url": "原文链接或空字符串"}}
      ],
      "projects": [
        {{"name": "项目名", "stars": "数字或字符串", "lang": "语言", "desc": "一句话", "tag": "你的方向/MCP/多Agent 或空字符串"}}
      ],
      "industry": ["行业信号1", "行业信号2"],
      "learning": ["学习建议1", "学习建议2"],
      "roadmap": [
        {{"name": "方向1", "heat": "🔥🔥🔥🔥🔥", "difficulty": "⭐⭐⭐", "reason": "岗位需求最大"}}
      ],
      "actions": [
        {{"name": "行动1", "feasible": "高", "cycle": "1-2 周", "leverage": "⭐⭐⭐⭐⭐", "hint": "补充说明"}}
      ],
      "weekly_focus": {{
        "title": "本周落地步骤 · 具体主题",
        "steps": [
          "① 步骤一：具体动作｜推荐项目 owner/repo：一句话价值",
          "② 步骤二：具体动作｜推荐项目 owner/repo：一句话价值",
          "③ 步骤三：具体动作｜推荐项目 owner/repo：一句话价值",
          "④ 步骤四：具体动作｜推荐项目 owner/repo：一句话价值"
        ]
      }},
      "interview_tips": [
        "面试话术1",
        "面试话术2"
      ],
      "one_liner": "本周一句话总结"
    }}
    规则：
    - **每个字段都必须填写，不能为空列表或空字符串。** 如果某个方向确实没有足够信息，用通用建议填充，不能留空。
    - intro 写一段流畅的引言，让读者快速了解本周核心动态。
    - top3 选 2-3 条最值得关注的信息，优先 与{direction}方向相关的开源项目、招聘信号、求职资源。
    - projects 来自上方 GitHub 列表，挑 8-10 个。desc 字段**必须用中文概述**（不要直接贴英文原文），用 1-2 句话说明这个项目是做什么的、对{direction}方向有什么价值。tag 仅对与{direction}密切相关的项目填"你的方向"/"MCP"/"多Agent"，其余留空。
    - industry 从 GitHub/HN/网页搜索 提取求职/技术趋势信号 2-3 条（优先采用联网检索到的真实动态）。**必须填 2-3 条，不能空。**
    - learning 给 2-3 条 actionable 建议，结合{direction}方向的求职需求。**必须填 2-3 条，不能空。**
    - roadmap：给出 3-4 个与{direction}最相关的细分方向/技术栈，含热度、难度、推荐理由。**必须填 3-4 个，不能空。**
    - actions：给出 3-4 个与{direction}相关的具体求职/学习行动，含可行性、周期、求职杠杆。**必须填 3-4 个，不能空。**
    - weekly_focus：给出一个本周可落地的具体主题（与{direction}相关），3-4 个步骤。**每个步骤必须关联一个 GitHub 上的开源项目作为实践载体**，格式："① 步骤说明｜推荐项目 owner/repo：一句话价值"。**必须填，不能空。**
    - interview_tips：给 2-3 条与{direction}相关的面试话术/简历关键词。**必须填 2-3 条，不能空。**
    - one_liner：一句话总结本周核心信号。**必须填，不能空。**
    只输出 JSON，不要解释。"""

    try:
        async with httpx.AsyncClient(timeout=60, verify=False) as cli:
            r = await cli.post(
                f"{BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 4096, "response_format": {"type": "json_object"}},
            )
            if r.status_code != 200:
                return {"error": f"LLM 汇总失败: {r.status_code}"}
            content = r.json()["choices"][0]["message"]["content"]
        # 容错提取 JSON（去掉可能包裹的代码块标记）
        import re
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            return {"error": "日报 JSON 解析失败", "raw": content[:200]}
        raw_json = m.group()
        # 常见容错：去掉对象/数组末尾多余的逗号
        raw_json = re.sub(r",(\s*[}\]])", r"\1", raw_json)
        try:
            data = json.loads(raw_json, strict=False)
        except json.JSONDecodeError:
            # 再试一次：去掉可能的 markdown 代码块标记
            raw_json = re.sub(r"^\s*```json?\s*|\s*```\s*$", "", raw_json, flags=re.MULTILINE)
            data = json.loads(raw_json, strict=False)
        data["date"] = today
        data["title"] = "AI 日报"
        return _ensure_report_fields(data, direction)
    except Exception as e:
        return {"error": f"日报生成异常: {e}"}


def _ensure_report_fields(data: dict, direction: str) -> dict:
    """补全日报/求职报告缺失字段，确保前端渲染不空。"""
    defaults = {
        "intro": f"本周 AI 领域持续活跃，{direction}方向机会涌现。以下为你精选最值得关注的信号与行动建议。",
        "top3": [],
        "projects": [],
        "industry": ["AI Agent 方向持续火热，各厂商加速布局", f"{direction}相关岗位需求稳中有升，建议持续关注"],
        "learning": [f"精读 1 个与{direction}相关的开源项目 README 和核心代码", "整理本周 top3 信息为面试谈资", "在 GitHub 上 star 并跟进 2 个感兴趣的项目"],
        "roadmap": [
            {"name": direction, "heat": "🔥🔥🔥🔥🔥", "difficulty": "⭐⭐⭐", "reason": "与你求职方向直接对口"},
            {"name": "AI Agent 框架", "heat": "🔥🔥🔥🔥🔥", "difficulty": "⭐⭐⭐⭐", "reason": "行业最热方向，岗位需求大"},
            {"name": "LLM 应用开发", "heat": "🔥🔥🔥🔥", "difficulty": "⭐⭐⭐", "reason": "入门门槛适中，变现路径清晰"},
        ],
        "actions": [
            {"name": f"跑通 1 个{direction}方向的 GitHub 项目", "feasible": "高", "cycle": "3-5 天", "leverage": "⭐⭐⭐⭐⭐", "hint": "可写进简历的项目经验"},
            {"name": "整理本周日报为面试谈资", "feasible": "高", "cycle": "1 天", "leverage": "⭐⭐⭐⭐", "hint": "面试时展示行业敏感度"},
            {"name": "优化简历中的项目描述", "feasible": "中", "cycle": "2-3 天", "leverage": "⭐⭐⭐⭐⭐", "hint": "用 STAR 法则重写"},
        ],
        "weekly_focus": {
            "title": f"本周落地 · {direction}技能提升",
            "steps": [
                "① 选定 1 个 GitHub 项目，克隆到本地并跑通 demo",
                "② 写 1 篇技术笔记或博客，总结项目核心逻辑",
                "③ 在简历中新增该项目经验，用数据量化成果",
                "④ 投递 3-5 个相关岗位，跟进反馈"
            ]
        },
        "interview_tips": [
            f"\"熟悉 {direction} 方向的主流框架与最佳实践\"",
            "\"具备 AI Agent 系统设计与落地经验\"",
            "\"持续关注行业动态，能快速跟进新技术\""
        ],
        "one_liner": f"本周{direction}方向机会持续释放，建议聚焦项目实战与简历优化。",
    }
    for key, default in defaults.items():
        val = data.get(key)
        if val is None or val == [] or val == {} or val == "":
            data[key] = default
    # bigtech 仅在求职报告中存在
    if "bigtech" in data:
        bt = data["bigtech"]
        if not isinstance(bt, dict) or not bt.get("domestic") or not bt.get("international"):
            data["bigtech"] = {
                "domestic": ["DeepSeek/字节/阿里等大厂持续加码 AI 方向招聘", "国内 AI 创业团队融资活跃，校招/实习机会增加"],
                "international": ["OpenAI/Google/Anthropic 持续发布新模型与 API", "海外 AI 产品商业化加速，远程岗位机会增多"]
            }
    return data


async def ai_job_report(direction: str = "数据科学") -> dict:
    """求职专题情报：大厂 AI 动态 + 招聘信号 + 开源项目 + 可投递方向"""
    today = datetime.now().strftime("%Y-%m-%d")
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    # Step1: 并行获取数据源
    async def fetch_github():
        async with httpx.AsyncClient(timeout=15, verify=False) as cli:
            r = await cli.get("https://api.github.com/search/repositories", params={
                "q": f"AI agent OR LLM OR agentic created:>{since}",
                "sort": "stars", "order": "desc", "per_page": 12
            })
            if r.status_code != 200:
                return []
            return r.json().get("items", [])

    async def fetch_hn():
        return await hackernews_top(8)

    async def fetch_news_job():
        return await web_search("大厂 AI 动态 招聘 本周", 6)

    gh_items, hn_text, news_text = await asyncio.gather(fetch_github(), fetch_hn(), fetch_news_job(), return_exceptions=True)
    if isinstance(gh_items, Exception):
        gh_items = []
    if isinstance(hn_text, Exception):
        hn_text = f"HN 失败: {hn_text}"
    if isinstance(news_text, Exception):
        news_text = f"网页搜索失败: {news_text}"

    # Step2: 用 LLM 生成结构化求职情报
    gh_summary = json.dumps([{
        "name": r["full_name"],
        "stars": r["stargazers_count"],
        "lang": r.get("language") or "?",
        "desc": (r.get("description") or "")[:100]
    } for r in gh_items[:12]], ensure_ascii=False)

    prompt = f"""你是 AI 求职情报分析师，为一位 2027 届 {direction} 方向本科生生成求职专题情报。

📅 {today}

📦 GitHub 7天热门 AI/Agent 项目：
{gh_summary}

🔥 HackerNews：
{hn_text[:1200]}

🔎 网页搜索（大厂 AI 动态/招聘）：
{news_text[:1500]}

要求——只输出一个 JSON 对象，不要任何 markdown 代码块或额外文字。结构严格如下：
{{
  "intro": "2-3 句概览，点明本周最值得关注的求职信号",
  "top3": [
    {{"title": "标题", "stars": "数字或字符串", "desc": "2-3 句为什么对求职重要", "value": "求职价值(10字内)", "url": "原文链接或空字符串"}}
  ],
  "projects": [
    {{"name": "项目名", "stars": "数字或字符串", "lang": "语言", "desc": "一句话", "tag": "你的方向/MCP/多Agent 或空字符串"}}
  ],
  "bigtech": {{
    "domestic": ["国内大厂 AI 动态1", "国内大厂 AI 动态2"],
    "international": ["国际 AI 动态1", "国际 AI 动态2"]
  }},
  "industry": ["行业趋势/招聘信号1", "行业趋势/招聘信号2"],
  "learning": ["学习/投递建议1", "学习/投递建议2"],
  "roadmap": [
    {{"name": "方向1", "heat": "🔥🔥🔥🔥🔥", "difficulty": "⭐⭐⭐", "reason": "岗位需求最大"}}
  ],
  "actions": [
    {{"name": "行动1", "feasible": "高", "cycle": "1-2 周", "leverage": "⭐⭐⭐⭐⭐", "hint": "补充说明"}}
  ],
  "weekly_focus": {{
    "title": "本周落地步骤 · 具体主题",
    "steps": [
      "① 步骤一：具体动作｜推荐项目 owner/repo：一句话价值",
      "② 步骤二：具体动作｜推荐项目 owner/repo：一句话价值",
      "③ 步骤三：具体动作｜推荐项目 owner/repo：一句话价值",
      "④ 步骤四：具体动作｜推荐项目 owner/repo：一句话价值"
    ]
  }},
  "interview_tips": [
    "面试话术1",
    "面试话术2"
  ],
  "one_liner": "本周一句话总结"
}}
规则：
- **每个字段都必须填写，不能为空列表或空字符串。** 如果某个方向确实没有足够信息，用通用建议填充，不能留空。
- top3 选 2-3 条最值得关注的信息，优先大厂招聘信号、Agent 开源项目、求职资源。
- projects 来自上方 GitHub 列表，挑 8-10 个，tag 仅对 Agent/MCP/多Agent 相关项目填"你的方向"/"MCP"/"多Agent"，其余留空。
- bigtech：优先基于上方【网页搜索】的真实检索结果，总结本周大厂 AI 战略/招聘/产品动向（国内 DeepSeek/字节/阿里/百度/腾讯，国际 OpenAI/Google/Anthropic 等）；若联网检索不可用，再退回到公开已知信息补充。国内归 domestic、国际归 international，各 2-3 条。**必须填，不能空。**
- industry 从 GitHub/HN 提取求职/技术/招聘趋势信号 2-3 条。**必须填 2-3 条，不能空。**
- learning 给 2-3 条 actionable 的求职/学习建议。**必须填 2-3 条，不能空。**
- roadmap：给出 3-4 个与{direction}最相关的细分方向/技术栈，含热度、难度、推荐理由。**必须填 3-4 个，不能空。**
- actions：给出 3-4 个与{direction}相关的具体求职/学习行动，含可行性、周期、求职杠杆。**必须填 3-4 个，不能空。**
- weekly_focus：给出一个本周可落地的具体主题（与{direction}相关），3-4 个步骤。**每个步骤必须关联一个 GitHub 上的开源项目作为实践载体**，格式："① 步骤说明｜推荐项目 owner/repo：一句话价值"。**必须填，不能空。**
- interview_tips：给 2-3 条与{direction}相关的面试话术/简历关键词。**必须填 2-3 条，不能空。**
- one_liner：一句话总结本周核心求职信号。**必须填，不能空。**
只输出 JSON，不要解释。"""

    try:
        async with httpx.AsyncClient(timeout=60, verify=False) as cli:
            r = await cli.post(
                f"{BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 4096, "response_format": {"type": "json_object"}},
            )
            if r.status_code != 200:
                return {"error": f"LLM 汇总失败: {r.status_code}"}
            content = r.json()["choices"][0]["message"]["content"]
        import re
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            return {"error": "岗位情报 JSON 解析失败", "raw": content[:200]}
        raw_json = m.group()
        raw_json = re.sub(r",(\s*[}\]])", r"\1", raw_json)
        try:
            data = json.loads(raw_json, strict=False)
        except json.JSONDecodeError:
            raw_json = re.sub(r"^\s*```json?\s*|\s*```\s*$", "", raw_json, flags=re.MULTILINE)
            data = json.loads(raw_json, strict=False)
        data["date"] = today
        data["title"] = "AI 岗位情报"
        return _ensure_report_fields(data, direction)
    except Exception as e:
        return {"error": f"岗位情报生成异常: {e}"}


TOOL_MAP = {
    "execute_command": execute_command,
    "read_file": read_file,
    "write_file": write_file,
    "search_github": search_github,
    "hackernews_top": hackernews_top,
    "arxiv_latest": arxiv_latest,
    "web_search": web_search,
    "ai_daily_report": ai_daily_report,
    "ai_job_report": ai_job_report,
    "kb_add": kb_add,
    "kb_search": kb_search,
    "kb_stats": kb_stats,
}


# ============================================================
# 核心聊天逻辑（带记忆）
# ============================================================
def _match_report_trigger(text: str):
    """识别按需报告口令，返回 'daily' / 'job' / None（不区分大小写）。"""
    t = (text or "").lower()
    job_kw = ("求职报告", "岗位报告", "招聘报告", "job report", "求职日报")
    daily_kw = ("日报", "daily", "推日报", "发日报", "来份日报", "每日日报",
                "生成日报", "给我日报", "ai日报", "推送日报")
    if any(k in t for k in job_kw):
        return "job"
    if any(k in t for k in daily_kw):
        return "daily"
    return None


async def process_chat(message: str, session_id: str = "default", direction: str = "数据科学") -> str:
    # 从 SQLite 加载历史
    history = load_history(session_id)
    if not any(m.get("role") == "system" for m in history):
        history.insert(0, {"role": "system", "content": _build_system_prompt(direction)})
    history.append({"role": "user", "content": message})
    save_message(session_id, "user", message)

    # RAG：自动检索长期知识库，注入系统上下文（让信息找人）
    try:
        kb_ctx = retrieve_context(message, top_k=3, path=str(KB_PATH))
        if kb_ctx:
            history[0]["content"] += "\n\n" + kb_ctx
    except Exception as e:
        logger.warning(f"RAG 检索失败（已跳过注入）: {e}")

    # ── 按需报告口令：用户喊一声即推送（确定触发，不依赖 LLM 工具选择）──
    # 命中则直接生成报告并渲染 HTML，由 adapter 回邮件 = 推送到手机。
    _trig = _match_report_trigger(message)
    if _trig:
        try:
            import report_html
            if _trig == "job":
                _rep = await ai_job_report(direction)
            else:
                _rep = await ai_daily_report(direction)
            if isinstance(_rep, dict) and not _rep.get("error"):
                _text, _html = report_html.render_report_html(_rep, direction)
                _save_report(_trig, _rep, _html)
                save_message(session_id, "assistant", _text)
                return {"text": _text, "html": _html}
            logger.warning(f"口令报告返回异常，转普通对话: {_rep}")
        except Exception as _e:
            logger.warning(f"口令报告生成失败，转普通对话: {_e}")

    # 限制长度
    if len(history) > 30:
        history = [history[0]] + history[-29:]

    try:
        async with httpx.AsyncClient(timeout=120, verify=False) as cli:
            payload = {"model": MODEL, "messages": history, "tools": TOOLS, "tool_choice": "auto", "max_tokens": 4096, "stream": False}

            r = await cli.post(
                f"{BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json=payload,
            )
            if r.status_code != 200:
                return f"API 错误 ({r.status_code}): {r.text[:300]}"

            data = r.json()
            msg = data["choices"][0]["message"]

            # 工具调用循环
            for _ in range(5):
                if not msg.get("tool_calls"):
                    break
                history.append(msg)

                for tc in msg["tool_calls"]:
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        fn_args = {}
                    fn = TOOL_MAP.get(fn_name)
                    # 日报/求职报告需传入用户方向，LLM 不会传此参数
                    if fn_name in ("ai_daily_report", "ai_job_report"):
                        result = await fn(direction=direction) if fn else f"未知工具: {fn_name}"
                    else:
                        result = await fn(**fn_args) if fn else f"未知工具: {fn_name}"

                    # 日报走模板渲染分支：结构化数据 -> HTML，直接返回，跳过 LLM 二次加工
                    if fn_name in ("ai_daily_report", "ai_job_report") and isinstance(result, dict):
                        if result.get("error"):
                            history.append({"role": "tool", "tool_call_id": tc["id"], "content": str(result.get("error"))})
                            continue
                        try:
                            import report_html
                            text, html = report_html.render_report_html(result, direction)
                            _save_report("job" if fn_name == "ai_job_report" else "daily", result, html)
                            save_message(session_id, "assistant", text)
                            return {"text": text, "html": html}
                        except Exception as render_err:
                            history.append({"role": "tool", "tool_call_id": tc["id"], "content": f"日报渲染失败: {render_err}"})
                            continue

                    if fn_name not in ("ai_daily_report", "ai_job_report"):
                        save_message(session_id, "tool", str(result)[:500], tool_name=fn_name)
                    history.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

                payload["messages"] = history
                r = await cli.post(
                    f"{BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                    json=payload,
                )
                if r.status_code != 200:
                    return f"API 错误 ({r.status_code})"
                data = r.json()
                msg = data["choices"][0]["message"]

            # 工具循环已结束，用清洗后的 history 发一次不带 tools 的最终请求，
            # 彻底消除模型在 content 里模拟工具调用的可能
            final_history = _compress_tool_history(history)
            final_payload = {
                "model": MODEL, "messages": final_history,
                "max_tokens": 4096, "stream": False,
            }
            r2 = await cli.post(
                f"{BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json=final_payload,
            )
            if r2.status_code == 200:
                msg = r2.json()["choices"][0]["message"]
            else:
                logger.warning(f"process_chat 最终请求失败 ({r2.status_code})，fallback 用上一轮回复")

            # 取回复内容：优先 content，否则 reasoning_content（deepseek-v4-flash 有时只输出 reasoning）
            raw_content = msg.get("content")
            reasoning = msg.get("reasoning_content")
            reply = raw_content if raw_content else (reasoning if reasoning else "（模型未返回内容，请重试）")
            history.append({"role": "assistant", "content": reply})

            # 对话已落库（user 在开头存、assistant 在此存、tool 在循环里存）
            save_message(session_id, "assistant", reply)

            return reply

    except httpx.TimeoutException:
        return "请求超时，请重试"
    except Exception as e:
        return f"出错了: {e}"


# ============================================================
# 流式对话（SSE）：字逐字蹦出，立即有反馈，降低"思考太久"的体感
# ============================================================
async def _stream_tokens(cli, history, with_tools=False):
    """向 DeepSeek 发起流式请求，逐块 yield 文本增量。"""
    payload = {"model": MODEL, "messages": history, "max_tokens": 4096, "stream": True}
    if with_tools:
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"
    async with cli.stream("POST", f"{BASE_URL}/chat/completions",
                          headers=AUTH_HEADERS, json=payload) as resp:
        async for line in resp.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if not obj.get("choices"):
                continue
            delta = obj["choices"][0].get("delta", {})
            # 优先 content；如果 content 为空则 fallback 到 reasoning_content
            # （deepseek-v4-flash 有时只输出 reasoning_content）
            text = delta.get("content") or delta.get("reasoning_content", "")
            if text:
                yield text


def _compress_tool_history(history):
    """把 assistant(tool_calls) + tool 消息对压缩成普通 assistant 摘要，
    消除模型在最终输出时继续模拟工具调用的诱因。"""
    clean = []
    i = 0
    while i < len(history):
        msg = history[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            results = []
            j = i + 1
            while j < len(history) and history[j].get("role") == "tool":
                results.append(str(history[j].get("content", ""))[:1000])
                j += 1
            summary = "【已完成的搜索/操作】\n" + "\n".join(f"- {r}" for r in results)
            clean.append({"role": "assistant", "content": summary})
            i = j
        elif msg.get("role") == "tool":
            # 孤立 tool 消息（不应出现，保险跳过）
            i += 1
        else:
            clean.append(dict(msg))
            i += 1
    return clean


async def stream_chat(message: str, session_id: str = "default", direction: str = "数据科学"):
    """SSE 生成器：status / token / done / error 事件。"""
    def ev(d):
        return f"data: {json.dumps(d, ensure_ascii=False)}\n\n"

    # —— 历史 + RAG（与 process_chat 同）——
    try:
        history = load_history(session_id)
    except Exception as e:
        logger.error(f"load_history 失败: {e}")
        yield ev({"type": "error", "text": f"读取历史失败: {e}"})
        return

    if not any(m.get("role") == "system" for m in history):
        history.insert(0, {"role": "system", "content": _build_system_prompt(direction)})
    history.append({"role": "user", "content": message})

    try:
        save_message(session_id, "user", message)
    except Exception as e:
        logger.warning(f"save_message(user) 失败（继续）: {e}")

    try:
        kb_ctx = retrieve_context(message, top_k=3, path=str(KB_PATH))
        if kb_ctx:
            history[0]["content"] += "\n\n" + kb_ctx
    except Exception as e:
        logger.warning(f"RAG 检索失败（已跳过注入）: {e}")
    if len(history) > 30:
        history = [history[0]] + history[-29:]

    # —— 按需报告口令 ——
    _trig = _match_report_trigger(message)
    if _trig:
        yield ev({"type": "status", "text": "正在生成情报报告…"})
        try:
            import report_html
            _rep = await (ai_job_report(direction) if _trig == "job" else ai_daily_report(direction))
            if isinstance(_rep, dict) and not _rep.get("error"):
                _text, _html = report_html.render_report_html(_rep, direction)
                _save_report(_trig, _rep, _html)
                save_message(session_id, "assistant", _text)
                yield ev({"type": "done", "text": _text, "html": _html})
                return
            logger.warning(f"口令报告返回异常，转普通对话: {_rep}")
        except Exception as _e:
            logger.warning(f"口令报告生成失败，转普通对话: {_e}")

    # —— 普通对话 ——
    async with httpx.AsyncClient(timeout=120, verify=False) as cli:
        # 工具循环：最多 5 轮，确保最终回答前不再残留 tool_calls
        for _ in range(5):
            probe = {"model": MODEL, "messages": history, "tools": TOOLS,
                     "tool_choice": "auto", "max_tokens": 4096, "stream": False}
            try:
                r = await cli.post(f"{BASE_URL}/chat/completions", headers=AUTH_HEADERS, json=probe)
            except Exception as e:
                yield ev({"type": "error", "text": f"出错了: {e}"})
                return
            if r.status_code != 200:
                yield ev({"type": "error", "text": f"API 错误 ({r.status_code}): {r.text[:200]}"})
                return
            msg = r.json()["choices"][0]["message"]

            if not msg.get("tool_calls"):
                break  # 没有工具了，退出循环进入最终流式输出

            yield ev({"type": "status", "text": "正在搜索资料…"})
            history.append(msg)
            for tc in msg["tool_calls"]:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}
                fn = TOOL_MAP.get(fn_name)
                if fn_name in ("ai_daily_report", "ai_job_report"):
                    result = await fn(direction=direction) if fn else f"未知工具: {fn_name}"
                else:
                    result = await fn(**fn_args) if fn else f"未知工具: {fn_name}"

                if fn_name in ("ai_daily_report", "ai_job_report") and isinstance(result, dict):
                    if result.get("error"):
                        history.append({"role": "tool", "tool_call_id": tc["id"], "content": str(result.get("error"))})
                        continue
                    try:
                        import report_html
                        text, html = report_html.render_report_html(result, direction)
                        _save_report("job" if fn_name == "ai_job_report" else "daily", result, html)
                        save_message(session_id, "assistant", text)
                        yield ev({"type": "status", "text": "报告生成完成，渲染中…"})
                        yield ev({"type": "done", "text": text, "html": html})
                        return
                    except Exception as render_err:
                        history.append({"role": "tool", "tool_call_id": tc["id"], "content": f"日报渲染失败: {render_err}"})
                        continue

                if fn_name not in ("ai_daily_report", "ai_job_report"):
                    save_message(session_id, "tool", str(result)[:500], tool_name=fn_name)
                history.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        else:
            # 循环 5 次后仍有 tool_calls，强制结束
            logger.warning("stream_chat: 工具调用超过 5 轮，强制结束")

        # —— 最终流式输出（此时已无 tool_calls）——
        yield ev({"type": "status", "text": "正在整理回答…"})
        full = ""
        final_history = _compress_tool_history(history)
        async for tok in _stream_tokens(cli, final_history, with_tools=False):
            full += tok
            yield ev({"type": "token", "content": tok})
        reply = full if full else "（模型未返回内容，请重试）"
        save_message(session_id, "assistant", reply)
        yield ev({"type": "done", "text": reply})


# ============================================================
# FastAPI 路由
# ============================================================
class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = "default"
    direction: Optional[str] = "数据科学"

class ChatResponse(BaseModel):
    reply: str
    session_id: str

@app.post("/chat")
async def chat_endpoint(req: ChatRequest):
    reply = await process_chat(req.message, req.session_id, req.direction)
    return ChatResponse(reply=reply, session_id=req.session_id)


@app.post("/chat/stream")
async def chat_stream_endpoint(req: ChatRequest):
    async def safe_stream():
        """包装 stream_chat，捕获所有未处理异常转为 SSE error 事件"""
        try:
            async for event in stream_chat(req.message, req.session_id, req.direction):
                yield event
        except Exception as e:
            logger.error(f"stream_chat 未捕获异常: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'text': f'服务内部错误: {e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        safe_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "close"},
    )

@app.get("/", response_class=HTMLResponse)
async def index():
    p = static_dir / "index.html"
    return HTMLResponse(p.read_text("utf-8") if p.exists() else "<h1>Hermes Sentinel</h1>")

@app.post("/reset")
async def reset(session_id: str = "default"):
    with get_db() as db:
        db.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))
    return {"status": "ok"}

@app.get("/health")
async def health():
    return {"status": "running", "model": MODEL, "db": str(DB_PATH)}


# ============================================================
# Dashboard（可视化后台：对话 / 知识库 / 工具 / 日报存档）
# ============================================================
def _dashboard_data() -> dict:
    """聚合 Dashboard 所需的全部数据。"""
    data: dict = {}
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n, COUNT(DISTINCT session_id) AS s "
                "FROM conversations WHERE role IN ('user','assistant')"
            ).fetchone()
            data["conversations"] = {"messages": row["n"] or 0, "sessions": row["s"] or 0}

            rows = db.execute(
                "SELECT tool_name, COUNT(*) AS c FROM conversations "
                "WHERE role='tool' AND tool_name IS NOT NULL GROUP BY tool_name ORDER BY c DESC"
            ).fetchall()
            data["tool_usage"] = [{"tool": r["tool_name"], "count": r["c"]} for r in rows]

            rows = db.execute(
                "SELECT role, content, created_at, tool_name FROM conversations "
                "WHERE role != 'tool' ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
            data["recent"] = [{
                "role": r["role"],
                "content": (r["content"] or "")[:200],
                "created_at": r["created_at"],
                "tool_name": r["tool_name"],
            } for r in rows]

            rows = db.execute(
                "SELECT id, report_type, created_at FROM reports ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            data["reports"] = [{
                "id": r["id"], "type": r["report_type"], "created_at": r["created_at"]
            } for r in rows]
    except Exception as e:
        data["db_error"] = str(e)

    try:
        data["knowledge_base"] = get_store(str(KB_PATH)).stats()
    except Exception as e:
        data["knowledge_base"] = {"error": str(e)}

    data["tools"] = [
        {"name": t["function"]["name"], "description": t["function"]["description"]}
        for t in TOOLS
    ]
    data["model"] = MODEL
    return data


@app.get("/api/dashboard")
async def dashboard_api():
    return _dashboard_data()


@app.get("/api/dashboard/report/{report_id}")
async def dashboard_report(report_id: int):
    with get_db() as db:
        row = db.execute(
            "SELECT html, report_type, created_at FROM reports WHERE id=?", (report_id,)
        ).fetchone()
    if not row:
        return HTMLResponse("<h1>未找到该报告</h1>", status_code=404)
    return HTMLResponse(row["html"] or "<p>（无 HTML 内容）</p>")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    p = static_dir / "dashboard.html"
    return HTMLResponse(p.read_text("utf-8") if p.exists() else "<h1>Hermes Dashboard</h1>")


# ============================================================
# Caspian 多渠道路由
# ============================================================
async def start_caspian():
    if not CASPIAN_API_KEY:
        print("⚠️ Caspian 未配置")
        return
    try:
        from caspian_sdk import CommClient
        from hermes_caspian.adapter import HermesCaspianAdapter

        client = CommClient()
        client.connect_email(display_name="Hermes")
        adapter = HermesCaspianAdapter(client, process_chat)
        print("📧 Caspian Email 已连接")

        # ⚠️ 关键修复：Caspian SDK 全同步（listen / events / reply 均为普通 def）。
        # 旧代码 `await client.listen()` 会把同步 while-True 死循环当协程 await，
        # 直接卡死 uvicorn 主事件循环 → 端口永远绑不上
        # （症状：日志缺 "Uvicorn running on"、netstat 无 LISTENING、Web UI 连不上）。
        #
        # 修复策略：
        #   1) listen() 放守护线程跑（同步阻塞死循环不进主事件循环）
        #   2) SDK 用 handler(message) 同步调用 handler，故 handler 用普通 def；
        #      async on_message 通过 run_coroutine_threadsafe 桥接回 uvicorn 主循环执行
        loop = asyncio.get_running_loop()

        @client.on_message
        def handle(message):  # 普通 def —— SDK 内部 handler(message) 同步调用
            # 把 async 处理逻辑提交到 uvicorn 主事件循环执行
            asyncio.run_coroutine_threadsafe(adapter.on_message(message), loop)

        import threading

        def _run_listen():
            try:
                client.listen()  # 同步阻塞死循环，在守护线程里跑，绝不阻塞主循环
            except Exception as e:
                logger.error(f"Caspian listen 异常: {e}")

        threading.Thread(target=_run_listen, daemon=True, name="caspian-listen").start()
        print("🤖 Hermes 全渠道已就绪")
        # 不再 await listen —— 立即返回，让 uvicorn 继续执行 main_loop 绑定端口

    except ImportError:
        print("⚠️ pip install caspian-sdk")
    except Exception as e:
        print(f"⚠️ Caspian 启动失败: {e}")


# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    import uvicorn

    init_db()

    # 取本机局域网 IP（供手机/同网段设备访问）
    try:
        import socket
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        LAN_IP = _s.getsockname()[0]
        _s.close()
    except Exception:
        LAN_IP = "<本机IP>"

    print(f"""
╔══════════════════════════════════════════════════╗
║           Hermes Sentinel — 全渠道 AI 助手          ║
║                                                  ║
║  ✅ 浏览器请开: http://127.0.0.1:{PORT}             ║
║  🌐 或:        http://localhost:{PORT}              ║
║  📱 手机连接:   http://{LAN_IP}:{PORT}              ║
║  📧 Email:     {'已连接' if CASPIAN_API_KEY else '未配置'}                       ║
║  🗄️ 记忆:      SQLite (hermes.db)                ║
║                                                  ║
║  💡 对话 · 电脑操控 · 日报 · HN · arXiv · 搜索    ║
║  ⌨️  按 Ctrl+C 停止                                ║
╚══════════════════════════════════════════════════╝
""")
    print("提示：浏览器若打不开，多为代理/VPN 劫持本机流量，")
    print(f"      请访问 http://127.0.0.1:{PORT} ，并把 127.0.0.1;localhost 加入代理「不使用代理」例外。\n")

    # Windows 下强制 SelectorEventLoop：避免 ProactorEventLoop 绑定端口卡住
    # （表现为日志缺 "Uvicorn running on" 行、netstat 无 LISTENING）
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

    # 标准 uvicorn.run 入口：Caspian 由 startup 事件拉起
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
