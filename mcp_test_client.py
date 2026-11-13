"""
MCP 测试客户端 — 验证 mcp_server.py 能跑通「问清单 → 调用」完整流程

运行：
  python mcp_test_client.py

它做的事：
  1. 以子进程方式启动 mcp_server.py（stdio 传输）
  2. 握手 initialize
  3. 问清单 tools/list —— 看 Server 暴露了哪些工具 + schema
  4. 调用 tools/call —— 真正执行 search_github 并打印返回内容
"""

import sys

# Windows GBK 控制台打印 emoji 会崩溃，统一切 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    server_params = StdioServerParameters(
        command=sys.executable,  # 用当前解释器，确保子进程也找得到 mcp 包
        args=["mcp_server.py"],
    )

    # stdio_client 启动子进程并建立连接，产出 (read, write) 两条流
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # ① 握手
            await session.initialize()
            print("✅ 握手成功 (initialize)\n")

            # ② 问清单 tools/list
            tools_result = await session.list_tools()
            print(f"✅ 拿到 {len(tools_result.tools)} 个工具：")
            for t in tools_result.tools:
                print(f"   · {t.name}: {t.description}")
            print()

            # ③ 调用 tools/call —— 真正执行 search_github
            print("▶ 调用 search_github(keyword='langgraph', max_results=3) ...\n")
            result = await session.call_tool(
                "search_github", {"keyword": "langgraph", "max_results": 3}
            )
            print("✅ 调用成功，Server 返回内容：")
            print("-" * 50)
            for content in result.content:
                if content.type == "text":
                    print(content.text)
            print("-" * 50)


if __name__ == "__main__":
    asyncio.run(main())
