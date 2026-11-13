"""
MCP 工具客户端 — 让 Hermes 作为 Host 动态发现并调用 MCP Server 的工具

职责：
  1. 以子进程方式启动 mcp_server.py（stdio 传输），保持持久连接
  2. 启动时 tools/list 拉取工具清单，缓存到 self._tools
  3. 提供 to_openai_tools() 把 MCP 工具 schema 转成 OpenAI function calling 格式
  4. 提供 call() 执行 tools/call，拼接所有文本片段返回

设计：
  连接失败时 is_ready() 返回 False，Hermes 自动降级回本地工具（容错）。
  call() 带超时保护：MCP 子进程若卡死不会拖垮主对话。
"""

import sys

# Windows GBK 控制台打印 emoji 会崩溃，统一切 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import os
import asyncio
import logging
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger("hermes")

# 单次 MCP 工具调用的最长耗时（秒）；外部 API 一般 <15s，留足余量
MCP_CALL_TIMEOUT = 30.0


class MCPToolClient:
    """连接 mcp_server.py，动态发现工具并调用。"""

    def __init__(self, server_script: str = "mcp_server.py"):
        # mcp_server.py 与本文件同目录（Hermes 项目根目录）
        self._cwd = str(Path(__file__).parent)
        self._server_script = server_script
        self._cm = None          # stdio_client 的 context manager
        self._read = None        # 读流
        self._write = None       # 写流
        self._session = None     # ClientSession
        self._tools = {}         # name -> MCP Tool 对象
        # 健康标记：调用超时 / 传输异常后置 True，使 is_ready() 立刻返回 False。
        # 否则子进程已经死掉、_tools 缓存还在，Hermes 会一直去撞死连接而不降级。
        self._broken = False
        self._healing = False

    async def start(self):
        """建立连接、握手、拉取工具清单。失败抛异常，由调用方降级。"""
        # 用主进程同款解释器启动子进程，确保子进程也找得到 mcp 包
        # （避免 PATH 上的 `python` 指向没装 mcp 的解释器导致静默降级）
        params = StdioServerParameters(
            command=sys.executable,
            args=[self._server_script],
            cwd=self._cwd,
        )
        # 手动管理 context manager，保持 stdio_client 内部 task group 存活
        self._cm = stdio_client(params)
        self._read, self._write = await self._cm.__aenter__()
        self._session = ClientSession(self._read, self._write)
        await self._session.__aenter__()
        await self._session.initialize()

        result = await self._session.list_tools()
        self._tools = {t.name: t for t in result.tools}
        logger.info(f"[MCP] 工具 Server 已连接，发现 {len(self._tools)} 个工具: {list(self._tools.keys())}")

    async def stop(self):
        """关闭连接（session → stdio_client），任一环节异常都不影响主进程退出。"""
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"[MCP] 关闭 session 异常: {e}")
            finally:
                self._session = None
        if self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"[MCP] 关闭连接异常: {e}")
            finally:
                self._cm = None
        self._tools = {}

    def is_ready(self) -> bool:
        """是否已成功连接、拿到工具清单，且连接未被判定为坏掉。"""
        return bool(self._tools) and not self._broken and self._session is not None

    async def _heal(self):
        """后台重建连接（超时/传输异常后触发）。失败就保持降级，不抛给主对话。

        _healing 做并发去重：一轮对话里多个工具同时超时只重连一次。
        """
        if self._healing:
            return
        self._healing = True
        try:
            await self.stop()
            await self.start()
            self._broken = False
            logger.info("[MCP] 连接已自愈重建")
        except Exception as e:
            self._broken = True
            logger.warning(f"[MCP] 自愈重连失败，继续走本地工具降级: {e}")
        finally:
            self._healing = False

    def has(self, name: str) -> bool:
        return name in self._tools

    def to_openai_tools(self) -> list[dict]:
        """把 MCP 工具 schema 转成 OpenAI function calling 格式。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.inputSchema or {"type": "object", "properties": {}},
                },
            }
            for t in self._tools.values()
        ]

    async def call(self, name: str, arguments: dict, timeout: float = MCP_CALL_TIMEOUT) -> str:
        """执行工具，拼接所有 text 内容返回。带超时保护，避免子进程卡死拖垮主对话。

        超时或传输异常后：标记连接不健康（后续调用直接降级本地工具）+ 后台异步自愈重连。
        旧版只抛 TimeoutError 但不复位状态，子进程一旦卡死就会每次都等满 30s。
        """
        if self._session is None:
            raise RuntimeError("MCP 未连接")
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(name, arguments or {}), timeout=timeout
            )
        except asyncio.TimeoutError:
            self._broken = True
            asyncio.create_task(self._heal())
            raise TimeoutError(f"MCP 工具 {name} 调用超时（{timeout}s），已中断避免阻塞对话")
        except Exception:
            # 传输层断开（子进程退出/管道关闭）同样要复位，否则后续全部卡住
            self._broken = True
            asyncio.create_task(self._heal())
            raise
        self._broken = False   # 成功一次即认为连接恢复健康
        parts = []
        for content in result.content:
            if content.type == "text":
                parts.append(content.text)
        return "\n".join(parts)
