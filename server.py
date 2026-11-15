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
import re
import ssl
import json
import sys
import time
import asyncio
import logging
import sqlite3
import contextlib
import subprocess
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# Windows GBK 控制台/pythonw 下打印 emoji（启动横幅等）会 UnicodeEncodeError 崩溃，
# 统一把标准流切到 UTF-8 + replace，禁止任何打印拖垮服务。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# RAG 长期知识库（hermes_rag 包，需与 server.py 同目录）
from hermes_rag import retrieve_context, get_store

import httpx
import certifi
from dotenv import load_dotenv


# 注：对外抓取已统一使用 verify=certifi.where()（系统 CA 校验，最安全默认）；
# 原 _areq 回退封装因回退实现有误且未被调用，已移除以避免死代码/假绿。
from fastapi import FastAPI, Request, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# MCP 工具客户端：让 Hermes 作为 Host 动态发现并调用 MCP Server 工具。
# import 失败（未装 mcp 包）时降级为纯本地工具，不影响核心功能。
try:
    from mcp_client import MCPToolClient
    _MCP_AVAILABLE = True
except Exception:
    _MCP_AVAILABLE = False
    MCPToolClient = None

logger = logging.getLogger("hermes")

# 加载 .env（Caspian 密钥等）
load_dotenv(Path(__file__).parent / ".env")

# 注意：不再全局禁用 SSL 证书校验（原 ssl._create_default_https_context=unverified 会让所有出站 HTTPS 不验证，属高危）。
# 仅 httpx 工具调用按需局部使用 verify=certifi.where()；其余恢复系统证书校验。

# ============================================================
# 配置
# ============================================================
API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
BASE_URL = "https://api.deepseek.com/v1"
MODEL = "deepseek-v4-flash"
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
# 仅默认监听本机回环地址：避免同网段任意设备访问本服务。
# 关键点：execute_command 工具可触发本机命令执行，0.0.0.0 暴露等同于把受限 RCE 暴露给局域网。
# Docker/云平台部署时通过环境变量 HOST=0.0.0.0 覆盖（容器内绑 127.0.0.1 会导致端口映射失效）。
HOST = os.getenv("HOST", "127.0.0.1")
# 端口优先读环境变量 PORT（云端平台动态分配端口），本地默认 9865
PORT = int(os.getenv("PORT", 9866))

CASPIAN_API_KEY = os.getenv("CASPIAN_API_KEY", "")
DB_PATH = Path(__file__).parent / "hermes.db"
# 浏览器扩展上报鉴权 token（可在 .env 覆盖；默认仅供本机使用）
EXT_TOKEN = os.getenv("EXT_TOKEN", secrets.token_hex(16))  # 默认随机，避免硬编码默认被猜解
# Web UI 调用 /chat 的访问令牌。默认随机生成（启动即打印），避免硬编码默认值被猜解。
# 由服务端在渲染 index.html/jobs.html 时注入（__WEB_TOKEN__ 占位），前端不再 fetch /api/token，
# 本机任意进程无法再领到有效令牌；配合 127.0.0.1 绑定确保外部不可读。
WEB_TOKEN = os.getenv("WEB_TOKEN", secrets.token_hex(16))
# GitHub API 可选令牌：填入可把匿名 60 次/小时 提额到 5000 次/小时，缓解限流
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
KB_PATH = Path(__file__).parent / "kb"           # 长期知识库（RAG）持久化目录
# 可观测性：单次工具调用耗时超过该阈值（毫秒）即触发自我诊断（即使没报错，也可能卡顿/降级）
SLOW_TRACE_MS = int(os.getenv("SLOW_TRACE_MS", 8000))

# ============================================================
# SQLite 记忆系统
# ============================================================
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    # 注意：库文件使用 DELETE 日志模式（非 WAL）。WAL 在多连接/多进程
    # 场景下会因 -wal 文件锁竞争导致端点内同步写间歇性报 readonly。
    # 统一 busy_timeout 让写锁竞争时等待而非立即失败。
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def _now_local() -> str:
    """统一时间口径：本地时间（UTC+8）字符串。

    SQLite 的 CURRENT_TIMESTAMP 存的是 **UTC**，而项目里 Python 侧（报告日期、
    first_seen、画像 updated_at）一律用 datetime.now() 的本地时间 —— 两者混用
    会差 8 小时，跨零点时「今天的对话/今天新增」会整体错一天。
    因此所有入库时间统一显式传本参数值，不再依赖 SQL 端默认值
    （显式传值还有个好处：对**已存在**的表也立刻生效，
    而改 CREATE TABLE 的 DEFAULT 只影响新建库）。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── Redis 热缓存层：会话热数据读加速（可选增强） ──
# 设计原则：
#   1. SQLite 是唯一事实源；Redis 只镜像"最近 60 条热消息"加速读取
#   2. 任何 Redis 异常一律静默降级回 SQLite——缓存失效最多变慢，绝不影响主流程
#   3. REDIS_URL 未配置 → 功能整体关闭，行为与旧版完全一致
REDIS_URL = os.getenv("REDIS_URL", "")
_HOT_TTL_S = 7 * 24 * 3600
_hot_redis = None
_hot_redis_fail_until = 0.0
_hot_redis_hits = 0
_hot_redis_misses = 0


def _hot_redis_client():
    """惰性连接 Redis；失败进入 60 秒退避（期间直接走 SQLite）。"""
    global _hot_redis, _hot_redis_fail_until
    if _hot_redis is not None:
        return _hot_redis
    if time.time() < _hot_redis_fail_until:
        return None
    if not REDIS_URL:
        _hot_redis_fail_until = time.time() + 60
        return None
    try:
        import redis as redis_lib
        client = redis_lib.Redis.from_url(
            REDIS_URL, socket_connect_timeout=2, socket_timeout=2, decode_responses=True
        )
        if client.ping():
            logger.info("Redis 热缓存已连接")
            _hot_redis = client
            return client
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Redis 连接失败，60 秒内降级 SQLite: {e}")
    _hot_redis_fail_until = time.time() + 60
    return None


def _cache_key(session_id: str) -> str:
    return f"hermes:conv:{session_id}"


def _cache_push(session_id: str, entry: dict) -> None:
    """写穿透：SQLite 已写成功后同步镜像到 Redis（失败静默降级）。"""
    global _hot_redis
    rc = _hot_redis_client()
    if rc is None:
        return
    try:
        key = _cache_key(session_id)
        rc.rpush(key, json.dumps(entry, ensure_ascii=False))
        rc.ltrim(key, -60, -1)
        rc.expire(key, _HOT_TTL_S)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Redis 写缓存失败（降级 SQLite）: {e}")
        _hot_redis = None
        _hot_redis_fail_until = time.time() + 60


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                tool_name TEXT,
                tool_result TEXT
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_session ON conversations(session_id, created_at)")
        # 公司情报缓存：投递前调研结果（学历门槛/口碑/动态/面经），7 天过期可刷新
        db.execute("""
            CREATE TABLE IF NOT EXISTS company_intel (
                company TEXT PRIMARY KEY,
                intel_json TEXT NOT NULL,
                searched_at TIMESTAMP DEFAULT (datetime('now','localtime'))
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_type TEXT NOT NULL,
                data_json TEXT NOT NULL,
                html TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime'))
            )
        """)
        # 可观测性：记录每次工具调用的客观 trace（入参/出参/耗时/状态/错误），供自我诊断回放
        db.execute("""
            CREATE TABLE IF NOT EXISTS traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_args TEXT,
                tool_result TEXT,
                status TEXT NOT NULL,
                error_msg TEXT,
                duration_ms REAL,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime'))
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_traces_session ON traces(session_id, id)")
        # 结构化用户画像：key-value 表，统一管理用户事实（毕业/城市/技能/方向），
        # 由 profile_update 工具动态维护，注入 system prompt（lemmalog 思路的轻量落地）。
        db.execute("""
            CREATE TABLE IF NOT EXISTS user_profile (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT (datetime('now','localtime'))
            )
        """)
        # 预置默认画像（INSERT OR IGNORE 保证重复初始化不覆盖用户已更新的值）
        _default_profile = {
            "毕业年份": "2027",
            "学校": "四川大学锦江学院",
            "专业": "数据科学与大数据技术",
            "目标城市": "武汉/孝感",
            "求职方向": "AI 应用开发 / 数据开发（避开算法岗）",
            "技能栈": "Python、LangGraph、Streamlit、SQLite、FastAPI、数据分析",
        }
        for _k, _v in _default_profile.items():
            db.execute("INSERT OR IGNORE INTO user_profile (key, value) VALUES (?, ?)", (_k, _v))
        db.commit()
    print("🗄️ SQLite 记忆已就绪")

def load_profile() -> dict:
    """读取结构化用户画像（key -> value）。"""
    with get_db() as db:
        rows = db.execute("SELECT key, value FROM user_profile").fetchall()
    return {r["key"]: r["value"] for r in rows}

def update_profile(key: str, value: str) -> None:
    """更新（UPSERT）一条用户画像。"""
    with get_db() as db:
        db.execute(
            "INSERT INTO user_profile (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, _now_local())
        )
        db.commit()

def load_history(session_id: str, max_messages: int = 30) -> list[dict]:
    """加载对话历史（Redis 热缓存优先命中；未命中回源 SQLite 并回填缓存）。"""
    global _hot_redis_hits, _hot_redis_misses
    rc = _hot_redis_client()
    if rc is not None:
        try:
            raw = rc.lrange(_cache_key(session_id), -max_messages, -1)
            msgs = [json.loads(x) for x in raw]
            msgs = [m for m in msgs if m.get("role") in ("user", "assistant", "system")]
            _hot_redis_hits += 1
            return msgs[-max_messages:]
        except Exception as e:  # noqa: BLE001
            _hot_redis_misses += 1
            logger.warning(f"Redis 读缓存失败，回源 SQLite: {e}")
    with get_db() as db:
        rows = db.execute(
            "SELECT role, content FROM conversations "
            "WHERE session_id = ? AND role != 'tool' "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (session_id, max_messages)
        ).fetchall()
    messages = []
    for row in reversed(rows):
        if row["role"] in ("system", "user", "assistant"):
            messages.append({"role": row["role"], "content": row["content"]})

    # 热缓存回填：本次从 SQLite 回源后写入 Redis，下次同会话直接命中
    rc = _hot_redis_client()
    if rc is not None and messages:
        try:
            key = _cache_key(session_id)
            pipe = rc.pipeline()
            pipe.delete(key)
            for m in messages:
                pipe.rpush(key, json.dumps(m, ensure_ascii=False))
            pipe.expire(key, _HOT_TTL_S)
            pipe.execute()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Redis 缓存回填失败: {e}")
    return messages

def save_message(session_id: str, role: str, content: str, tool_name: str = None, tool_result: str = None):
    """保存单条消息"""
    with get_db() as db:
        db.execute(
            "INSERT INTO conversations (session_id, role, content, tool_name, tool_result, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (session_id, role, content, tool_name, tool_result, _now_local())
        )
    # 热缓存同步：写穿透（SQLite 为事实源，Redis 加速后续读取）
    _cache_push(session_id, {"role": role, "content": content})


def _save_trace(session_id: str, tool_name: str, tool_args: dict,
                result, status: str, error_msg: str = "", duration_ms: float = 0.0):
    """记录一次工具调用的客观 trace（完整入参/出参/耗时/状态），供自我诊断回放。
    与 conversations 表（业务对话）分离：trace 是「客观发生了什么」，诊断只看这里，不看模型自述。"""
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO traces (session_id, tool_name, tool_args, tool_result, status, error_msg, "
                "duration_ms, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (session_id, tool_name, json.dumps(tool_args, ensure_ascii=False),
                 str(result)[:2000], status, error_msg, round(duration_ms, 1), _now_local())
            )
    except Exception as e:
        logger.warning(f"trace 落库失败（不影响主流程）: {e}")

def save_conversation(session_id: str, messages: list[dict]):
    """全量替换保存对话"""
    with get_db() as db:
        db.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))
        for msg in messages:
            db.execute(
                "INSERT INTO conversations (session_id, role, content, created_at) VALUES (?,?,?,?)",
                (session_id, msg["role"], msg.get("content", ""), _now_local())
            )
        db.commit()

def _save_report(report_type: str, data: dict, html: str = None):
    """把生成的日报/求职报告存档到 reports 表（结构化 + HTML），供 Dashboard 回溯。"""
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO reports (report_type, data_json, html, created_at) VALUES (?,?,?,?)",
                (report_type, json.dumps(data, ensure_ascii=False), html or "", _now_local())
            )
    except Exception as e:
        logger.warning(f"报告存档失败（不影响推送）: {e}")

# ============================================================
# FastAPI
# ============================================================
app = FastAPI(title="Hermes Sentinel")
# CORS 收敛：仅允许本机回环源（Web UI 同源访问），杜绝外部站点跨域读取本服务响应。
_ALLOWED_ORIGINS = [
    f"http://127.0.0.1:{PORT}",
    f"http://localhost:{PORT}",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局禁用响应缓存：岗位/对话/trace 等都是实时数据接口。
# 若被浏览器或中间代理按启发式缓存，会出现「改了画像→重算→列表仍显示旧数据」的陈旧问题。
@app.middleware("http")
async def _no_store(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# 全局关停信号：Caspian 监听线程靠它退出重连循环（守护线程不设标记会一直重连）
import threading as _threading
_SHUTDOWN = _threading.Event()

# Caspian 客户端全局引用（start_caspian 成功后赋值）：启动扫描推送等主动发信场景复用
_caspian_client = None


# ── MCP 工具接入 ──
# 已迁到 MCP Server 的 4 个情报工具：由 mcp_server.py 动态提供，
# Hermes 通过 mcp_tools 运行时发现并调用，实现「工具与 Host 解耦」。
MCP_TOOL_NAMES = {"search_github", "hackernews_top", "arxiv_latest", "web_search"}
mcp_tools = MCPToolClient() if _MCP_AVAILABLE else None


# Caspian 邮件渠道作为后台协程启动（用 uvicorn.run 标准入口时，
# 靠 startup 事件拉起，避免 Windows 下 asyncio.run + Server.serve 的信号冲突）
@app.on_event("startup")
async def _startup_caspian():
    asyncio.create_task(start_caspian())
    # 启动自动扫描：延迟触发（等服务就绪 + Caspian 连上），高分新岗推邮件
    asyncio.create_task(_startup_auto_scan())
    # 每日定时情报推送：08:30 扫岗位 + 发日报，错过开机窗口自动补发
    asyncio.create_task(_daily_scheduler())
    # MCP 工具 Server 启动：失败仅告警并降级为纯本地工具，不阻塞服务
    if mcp_tools is not None:
        try:
            await mcp_tools.start()
        except Exception as e:
            logger.warning(f"[MCP] 工具 Server 启动失败，降级为纯本地工具: {e}")


@app.on_event("shutdown")
async def _shutdown_signal():
    _SHUTDOWN.set()
    if mcp_tools is not None:
        try:
            await mcp_tools.stop()
        except Exception as e:
            logger.warning(f"[MCP] 关闭工具 Server 异常: {e}")

# ============================================================
# System Prompt
# ============================================================
SYSTEM_PROMPT = """你是 Hermes，用户的专属 AI 情报官。

用户画像：{profile}

规则：中文回答，简洁务实。信息要筛选——只告诉用户对他求职和学习有用的东西，不用什么都报。提供可操作的建议。
**当用户在对话中明确告知新的个人信息（如换了目标城市、新增技能、更新求职方向、毕业年份等）时，调用 profile_update 工具更新用户画像。**
**不要预设固定的区块/表格结构。如果某个方面没有足够信息，直接不写，不要留空标题。**
**必须使用 content 字段输出回答，不要只输出 reasoning_content。**
**绝对禁止在回复中输出任何内部分析语言或指令复述**——不得出现\"我们注意到\"\"需要根据用户画像\"\"需要注意的是\"\"只输出content\"\"回答要简洁\"之类的话，也不得复述系统指令。直接给出面向用户的最终回答正文，第一句就进入正题。**"""

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
    """根据方向构建动态 SYSTEM_PROMPT，并附加结构化用户画像。"""
    profile = DIRECTION_PROFILES.get(direction, DIRECTION_PROFILES["数据科学"])
    try:
        up = load_profile()
        if up:
            extra = "；".join(f"{k}: {v}" for k, v in up.items())
            profile = f"{profile} 用户结构化画像：{extra}"
    except Exception as e:
        logger.warning(f"加载用户画像失败（已跳过注入）: {e}")
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
        {
            "type": "function",
            "function": {
            "name": "job_sentinel_scan",
            "description": "秋招哨兵：扫描多渠道校招岗位（含武汉/湖北央企国企），按用户画像(地区/方向/学历/院校/批次)硬过滤+规则匹配打分，存入岗位库供前端卡片流查看与投递追踪。用户说「扫岗位/查秋招/找岗位/岗位哨兵/查岗位」时触发。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "job_sync_email",
            "description": "解析投递确认/笔试/面试/offer/拒信邮件，自动回写「岗位哨兵」里对应公司的投递状态。用户把邮件正文粘贴过来并说「解析这封邮件」，或 QQ 邮箱自动转发到本地址时触发。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "邮件正文全文（含公司名与状态字样如 投递成功/笔试/面试/录用/未通过）"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "profile_update",
            "description": "更新用户结构化画像（如 毕业年份/目标城市/技能栈/求职方向）。当用户在对话中明确告知新的个人信息时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "画像字段名，如 目标城市/技能栈/求职方向"},
                    "value": {"type": "string", "description": "新的字段值"}
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "company_research",
            "description": "投递前公司情报调研：搜索该公司的校招学历门槛、员工口碑（加班/氛围）、近期动态（裁员/扩张）与面经，生成结构化情报卡。用户问「XX公司怎么样/值不值得投/对双非友好吗/加班严重吗」时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "公司名"},
                    "focus": {"type": "string", "description": "重点关注方向，如 学历门槛/加班强度/稳定性，留空则全面调研"}
                },
                "required": ["company"]
            }
        }
    }
]


