"""Hermes RAG —— 嵌入层（可插拔）。

嵌入（Embedding）把文本映射为稠密向量，是 RAG 检索的数学基础。
本模块提供两种实现，通过统一接口切换：

1. ``TfidfEmbedder``（默认）：自实现 TF-IDF + 余弦相似度。
   - 零额外依赖（仅 numpy），纯本地、可离线、可解释，适合中小知识库。
   - 中文分词优先用 jieba（若已安装），否则降级为「字符 + 英文词」规则分词，
     保证零依赖也能跑、也能检索。
   - 工程价值：不黑盒依赖第三方向量库，核心算法完全可控可讲。

2. ``DenseEmbedder``（可选升级）：基于 sentence-transformers 的语义向量
   （如 BAAI/bge-small-zh-v1.5），语义召回更强，但需下载模型权重。
   通过 ``embedding="dense"`` 或环境变量 ``HERMES_EMBEDDING=dense`` 启用。

设计原则：两种嵌入器对外暴露相同的 ``fit`` / ``transform`` / ``dim`` 接口，
store 层不感知具体实现 —— 体现「面向接口编程、可插拔替换」的工程成熟度。
"""

from __future__ import annotations

import math
import os
import re
from typing import List, Optional

import numpy as np

try:  # jieba 为可选增强，未安装时自动降级
    import jieba

    jieba.setLogLevel("ERROR")
    _HAS_JIEBA = True
except Exception:  # pragma: no cover - 环境相关
    _HAS_JIEBA = False


# 中文常见停用词（减少噪声、提升区分度）
_DEFAULT_STOP = set(
    "的 了 和 是 在 我 有 也 就 不 人 都 一 上 来 到 时 大 为 子 中 你 说 生 国 年 "
    "着 与 及 等 被 让 把 给 对 这 那 它 他 她 们 我们 你们 他们 这个 那个 一个 "
    "可以 这样 那样 因为 所以 如果 但是 一些 没有 自己 什么 怎么 怎么 这些 那些".split()
)


def tokenize(text: str) -> List[str]:
    """中文友好的轻量分词。

    - 有 jieba：用 ``jieba.lcut`` 做中文分词。
    - 无 jieba：英文 / 数字按词，中文按单字（配合 TF-IDF 仍可召回）。
    """
    text = (text or "").lower()
    if _HAS_JIEBA:
        raw = jieba.lcut(text)
    else:
        # 英文数字词 + 中文单字
        raw = re.findall(r"[a-z0-9]+|[一-鿿]", text)
    out: List[str] = []
    for t in raw:
        t = t.strip()
        if not t or t in _DEFAULT_STOP:
            continue
        out.append(t)
    return out


class TfidfEmbedder:
    """自实现 TF-IDF 嵌入器（稀疏语义 → 归一化向量）。"""

    def __init__(self) -> None:
        self.vocab: dict[str, int] = {}
        self.idf: Optional[np.ndarray] = None
        self._df: dict[str, int] = {}
        self._n_docs: int = 0

    def fit(self, corpus: List[str]) -> None:
        """在全部文档片段上计算 IDF 与词表。"""
        self._df = {}
        self._n_docs = max(len(corpus), 1)
        for doc in corpus:
            for w in set(tokenize(doc)):
                self._df[w] = self._df.get(w, 0) + 1
        self.vocab = {w: i for i, w in enumerate(sorted(self._df.keys()))}
        n = len(self.vocab)
        idf = np.zeros(n, dtype=np.float32)
        for w, df in self._df.items():
            # 平滑 IDF，避免零除
            idf[self.vocab[w]] = math.log((self._n_docs + 1) / (df + 1)) + 1.0
        self.idf = idf

    def transform(self, text: str) -> np.ndarray:
        """将单条文本转为 L2 归一化 TF-IDF 向量。"""
        if not self.vocab or self.idf is None:
            return np.zeros(1, dtype=np.float32)
        vec = np.zeros(len(self.vocab), dtype=np.float32)
        tf: dict[str, int] = {}
        for t in tokenize(text):
            tf[t] = tf.get(t, 0) + 1
        for t, c in tf.items():
            if t in self.vocab:
                idx = self.vocab[t]
                vec[idx] = (1.0 + math.log(c)) * self.idf[idx]
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        return vec.astype(np.float32)

    def dim(self) -> int:
        return len(self.vocab) or 1


class DenseEmbedder:
    """基于 sentence-transformers 的稠密语义嵌入器（可选升级）。"""

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5") -> None:
        self.model_name = model_name
        self._model = None

    def _ensure(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "使用稠密嵌入需先安装 sentence-transformers：pip install sentence-transformers"
                ) from e
            self._model = SentenceTransformer(self.model_name)

    def fit(self, corpus: Optional[List[str]] = None) -> None:
        """稠密模型无需拟合词表，仅触发懒加载以便尽早暴露配置问题。"""
        self._ensure()

    def transform(self, text: str) -> np.ndarray:
        self._ensure()
        vec = self._model.encode([text], normalize_embeddings=True)[0]
        return np.asarray(vec, dtype=np.float32)

    def dim(self) -> int:
        self._ensure()
        return int(self._model.get_sentence_embedding_dimension())


def create_embedder(kind: Optional[str] = None):
    """工厂：按名字创建嵌入器。

    kind 优先级：显式参数 > 环境变量 HERMES_EMBEDDING > 默认 'tfidf'。
    """
    kind = (kind or os.getenv("HERMES_EMBEDDING", "tfidf")).lower()
    if kind in ("dense", "sentence-transformers", "st"):
        return DenseEmbedder()
    return TfidfEmbedder()
