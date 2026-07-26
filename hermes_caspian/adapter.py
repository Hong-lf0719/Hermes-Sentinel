"""
Hermes-Caspian Adapter 核心类

这是一个生产级的 Caspian SDK 胶水层，将官方 SDK 的 async 原生能力与 Hermes 业务逻辑无缝集成。
它提供：
- 零线程开销的 async handler
- 统一错误处理（ValidationError, LLMTimeoutError, DBConnectionError）
- 结构化重试（指数退避）
- channel-aware session ID 生成
- 可插拔架构（未来可轻松替换为 Discord/Telegram SDK）
"""

import asyncio
import logging
import httpx
from typing import Optional, Dict, Any
from caspian_sdk import CommClient

from hermes_caspian.errors import (
    HermesCaspianError,
    ValidationError,
    LLMTimeoutError,
    DBConnectionError,
    ChannelNotSupportedError,
)
from hermes_caspian.config import load_config

# 全局 logger
logger = logging.getLogger(__name__)


class HermesCaspianAdapter:
    """
    Hermes-Caspian Adapter 主类

    :param client: 已初始化的 Caspian CommClient 实例
    :param process_chat: Hermes 的核心异步聊天处理函数，签名应为:
        async def process_chat(message: str, session_id: str) -> str
    """

    def __init__(
        self,
        client: CommClient,
        process_chat: callable,
        httpx_client: Optional[httpx.AsyncClient] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.client = client
        self.process_chat = process_chat
        self.httpx_client = httpx_client or httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        self.config = config or load_config()
        self._setup_logger()

    def _setup_logger(self):
        """设置日志器"""
        level = getattr(logging, self.config.get("log_level", "INFO"), logging.INFO)
        logging.basicConfig(level=level)
        logger.setLevel(level)

    async def on_message(self, message) -> None:
        """
        Caspian 消息处理主入口 —— 替代 server.py 中的线程 hack

        这是一个真正的 async def，可直接 await process_chat 和 message.reply。

        :param message: Caspian SDK 的 Message 对象
        """
        trace_id = getattr(message, "id", "unknown")
        sender = getattr(message, "sender", {})
        sender_address = (sender or {}).get("address", "unknown")
        platform = getattr(message, "platform", "unknown")

        logger.info(f"[TRACE:{trace_id}] 收到 {platform} 消息 from {sender_address}: {message.text[:50]}...")

        try:
            # Step 1: 输入验证
            if not message.text or not isinstance(message.text, str) or not message.text.strip():
                raise ValidationError("消息内容为空或非法", field="text")

            # Step 2: 生成 channel-aware session_id
            # 示例：caspian-email-user@example.com
            #        caspian-telegram-123456789
            session_id = self._generate_session_id(platform, sender_address)

            # Step 3: 调用 Hermes 核心逻辑
            reply = await self.process_chat(message.text, session_id=session_id)

            # Step 4: 安全回复（带重试）
            # process_chat 可能返回 str，也可能返回 {"text":..., "html":...}
            # （口令报告 / 日报触发时，走 HTML 邮件渲染）
            if isinstance(reply, dict) and "html" in reply:
                await self._safe_reply(message, reply.get("text", ""), html=reply["html"])
            else:
                await self._safe_reply(message, str(reply))

        except ValidationError as e:
            logger.warning(f"[TRACE:{trace_id}] 输入验证失败: {e}")
            await self._safe_reply(message, f"⚠️ 输入格式错误: {e}")

        except LLMTimeoutError as e:
            logger.error(f"[TRACE:{trace_id}] LLM 超时: {e}")
            await self._safe_reply(message, "⏳ AI 正在思考，请稍候...")

        except DBConnectionError as e:
            logger.critical(f"[TRACE:{trace_id}] 数据库连接失败: {e}")
            await self._safe_reply(message, "❌ 记忆服务暂时不可用，请稍后重试")

        except ChannelNotSupportedError as e:
            logger.error(f"[TRACE:{trace_id}] 通道不支持: {e}")
            await self._safe_reply(message, f"❌ 当前通道暂不支持: {e}")

        except Exception as e:
            logger.exception(f"[TRACE:{trace_id}] 未预期错误")
            await self._safe_reply(message, "❌ 服务暂时异常，请重试")

    def _generate_session_id(self, platform: str, sender_address: str) -> str:
        """
        生成唯一、channel-aware 的 session_id
        """
        # 清洗 sender_address，避免特殊字符
        clean_address = sender_address.replace("@", "_at_").replace(".", "_dot_").replace("+", "_plus_")
        return f"caspian-{platform.lower()}-{clean_address}"

    async def _safe_reply(self, message, content: str, html: Optional[str] = None, max_retries: Optional[int] = None, timeout: Optional[float] = None) -> None:
        """
        带指数退避重试的安全回复方法

        注：Caspian SDK 的 message.reply() 是【同步】方法（返回 dict），
        不能直接 await（会 TypeError: dict is not awaitable）。
        用 run_in_executor 在线程池里执行，避免阻塞 uvicorn 主事件循环。

        :param content: 要发送的回复文本
        :param html: 可选 HTML 内容（日报等富文本，走邮件 HTML 渲染）
        """
        max_retries = max_retries or self.config.get("retry_max_attempts", 3)
        timeout = timeout or self.config.get("retry_timeout_seconds", 30.0)
        loop = asyncio.get_event_loop()

        for attempt in range(max_retries + 1):
            try:
                # message.reply 是同步方法（返回 dict），用 executor 包裹避免阻塞事件循环；
                # 有 html 时走 HTML 渲染分支（日报等富文本邮件）
                await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda c=content, h=html: message.reply(c, html=h) if h else message.reply(c),
                    ),
                    timeout=timeout,
                )
                logger.debug(f"[TRACE:{getattr(message, 'id', 'unknown')}] 消息已成功回复 (尝试 {attempt + 1}/{max_retries + 1})")
                return

            except asyncio.TimeoutError:
                if attempt == max_retries:
                    raise LLMTimeoutError(f"message.reply() 在 {timeout} 秒内超时，已重试 {max_retries} 次")
                backoff = 1 * (2 ** attempt)  # exponential backoff: 1s, 2s, 4s...
                logger.warning(f"[TRACE:{getattr(message, 'id', 'unknown')}] reply 超时，{backoff}s 后重试 ({attempt + 1}/{max_retries + 1})")
                await asyncio.sleep(backoff)

            except Exception as e:
                if attempt == max_retries:
                    raise HermesCaspianError(f"message.reply() 失败: {e}")
                logger.warning(f"[TRACE:{getattr(message, 'id', 'unknown')}] reply 失败: {e}，{1}s 后重试 ({attempt + 1}/{max_retries + 1})")
                await asyncio.sleep(1)