def _build_effective_tools() -> list[dict]:
    """合并本地工具 + MCP Server 动态工具，生成 OpenAI function calling 列表。

    - MCP 就绪：4 个情报工具由 mcp_server.py 提供（schema 运行时发现），本地只保留其余 10 个
    - MCP 未就绪/失败：全部回退本地 TOOLS（降级，不影响核心功能）
    """
    if mcp_tools is not None and mcp_tools.is_ready():
        local = [t for t in TOOLS if t["function"]["name"] not in MCP_TOOL_NAMES]
        return local + mcp_tools.to_openai_tools()
    return list(TOOLS)


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
# 解释器类：禁止直接执行代码字符串 / 重定向，只允许运行脚本文件(.py/.js)
_INTERPRETERS = {"python", "python3", "node"}
_INTERPRETER_CODE_FLAGS = {"-c", "-m", "-e", "-x", "-i", "-r",
                           "--command", "--code", "--eval"}
import re as _re
# shell 元字符：出现任意一种即拒绝（Windows 与 POSIX 通用高危，防链式注入）
_SHELL_META = set("&|;><^`%()") | {"\n", "\r"}


def _command_allowed(command: str):
    """返回 (allowed, reason)。白名单放行 + 黑名单兜底拒绝。"""
    c = (command or "").strip()
    low = c.lower()
    if not low:
        return False, "命令为空"
    # 0) shell 元字符拦截（防 `dir & whoami`、`a | b` 等链式注入，最高优先级）
    if any(ch in c for ch in _SHELL_META):
        return False, "含禁止的 shell 元字符（& | ; > < ^ ` % ( ) 及换行）"
    # 1) 危险命令词边界匹配（避免误伤 git status 中的 'at'、format 中的 'rm'）
    for d in COMMAND_DENYLIST:
        if _re.search(r"(?<![a-z0-9])" + _re.escape(d) + r"(?![a-z0-9])", low):
            return False, f"命中危险指令拦截: '{d}'"
    # 2) 提取首个 token 的 basename（去路径、去引号、去扩展名）
    first = low.split()[0].replace('"', "").replace("'", "")
    base = first.split("\\")[-1].split("/")[-1]
    for ext in (".exe", ".bat", ".cmd", ".ps1"):
        if base.endswith(ext):
            base = base[: -len(ext)]
    # 3) 解释器加固：禁止 -c/-m 等直接执行代码；必须运行脚本文件
    if base in _INTERPRETERS:
        toks = low.split()
        bad = [t for t in toks if t in _INTERPRETER_CODE_FLAGS]
        if bad:
            return False, f"解释器禁止 {bad} 直接执行代码"
        script_toks = [t for t in toks if t.endswith((".py", ".js", ".ipynb"))]
        if not script_toks:
            return False, "解释器须运行脚本文件（.py/.js/.ipynb），不允许裸执行"
        # 读写分区：脚本必须位于项目 scripts/ 目录（write_file 写不进该区）
        for t in script_toks:
            t2 = t.replace('"', "").replace("'", "")
            sp = (Path.cwd() / t2).resolve() if not os.path.isabs(t2) else Path(t2).resolve()
            if not (str(sp) == str(_SCRIPT_DIR) or str(sp).startswith(str(_SCRIPT_DIR) + os.sep)):
                return False, "脚本必须位于项目 scripts/ 目录（模型不可写，防提权）"
    if base in COMMAND_ALLOWLIST:
        return True, ""
    return False, f"'{base}' 不在执行白名单（允许: {', '.join(sorted(COMMAND_ALLOWLIST))}）"


async def execute_command(command: str) -> str:
    # 安全白名单检查（先于任何执行）
    ok, why = _command_allowed(command)
    if not ok:
        return f"⛔ 已拦截: {why}"

    def _dec(b: bytes) -> str:
        """cmd /c 的中文输出是 GBK(CP936)，硬按 utf-8 解会产生乱码
        （实测 `dir` 输出变成 'D:\\31227\\Ӧ��'）。先试 utf-8（python 等脚本输出），
        失败再回退 gbk（cmd 原生中文）。"""
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return b.decode("gbk")
            except UnicodeDecodeError:
                return b.decode("utf-8", errors="replace")

    def _run() -> str:
        try:
            proc = subprocess.run(["cmd", "/c", command], capture_output=True, timeout=30)
            out = _dec(proc.stdout or b"").strip()
            err = _dec(proc.stderr or b"").strip()
            r = f"[exit={proc.returncode}]\n{out}"
            if err:
                r += f"\n[stderr]\n{err}"
            return r[:3000]
        except subprocess.TimeoutExpired:
            return "超时 (30s)"
        except Exception as e:
            return f"错误: {e}"

    # 启动入口强制 WindowsSelectorEventLoopPolicy（修端口绑定问题），但 Selector 循环
    # 不支持 create_subprocess_*（NotImplementedError）。改用线程池跑同步 subprocess，
    # 既不阻塞事件循环，也不依赖 Proactor。
    return await asyncio.to_thread(_run)


# 文件读写白名单：仅允许在项目根目录及用户常用目录内操作，
# 并禁止访问敏感文件（.env / server.py / 数据库等），
# 切断"任意读 .env + 写 evil.py 后 python 执行"的提权链。
_FILE_DENY_NAMES = {".env", "server.py", "watchdog.py", "启动.bat", "停止.bat",
                    "requirements.txt", "test_dashboard.py"}
# ── 读写分区（封堵"写 evil.py + python 执行"提权链）──
# 模型可写区：仅 data/ 与 outputs/（数据目录，不含可执行文件）；
# 模型可执行区：仅项目 scripts/ 预置脚本（write_file 写不进去）。
_WRITABLE_SUBDIRS = ("data", "outputs")
_EXEC_EXTENSIONS = {".py", ".pyw", ".js", ".mjs", ".ipynb",
                    ".bat", ".cmd", ".ps1", ".vbs", ".exe", ".sh", ".psm1"}
_SCRIPT_DIR = (Path(__file__).parent / "scripts").resolve()


def _write_allowed(p: Path) -> tuple[bool, str]:
    """write_file 专用：仅允许写入 data/ 与 outputs/，且禁止可执行扩展名。"""
    rp = p.resolve()
    root = Path(__file__).parent.resolve()
    if not any(str(rp).startswith(str((root / sub).resolve()) + os.sep)
               for sub in _WRITABLE_SUBDIRS):
        return False, "仅允许写入 data/ 与 outputs/ 数据目录（防提权）"
    if p.suffix.lower() in _EXEC_EXTENSIONS:
        return False, f"禁止写入可执行文件类型: {p.suffix}"
    return True, ""


def _file_allowed(p: Path) -> tuple[bool, str]:
    name = (p.name or "").lower()
    if name in _FILE_DENY_NAMES or name.endswith(".db") or name.startswith(".env"):
        return False, f"禁止访问敏感文件: {p.name}"
    roots = [(Path(__file__).parent).resolve()]
    for sub in ("Documents", "Downloads", "Desktop"):
        hp = Path.home() / sub
        if hp.exists():
            roots.append(hp.resolve())
    rp = p.resolve()
    for root in roots:
        if rp == root or str(rp).startswith(str(root) + os.sep):
            return True, ""
    return False, "路径越界：仅允许项目目录及用户文档/下载/桌面"


async def read_file(path: str) -> str:
    try:
        p = Path(path).expanduser().resolve()
        ok, why = _file_allowed(p)
        if not ok:
            return f"拒绝访问: {why}"
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
        ok, why = _write_allowed(p)
        if not ok:
            return f"拒绝访问: {why}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, "utf-8")
        return f"✅ 已写入: {p} ({len(content)} 字符)"
    except Exception as e:
        return f"错误: {e}"


async def search_github(keyword: str, max_results: int = 5) -> str:
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=15, verify=certifi.where()) as cli:
            r = await cli.get("https://api.github.com/search/repositories", params={
                "q": keyword, "sort": "stars", "order": "desc", "per_page": max_results
            }, headers=headers)
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
        async with httpx.AsyncClient(timeout=15, verify=certifi.where()) as cli:
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
    """搜索 arXiv 最新论文（带超时退避重试，缓解限流/偶发连接失败）"""
    last_err = "arXiv 搜索失败"
    r = None
    for _attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=20, verify=certifi.where()) as cli:
                r = await cli.get("https://export.arxiv.org/api/query", params={
                    "search_query": f"all:{keyword}",
                    "start": 0,
                    "max_results": max_results,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                })
            if r.status_code == 200:
                last_err = ""
                break
            last_err = f"arXiv API {r.status_code}"
        except Exception as e:
            last_err = f"arXiv 请求异常: {e}"
        if _attempt < 2:
            await asyncio.sleep(2 * (_attempt + 1))
    if last_err:
        return last_err

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


# ============================================================
# 网页搜索（多源容错：Bing 可选 → DuckDuckGo 免密钥 → 优雅降级）
# ============================================================
def _parse_bing_cn_html(html_text: str, max_results: int) -> str:
    """从 Bing 中国站结果页解析 标题/链接/摘要（<li class="b_algo"> 结构，无外部依赖）"""
    import re
    out = []
    blocks = re.findall(r'<li class="b_algo".*?</li>', html_text, flags=re.DOTALL | re.IGNORECASE)
    for b in blocks[:max_results]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, flags=re.DOTALL | re.IGNORECASE)
        if not m:
            continue
        url = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        snip = ""
        ms = re.search(r"<p[^>]*>(.*?)</p>", b, flags=re.DOTALL | re.IGNORECASE)
        if ms:
            snip = re.sub(r"<[^>]+>", "", ms.group(1)).strip()
        if not title:
            continue
        out.append(f"{len(out) + 1}. **{title[:80]}**\n   {snip[:140]}\n   🔗 {url}\n")
    return "\n".join(out)


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
      0. Tavily Search API —— 为 AI Agent 设计的搜索 API（免费额度 1000 次/月），
         注册 tavily.com 取 key 后设 TAVILY_API_KEY 即启用；返回干净正文，国内可达
      1. Bing Web Search v7 —— 若环境变量 BING_SEARCH_KEY 已配置
      2. DuckDuckGo HTML —— 免密钥（大陆网络常被墙）
      3. Bing 中国站 HTML 抓取 —— 免密钥国内直连（注意：连续自动化查询会被
         Bing 软封锁——返回不相关的词典类结果，此时应改用 Tavily）
      4. 全部失败 → 返回友好提示（不抛异常、不中断主流程）

    设计目标：任一搜索源抖动都不应导致 Agent 整体失败 —— 这是
    “多源容错降级”工程能力的体现。
    """
    # 数据源 0：Tavily（推荐：AI Agent 专用搜索 API，免费额度充足）
    tavily_key = os.getenv("TAVILY_API_KEY")
    if tavily_key:
        try:
            async with httpx.AsyncClient(timeout=20, verify=certifi.where()) as cli:
                r = await cli.post("https://api.tavily.com/search", json={
                    "api_key": tavily_key, "query": query,
                    "max_results": max_results, "search_depth": "advanced",
                    "include_answer": False,
                })
                if r.status_code == 200:
                    items = r.json().get("results", [])
                    if items:
                        lines = [f"🔎 **Tavily 搜索: 「{query}」**\n"]
                        for i, it in enumerate(items[:max_results], 1):
                            lines.append(f"{i}. **{it.get('title', '')}**")
                            content = (it.get("content", "") or "")[:160]
                            if content:
                                lines.append(f"   {content}")
                            lines.append(f"   🔗 {it.get('url', '')}\n")
                        return "\n".join(lines)
        except Exception as e:
            logger.warning(f"Tavily 搜索失败，降级下一数据源: {e}")

    # 数据源 1：Bing（可选）
    bing_key = os.getenv("BING_SEARCH_KEY")
    if bing_key:
        try:
            async with httpx.AsyncClient(timeout=15, verify=certifi.where()) as cli:
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
        async with httpx.AsyncClient(timeout=15, verify=certifi.where(), headers={"User-Agent": "Mozilla/5.0"}) as cli:
            r = await cli.post("https://html.duckduckgo.com/html/", data={"q": query})
            if r.status_code == 200:
                parsed = _parse_ddg_html(r.text, max_results)
                if parsed:
                    return f"🔎 **DuckDuckGo 搜索: 「{query}」**\n{parsed}"
    except Exception as e:
        logger.warning(f"DuckDuckGo 搜索失败: {e}")

    # 数据源 2.5：Bing 中国站 HTML（免密钥 · 国内直连可用——DDG 被墙时的主兜底）
    try:
        async with httpx.AsyncClient(timeout=15, verify=certifi.where(),
                                     headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
                                              "Accept-Language": "zh-CN,zh;q=0.9"}) as cli:
            r = await cli.get("https://cn.bing.com/search", params={"q": query, "count": max_results})
            if r.status_code == 200 and "b_results" in r.text:
                parsed = _parse_bing_cn_html(r.text, max_results)
                if parsed:
                    return f"🔎 **Bing(国内) 搜索: 「{query}」**\n{parsed}"
    except Exception as e:
        logger.warning(f"Bing 国内站搜索失败: {e}")

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
            async with httpx.AsyncClient(timeout=20, verify=certifi.where(), headers={"User-Agent": "Mozilla/5.0"}) as c:
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


# 用户画像白名单：profile_update 只允许这些字段，防止 LLM 乱写画像
_PROFILE_ALLOWED_KEYS = {"毕业年份", "学校", "专业", "目标城市", "求职方向", "技能栈", "求职状态", "期望薪资", "备注"}


async def profile_update(key: str, value: str) -> str:
    """更新用户结构化画像（白名单字段）。"""
    key = (key or "").strip()
    value = (value or "").strip()
    if not key or not value:
        return "⚠️ key 和 value 都不能为空"
    if key not in _PROFILE_ALLOWED_KEYS:
        return f"⚠️ 字段「{key}」不在白名单（允许: {', '.join(sorted(_PROFILE_ALLOWED_KEYS))}）"
    try:
        update_profile(key, value)
        return f"✅ 已更新画像「{key}」= {value}"
    except Exception as e:
        return f"⚠️ 画像更新失败: {e}"


# ── 公司情报：投递前调研（学历门槛 / 员工口碑 / 近期动态 / 面经） ──
# 面向"民办二本投大厂容易石沉大海"的真实痛点：投之前先看这家公司
# 对学历的态度、口碑与近期稳定性，把有限的精力投给回复率更高的公司。
_COMPANY_INTEL_TTL_DAYS = 7
_INTEL_QUERIES = [
    "{c} 校园招聘 学历要求 本科 双非 一本",
    "{c} 员工评价 加班 工作强度",
    "{c} 裁员 扩张 经营状况",
    "{c} 面经 面试经验 校招",
]


def _save_company_intel(company: str, intel: dict) -> None:
    with get_db() as db:
        db.execute(
            "INSERT INTO company_intel (company, intel_json, searched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(company) DO UPDATE SET intel_json=excluded.intel_json, "
            "searched_at=excluded.searched_at",
            (company, json.dumps(intel, ensure_ascii=False), _now_local()),
        )
        db.commit()


def _load_company_intel(company: str) -> dict | None:
    with get_db() as db:
        r = db.execute(
            "SELECT intel_json, searched_at FROM company_intel WHERE company = ?", (company,)
        ).fetchone()
    if not r:
        return None
    try:
        return {"intel": json.loads(r["intel_json"]), "searched_at": r["searched_at"]}
    except Exception:  # noqa: BLE001
        return None


def _intel_stale(cached: dict) -> bool:
    sa = (cached.get("searched_at") or "")[:19]
    try:
        dt = datetime.strptime(sa, "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - dt).days >= _COMPANY_INTEL_TTL_DAYS
    except Exception:  # noqa: BLE001
        return True


async def _ai_json(prompt: str, max_tokens: int = 2600) -> dict | None:
    """LLM 结构化 JSON 输出（json_object 模式 + 3 次重试）。失败返回 None。"""
    last_err = None
    for _attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=90, verify=certifi.where()) as cli:
                r = await cli.post(
                    f"{BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                    json={"model": MODEL,
                          "messages": [{"role": "user", "content": prompt}],
                          "max_tokens": max_tokens, "temperature": 0.4,
                          "response_format": {"type": "json_object"}},
                )
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                await asyncio.sleep(2)
                continue
            content = ((r.json().get("choices", [{}])[0].get("message", {}) or {}).get("content") or "").strip()
            if not content:
                await asyncio.sleep(2)
                continue
            return json.loads(content)
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            await asyncio.sleep(2)
    logger.warning(f"_ai_json 生成失败: {last_err}")
    return None


def _intel_reply_text(company: str, intel: dict) -> str:
    bar = intel.get("school_barrier") or "未知"
    lines = [f"🏢 {company} · 学历门槛信号：{bar}（置信度 {intel.get('confidence', '低')}）"]
    if intel.get("school_note"):
        lines.append(f"· {intel['school_note']}")
    for k, icon in (("employee_voice", "🗣 员工口碑"), ("recent_moves", "📈 近期动态"), ("interview_tips", "🧪 面经要点")):
        vals = intel.get(k) or []
        if vals:
            lines.append(f"{icon}：" + "；".join(vals[:3]))
    if intel.get("w2w_advice"):
        lines.append(f"💡 建议：{intel['w2w_advice']}")
    lines.append("（信息来自公开搜索摘要，供参考非事实结论）")
    return "\n".join(lines)


async def company_research(company: str = "", focus: str = "") -> str:
    """工具入口：多路搜索 + LLM 结构化汇总，生成公司情报卡（7 天缓存）。"""
    company = (company or "").strip()
    if not company:
        return "⚠️ 请告诉我要调研的公司名"
    cached = _load_company_intel(company)
    if cached and not _intel_stale(cached):
        return _intel_reply_text(company, cached["intel"])

    queries = [q.format(c=company) for q in _INTEL_QUERIES]
    gathered = await asyncio.gather(
        *[_cached(f"intel_{company}_{i}", 600, lambda qs=q: web_search(qs, 6))
          for i, q in enumerate(queries)],
        return_exceptions=True,
    )
    texts = []
    for i, r in enumerate(gathered):
        body = r if isinstance(r, str) else f"搜索失败: {r}"
        texts.append(f"【检索{i + 1}】{body}")
    material = "\n".join(texts)[:6000]

    prompt = f"""你是求职情报分析师。用户背景：民办二本（独立学院）2027 届本科，找 AI 应用开发/数据方向，关心学历门槛。
