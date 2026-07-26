"""
Hermes-Caspian Adapter 快速启动示例

这个脚本演示了如何在 5 行代码内，用 HermesCaspianAdapter 替换 server.py 中的线程 hack。
"""

import asyncio
from caspian_sdk import CommClient

# 1. 导入你的 Hermes 核心逻辑（从你的 server.py）
# （这里用一个 dummy 函数占位，实际使用时请替换为你的真实 process_chat）
async def dummy_process_chat(message: str, session_id: str = "default") -> str:
    return f"✅ Hermes 已收到: {message[:30]}...\n（此为 demo，实际将调用 DeepSeek + SQLite）"

# 2. 初始化 Caspian Client
client = CommClient()
client.connect_email(display_name="Hermes Demo")

# 3. 初始化 Adapter
from hermes_caspian.adapter import HermesCaspianAdapter
adapter = HermesCaspianAdapter(client, process_chat=dummy_process_chat)

# 4. 注册 async handler（关键！不再是线程）
@client.on_message
async def handle(message):
    await adapter.on_message(message)

# 5. 启动监听
asyncio.run(client.listen())
