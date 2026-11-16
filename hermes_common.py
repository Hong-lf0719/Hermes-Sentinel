"""Hermes 基础层（2026-11-16 从 server.py 拆出）——配置 / SQLite / 鉴权 / 缓存 / LLM 助手。

拆分原则：本模块不 import server，只被 server 与各功能层 import，
杜绝循环依赖。server.py 保留：app、聊天管线、工具注册、扫描、看板、Caspian。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import certifi
import httpx
from dotenv import load_dotenv
from fastapi import Header, HTTPException, Request

# .env 必须在本模块读任何 os.getenv 之前加载——server.py 与所有分层模块
# 的配置常量都从这里取值（2026-11-16：原先 load_dotenv 在 server.py 里
# 排在 import hermes_common 之后，导致拆分后令牌/密钥回落到随机默认值）。
load_dotenv(Path(__file__).parent / ".env")

# ============================================================
# 配置（.env 可覆盖；令牌默认随机生成，避免硬编码默认被猜解）
# ============================================================
logger = logging.getLogger("hermes")

API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
BASE_URL = "https://api.deepseek.com/v1"
MODEL = "deepseek-v4-flash"
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

# 仅默认监听本机回环地址：避免同网段任意设备访问本服务。
# 关键点：execute_command 工具可触发本机命令执行，0.0.0.0 暴露等同于把受限 RCE 暴露给局域网。
# Docker/云平台部署时通过环境变量 HOST=0.0.0.0 覆盖（容器内绑 127.0.0.1 会导致端口映射失效）。
HOST = os.getenv("HOST", "127.0.0.1")
# 端口优先读环境变量 PORT（云端平台动态分配端口），本地默认 9866
PORT = int(os.getenv("PORT", 9866))

CASPIAN_API_KEY = os.getenv("CASPIAN_API_KEY", "")
DB_PATH = Path(__file__).parent / "hermes.db"
# 浏览器扩展上报鉴权 token（可在 .env 覆盖；默认仅供本机使用）
EXT_TOKEN = os.getenv("EXT_TOKEN", secrets.token_hex(16))
# Web UI 调用 /chat 的访问令牌。默认随机生成（启动即打印），避免硬编码默认值被猜解。
# 由服务端在渲染 index.html/jobs.html 时注入（__WEB_TOKEN__ 占位），前端不再 fetch /api/token，
# 本机任意进程无法再领到有效令牌；配合 127.0.0.1 绑定确保外部不可读。
WEB_TOKEN = os.getenv("WEB_TOKEN", secrets.token_hex(16))
# GitHub API 可选令牌：填入可把匿名 60 次/小时 提额到 5000 次/小时，缓解限流
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
KB_PATH = Path(__file__).parent / "kb"           # 长期知识库（RAG）持久化目录
# 可观测性：单次工具调用耗时超过该阈值（毫秒）即触发自我诊断（即使没报错，也可能卡顿/降级）
SLOW_TRACE_MS = int(os.getenv("SLOW_TRACE_MS", 8000))

# 全局关停信号：Caspian 守护线程 / 定时调度器据此退出
_SHUTDOWN = threading.Event()


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
    因此所有入库时间统一显式传本参数值，不再依赖 SQL 端默认值。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_profile() -> dict:
    """读取结构化用户画像（user_profile KV 表，key -> value）。"""
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


# ============================================================
# 通用 TTL 内存缓存（外部 API 限流保护）
# ============================================================
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


# ============================================================
# LLM 结构化输出助手
# ============================================================
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


# ============================================================
# Web API 鉴权
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
