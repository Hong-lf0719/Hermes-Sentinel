"""Hermes RAG —— 自定义异常。

统一异常体系，便于上层（server / adapter）做针对性容错与日志。
"""


class RAGError(Exception):
    """RAG 模块基础异常。"""


class DocumentError(RAGError):
    """文档读取 / 解析失败（文件不存在、编码错误、不支持的格式等）。"""


class EmbeddingError(RAGError):
    """文本向量化失败（嵌入模型加载失败、输入为空等）。"""


class StoreError(RAGError):
    """知识库持久化 / 加载失败（磁盘不可写、索引损坏等）。"""
