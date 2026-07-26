"""
配置加载模块
"""

import os
from pathlib import Path
from typing import Dict, Any


def load_config() -> Dict[str, Any]:
    """
    加载 Hermes-Caspian 配置。
    优先级：环境变量 > 当前目录 .env > 默认值。
    """
    config = {
        "caspian_api_key": os.getenv("CASPIAN_API_KEY", ""),
        "caspian_base_url": os.getenv("CASPIAN_BASE_URL", "https://api.trycaspianai.com/v1"),
        "retry_max_attempts": int(os.getenv("CASPIAN_RETRY_MAX", "3")),
        "retry_timeout_seconds": float(os.getenv("CASPIAN_TIMEOUT", "30.0")),
        "log_level": os.getenv("CASPIAN_LOG_LEVEL", "INFO"),
    }

    # 如果没有 API key，尝试从项目根目录的 .env 读取（兼容 Hermes 项目结构）
    if not config["caspian_api_key"]:
        env_path = Path(__file__).parent.parent.parent / ".env"
        if env_path.exists():
            from dotenv import load_dotenv
            load_dotenv(env_path)
            config["caspian_api_key"] = os.getenv("CASPIAN_API_KEY", "")

    return config