调研公司：{company}{('，重点关注：' + focus) if focus else ''}

以下是四路公开搜索摘要（可能含广告或无关内容，请甄别；没有证据的维度如实写"未检索到"）：
{material}

输出 JSON（不要解释）：
{{
  "school_barrier": "宽松|一般|严格|未知",
  "school_note": "学历门槛判断依据一句话（引用搜索证据；无证据则写'未检索到明确学历门槛信息'）",
  "employee_voice": ["员工口碑要点（加班强度/氛围/待遇，标注来源网站提示）"],
  "recent_moves": ["近期动态（扩张/裁员/经营状况）"],
  "interview_tips": ["面经要点"],
  "w2w_advice": "结合民办二本背景给一句投递建议",
  "confidence": "低|中|高",
  "sources_hint": ["来源网站提示"]
}}
规则：只依据搜索材料，绝不编造；证据越少 confidence 越低。"""

    intel = await _ai_json(prompt)
    if not intel:
        return "⚠️ 公司情报生成失败（搜索/模型暂时不可用），稍后再试"
    intel.setdefault("company", company)
    _save_company_intel(company, intel)
    return _intel_reply_text(company, intel)


# —— 简单 TTL 缓存：缓解 GitHub/arXiv/HN/新闻 外部 API 限流（日报频繁生成时不重复打）——
_FETCH_CACHE: dict[str, tuple[float, object]] = {}

async def _cached(key: str, ttl: int, coro_fn):
    """带 TTL 的内存缓存：命中未过期直接返回，避免重复请求外部 API 触发限流。"""
    now = time.time()
    hit = _FETCH_CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    data = await coro_fn()
    _FETCH_CACHE[key] = (now, data)
    return data


def _user_job_context() -> str:
    """从 jobs 表读取用户真实投递统计，注入日报 prompt 做个性化建议。"""
    try:
        with contextlib.closing(sqlite3.connect(str(DB_PATH))) as c:
            c.row_factory = sqlite3.Row
            total = c.execute("SELECT count(*) FROM jobs").fetchone()[0]
            status_rows = c.execute("SELECT status, count(*) as cnt FROM jobs GROUP BY status ORDER BY cnt DESC").fetchall()
            status_dist = ", ".join(f"{r['status']} {r['cnt']}" for r in status_rows)
            # 最近投递的公司（去重，最多 8 家）
            companies = [r[0] for r in c.execute(
                "SELECT DISTINCT company FROM jobs WHERE status IN ('已投','面试','OC') ORDER BY rowid DESC LIMIT 8"
            ).fetchall()]
            # 目标城市
            cities = c.execute("SELECT DISTINCT city FROM jobs WHERE city != '' LIMIT 5").fetchall()
            city_list = ", ".join(r[0] for r in cities if r[0]) or "武汉/孝感"
        return f"""用户当前求职画像：
- 岗位库共 {total} 条，状态分布：{status_dist}
- 已投递/面试中的公司：{', '.join(companies) if companies else '暂无'}
- 目标城市：{city_list}"""
    except Exception:
        return "（无法读取用户投递数据）"


def _parse_report_json(content: str):
    """鲁棒解析模型返回的 JSON：去 markdown、去尾逗号、转义字符串内裸换行、json5 兜底、截断自愈。"""
    import re, json5
    if not content or not content.strip():
        return None
    raw = re.sub(r"^\s*```json?\s*|\s*```\s*$", "", content, flags=re.MULTILINE)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    blob = m.group()
    blob = re.sub(r",(\s*[}\]])", r"\1", blob)  # 常见尾逗号
    # 字符串值内的裸换行/回车会破坏 JSON 结构，转义成空格
    blob = re.sub(r'("(?:\\.|[^"\\])*?")',
                  lambda mm: mm.group(1).replace("\n", " ").replace("\r", " "), blob, flags=re.S)
    for _parser in (json5.loads, lambda b: json.loads(b, strict=False)):
        try:
            _d = _parser(blob)
            if isinstance(_d, dict):
                return _d
        except Exception:
            continue
    # 截断自愈：模型输出超出 max_tokens 被截断时，尝试闭合字符串与括号
    return _repair_truncated_json(blob)


def _repair_truncated_json(blob: str):
    """修复被截断的 JSON：闭合未结束的字符串，再平衡括号。失败返回 None。"""
    import re, json5
    s = blob.rstrip()
    if not s:
        return None
    # 1) 闭合未结束的字符串（未转义引号数为奇数）
    in_str = False
    escaped = False
    for ch in s:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
    if in_str:
        s += '"'
    # 2) 平衡括号（忽略字符串内字符）
    opens = {"{": "}", "[": "]"}
    stack = []
    in_str = False
    escaped = False
    for ch in s:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in opens:
            stack.append(opens[ch])
        elif ch in ("}", "]"):
            if stack and stack[-1] == ch:
                stack.pop()
    s = re.sub(r",(\s*[}\]])", r"\1", s)  # 去掉可能因截断留下的尾逗号
    s += "".join(reversed(stack))
    try:
        _d = json5.loads(s)
        return _d if isinstance(_d, dict) else None
    except Exception:
        return None


# 精选热门 AI/Agent 开源库：GitHub 限流(无 GITHUB_TOKEN 匿名 60次/小时)时的兜底候选，均为真实可查仓库
_CURATED_OSS = [
    {"name": "langchain-ai/langchain", "stars": "?", "lang": "Python", "desc": "LLM 应用开发事实标准框架"},
    {"name": "microsoft/autogen", "stars": "?", "lang": "Python", "desc": "多智能体编排框架"},
    {"name": "crewAIInc/crewAI", "stars": "?", "lang": "Python", "desc": "角色化多 Agent 协作框架"},
    {"name": "vllm-project/vllm", "stars": "?", "lang": "Python", "desc": "高性能 LLM 推理与服务引擎"},
    {"name": "huggingface/transformers", "stars": "?", "lang": "Python", "desc": "主流模型与微调生态"},
    {"name": "run-llama/llama_index", "stars": "?", "lang": "Python", "desc": "RAG 数据框架"},
    {"name": "mem0ai/mem0", "stars": "?", "lang": "Python", "desc": "AI 长期记忆层"},
    {"name": "n8n-io/n8n", "stars": "?", "lang": "TypeScript", "desc": "工作流自动化平台"},
]


def _extract_github_mentions(text: str):
    """从文本抽取 github.com/owner/repo 提及，返回去重后的候选列表。"""
    import re
    found = re.findall(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text or "")
    seen = set()
    out = []
    for fr in found:
        parts = fr.split("/")[:2]
        if len(parts) < 2 or parts[1].lower().startswith("gist"):
            continue
        name = f"{parts[0]}/{parts[1]}"
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "stars": "?", "lang": "?", "desc": "HN/arXiv 提及的开源项目"})
    return out


def _build_project_pool(gh_items, texts, target: int = 5):
    """构建 projects 候选池：GitHub 优先，不足时从 HN/arXiv 提及 + 精选库补足，保证 >=target 个真实候选。"""
    pool = [{
        "name": (r.get("full_name") or r.get("name") or "").strip(),
        "stars": r.get("stargazers_count", "?"),
        "lang": r.get("language") or "?",
        "desc": (r.get("description") or "")[:100],
    } for r in gh_items if (r.get("full_name") or r.get("name"))]
    pool = [p for p in pool if p["name"]]
    seen = {p["name"] for p in pool}
    for t in texts:
        for c in _extract_github_mentions(t):
            if c["name"] not in seen:
                seen.add(c["name"])
                pool.append(c)
            if len(pool) >= target:
                break
        if len(pool) >= target:
            break
    for c in _CURATED_OSS:
        if len(pool) >= target:
            break
        if c["name"] not in seen:
            seen.add(c["name"])
            pool.append(dict(c))
    return pool


# 报告级缓存：同一 (类型, 方向) 30 分钟内复用，避免重复触发慢生成（单次 ~100-170s）
_REPORT_CACHE = {}
_REPORT_CACHE_TTL = 1800.0


async def ai_daily_report(direction: str = "数据科学") -> dict:
    """三源日报：GitHub + HN + arXiv"""
    _ck = ("daily", direction)
    _hit = _REPORT_CACHE.get(_ck)
    if _hit is not None and (datetime.now().timestamp() - _hit[0]) < _REPORT_CACHE_TTL:
        return _hit[1]
    today = datetime.now().strftime("%Y-%m-%d")
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    # Step 1: 并行获取三个数据源
    async def fetch_github():
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
        async with httpx.AsyncClient(timeout=15, verify=certifi.where()) as cli:
            r = await cli.get("https://api.github.com/search/repositories", params={
                "q": f"ai agent created:>{since}",
                "sort": "stars", "order": "desc", "per_page": 10
            }, headers=headers)
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
        _cached("daily_github", 600, fetch_github),
        _cached("daily_hn", 600, fetch_hn),
        _cached("daily_arxiv", 600, fetch_arxiv),
        _cached("daily_news", 600, fetch_news_web),
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

    # Step 2: 注入用户真实数据 + 用 LLM 生成结构化日报
    user_ctx = _user_job_context()

    gh_summary = json.dumps(
        _build_project_pool(gh_items, [hn_text, arxiv_text, news_text], target=5)[:8],
        ensure_ascii=False
    )

    # 方向描述从 DIRECTION_PROFILES 取，避免维护两份数据
    _profile = DIRECTION_PROFILES.get(direction, DIRECTION_PROFILES["数据科学"])

    prompt = f"""你是 AI 行业分析师+求职顾问，为一位 {_profile} 生成深度个性化日报。

📅 {today}

{user_ctx}

📦 开源项目候选（GitHub 7天热门 + HN/arXiv 提及，已为你补足到≥5个）：
{gh_summary}

🔥 HackerNews：
{hn_text[:1200]}

📚 arXiv：
{arxiv_text[:600]}

