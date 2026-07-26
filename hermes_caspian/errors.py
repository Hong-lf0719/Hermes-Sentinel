"""
自定义错误类型
"""

from typing import Optional


class HermesCaspianError(Exception):
    """Hermes-Caspian Adapter 的基类异常"""
    pass


class ValidationError(HermesCaspianError):
    """输入验证失败（如空消息、非法 sender）"""
    def __init__(self, message: str, field: Optional[str] = None):
        self.field = field
        super().__init__(message)


class LLMTimeoutError(HermesCaspianError):
    """LLM 调用超时"""
    pass


class DBConnectionError(HermesCaspianError):
    """SQLite 连接失败"""
    pass


class ChannelNotSupportedError(HermesCaspianError):
    """当前通道不支持（如未配置 Telegram）"""
    pass
