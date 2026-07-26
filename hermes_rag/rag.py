"""Hermes RAG —— 检索编排（对外统一入口）。

核心思想：RAG 的价值不在于「用户手动问知识库」，而在于「每次对话自动带着
长期记忆」。``retrieve_context`` 把检索结果格式化为上下文片段，由调用方
（server.py 的 process_chat）注入 System Prompt —— 这就是「让信息找人」。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from .store import KnowledgeStore
from .embeddings import create_embedder


_stores: dict = {}


def get_store(path: Optional[str] = None, embedding: Optional[str] = None) -> KnowledgeStore:
    """获取（带缓存的）知识库单例。path 相同复用同一实例。"""
    key = path or "default"
    if key not in _stores:
        emb = create_embedder(embedding)
        _stores[key] = KnowledgeStore(path, embedder=emb)
    return _stores[key]


def retrieve_context(
    query: str,
    top_k: int = 3,
    path: Optional[str] = None,
    min_score: float = 0.0,
    max_chars: int = 1200,
) -> str:
    """检索与 query 相关的知识片段，格式化为可注入的上下文文本。

    返回空字符串表示知识库为空或无相关内容 —— 调用方据此决定是否注入。
    """
    store = get_store(path)
    hits = store.search(query, top_k, min_score=min_score)
    if not hits:
        return ""
    lines: List[str] = []
    for i, h in enumerate(hits, 1):
        text = h["text"]
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        lines.append(
            f"[{i}]（来源：{h.get('source', '?')}，相关度 {h['score']:.2f}）\n{text}"
        )
    return "【长期知识库检索到的相关内容】\n" + "\n\n".join(lines)