🔎 网页搜索（行业动态）：
{news_text[:600]}

    要求——只输出一个 JSON 对象，不要任何 markdown 代码块或额外文字。结构严格如下（以下用单引号示意，你返回时请使用标准双引号 JSON）：
    {{
      'intro': '一段 2-3 句的日报引言/概览，概括本周最值得关注的信号',
      'top3': [
        {{'title': '标题', 'stars': '数字或字符串', 'desc': '1 句说清对求职的价值（不超过40字）', 'value': '求职价值(10字内)', 'url': '原文链接或空字符串'}}
      ],
      'projects': [
        {{'name': '项目名', 'stars': '数字或字符串', 'lang': '语言', 'desc': '一句话概述（不超过30字）', 'tag': '你的方向/MCP/多Agent 或空'}}
      ],
      'bigtech': {{
        'domestic': ['国内大厂动态1（含公司名+具体动作+对求职的影响）', '...'],
        'international': ['海外动态1（含公司名+具体动作）', '...']
      }},
      'industry_news': [
        {{'num': 1, 'title': '新闻标题', 'analysis': '1-2句洞察：为什么重要、对{direction}方向求职者有什么影响（不超过35字）'}}
      ],
      'learning_opportunities': [
        {{'priority': '高/中/长朗', 'topic': '具体技术方向或技能', 'stack': '相关技术栈（如 LangChain/AutoGen/CrewAI）', 'rationale': '一句话（不超过30字）'}}
      ],
      'application_strategy': {{
        'companies': ['建议投递的公司及理由（结合用户已投情况给出差异化建议）'],
        'industries': ['建议关注的行业赛道及理由'],
        'open_source': ['建议参与/贡献的开源项目及理由']
      }},
      'roadmap': [
        {{'name': '方向1', 'heat': '🔥🔥🔥🔥🔥', 'difficulty': '⭐⭐⭐', 'reason': '岗位需求最大'}}
      ],
      'actions': [
        {{'name': '行动1', 'feasible': '高', 'cycle': '1-2 周', 'leverage': '⭐⭐⭐⭐⭐', 'hint': '补充说明'}}
      ],
      'weekly_focus': {{
        'title': '本周落地步骤 · 具体主题',
        'steps': [
          '① 步骤一：具体动作｜推荐项目 owner/repo：一句话价值',
          '② 步骤二：具体动作｜推荐项目 owner/repo：一句话价值',
          '③ 步骤三：具体动作｜推荐项目 owner/repo：一句话价值'
        ]
      }},
      'interview_tips': [
        '面试话术1（可直接在面试中说）',
        '面试话术2'
      ],
      'one_liner': '本周一句话总结'
    }}
    规则：
    - 每个字段都必须填写，不能为空列表或空字符串。如果某个方向确实没有足够信息，用通用建议填充，不能留空。
    - intro 写一段流畅的引言，让读者快速了解本周核心动态。
    - top3 选 2-3 条最值得关注的信息，优先与{direction}方向相关的开源项目、招聘信号。desc 用 1 句说清对求职的价值（不超过40字）。
    - projects 来自上方已补足的项目候选列表（GitHub 热门 + HN/arXiv 提及），挑 5 个。desc 用中文一句话（不超过30字）。
    - bigtech 分国内/海外，各 2 条，每条一句话（不超过40字）：公司名 + 具体做了什么 + 对求职者的实际影响。不要泛泛而谈。
    - industry_news 是洞察板块（区别于简短的 industry）：选 3 条本周最重要的行业信号，每条只写 1-2 句洞察——为什么重要、对{direction}方向有什么影响（不超过 35 字）。重质不重量，写出判断而非堆字数。
    - learning_opportunities 给 3 条学习方向建议，每条含：优先级(高/中/长朗)、具体主题、技术栈、一句话推荐理由（不超过30字）。结合用户当前投递情况给差异化建议。
    - application_strategy 给精炼的投递策略：companies/industries/open_source 各 1-2 条，每条一句话（不超过40字）指明冲谁、关注什么、参与什么开源。这是用户最需要的行动指引，要具体。
    - roadmap：3 个细分方向，含热度/难度/理由。
    - actions：3 个具体行动，含可行性/周期/杠杆。
    - weekly_focus：本周可落地主题，3 个步骤，每个步骤关联 GitHub 项目。
    - interview_tips：2-3 条可直接说的面试话术。
    - one_liner：一句话总结。

    只输出 JSON，不要解释。"""

    last_err = None
    for _attempt in range(5):
        try:
            # 网关偶发返回空内容：重试时轻微抖动温度以打破相关性
            _temp = round(0.6 + 0.05 * _attempt, 2)
            async with httpx.AsyncClient(timeout=90, verify=certifi.where()) as cli:
                r = await cli.post(
                    f"{BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                    json={"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                          "max_tokens": 4096, "temperature": _temp,
                          "response_format": {"type": "json_object"}},
                )
            if r.status_code != 200:
                last_err = f"LLM 汇总失败: {r.status_code}"
                await asyncio.sleep(2 * (_attempt + 1))
                continue
            _j = r.json()
            _msg = (_j.get("choices", [{}])[0].get("message", {}) or {})
            content = (_msg.get("content") or "")
            if not content.strip():
                _fr = _j.get("choices", [{}])[0].get("finish_reason")
                last_err = f"模型返回空内容(网关偶发) finish={_fr}"
                await asyncio.sleep(2 * (_attempt + 1))
                continue
            data = _parse_report_json(content)
            if data is None:
                last_err = "JSON 解析失败"
                await asyncio.sleep(2 * (_attempt + 1))
                continue
            data["date"] = today
            data["title"] = "AI 日报"
            data["report_type"] = "daily"
            _REPORT_CACHE[_ck] = (datetime.now().timestamp(), _ensure_report_fields(data, direction))
            return _REPORT_CACHE[_ck][1]
        except Exception as e:
            last_err = f"日报生成异常: {e}"
            await asyncio.sleep(2 * (_attempt + 1))
            continue
    return {"error": f"日报生成失败(重试耗尽): {last_err}"}


def _ensure_report_fields(data: dict, direction: str) -> dict:
    """补全日报/求职报告缺失字段，确保前端渲染不空。"""
    defaults = {
        "intro": f"本周 AI 领域持续活跃，{direction}方向机会涌现。以下为你精选最值得关注的信号与行动建议。",
        "top3": [],
        "projects": [],
        "bigtech": {
            "domestic": ["字节/阿里/腾讯等大厂持续加码 AI Agent 方向招聘", "国内 AI 创业团队融资活跃，校招/实习机会增加"],
            "international": ["OpenAI/Google/Anthropic 持续发布新模型与 API", "海外 AI 产品商业化加速，远程岗位机会增多"]
        },
        "industry_news": [
            {"num": 1, "title": f"{direction}方向持续火热，各厂商加速布局",
             "analysis": f"从 GitHub 新项目数量和招聘需求来看，{direction}相关岗位稳中有升。建议关注头部公司的校招/实习岗位，同时通过开源项目积累实战经验。面试时可聊'我对{direction}方向的行业趋势有自己的判断'。"},
            {"num": 2, "title": "LLM 应用工程化成为新热点",
             "analysis": "从 RAG 到 Agent 到多智能体协作，LLM 正从'能对话'走向'能干活'。对数据科学背景的同学来说，这是很好的切入点——你既懂数据又懂模型，比纯算法或纯工程都有优势。"},
            {"num": 3, "title": "开源项目是最佳简历加分项",
             "analysis": "HR 和技术面试官越来越看重 GitHub 活动。与其刷题不如跑通一个有深度的开源项目，面试时能讲清楚'我做了什么、为什么这么做、遇到了什么问题怎么解决的'。"},
        ],
        "learning_opportunities": [
            {"priority": "高", "topic": "Agent 框架实战（LangChain/AutoGen/CrewAI）",
             "stack": "LangChain / AutoGen / CrewAI", "rationale": "面试高频 + 实际工作刚需"},
            {"priority": "高", "topic": "Python + LLM API 开发",
             "stack": "Python / OpenAI API / FastAPI", "rationale": "基本功底，所有公司都需要"},
            {"priority": "中", "topic": "RAG 系统设计与实现",
             "stack": "LangChain / Chroma / Elasticsearch", "rationale": "企业级应用最常见的技术栈"},
        ],
        "application_strategy": {
            "companies": [f"大厂：字节(豆包)、阿里(通义)、百度(文心) 都在扩招 AI {direction} 方向", "创业公司：关注 Agent 工具链相关的早期项目，成长空间大", "外企：Anthropic/OpenAI 中国区如有岗位值得冲"],
            "industries": ["AI 基础设施（模型服务/MCP平台）", "AI 应用层（客服/营销/代码生成）", "传统行业 AI 转型（金融/制造/医疗）"],
            "open_source": ["给热门 Agent 项目提 PR 或 issue 是最好的简历加分项", "从文档/测试贡献开始，逐步深入核心代码"],
        },
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
    # 强制长度上限：无论模型超发多少，渲染都保持精简契约
    _cap = {
        "top3": 3, "projects": 5, "industry_news": 3, "learning_opportunities": 3,
        "roadmap": 3, "actions": 3, "interview_tips": 3, "industry": 3, "learning": 3,
    }
    for _k, _n in _cap.items():
        _v = data.get(_k)
        if isinstance(_v, list) and len(_v) > _n:
            data[_k] = _v[:_n]
    _bt = data.get("bigtech")
    if isinstance(_bt, dict):
        for _side in ("domestic", "international"):
            _bv = _bt.get(_side)
            if isinstance(_bv, list) and len(_bv) > 2:
                _bt[_side] = _bv[:2]
    _as = data.get("application_strategy")
    if isinstance(_as, dict):
        for _side in ("companies", "industries", "open_source"):
            _av = _as.get(_side)
            if isinstance(_av, list) and len(_av) > 2:
                _as[_side] = _av[:2]
    _wf = data.get("weekly_focus")
    if isinstance(_wf, dict) and isinstance(_wf.get("steps"), list) and len(_wf["steps"]) > 3:
        _wf["steps"] = _wf["steps"][:3]
    # bigtech 已在 defaults 中，无需单独兜底
    return data


async def ai_job_report(direction: str = "数据科学") -> dict:
    """求职专题情报：大厂 AI 动态 + 招聘信号 + 开源项目 + 可投递方向"""
    _ck = ("job", direction)
    _hit = _REPORT_CACHE.get(_ck)
    if _hit is not None and (datetime.now().timestamp() - _hit[0]) < _REPORT_CACHE_TTL:
        return _hit[1]
    today = datetime.now().strftime("%Y-%m-%d")
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    # Step1: 并行获取数据源
    async def fetch_github():
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
        async with httpx.AsyncClient(timeout=15, verify=certifi.where()) as cli:
            r = await cli.get("https://api.github.com/search/repositories", params={
                "q": f"AI agent OR LLM OR agentic created:>{since}",
                "sort": "stars", "order": "desc", "per_page": 12
            }, headers=headers)
            if r.status_code != 200:
                return []
            return r.json().get("items", [])

    async def fetch_hn():
        return await hackernews_top(8)

    async def fetch_news_job():
        return await web_search("大厂 AI 动态 招聘 本周", 6)

    gh_items, hn_text, news_text = await asyncio.gather(
        _cached("job_github", 600, fetch_github),
        _cached("job_hn", 600, fetch_hn),
        _cached("job_news", 600, fetch_news_job),
        return_exceptions=True
    )
    if isinstance(gh_items, Exception):
        gh_items = []
    if isinstance(hn_text, Exception):
        hn_text = f"HN 失败: {hn_text}"
    if isinstance(news_text, Exception):
        news_text = f"网页搜索失败: {news_text}"

    # Step2: 用 LLM 生成结构化求职情报
    gh_summary = json.dumps(
        _build_project_pool(gh_items, [hn_text, news_text], target=5)[:10],
        ensure_ascii=False
    )

    # 注入用户真实投递画像，做差异化建议
    user_ctx = _user_job_context()

    prompt = f"""你是 AI 求职情报分析师，为一位 2027 届 {direction} 方向本科生生成求职专题情报。

📅 {today}

{user_ctx}

📦 开源项目候选（GitHub 7天热门 + HN/arXiv 提及，已为你补足到≥5个）：
{gh_summary}

🔥 HackerNews：
{hn_text[:1200]}

🔎 网页搜索（大厂 AI 动态/招聘）：
{news_text[:900]}

