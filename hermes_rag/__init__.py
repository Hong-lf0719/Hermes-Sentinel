"""Hermes RAG —— 轻量可插拔的长期知识库（RAG）引擎。

让 Hermes 拥有「长期记忆」：把简历、项目经历、学习笔记、行业资料灌入知识库，
之后每次对话自动检索相关片段注入上下文，实现个性化增强。

核心特性：
  - 自实现 TF-IDF 嵌入（零硬依赖、可离线、可解释），可选升级稠密语义向量
  - 本地持久化向量索引（numpy + JSON），重启不丢
  - 中文友好的分块与分词
  - 面向接口、可插拔、带测试，可直接打包发布（pip install hermes-rag）
"""

from __future__ import annotations

from .store import KnowledgeStore
from .embeddings import TfidfEmbedder, DenseEmbedder, create_embedder
from .chunking import chunk_text, chunk_markdown
from .rag import get_store, retrieve_context
from .exceptions import RAGError, DocumentError, EmbeddingError, StoreError

__version__ = "0.1.0"

__all__ = [
    "KnowledgeStore",
    "TfidfEmbedder",
    "DenseEmbedder",
    "create_embedder",
    "chunk_text",
    "chunk_markdown",
    "get_store",
    "retrieve_context",
    "RAGError",
    "DocumentError",
    "EmbeddingError",
    "StoreError",
]
