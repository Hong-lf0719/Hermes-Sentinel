# Hermes-Caspian Adapter 使用说明

## 安装

```bash
pip install hermes-caspian  # （未来可发布）
# 或本地开发安装：
pip install -e .
```

## 快速开始

1. 确保你已安装 `caspian-sdk>=0.5.0` 和 `httpx`
2. 将你的 `process_chat` 函数导入（见 `example/quickstart.py`）
3. 替换 `server.py` 中的 Caspian 启动逻辑（见下文）

## 在 `server.py` 中替换线程 hack

将原 `server.py` 的第 564–600 行（`start_caspian()` 函数）替换为：

```python
# --- 替换开始 ---
async def start_caspian():
    if not CASPIAN_API_KEY:
        print("⚠️ Caspian 未配置")
        return
    try:
        from caspian_sdk import CommClient
        from hermes_caspian.adapter import HermesCaspianAdapter
        
        client = CommClient()
        client.connect_email(display_name="Hermes")
        print("📧 Caspian Email 已连接")
        
        # 初始化 Adapter，传入你的 process_chat
        adapter = HermesCaspianAdapter(client, process_chat=process_chat)
        
        @client.on_message
        async def handle(message):
            await adapter.on_message(message)
        
        print("🤖 Hermes-Caspian Adapter 已就绪")
        await client.listen()
    except ImportError:
        print("⚠️ pip install caspian-sdk hermes-caspian")
    except Exception as e:
        print(f"⚠️ Caspian 启动失败: {e}")
# --- 替换结束 ---
```

## 特性

- ✅ **零线程开销**：所有消息在主 event loop 中处理
- ✅ **统一错误码**：`ValidationError`, `LLMTimeoutError`, `DBConnectionError`
- ✅ **结构化重试**：指数退避，可配置 `CASPIAN_RETRY_MAX` / `CASPIAN_TIMEOUT`
- ✅ **channel-aware session ID**：自动区分 email/telegram/discord
- ✅ **可插拔架构**：未来接入 Telegram 只需新增 `adapter_telegram.py`
- ✅ **生产级日志**：每条消息带 `TRACE:<id>`，便于排查

## 配置

通过环境变量配置：
- `CASPIAN_API_KEY`: 必填
- `CASPIAN_BASE_URL`: 可选，默认 `https://api.trycaspianai.com/v1`
- `CASPIAN_RETRY_MAX`: 重试次数，默认 `3`
- `CASPIAN_TIMEOUT`: 单次 reply 超时（秒），默认 `30.0`
- `CASPIAN_LOG_LEVEL`: 日志级别，默认 `INFO`