要求——只输出一个 JSON 对象，不要任何 markdown 代码块或额外文字。结构严格如下（以下用单引号示意，你返回时请使用标准双引号 JSON）：
{{
  'intro': '2-3 句概览，点明本周最值得关注的求职信号',
  'top3': [
    {{'title': '标题', 'stars': '数字或字符串', 'desc': '1 句说清对求职的价值（不超过40字）', 'value': '求职价值(10字内)', 'url': '原文链接或空字符串'}}
  ],
  'projects': [
    {{'name': '项目名', 'stars': '数字或字符串', 'lang': '语言', 'desc': '一句话（不超过30字）', 'tag': '你的方向/MCP/多Agent 或空字符串'}}
  ],
  'bigtech': {{
    'domestic': ['国内大厂 AI 动态1', '国内大厂 AI 动态2'],
    'international': ['国际 AI 动态1', '国际 AI 动态2']
  }},
  'industry_news': [
    {{'num': 1, 'title': '新闻标题', 'analysis': '1-2句洞察：为什么重要、对求职者的影响（不超过35字）'}}
  ],
  'learning_opportunities': [
    {{'priority': '高/中/长朗', 'topic': '具体技术方向或技能', 'stack': '相关技术栈（如 LangChain/AutoGen/CrewAI）', 'rationale': '一句话（不超过30字）'}}
  ],
  'application_strategy': {{
    'companies': ['建议投递的公司及理由（结合用户已投情况给出差异化建议）'],
    'industries': ['建议关注的行业赛道及理由'],
    'open_source': ['建议参与/贡献的开源项目及理由']
  }},
  'industry': ['行业趋势/招聘信号1', '行业趋势/招聘信号2'],
  'learning': ['学习/投递建议1', '学习/投递建议2'],
  'roadmap': [
    {{'name': '方向1', 'heat': '🔥🔥🔥🔥🔥', 'difficulty': '⭐⭐⭐', 'reason': '岗位需求最大'}}
  ],
  'actions': [
    {{'name': '行动1', 'feasible': '高', 'cycle': '1-2 周', 'leverage': '⭐⭐⭐⭐⭐', 'hint': '补充说明'}}
  ],
  'weekly_focus': {{
    'title': '本周落地步骤 · 具体主题',
    'steps': [
      '① 步骤一：具体动作｜推荐项目 owner/repo：一句话价值',
      '② 步骤二：具体动作｜推荐项目 owner/repo：一句话价值',
      '③ 步骤三：具体动作｜推荐项目 owner/repo：一句话价值'
    ]
  }},
  'interview_tips': [
    '面试话术1',
    '面试话术2'
  ],
  'one_liner': '本周一句话总结'
}}
规则：
- **每个字段都必须填写，不能为空列表或空字符串。** 如果某个方向确实没有足够信息，用通用建议填充，不能留空。
- top3 选 2-3 条最值得关注的信息，优先大厂招聘信号、Agent 开源项目、求职资源。desc 用 1 句说清对求职的价值（不超过40字）。
- projects 来自上方已补足的项目候选列表（GitHub 热门 + HN/arXiv 提及），挑 5 个。tag 仅对 Agent/MCP/多Agent 相关项目填你的方向/MCP/多Agent，其余留空。desc 一句话（不超过30字）。
- bigtech：优先基于上方【网页搜索】的真实检索结果，总结本周大厂 AI 战略/招聘/产品动向（国内 DeepSeek/字节/阿里/百度/腾讯，国际 OpenAI/Google/Anthropic 等）；若联网检索不可用，再退回到公开已知信息补充。国内归 domestic、国际归 international，各 2 条，每条一句话（不超过40字）。**必须填，不能空。**
- industry 从 GitHub/HN 提取求职/技术/招聘趋势信号 2-3 条，每条（不超过40字）。**必须填 2-3 条，不能空。**
- learning 给 2-3 条 actionable 的求职/学习建议，每条（不超过40字）。**必须填 2-3 条，不能空。**
- industry_news 选 3 条本周最重要的行业/招聘信号，每条只写 1-2 句洞察——为什么重要、对求职者有什么影响（不超过35字）。重质不重量。
- learning_opportunities 给 3 条学习方向建议，每条含：优先级(高/中/长朗)、具体主题、技术栈、一句话推荐理由（不超过30字）。结合用户已投情况给差异化建议。
- application_strategy 给精炼投递策略：companies/industries/open_source 各 1-2 条，每条一句话（不超过40字）指明冲谁、关注什么、参与什么开源。这是用户最需要的行动指引，要具体。
- roadmap：给出 3 个与{direction}最相关的细分方向/技术栈，含热度、难度、推荐理由。**必须填 3 个，不能空。**
- actions：给出 3 个与{direction}相关的具体求职/学习行动，含可行性、周期、求职杠杆。**必须填 3 个，不能空。**
- weekly_focus：给出一个本周可落地的具体主题（与{direction}相关），3 个步骤。**每个步骤必须关联一个 GitHub 上的开源项目作为实践载体**，格式："① 步骤说明｜推荐项目 owner/repo：一句话价值"。**必须填，不能空。**
- interview_tips：给 2-3 条与{direction}相关的面试话术/简历关键词。**必须填 2-3 条，不能空。**
- one_liner：一句话总结本周核心求职信号。**必须填，不能空。**
只输出 JSON，不要解释。"""

    last_err = None
    for _attempt in range(5):
        try:
            # 网关偶发返回空内容：重试时轻微抖动温度以打破相关性
            _temp = round(0.6 + 0.05 * _attempt, 2)
            async with httpx.AsyncClient(timeout=90, verify=certifi.where()) as cli:
                r = await cli.post(
                    f"{BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                    json={"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                          "max_tokens": 4096, "temperature": _temp,
                          "response_format": {"type": "json_object"}},
                )
            if r.status_code != 200:
                last_err = f"LLM 汇总失败: {r.status_code}"
                await asyncio.sleep(2 * (_attempt + 1))
                continue
            _j = r.json()
            _msg = (_j.get("choices", [{}])[0].get("message", {}) or {})
            content = (_msg.get("content") or "")
            if not content.strip():
                _fr = _j.get("choices", [{}])[0].get("finish_reason")
                last_err = f"模型返回空内容(网关偶发) finish={_fr}"
                await asyncio.sleep(2 * (_attempt + 1))
                continue
            data = _parse_report_json(content)
            if data is None:
                last_err = "JSON 解析失败"
                await asyncio.sleep(2 * (_attempt + 1))
                continue
            data["date"] = today
            data["title"] = "AI 岗位情报"
            data["report_type"] = "job"
            _REPORT_CACHE[_ck] = (datetime.now().timestamp(), _ensure_report_fields(data, direction))
            return _REPORT_CACHE[_ck][1]
        except Exception as e:
            last_err = f"岗位情报生成异常: {e}"
            await asyncio.sleep(2 * (_attempt + 1))
            continue
    return {"error": f"岗位情报生成失败(重试耗尽): {last_err}"}


# 服务端扫描并发锁：网页按钮 / 聊天口令 / LLM 工具三条触发路径共用，
# 防止并发双扫描对同一 hermes.db 双写（database is locked）。
_scan_lock = asyncio.Lock()


async def _scan_once() -> dict:
    """跑一次哨兵扫描（三条触发路径的统一入口）。

    扫描内含逐行 commit 的同步 SQLite 写循环，直接 await 会卡死主事件循环
    约 1-2 分钟（期间扩展上报/看板全部挂起）。改为在工作线程里用独立事件
    循环执行整个 async 扫描，主循环保持响应；并发触发直接拒绝而非排队。
    """
    from job_sentinel.scan import scan_and_match
    from pathlib import Path
    base = Path(__file__).parent
    if _scan_lock.locked():
        raise RuntimeError("已有一次扫描正在进行，请等它结束再试")
    async with _scan_lock:
        def _run() -> dict:
            return asyncio.run(scan_and_match(
                db_path=str(DB_PATH),
                profile_path=str(base / "job_sentinel" / "profile.json"),
                seed_path=str(base / "job_sentinel" / "seed_jobs.json"),
            ))
        return await asyncio.to_thread(_run)


async def run_sentinel() -> str:
    """执行一次秋招哨兵扫描，返回人类可读摘要（供 LLM 转述 / 口令直接返回）。"""
    try:
        res = await _scan_once()
    except RuntimeError as e:  # noqa: BLE001
        return f"⏳ {e}"
    except Exception as e:  # noqa: BLE001
        return f"⚠️ 哨兵扫描失败: {e}"
    top = res["top"][:5]
    lines = [f"✅ 秋招哨兵扫描完成：采集 {res['collected']} 条，匹配入库 {res['kept']} 条（新增 {res['new']}）"]
    for j in top:
        city = j.get("city") or "地点未知"
        lines.append(f"· [{j.get('match_score')}分] {j.get('company')} · {j.get('title')} · {city} · {j.get('batch')}｜{j.get('match_reason')}")
    lines.append("打开「岗位哨兵」页面查看全部，并可一键标记投递状态。")
    return "\n".join(lines)


# ── 启动自动扫描：每次服务启动后自动跑一轮，高分新岗推送邮件 ──
# 用户不是 7×24 开机，改为「开电脑启动 Hermes → 自动扫一次」，新岗位主动找人。
AUTO_SCAN_DELAY_S = 10        # 等 Caspian 连上、服务完全就绪再扫
AUTO_SCAN_NEW_MIN_SCORE = 60  # 新岗位达到该分数才推送（低分新岗只在看板安静躺着）
AUTO_SCAN_PUSH_TOP_N = 10


def _push_email_via_caspian(text: str, html: str = "") -> bool:
    """主动发邮件到用户 QQ 邮箱（后台线程发送，绝不阻塞事件循环）。

    修复记录：旧实现用 run_coroutine_threadsafe(...).result(timeout=30) 在事件循环
    线程里同步等待——一旦 Caspian SDK 的 list_conversations 在代理环境下挂住，
    整个服务（/health 都）会被拖死 30 秒起步。改为后台守护线程即发即忘，
    发送结果写日志；调用方只关心"是否受理"。"""
    if _caspian_client is None:
        logger.warning("推送邮件失败：Caspian 未连接")
        return False
    import threading

    def _send():
        try:
            convs = _caspian_client.list_conversations() or []
            if not convs:
                logger.warning("推送邮件失败：Caspian 会话列表为空")
                return
            cid = convs[0].get("id") or convs[0].get("conversation_id")
            if not cid:
                logger.warning("推送邮件失败：Caspian 会话缺少 id")
                return
            _caspian_client.send_message(cid, text=text, html=html or None)
            logger.info("推送邮件已发送")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"推送邮件发送异常: {e}")

    threading.Thread(target=_send, name="hermes-email-push", daemon=True).start()
    return True


async def _startup_auto_scan():
    """启动后自动扫描一次；有高分新岗则推邮件摘要。任何失败只记日志。"""
    await asyncio.sleep(AUTO_SCAN_DELAY_S)
    if _SHUTDOWN.is_set():
        return
    try:
        res = await _scan_once()
    except RuntimeError:
        return  # 启动窗口内用户手动触发了扫描，不重复跑
    except Exception as e:  # noqa: BLE001
        logger.warning(f"启动自动扫描失败（不影响服务）: {e}")
        return
    logger.info(
        f"启动自动扫描完成：采集 {res.get('collected', 0)}，"
        f"入库 {res.get('kept', 0)}，新增 {res.get('new', 0)}"
    )
    # 高分新岗推送：top 里只挑本次新增且达分数线的（top 是按分数排序的前 10）
    fresh = [j for j in res.get("top", [])
             if (j.get("first_seen") or "") >= datetime.now().strftime("%Y-%m-%d")]
    hot = [j for j in fresh if (j.get("match_score") or 0) >= AUTO_SCAN_NEW_MIN_SCORE]
    if not hot:
        return
    lines = [f"🎯 本次扫描发现 {len(hot)} 个高分新岗位：", ""]
    for j in hot[:AUTO_SCAN_PUSH_TOP_N]:
        city = j.get("city") or "地点未知"
        lines.append(
            f"[{j.get('match_score')}分] {j.get('company')} · {j.get('title')} · {city}"
            + (f" · 截止 {j.get('deadline')}" if j.get("deadline") else "")
        )
    lines.append("")
    lines.append("完整列表打开岗位哨兵看板：http://127.0.0.1:" + str(PORT) + "/jobs")
    if _push_email_via_caspian("\n".join(lines)):
        logger.info(f"已推送 {len(hot)} 个高分新岗到邮箱")


# ── 每日定时情报推送：每天 08:30 自动「扫岗位 + 生成日报 + 推邮件」──
# 错过补发：服务启动时若已过当天时点且今天尚未推送，立即补跑一次。
# 推送日期记在 user_profile KV（_DAILY_LAST_KEY），重启不会重复推。
DAILY_PUSH_HOUR = int(os.getenv("HERMES_DAILY_PUSH_HOUR", "8"))
DAILY_PUSH_MIN = int(os.getenv("HERMES_DAILY_PUSH_MIN", "30"))
_DAILY_LAST_KEY = "每日推送最后日期"


def _daily_push_done_today() -> bool:
    return load_profile().get(_DAILY_LAST_KEY, "") == datetime.now().strftime("%Y-%m-%d")


async def _run_daily_push() -> str:
    """每日推送统一入口：扫岗位 → 生成 AI 日报 → 邮件推送；记录日期防重复。"""
    scan_summary = ""
    try:
        res = await _scan_once()
        scan_summary = (f"采集 {res.get('collected', 0)} 条，匹配入库 {res.get('kept', 0)} 条"
                        f"（新增 {res.get('new', 0)}）")
    except RuntimeError:
        pass  # 并发窗口内已有扫描在跑——跳过扫描，日报照推
    except Exception as e:  # noqa: BLE001
        logger.warning(f"每日推送-扫描失败（继续推日报）: {e}")
    report_text, report_html_str = "AI 日报生成失败", ""
    try:
        rep = await ai_daily_report()
        if isinstance(rep, dict) and not rep.get("error"):
            report_text, report_html_str = report_html.render_report_html(rep, "数据科学")
            _save_report("daily", rep, report_html_str)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"每日推送-日报生成失败: {e}")
    lines = [f"📰 每日情报推送（{datetime.now().strftime('%Y-%m-%d %H:%M')}）", ""]
    if scan_summary:
        lines.append(f"✅ 岗位扫描：{scan_summary}")
    lines.append(report_text or "")
    lines.append("")
    lines.append("打开岗位哨兵看板：http://127.0.0.1:" + str(PORT) + "/jobs")
    update_profile(_DAILY_LAST_KEY, datetime.now().strftime("%Y-%m-%d"))
    if _push_email_via_caspian("\n".join(lines), html=report_html_str or ""):
        logger.info("每日情报推送已发送到邮箱")
        return "已推送"
    logger.warning("每日情报推送邮件发送失败（Caspian 未连接）")
    return "推送失败"


async def _daily_scheduler():
    """每分钟醒来检查一次：过了当天推送时点且今天还没推 → 触发（错过开机窗口自动补发）。
    注意：必须用 await asyncio.sleep（异步让出循环），同步 Event.wait 会阻塞整个事件循环。"""
    logger.info(
        f"每日定时推送已启用：每天 {DAILY_PUSH_HOUR:02d}:{DAILY_PUSH_MIN:02d} 扫岗位+发日报（错过补发）"
    )
    while not _SHUTDOWN.is_set():
        try:
            now = datetime.now()
            target = now.replace(hour=DAILY_PUSH_HOUR, minute=DAILY_PUSH_MIN,
                                 second=0, microsecond=0)
            if now >= target and not _daily_push_done_today():
                logger.info("到达每日推送时点，执行今日情报推送")
                await _run_daily_push()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"每日定时推送异常（下一分钟重试）: {e}")
        await asyncio.sleep(60)  # 异步让出循环；进程退出时任务随循环终止


async def job_sentinel_scan() -> str:
    """工具入口：供 LLM 工具循环调用（与 run_sentinel 同实现）。"""
    return await run_sentinel()


async def job_sync_email(text: str = "") -> str:
    """工具入口：解析一封投递确认/笔试/面试/offer/拒信邮件，自动回写岗位状态。

    用户把邮件正文粘贴过来并说「解析这封邮件」，或 QQ 邮箱自动转发到本地址时触发。
    """
    from job_sentinel.email_sync import sync_email_text
    if not text or not text.strip():
        return "请提供邮件正文（直接粘贴内容），或把投递确认邮件转发到本地址自动解析。"
    return sync_email_text(text, str(DB_PATH))


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
    "job_sentinel_scan": job_sentinel_scan,
    "job_sync_email": job_sync_email,
    "profile_update": profile_update,
    "company_research": company_research,
}


async def _execute_tool(fn_name: str, fn, fn_args: dict, direction: str):
    """统一工具执行：MCP 工具优先 → 日报特殊处理 → 本地普通工具。"""
    if mcp_tools is not None and mcp_tools.has(fn_name):
        return await mcp_tools.call(fn_name, fn_args)
    if fn_name in ("ai_daily_report", "ai_job_report"):
        return await fn(direction=direction) if fn else f"未知工具: {fn_name}"
    return await fn(**fn_args) if fn else f"未知工具: {fn_name}"


async def _execute_tool_with_retry(fn_name: str, fn, fn_args: dict, direction: str,
                                   max_retries: int = 1):
    """带自动重试的工具执行（dot-reflex 轻量版）。

    失败后自动重试 max_retries 次，全部失败才回灌错误。
    返回 (result, status, err)，status ∈ {ok, ok_retry, error}。
    """
    last_err = ""
    for attempt in range(max_retries + 1):
        try:
            result = await _execute_tool(fn_name, fn, fn_args, direction)
            ok, note = _verify_result(fn_name, fn_args, result)
            if not ok:
                # 瞬时故障（限流/网络抖动/超时）才重试；确定性失败（如被拦截）重试无意义
                _retryable = note.startswith(_RETRY_MARK)
                _clean = note[len(_RETRY_MARK):] if _retryable else note
                if _retryable and attempt < max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    logger.warning(
                        f"[恢复] 工具 {fn_name} 校验未通过（瞬时故障），重试: {_clean}")
                    continue
                logger.warning(f"[校验] 工具 {fn_name} 结果未通过: {_clean}")
                _fail = f"[结果校验失败] {fn_name}: {_clean}\n原始输出: {str(result)[:500]}"
                return _fail, "error", _clean
            return result, ("ok_retry" if attempt > 0 else "ok"), ""
        except Exception as e:
            last_err = str(e)
            if attempt < max_retries:
                await asyncio.sleep(0.5 * (attempt + 1))
                logger.warning(f"[恢复] 工具 {fn_name} 第 {attempt + 1} 次失败，自动重试: {e}")
    return f"[工具执行失败] {fn_name}: {last_err}", "error", last_err


# 校验未通过时，note 带此前缀表示「瞬时故障、值得重试」；
# 无前缀表示「确定性失败」（如被安全白名单拦截），重试无意义。
_RETRY_MARK = "可重试|"


def _verify_result(fn_name: str, fn_args: dict, result) -> tuple[bool, str]:
    """确定性校验工具结果（reverify「模型提议、工具裁决」落地）。

    工具返回后不直接信模型转述，而是用 exit code / 文件操作标记 / 搜索异常等
    客观信号做即时验证，拦截「模型以为成功但实际失败」的幻觉。
    返回 (是否通过, 校验说明)。"""
    r = str(result) if result is not None else ""

    # 1. 命令执行：只把「工具本身故障」判失败。
    #    exit≠0 是命令成功执行后的「否定结论」，交给模型按语义解读；
    #    典型误杀场景：grep 无匹配(1) / pytest 失败(1) / git diff 有差异(1) / 端口未监听。
    if fn_name == "execute_command":
        if r.startswith("⛔ 已拦截"):
            return False, "命令被安全白名单拦截，未执行"
        if r.startswith("超时"):
            return False, _RETRY_MARK + "命令执行超时（30s）"
        if r.startswith("错误"):
            return False, _RETRY_MARK + "命令执行抛异常"
        m = re.search(r"\[exit=(-?\d+)\]", r)
        if m and int(m.group(1)) != 0:
            return True, (f"命令已执行完毕，exit code = {m.group(1)}"
                          "（非零＝否定结论，非工具故障，请按语义解读）")
        return True, "exit code = 0"

    # 2. 文件读写：核对失败标记（拒绝访问 / 不存在 / 异常）
    if fn_name in ("write_file", "read_file"):
        for bad in ("拒绝访问", "不存在", "错误:"):
            if r.startswith(bad):
                return False, f"{fn_name} 失败: {r[:80]}"
        return True, "文件操作成功"

    # 3. 搜索类：API 错误 / 异常 判失败且可重试（限流、网络抖动多为瞬时）；
    #    空结果不判失败——搜冷门话题没匹配是合法答案，应由模型自行决定是否换关键词。
    if fn_name in ("search_github", "web_search", "arxiv_latest", "hackernews_top"):
        if r.startswith("错误") or r.startswith("超时") or "GitHub API 4" in r or "GitHub API 5" in r:
            return False, _RETRY_MARK + f"搜索失败: {r[:80]}"
        if not r.strip():
            return True, "无匹配结果（合法空结果，非故障）"
        return True, "返回有效内容"

    # 默认：无专项校验，放行
    return True, "无专项校验"


async def _run_one_tool(tc: dict, session_id: str, direction: str):
    """执行单个工具调用并落 trace（供 asyncio.gather 并行调用）。返回 (tc, result)。

    双层兜底：①工具异常转错误文本 ②trace 落库异常仅记日志。
    保证单个工具的失败不会经 gather 拖垮同一轮内的其他工具。
    """
    fn_name = tc["function"]["name"]
    try:
        fn_args = json.loads(tc["function"]["arguments"])
    except json.JSONDecodeError:
        fn_args = {}
    fn = TOOL_MAP.get(fn_name)
    _t0 = time.perf_counter()
    try:
        result, _status, _err = await _execute_tool_with_retry(fn_name, fn, fn_args, direction)
    except Exception as e:  # _execute_tool_with_retry 之外抛出的异常（理论上极少）
        result, _status, _err = f"[工具异常] {fn_name}: {e}", "error", str(e)
        logger.warning(f"工具 {fn_name} 执行异常（已隔离）: {e}")
    try:
        _save_trace(session_id, fn_name, fn_args, result, _status, _err,
                    (time.perf_counter() - _t0) * 1000)
    except Exception as te:  # trace 落库失败不应影响主流程
        logger.warning(f"trace 落库失败（不影响主流程）: {te}")
    return tc, result


# ============================================================
# 核心聊天逻辑（带记忆）
# ============================================================
def _match_report_trigger(text: str):
    """识别按需报告口令，返回 'daily' / 'job' / 'sentinel' / None（不区分大小写）。"""
    t = (text or "").lower()
    sentinel_kw = ("扫岗位", "查秋招", "找岗位", "岗位哨兵", "查岗位", "秋招岗位", "找秋招", "招聘信息")
    if any(k in t for k in sentinel_kw):
        return "sentinel"
    job_kw = ("求职报告", "岗位报告", "招聘报告", "job report", "求职日报")
    daily_kw = ("日报", "daily", "推日报", "发日报", "来份日报", "每日日报",
                "生成日报", "给我日报", "ai日报", "推送日报")
    if any(k in t for k in job_kw):
        return "job"
    if any(k in t for k in daily_kw):
        return "daily"
    return None


def _match_email_trigger(text: str) -> bool:
    """识别「解析/同步投递邮件」口令，或判断文本本身就是一封投递状态邮件。"""
    t = (text or "").lower()
    kw = ("解析投递邮件", "解析邮件", "同步投递", "同步投递状态", "同步邮件",
          "投递进度同步", "识别投递邮件", "解析这封邮件", "这封邮件")
    if any(k in t for k in kw):
        return True
    try:
        from job_sentinel.email_sync import is_delivery_email
        return is_delivery_email(t)
    except Exception:
        return False


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
    if _trig == "sentinel":
        try:
            _text = await run_sentinel()
            save_message(session_id, "assistant", _text)
            return _text
        except Exception as _e:
            logger.warning(f"哨兵扫描失败，转普通对话: {_e}")
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

    # ── 聊天一句话状态更新 ──
    # "启云方进笔试了" / "小米挂了" / "字节oc了" / "烽火面试了"
    # 在邮件解析之前拦截，因为一句话更新更轻量、更常用
    try:
        from job_sentinel.email_sync import sync_chat_status
        _chat_up = sync_chat_status(message, str(DB_PATH))
        if _chat_up:
            save_message(session_id, "assistant", _chat_up)
            return _chat_up
    except Exception as _e:
        logger.warning(f"聊天状态更新失败，转普通对话: {_e}")

    # ── 投递邮件自动解析：QQ 邮箱转发 / 用户粘贴的投递确认邮件 ──
    # 命中（口令或强信号启发式）即解析并回写岗位状态，返回汇总（自动推到手机）。
    if _match_email_trigger(message):
        try:
            from job_sentinel.email_sync import sync_email_text
            _text = sync_email_text(message, str(DB_PATH))
            save_message(session_id, "assistant", _text)
            return _text
        except Exception as _e:
            logger.warning(f"投递邮件解析失败，转普通对话: {_e}")

    # 限制长度
    if len(history) > 30:
        history = [history[0]] + history[-29:]

    try:
        async with httpx.AsyncClient(timeout=120, verify=certifi.where()) as cli:
            payload = {"model": MODEL, "messages": history, "tools": _build_effective_tools(), "tool_choice": "auto", "max_tokens": 4096, "stream": False}

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

                # 本轮是否含日报/求职报告工具（它们走模板渲染分支直接返回，需串行处理）
                _has_report = any(
                    tc["function"]["name"] in ("ai_daily_report", "ai_job_report")
                    for tc in msg["tool_calls"]
                )

                if _has_report:
                    # 日报分支：保留原串行逻辑（结构化数据 -> HTML 直接返回，跳过 LLM 二次加工）
                    for tc in msg["tool_calls"]:
                        fn_name = tc["function"]["name"]
                        try:
                            fn_args = json.loads(tc["function"]["arguments"])
                        except json.JSONDecodeError:
                            fn_args = {}
                        fn = TOOL_MAP.get(fn_name)
                        _t0 = time.perf_counter()
                        result, _status, _err = await _execute_tool_with_retry(fn_name, fn, fn_args, direction)
                        _save_trace(session_id, fn_name, fn_args, result, _status, _err,
                                    (time.perf_counter() - _t0) * 1000)

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
                else:
                    # 普通工具：一轮内多个工具通常无相互依赖，并行执行（asyncio.gather），
                    # 降低多信息源查询的整体延迟（StreamMA 流水线思想）。
                    # gather 按传入顺序返回，天然保证 history 顺序与 msg["tool_calls"] 对齐。
                    _results = await asyncio.gather(
                        *[_run_one_tool(tc, session_id, direction) for tc in msg["tool_calls"]],
                        return_exceptions=True,
                    )
                    # 关键：每个 tool_call 都必须补一条 tool 消息（成功或异常都要），
                    # 否则 history 里的 tool_calls 缺少对应结果，上游 API 会直接 400。
                    for _i, _item in enumerate(_results):
                        tc = msg["tool_calls"][_i]
                        fn_name = tc["function"]["name"]
                        if isinstance(_item, BaseException):
                            result = f"[工具异常] {fn_name}: {_item}"
                            logger.warning(f"并行工具异常已隔离: {_item}")
                        else:
                            result = _item[1]
                        save_message(session_id, "tool", str(result)[:500], tool_name=fn_name)
                        history.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

                payload["messages"] = history
                _ok = False
                r = None
                for _attempt in range(3):
                    try:
                        r = await cli.post(
                            f"{BASE_URL}/chat/completions",
                            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                            json=payload,
                        )
                        if r.status_code == 200:
                            _ok = True
                            break
                        if r.status_code == 429:
                            await asyncio.sleep(1 * (_attempt + 1))
                            continue
                        break
                    except (httpx.TimeoutException, httpx.ConnectError):
                        await asyncio.sleep(1 * (_attempt + 1))
                        continue
                if not _ok:
                    return f"API 错误 ({getattr(r, 'status_code', '?')}): {getattr(r, 'text', '')[:300]}"
                data = r.json()
                msg = data["choices"][0]["message"]
            if msg.get("tool_calls"):
                logger.warning("工具调用达 5 轮上限，直接生成最终回答")

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
            # 裁掉模型把思考过程混入 content 的开头文本
            reply = _strip_reasoning(reply)

            # ── 自我诊断：本次任务若发生工具失败/显著超时，自动回放客观 trace 做根因定位 ──
            try:
                _diag_trigger = None
                if _session_had_error(session_id):
                    _diag_trigger = "工具执行失败"
                elif _session_had_slow(session_id, SLOW_TRACE_MS):
                    _diag_trigger = f"工具耗时超阈值（>{SLOW_TRACE_MS}ms）"
                if _diag_trigger:
                    _diag = await self_diagnose(session_id, _diag_trigger)
                    if _diag:
                        reply = reply + f"\n\n---\n🔍 自我诊断（基于客观 trace）\n{_diag}"
            except Exception as _de:
                logger.warning(f"自我诊断失败（不影响主回复）: {_de}")

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
# deepseek-v4-flash 经代理网关把"思考过程"放在独立的 reasoning_content 字段，
# 真正的回答在 content 字段。流式只取 content，绝不泄露推理。
def _strip_reasoning(text: str) -> str:
    """兜底：若 content 仍夹带少量推理前缀，裁掉开头直到正文（首个加粗/标题）。"""
    t = text.strip()
    if not (t.startswith("我们注意到") or t.startswith("根据") or t.startswith("注意")
            or t.startswith("用户") or t.startswith("回答要点") or t.startswith("首先")):
        return text
    m = re.search(r"\*\*[^*]{3,}\*\*", t) or re.search(r"\n#{1,6}\s", t)
    return t[m.start():] if m else text

async def _stream_tokens(cli, history, with_tools=False):
    """向 DeepSeek 发起流式请求，逐块 yield 文本增量。"""
    payload = {"model": MODEL, "messages": history, "max_tokens": 4096, "stream": True}
    if with_tools:
        payload["tools"] = _build_effective_tools()
        payload["tool_choice"] = "auto"
    full_content = ""
    fallback_reasoning = ""
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
            # 只输出 content；reasoning_content 是模型内部思考，绝不输出给用户
            c = delta.get("content") or ""
            r = delta.get("reasoning_content") or ""
            if c:
                full_content += c
                yield c
            elif r:
                fallback_reasoning += r
    # 极端情况：整段 content 为空，退而用 reasoning_content 兜底
    if not full_content and fallback_reasoning:
        yield fallback_reasoning


async def self_diagnose(session_id: str, trigger: str = "工具执行失败") -> str:
    """加载最近 N 步客观 trace，喂给 LLM 做自我诊断（self-reflection + 可观测性）。
    关键：给 LLM 的是「真实发生的 trace」（客观记录），不是它脑中的记忆，
    从而绕开「模型对着自己的幻觉反思」的盲区。独立 LLM 调用、独立角色，不污染业务回复。"""
    with get_db() as db:
        rows = db.execute(
            "SELECT tool_name, tool_args, tool_result, status, error_msg, duration_ms "
            "FROM traces WHERE session_id = ? ORDER BY id DESC LIMIT 10",
            (session_id,),
        ).fetchall()
    if not rows:
        return "没有找到 trace 记录，无法诊断。"

    trace_text = "\n".join(
        f"{i}. [{r['tool_name']}] status={r['status']} 耗时={r['duration_ms']:.0f}ms\n"
        f"   入参: {r['tool_args']}\n"
        f"   输出: {str(r['tool_result'])[:800]}\n"
        f"   错误: {r['error_msg'] or '无'}"
        for i, r in enumerate(reversed(rows), 1)
    )

    diag_prompt = f"""你是 Hermes 的自我诊断器。下面是本次任务**真实发生**的工具调用 trace（客观记录，不是模型自述）：

{trace_text}

触发诊断的原因：{trigger}

请只基于上面的 trace 回答，分三点：
1. 根因定位：哪一步出了问题？（工具失败 / 入参错误 / 逻辑走偏）
2. 影响：这个错误会让最终回答哪里不可靠？
3. 修复建议：具体改什么（一句话，可执行）。

要求：只输出客观结论，不要编造 trace 里没有的细节；用中文。"""

    try:
        async with httpx.AsyncClient(timeout=120, verify=certifi.where()) as cli:
            r = await cli.post(
                f"{BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={"model": MODEL,
                      "messages": [{"role": "user", "content": diag_prompt}],
                      "max_tokens": 1024, "stream": False},
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"].get("content", "诊断失败：空回复")
            return f"诊断 API 错误 ({r.status_code}): {r.text[:200]}"
    except Exception as e:
        return f"诊断失败: {e}"


def _session_had_error(session_id: str, limit: int = 5) -> bool:
    """判断最近 limit 条 trace 中是否有工具执行失败。"""
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM (SELECT status FROM traces "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?) WHERE status = 'error'",
            (session_id, limit),
        ).fetchone()
    return (row["c"] if row else 0) > 0


def _session_had_slow(session_id: str, threshold_ms: float, limit: int = 5) -> bool:
    """判断最近 limit 条 trace 中是否有工具单次耗时超过阈值（毫秒）。"""
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM (SELECT duration_ms FROM traces "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?) WHERE duration_ms >= ?",
            (session_id, limit, threshold_ms),
        ).fetchone()
    return (row["c"] if row else 0) > 0


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
    async with httpx.AsyncClient(timeout=120, verify=certifi.where()) as cli:
        # 工具循环：最多 5 轮，确保最终回答前不再残留 tool_calls
        for _ in range(5):
            probe = {"model": MODEL, "messages": history, "tools": _build_effective_tools(),
                     "tool_choice": "auto", "max_tokens": 4096, "stream": False}
            _ok = False
            for _attempt in range(3):
                try:
                    r = await cli.post(f"{BASE_URL}/chat/completions", headers=AUTH_HEADERS, json=probe)
                except (httpx.TimeoutException, httpx.ConnectError) as e:
                    if _attempt < 2:
                        await asyncio.sleep(1 * (_attempt + 1))
                        continue
                    yield ev({"type": "error", "text": f"出错了: {e}"})
                    return
                if r.status_code == 200:
                    _ok = True
                    break
                if r.status_code == 429:
                    await asyncio.sleep(1 * (_attempt + 1))
                    continue
                yield ev({"type": "error", "text": f"API 错误 ({r.status_code}): {r.text[:200]}"})
                return
            if not _ok:
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
                result, _status, _err = await _execute_tool_with_retry(fn_name, fn, fn_args, direction)

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
async def require_auth(request: Request,
                      authorization: Optional[str] = Header(None),
                      x_token: Optional[str] = Header(None)):
    """校验 /chat 访问令牌：支持 Authorization: Bearer <TOKEN> / X-Token / ?token= 三种方式。"""
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_token:
        token = x_token
    else:
        token = request.query_params.get("token")
    if not token or token != WEB_TOKEN:
        raise HTTPException(status_code=401, detail="未授权：缺少或错误的访问令牌")
    return True


# 注：/api/token 已移除——WEB_TOKEN 改为服务端渲染注入 index.html/jobs.html（__WEB_TOKEN__ 占位），
# 前端不再 fetch 该端点，本机任意进程无法再领到有效令牌（详见 serve_index / serve_jobs）。


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = "default"
    direction: Optional[str] = "数据科学"

class ChatResponse(BaseModel):
    reply: str
    session_id: str

@app.post("/chat")
async def chat_endpoint(req: ChatRequest, _: bool = Depends(require_auth)):
    reply = await process_chat(req.message, req.session_id, req.direction)
    # process_chat 在日报/求职报告分支返回 dict({"text","html"})，
    # ChatResponse.reply 为 str，需兼容以免 500
    if isinstance(reply, dict):
        reply = reply.get("text") or reply.get("html") or str(reply)
    return ChatResponse(reply=str(reply), session_id=req.session_id)


@app.post("/chat/stream")
async def chat_stream_endpoint(req: ChatRequest, _: bool = Depends(require_auth)):
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
    if not p.exists():
        return HTMLResponse("<h1>Hermes Sentinel</h1>")
    # 注入 WEB_TOKEN 到占位符，前端不再需要 fetch /api/token（避免本机任意进程领 token）
    html = p.read_text("utf-8").replace("__WEB_TOKEN__", WEB_TOKEN)
    return HTMLResponse(html)

@app.post("/reset", dependencies=[Depends(require_auth)])
async def reset(session_id: str = "default"):
    with get_db() as db:
        db.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))
    rc = _hot_redis_client()
    if rc is not None:
        try:
            rc.delete(_cache_key(session_id))
        except Exception:  # noqa: BLE001
            pass
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
                "WHERE role != 'tool' ORDER BY created_at DESC, rowid DESC LIMIT 20"
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

            # 近 7 日消息趋势（dashboard 折线图）
            from datetime import timedelta as _td
            trend_rows = db.execute(
                "SELECT date(created_at, 'localtime') AS d, COUNT(*) AS n FROM conversations "
                "WHERE role IN ('user','assistant') "
                "AND created_at >= datetime('now','localtime','-6 days') "
                "GROUP BY d ORDER BY d"
            ).fetchall()
            by_day = {r["d"]: r["n"] for r in trend_rows}
            _base = datetime.now()
            data["trend"] = [{"label": (_base - _td(days=i)).strftime("%m-%d"),
                              "value": by_day.get((_base - _td(days=i)).strftime("%Y-%m-%d"), 0)}
                             for i in range(6, -1, -1)]

            # 投递漏斗聚合（jobs 表与 conversations 同库）
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
                funnel_rows = db.execute(
                    "SELECT status, COUNT(*) AS n FROM jobs "
                    "WHERE status IN ('已投','笔试','面试','OC','挂') GROUP BY status"
                ).fetchall()
                fmap = {r["status"]: r["n"] for r in funnel_rows}
                data["funnel"] = [{"label": s, "value": fmap.get(s, 0)}
                                  for s in ("已投", "笔试", "面试", "OC", "挂")]

            # 工具调用健康摘要（来自 traces 表）
            agg = db.execute(
                "SELECT COUNT(*) AS n, SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errs, "
                "AVG(duration_ms) AS avg_ms FROM traces"
            ).fetchone()
            data["traces_summary"] = {
                "total": agg["n"] or 0,
                "errors": agg["errs"] or 0,
                "error_rate": round((agg["errs"] or 0) / (agg["n"] or 1), 3),
                "avg_ms": round(agg["avg_ms"] or 0, 1),
            }

            # 结构化用户画像（画像中心卡片展示/编辑）
            data["user_profile"] = load_profile()
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

    # Redis 热缓存状态（可选增强：未配置/不可达时为关闭态）
    rc = _hot_redis_client()
    data["cache"] = {
        "backend": "redis" if rc is not None else "sqlite",
        "hits": _hot_redis_hits,
        "misses": _hot_redis_misses,
    }
    if rc is not None:
        try:
            data["cache"]["used_memory"] = rc.info("memory").get("used_memory_human", "")
        except Exception:  # noqa: BLE001
            pass
    return data


@app.get("/api/dashboard")
async def dashboard_api():
    return _dashboard_data()


@app.get("/api/demo/summary")
async def demo_summary():
    """演示页用的轻量聚合数据：不返回 5000+ 岗位全文，避免首屏拖慢。"""
    with get_db() as db:
        has_jobs = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone() is not None
        if has_jobs:
            total_jobs = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            status_rows = db.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
            status_counts = {r["status"]: r["n"] for r in status_rows}
            source_rows = db.execute(
                "SELECT source, COUNT(*) AS n FROM jobs GROUP BY source ORDER BY n DESC LIMIT 8"
            ).fetchall()
            # 演示页 top 榜按公司去重：一家公司只占一席（展示整体多样性，
            # 避免"都是一家"的观感），公司内取其最高分岗位（SQLite 裸列
            # 与 MAX() 同用时会取自 max 行，符合预期）
            top_rows = db.execute(
                "SELECT company, title, city, MAX(match_score) AS ms, status, url, source FROM jobs "
                "WHERE user_hidden=0 "
                "GROUP BY company ORDER BY ms DESC LIMIT 8"
            ).fetchall()
            high_match = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE user_hidden=0 AND match_score>=75"
            ).fetchone()[0]
        else:
            # 全新安装、尚未扫岗位时也要能打开 Demo，而不是 500。
            total_jobs, status_counts, source_rows, top_rows, high_match = 0, {}, [], [], 0
        reports = db.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        conversations = db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        traces = db.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        avg_ms = db.execute("SELECT AVG(duration_ms) FROM traces").fetchone()[0] or 0
        latest_report = db.execute(
            "SELECT created_at FROM reports ORDER BY id DESC LIMIT 1"
        ).fetchone()
    applied = sum(status_counts.get(s, 0) for s in ("已投", "笔试", "面试", "OC"))
    return {
        "generated_at": _now_local(),
        "jobs": {
            "total": total_jobs,
            "high_match": high_match,
            "status_counts": status_counts,
            "applied": applied,
            "tests": status_counts.get("笔试", 0),
            "interviews": status_counts.get("面试", 0),
            "offers": status_counts.get("OC", 0),
        },
        "sources": [{"name": r["source"], "count": r["n"]} for r in source_rows],
        "top_jobs": [
            {
                "company": r["company"],
                "title": r["title"],
                "city": r["city"],
                "score": r["ms"],
                "status": r["status"],
                "url": r["url"],
                "source": r["source"],
            }
            for r in top_rows
        ],
        "activity": {
            "reports": reports,
            "conversations": conversations,
            "traces": traces,
            "avg_trace_ms": round(float(avg_ms), 1),
            "latest_report": latest_report["created_at"] if latest_report else None,
        },
    }


@app.get("/api/dashboard/report/{report_id}")
async def dashboard_report(report_id: int):
    with get_db() as db:
        row = db.execute(
            "SELECT html, report_type, created_at FROM reports WHERE id=?", (report_id,)
        ).fetchone()
    if not row:
        return HTMLResponse("<h1>未找到该报告</h1>", status_code=404)
    return HTMLResponse(row["html"] or "<p>（无 HTML 内容）</p>")


@app.get("/api/traces", dependencies=[Depends(require_auth)])
async def traces_api(session_id: str = None, status: str = None,
                     tool: str = None, limit: int = 200):
    """可观测性：查询工具调用 trace，支持按 session_id / status / tool_name 过滤 + 汇总统计。"""
    clauses, params = [], []
    if session_id:
        clauses.append("session_id = ?"); params.append(session_id)
    if status:
        clauses.append("status = ?"); params.append(status)
    if tool:
        clauses.append("tool_name = ?"); params.append(tool)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db() as db:
        rows = db.execute(
            "SELECT id, session_id, tool_name, tool_args, tool_result, status, "
            "error_msg, duration_ms, created_at FROM traces "
            f"{where} ORDER BY id DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        recs = [dict(r) for r in rows]
        agg = db.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errs, "
            "AVG(duration_ms) AS avg_ms, MAX(duration_ms) AS max_ms "
            f"FROM traces {where}",
            params,
        ).fetchone()
    return {
        "count": len(recs),
        "summary": {
            "total": agg["n"] or 0,
            "errors": agg["errs"] or 0,
            "error_rate": round((agg["errs"] or 0) / (agg["n"] or 1), 3),
            "avg_ms": round(agg["avg_ms"] or 0, 1),
            "max_ms": round(agg["max_ms"] or 0, 1),
        },
        "traces": recs,
    }


@app.get("/traces", response_class=HTMLResponse)
async def traces_page():
    p = static_dir / "traces.html"
    if not p.exists():
        return HTMLResponse("<h1>可观测性 Trace</h1>")
    html = p.read_text("utf-8").replace("__WEB_TOKEN__", WEB_TOKEN)
    return HTMLResponse(html)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    p = static_dir / "dashboard.html"
    # 注入 WEB_TOKEN：画像编辑（POST /api/user-profile）需要鉴权
    return HTMLResponse(
        p.read_text("utf-8").replace("__WEB_TOKEN__", WEB_TOKEN) if p.exists()
        else "<h1>Hermes Dashboard</h1>"
    )


@app.get("/demo", response_class=HTMLResponse)
async def demo_page():
    p = static_dir / "demo.html"
    return HTMLResponse(p.read_text("utf-8") if p.exists() else "<h1>Hermes Sentinel Demo</h1>")


# ============================================================
# 秋招哨兵（岗位卡片流 + 投递追踪 + 画像配置）
# ============================================================
@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page():
    p = static_dir / "jobs.html"
    if not p.exists():
        return HTMLResponse("<h1>岗位哨兵</h1>")
    # 注入 WEB_TOKEN 到占位符，前端不再需要 fetch /api/token
    html = p.read_text("utf-8").replace("__WEB_TOKEN__", WEB_TOKEN)
    return HTMLResponse(html)


@app.get("/deliveries", response_class=HTMLResponse)
async def deliveries_page():
    p = static_dir / "deliveries.html"
    if not p.exists():
        return HTMLResponse("<h1>投递进度</h1>")
    # 注入 WEB_TOKEN 到占位符，前端不再需要 fetch /api/token
    html = p.read_text("utf-8").replace("__WEB_TOKEN__", WEB_TOKEN)
    return HTMLResponse(html)


@app.get("/api/jobs")
async def api_jobs(
    status: str = "",
    batch: str = "",
    city: str = "",
    min_score: int = 0,
    max_score: int = 0,
    source: str = "",
    nature: str = "",
    enterprise_type: str = "",
    exam_free: str = "",
    sort: str = "score",
    urgent_days: int = 0,
    include_hidden: bool = False,
    only_region: bool = False,
    edu_filter: bool = False,
    profile_focus: bool = False,
    school_friendly: bool = False,
):
    """岗位列表。

    新增筛选：source（来源）/ nature（单位性质）/ max_score（配合分档）/
    sort（score|deadline|new）/ urgent_days（只看 N 天内截止）/
    include_hidden（含「标记不投」的岗位）。
    地区聚焦(only_region) 与 学历门槛(edu_filter) 默认关闭——全量可见，
    由看板开关触发，按画像做语义过滤（见 job_sentinel.store）。
    """
    from pathlib import Path as _P
    from job_sentinel.store import get_db as js_get_db, get_jobs
    from job_sentinel.profile import load_profile
    from job_sentinel.matcher import days_to_deadline, success_signal, suggest_fill

    prof_path = _P(__file__).parent / "job_sentinel" / "profile.json"
    profile = load_profile(prof_path)
    # 仅当开关开启时才把画像的地区作为过滤条件，否则全量可见
    regions = profile.get("regions") if only_region else None
    # 学历门槛：开关 ON 时按**画像当前值**过滤（本科可投 / 需硕士）；
    # 画像为「不限」则不过滤（此时开关无害地保持 ON，前端会把按钮置灰提示）。
    # 2026-09-02 修正：此前后端硬编码「本科可投」，画像里的学历门槛是死字段。
    edu_thr = (profile.get("edu_threshold") or "不限") if edu_filter else None
    if edu_thr == "不限":
        edu_thr = None
    # 最低薪资：画像 salary_min>0 时剔除可解析出月薪且低于门槛的岗（面议/空不剔除）
    salary_min = int(profile.get("salary_min") or 0)
    # 排除词：看板层硬过滤（此前只在扫描入库时生效，改了词已入库的岗不动）
    exclude_kw = profile.get("exclude_keywords") or []
    # 画像聚焦：开启后按画像的「求职方向 / 专业 / 招聘类型」软过滤岗位集
    prof_focus = None
    if profile_focus:
        prof_focus = {
            "directions": profile.get("directions") or [],
            "major": profile.get("major") or [],
            "recruit_type": profile.get("recruit_type") or [],
        }
    with js_get_db(str(DB_PATH)) as c:
        jobs = get_jobs(c, {
            "status": status, "batch": batch, "city": city,
            "min_score": min_score, "max_score": max_score,
            "source": source, "nature": nature, "sort": sort,
            "enterprise_type": enterprise_type, "exam_free": exam_free,
            "include_hidden": include_hidden,
            "regions": regions,
            "edu_threshold": edu_thr,
            "salary_min": salary_min,
            "exclude_keywords": exclude_kw,
            "profile_focus": bool(prof_focus), "_profile": prof_focus,
        })
    # 「N 天内截止」需按日期解析过滤，SQL 里 deadline 是混合格式文本做不了
    if urgent_days > 0:
        picked = []
        for j in jobs:
            d = days_to_deadline(j)
            if d is not None and 0 <= d <= urgent_days:
                picked.append(j)
        jobs = picked

    # 补全建议：仅对 industry/major 为空的岗，按标题启发式给「建议方向」，不落库
    out = []
    # 成功率信号：三路可溯源信号合成（学历文本 + 公司情报实搜 + 自身历史回复率）
    researched = {}
    try:
        with get_db() as db:
            for r in db.execute("SELECT company, intel_json FROM company_intel").fetchall():
                try:
                    researched[r["company"]] = json.loads(r["intel_json"])
                except Exception:  # noqa: BLE001
                    pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"公司情报读取失败（成功率信号降级）: {e}")
    resp_by_type = {}
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT enterprise_type AS g, status, COUNT(*) AS n FROM jobs "
                "WHERE status IN ('已投','笔试','面试','OC','挂') "
                "AND TRIM(COALESCE(enterprise_type,'')) != '' "
                "GROUP BY enterprise_type, status"
            ).fetchall()
        agg: dict = {}
        for r in rows:
            g = agg.setdefault(r["g"], {"applied": 0, "responded": 0})
            g["applied"] += r["n"]
            if r["status"] in ("笔试", "面试", "OC", "挂"):
                g["responded"] += r["n"]
        resp_by_type = {g: {"applied": v["applied"],
                            "response_rate": round(v["responded"] / v["applied"], 2) if v["applied"] else 0}
                        for g, v in agg.items()}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"回复率统计读取失败（成功率信号降级）: {e}")

    for j in jobs:
        d = j.to_dict()
        d["success"] = success_signal(j, researched, resp_by_type)
        sug = suggest_fill(j)
        if sug:
            d["suggest"] = sug
        out.append(d)

    # 只看学历友好（成功率信号非"低"），并支持按成功率信号排序
    if school_friendly:
        out = [d for d in out if (d.get("success") or {}).get("level") != "低"]
    if sort == "success":
        out.sort(key=lambda d: (
            (d.get("success") or {}).get("add", 0),
            d.get("match_score", 0),
        ), reverse=True)
    return {"jobs": out, "count": len(out)}


@app.get("/api/deliveries")
async def api_deliveries():
    """投递进度：返回所有已产生投递行为（已投/笔试/面试/OC/挂）的岗位。

    与匹配分无关——投递是真实动作，永不因分数被过滤。
    排序：阶段倒序（OC>面试>笔试>已投>挂） + 投递时间倒序 + 匹配分倒序。
    """
    from job_sentinel.models import Job
    from job_sentinel.store import get_db as js_get_db
    RANK = {"OC": 5, "面试": 4, "笔试": 3, "已投": 2, "挂": 1}
    with js_get_db(str(DB_PATH)) as c:
        rows = c.execute(
            "SELECT * FROM jobs WHERE status IN ('已投','笔试','面试','OC','挂') "
            "ORDER BY CASE status WHEN 'OC' THEN 5 WHEN '面试' THEN 4 WHEN '笔试' THEN 3 "
            "WHEN '已投' THEN 2 WHEN '挂' THEN 1 ELSE 0 END DESC, "
            "first_seen DESC, match_score DESC"
        ).fetchall()
        jobs = [Job.from_dict(dict(r)) for r in rows]
    out = []
    for j in jobs:
        d = j.to_dict()
        d["stage_rank"] = RANK.get(j.status, 0)
        out.append(d)
    counts = {}
    for j in jobs:
        counts[j.status] = counts.get(j.status, 0) + 1
    ongoing = counts.get("已投", 0) + counts.get("笔试", 0) + counts.get("面试", 0)
    return {"jobs": out, "count": len(out), "counts": counts,
            "ongoing": ongoing, "statuses": ["已投", "笔试", "面试", "OC", "挂"]}


@app.post("/api/jobs/rescore", dependencies=[Depends(require_auth)])
async def api_jobs_rescore():
    """用当前画像+打分规则重算全库分数（秒级，不重抓）。"""
    from pathlib import Path as _P
    from job_sentinel.store import get_db as js_get_db, rescore_all
    from job_sentinel.profile import load_profile
    prof_path = _P(__file__).parent / "job_sentinel" / "profile.json"
    try:
        profile = load_profile(prof_path)   # 经 load_profile 做字段迁移（旧方向标签→新类别）
        with js_get_db(str(DB_PATH)) as c:
            res = rescore_all(c, profile)
    except Exception as e:  # noqa: BLE001
        logger.error(f"重算分数失败: {e}")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
    return {"ok": True, **res}


@app.post("/api/jobs/{raw_hash}/status", dependencies=[Depends(require_auth)])
async def api_job_status(raw_hash: str, request: Request):
    data = await request.json()
    status = (data or {}).get("status", "")
    from job_sentinel.store import get_db as js_get_db, update_status
    with js_get_db(str(DB_PATH)) as c:
        if not update_status(c, raw_hash, status):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": f"非法状态: {status!r}"},
            )
    return {"ok": True, "raw_hash": raw_hash, "status": status}


@app.post("/api/jobs/{raw_hash}/hide", dependencies=[Depends(require_auth)])
async def api_job_hide(raw_hash: str):
    """标记不投（看板隐藏，非删除）。"""
    from job_sentinel.store import get_db as js_get_db, set_hidden
    with js_get_db(str(DB_PATH)) as c:
        set_hidden(c, raw_hash, True)
    return {"ok": True, "raw_hash": raw_hash, "hidden": True}


@app.post("/api/jobs/{raw_hash}/unhide", dependencies=[Depends(require_auth)])
async def api_job_unhide(raw_hash: str):
    """恢复「标记不投」的岗位。"""
    from job_sentinel.store import get_db as js_get_db, set_hidden
    with js_get_db(str(DB_PATH)) as c:
        set_hidden(c, raw_hash, False)
    return {"ok": True, "raw_hash": raw_hash, "hidden": False}


@app.post("/api/jobs/{raw_hash}/adopt", dependencies=[Depends(require_auth)])
async def api_job_adopt(raw_hash: str, request: Request):
    """采纳「补全建议」：把推断的 industry/major 写回岗位记录（仅补空字段），
    并立即用新字段重算该岗分数，让方向匹配更准。
    """
    from job_sentinel.store import get_db as js_get_db, adopt_fill, rescore_all
    from job_sentinel.profile import load_profile
    from pathlib import Path as _P
    data = await request.json()
    fill = {k: v for k, v in (data or {}).items() if k in ("industry", "major")}
    prof_path = _P(__file__).parent / "job_sentinel" / "profile.json"
    with js_get_db(str(DB_PATH)) as c:
        ok = adopt_fill(c, raw_hash, fill)
        if ok:
            rescore_all(c, load_profile(prof_path))
    return {"ok": ok, "raw_hash": raw_hash}


@app.get("/api/jobs/hidden_count")
async def api_hidden_count():
    """「标记不投」的岗位数量（看板恢复条用）。"""
    from job_sentinel.store import get_db as js_get_db
    with js_get_db(str(DB_PATH)) as c:
        n = c.execute("SELECT COUNT(*) n FROM jobs WHERE user_hidden=1").fetchone()["n"]
    return {"count": n}


@app.post("/api/scan", dependencies=[Depends(require_auth)])
async def api_scan():
    """手动触发哨兵扫描：重抓各源（含国聘全国岗）并入库，返回统计。

    前端「扫岗位」按钮直连此接口，不走 /chat 的 LLM 路由（更快更稳）。
    走 _scan_once：工作线程执行 + 服务端并发锁（409 拒绝并发扫描）。
    """
    try:
        res = await _scan_once()
    except RuntimeError as e:  # 并发扫描
        return JSONResponse(status_code=409, content={"ok": False, "error": str(e)})
    except Exception as e:  # noqa: BLE001
        logger.error(f"哨兵扫描失败: {e}")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
    return {
        "ok": True,
        "collected": res.get("collected", 0),
        "kept": res.get("kept", 0),
        "new": res.get("new", 0),
    }


@app.post("/api/extension/apply")
async def api_extension_apply(request: Request):
    """浏览器扩展上报：网申提交后把公司+岗位写入投递记录(状态=已投)。"""
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "", 1).strip()
    if token != EXT_TOKEN:
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
    data = await request.json()
    company = (data or {}).get("company", "").strip()
    title = (data or {}).get("title", "").strip()
    url = (data or {}).get("url", "").strip()
    note = (data or {}).get("note", "").strip()
    from job_sentinel.store import get_db as js_get_db, upsert_job, update_status
    from job_sentinel.email_sync import (
        reconcile_company, normalize_company, find_extension_row, _advance_ok,
    )
    from job_sentinel.models import Job
    with js_get_db(str(DB_PATH)) as c:
        c.row_factory = sqlite3.Row
        pool = [r["company"] for r in c.execute("SELECT DISTINCT company FROM jobs")]
        canon, score, kind = reconcile_company(company, pool, url)
        # ① 命中库内已有公司（含别名/域名回推）→ 回写漏斗，即"投到已跟踪公司"同步
        if canon and kind in ("pool", "alias"):
            # 先按 公司+岗位 匹配（投"软件工程师"不能改到"游戏客户端"行上），再退回该公司最高分行
            row = None
            if title:
                row = c.execute(
                    """SELECT raw_hash,url,status FROM jobs
                       WHERE company=? AND (title LIKE ? OR ? LIKE '%'||?||'%')
                       ORDER BY match_score DESC LIMIT 1""",
                    (canon, f"%{title}%", title, title),
                ).fetchone()
            if row is None:
                row = c.execute(
                    "SELECT raw_hash,url,status FROM jobs WHERE company=? ORDER BY match_score DESC LIMIT 1",
                    (canon,),
                ).fetchone()
            if row:
                final = "已投" if _advance_ok(row["status"], "已投") else row["status"]
                update_status(c, row["raw_hash"], final)
                # 已有扩展自有投递行（公司名口径不同）→ 同步状态，避免留下旧的"已投"行
                ext = find_extension_row(c, [company, canon])
                if ext is not None and ext["raw_hash"] != row["raw_hash"]:
                    ext_final = "已投" if _advance_ok(ext["status"], "已投") else ext["status"]
                    update_status(c, ext["raw_hash"], ext_final)
                return {"ok": True, "company": canon, "matched": True,
                        "status": final, "job_url": row["url"], "via": kind}
        # ② 扩展自有投递：按归一公司查重（多口径），重复捕获只更新不新建
        norm = normalize_company(company)
        if norm or canon:
            exist = find_extension_row(c, [norm, canon])
            if exist:
                final = "已投" if _advance_ok(exist["status"], "已投") else exist["status"]
                update_status(c, exist["raw_hash"], final)
                c.execute(
                    "UPDATE jobs SET title=?, url=?, match_reason=? WHERE raw_hash=?",
                    (title or "网申投递", url, note or "浏览器扩展网申记录", exist["raw_hash"]),
                )
                c.commit()  # update_status 只提交它自己的语句；这条 UPDATE 不提交会随连接回滚
                return {"ok": True, "company": exist["company"], "matched": True,
                        "status": final, "job_url": url, "via": "dedup"}
        # ③ 新建：归一后是真实新公司 / 域名推断 / 兜底未知
        final_company = canon if kind in ("domain", "new") else (norm or "未知公司")
        job = Job(
            company=final_company,
            title=title or "网申投递",
            city="", education="", batch="", deadline="",
            url=url, link_type="源详情页", source="浏览器扩展",
            nature="", industry="", major="", school_level="", salary="",
            first_seen=datetime.now().strftime("%Y-%m-%d"),
            match_score=0, match_reason=note or "浏览器扩展网申记录",
            status="已投",
        )
        upsert_job(c, job)
        # 库外新公司：自动搜 JD/要求 + 匹配度补全，让看板上能看到公司信息
        enriched_info = None
        try:
            q = f"{job.company} {job.title} 招聘 岗位职责 任职要求".strip()
            md = await web_search(q, 5)
            from job_sentinel.enrich import enrich_job_from_search
            enriched_info = enrich_job_from_search(
                job.raw_hash, job.company, job.title, md, str(DB_PATH)
            )
        except Exception as e:
            logger.warning(f"补全公司 {job.company} 失败: {e}")
        resp = {"ok": True, "company": job.company, "matched": kind != "none",
                "status": "已投", "job_url": url, "new": True, "via": kind}
        if enriched_info:
            resp["enriched"] = enriched_info
        return resp


@app.post("/api/extension/deliveries")
async def api_extension_deliveries(request: Request):
    """浏览器扩展上报：从「我的投递」列表页批量抓取 (公司, 状态) 回写看板。"""
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "", 1).strip()
    if token != EXT_TOKEN:
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
    data = await request.json()
    rows = (data or {}).get("rows", []) or []
    url = (data or {}).get("url", "") or ""
    if not isinstance(rows, list):
        rows = []
    from job_sentinel.email_sync import parse_delivery_list
    result = parse_delivery_list(rows, str(DB_PATH), url)
    # 补全库外新建的真实投递公司：自动搜 JD/要求 + 复用 matcher 算匹配度
    enriched = []
    for item in result.get("created", [])[:8]:  # 一次最多补全前 8 家，避免列表页过大卡接口
        try:
            q = f"{item['company']} {item.get('title', '')} 招聘 岗位职责 任职要求".strip()
            md = await web_search(q, 5)
            from job_sentinel.enrich import enrich_job_from_search
            enr = enrich_job_from_search(
                item["raw_hash"], item["company"], item.get("title", ""), md, str(DB_PATH)
            )
            enriched.append(enr)
        except Exception as e:
            logger.warning(f"补全公司 {item.get('company')} 失败: {e}")
    result["enriched"] = enriched
    return result


@app.get("/api/profile")
async def api_profile_get():
    from job_sentinel.profile import load_profile
    from pathlib import Path
    base = Path(__file__).parent
    return load_profile(base / "job_sentinel" / "profile.json")


@app.post("/api/profile", dependencies=[Depends(require_auth)])
async def api_profile_post(request: Request):
    data = await request.json()
    from job_sentinel.profile import save_profile
    from pathlib import Path
    base = Path(__file__).parent
    return save_profile(data or {}, base / "job_sentinel" / "profile.json")


@app.get("/api/profile/options")
async def api_profile_options():
    """聚合岗位库真实分布，给画像设置页提供「可勾选」选项（城市/公司/性质/方向…）。"""
    from job_sentinel.store import get_db as js_get_db, get_profile_options
    with js_get_db(str(DB_PATH)) as c:
        return get_profile_options(c)


@app.get("/api/profile/suggest")
async def api_profile_suggest(kw: str = ""):
    """关键词/专业 → 推荐相关方向类别。前端「写AI开发→推荐相关方向」用。"""
    from job_sentinel.matcher import suggest_directions
    return {"directions": suggest_directions(kw)}


# ── 公司情报 API：投递前调研（首次现场生成 10~40 秒，之后 7 天缓存秒回） ──
@app.get("/api/company-intel", dependencies=[Depends(require_auth)])
async def api_company_intel(company: str, refresh: bool = False, focus: str = ""):
    company = (company or "").strip()
    if not company:
        raise HTTPException(status_code=400, detail="缺少 company 参数")
    cached = _load_company_intel(company)
    if cached and not refresh and not _intel_stale(cached):
        return {"company": company, "intel": cached["intel"],
                "searched_at": cached["searched_at"], "cached": True}
    await company_research(company, focus)
    fresh = _load_company_intel(company)
    return {"company": company, "intel": (fresh or {}).get("intel", {}),
            "searched_at": (fresh or {}).get("searched_at", ""), "cached": False}


# ── 投递回复率分析：把"投了没回应"变成可视化的分组转化率 ──
@app.get("/api/response-analysis")
async def api_response_analysis():
    """按 企业性质 / 学历要求 / 来源 三维度统计投递转化率。

    口径：applied = 已投+笔试+面试+OC+挂（真实投出）；responded = 笔试/面试/OC/挂
    （拒信也算回应，石沉大海不算）；response_rate = responded / applied。
    每组 applied < 3 标 low_confidence——样本不足时不编造结论。
    """
    def _collect(col: str) -> list[dict]:
        with get_db() as db:
            rows = db.execute(
                f"SELECT {col} AS g, status, COUNT(*) AS n FROM jobs "
                "WHERE status IN ('已投','笔试','面试','OC','挂') AND TRIM(COALESCE(g,'')) != '' "
                f"GROUP BY {col}, status"
            ).fetchall()
        agg: dict = {}
        for r in rows:
            g = agg.setdefault(r["g"], {"applied": 0, "responded": 0, "interview": 0,
                                        "oc": 0, "rejected": 0, "pending": 0})
            s, n = r["status"], r["n"]
            g["applied"] += n
            if s in ("笔试", "面试", "OC", "挂"):
                g["responded"] += n
            if s in ("面试", "OC"):
                g["interview"] += n
            if s == "OC":
                g["oc"] += n
            if s == "挂":
                g["rejected"] += n
        out = []
        for g, v in agg.items():
            v["g"] = g
            v["response_rate"] = round(v["responded"] / v["applied"], 2) if v["applied"] else 0
            v["low_confidence"] = v["applied"] < 3
            out.append(v)
        out.sort(key=lambda x: (-x["applied"], x["g"]))
        return out

    if not all(_has_table(t) for t in ("jobs",)):
        return {"dimensions": [], "note": "岗位库尚未初始化"}
    return {"dimensions": [
        {"dim": "enterprise_type", "label": "企业性质", "groups": _collect("enterprise_type")},
        {"dim": "education", "label": "学历要求", "groups": _collect("education")},
        {"dim": "source", "label": "投递来源", "groups": _collect("source")},
    ]}


def _has_table(name: str) -> bool:
    with get_db() as db:
        return db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None


# ── 用户画像（user_profile 表）读写：画像中心 UI 用 ──
@app.get("/api/user-profile")
async def api_user_profile_get():
    """读结构化用户画像 + 可编辑字段白名单（画像中心卡片用）。"""
    return {"profile": load_profile(), "allowed_keys": sorted(_PROFILE_ALLOWED_KEYS)}


@app.post("/api/user-profile", dependencies=[Depends(require_auth)])
async def api_user_profile_post(request: Request):
    """批量更新结构化用户画像（仅白名单字段；空值跳过并说明原因）。"""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
    updates = (data or {}).get("updates")
    if not isinstance(updates, dict) or not updates:
        raise HTTPException(status_code=400, detail="缺少 updates 字段（{字段: 值} 字典）")
    applied, rejected = {}, []
    for k, v in updates.items():
        k = str(k).strip()
        v = str(v).strip()
        if k not in _PROFILE_ALLOWED_KEYS:
            rejected.append(f"{k}（不在白名单）")
            continue
        if not v:
            rejected.append(f"{k}（空值）")
            continue
        update_profile(k, v)
        applied[k] = v
    return {"ok": True, "applied": applied, "rejected": rejected}


# ── 聊天历史：index 页刷新后恢复对话视图 ──
@app.get("/api/history", dependencies=[Depends(require_auth)])
async def api_history(session_id: str = "default", limit: int = 60):
    """返回指定会话的历史消息（不含 tool 中间产物），按时间正序渲染。"""
    limit = max(1, min(limit, 200))
    return {"session_id": session_id, "messages": load_history(session_id, limit)}


# ── 岗位轻量汇总：jobs 页统计角标专用（不回岗位明细，避免全量拉取放大） ──
@app.get("/api/jobs/stats")
async def api_jobs_stats():
    """分档计数 / 状态分布 / 来源分布 / 免笔试与官方直链计数。"""
    from job_sentinel.store import get_db as js_get_db, job_stats
    with js_get_db(str(DB_PATH)) as c:
        base = job_stats(c)
        n = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        tiers = {
            "hi": n("SELECT COUNT(*) FROM jobs WHERE match_score >= 75"),
            "mid": n("SELECT COUNT(*) FROM jobs WHERE match_score >= 60 AND match_score < 75"),
            "ok": n("SELECT COUNT(*) FROM jobs WHERE match_score >= 45 AND match_score < 60"),
            "obs": n("SELECT COUNT(*) FROM jobs WHERE match_score < 45"),
        }
        sources = {r["source"]: r["n"] for r in
                   c.execute("SELECT source, COUNT(*) n FROM jobs GROUP BY source ORDER BY n DESC LIMIT 10")}
        exam_free = n("SELECT COUNT(*) FROM jobs WHERE exam_free = '免笔试'")
        official_links = n("SELECT COUNT(*) FROM jobs WHERE link_type IN ('官方网申', '简章详情')")
    return {
        "total": base["total"],
        "by_status": base["by_status"],
        "new": base["new"],
        "tiers": tiers,
        "sources": sources,
        "exam_free": exam_free,
        "official_links": official_links,
    }


# ============================================================
# Caspian 多渠道路由
# ============================================================
async def start_caspian():
    global _caspian_client
    if not CASPIAN_API_KEY:
        print("⚠️ Caspian 未配置")
        return
    try:
        from caspian_sdk import CommClient
        from hermes_caspian.adapter import HermesCaspianAdapter

        client = CommClient()
        client.connect_email(display_name="Hermes")
        adapter = HermesCaspianAdapter(client, process_chat)
        _caspian_client = client  # 供启动扫描推送等主动发信场景复用
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
            # 断线自愈：listen() 抛异常（网络抖动/SDK 内部错）后 5 秒重连，
            # 否则邮件渠道会永久离线。关停时由 _SHUTDOWN 事件跳出，不再重连。
            fails = 0
            while not _SHUTDOWN.is_set():
                try:
                    client.listen()  # 同步阻塞死循环，在守护线程里跑，绝不阻塞主循环
                    fails = 0
                except Exception as e:
                    fails += 1
                    logger.error(f"Caspian listen 异常（第 {fails} 次），5 秒后重连: {e}")
                # 用 wait 而非 sleep：关停时能立刻醒来退出，不用干等 5 秒
                if _SHUTDOWN.wait(5):
                    break

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
║  🔧 扩展Token: {EXT_TOKEN[:8]}…（完整值见下行 / 浏览器扩展设置里填）      ║
║     {EXT_TOKEN}  ║
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
