"""Hermes RAG —— 知识库存储与持久化。

``KnowledgeStore`` 是 RAG 的「大脑」：
  - 持有嵌入器（TF-IDF / 稠密，可插拔）
  - ``add_text``：长文本 → 分块 → 嵌入 → 增量重建索引 → 落盘
  - ``search``：查询向量化 → 余弦相似度 → 返回 Top-K 片段
  - 持久化到本地目录（meta.json + vectors.npy），重启不丢

工程取舍：知识库规模通常中小（几百~几千片段），因此「每次新增后全量重建
索引」代价极低且保证正确性；若未来规模变大，可平滑替换为 chromadb /
sqlite-vec 等，对外接口不变。这体现「先用最简单正确的方案跑通，再按需演进」。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import List, Optional

import numpy as np

from .chunking import chunk_text, chunk_markdown
from .embeddings import create_embedder, TfidfEmbedder
from .exceptions import StoreError

logger = logging.getLogger("hermes")


class KnowledgeStore:
    def __init__(self, path: Optional[str] = None, embedder=None) -> None:
        self.root = Path(path) if path else (Path(__file__).parent.parent / "kb")
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.root / "meta.json"
        self.vec_path = self.root / "vectors.npy"
        self.embedder = embedder or create_embedder()
        self.docs: dict[str, dict] = {}
        self.chunks: List[dict] = []
        self.vectors: Optional[np.ndarray] = None
        self._doc_seq = 0
        self._chunk_seq = 0
        self._load()

    # ---------------------------------------------------------------- 持久化
    def _load(self) -> None:
        if self.meta_path.exists():
            try:
                data = json.loads(self.meta_path.read_text("utf-8"))
            except Exception as e:  # 损坏的元数据
                raise StoreError(f"知识库元数据损坏: {e}")
            self.docs = {d["id"]: d for d in data.get("docs", [])}
            self.chunks = data.get("chunks", [])
            self._doc_seq = data.get("doc_seq", len(self.docs))
            self._chunk_seq = data.get("chunk_seq", len(self.chunks))
        # 重启后重建词表 / IDF：保证检索维度与已存向量一致（不影响向量本身）
        if self.chunks:
            try:
                self.embedder.fit([c["text"] for c in self.chunks])
            except Exception:
                pass
        if self.vec_path.exists():
            try:
                self.vectors = np.load(self.vec_path)
            except Exception as e:
                raise StoreError(f"向量索引损坏: {e}")

        # 一致性校验：向量行数必须与 chunk 数严格相等。
        # 不校验的后果比崩溃更糟——search 用 argsort 的下标去索引 self.chunks，
        # 行数错位会「静默」返回张冠李戴的片段（答案看起来正常但内容对不上）。
        # 不一致时丢弃旧向量、从 chunks 全量重建（数据本体在 meta.json，可无损恢复）。
        n_chunks = len(self.chunks)
        n_vecs = 0 if self.vectors is None else int(self.vectors.shape[0])
        if n_chunks and n_vecs != n_chunks:
            logger.warning(
                f"[RAG] 索引与元数据不一致（向量 {n_vecs} 行 / 片段 {n_chunks} 条），正在重建索引"
            )
            try:
                self._rebuild_and_persist()
            except Exception as e:
                raise StoreError(f"索引重建失败: {e}")

    def _save(self) -> None:
        """原子落盘：先写向量、再写元数据，且都经临时文件 + os.replace 替换。

        顺序与原子性都重要：meta.json 是「提交点」，若先写 meta 后写向量而中途
        崩溃，重启就会读到「有 N 条片段但向量只有 M 行」的错位库；而直接
        write_text 被打断则会留下半截 JSON，直接让知识库打不开。
        """
        if self.vectors is not None:
            # 注意：np.save(路径) 若路径不以 .npy 结尾会自动追加 .npy，
            # 导致临时文件实际叫 vectors.npy.tmp.npy、os.replace 找不到源文件。
            # 传文件对象可绕过该行为。
            tmp_vec = self.vec_path.with_name(self.vec_path.name + ".tmp")
            with open(tmp_vec, "wb") as f:
                np.save(f, self.vectors)
            os.replace(tmp_vec, self.vec_path)

        data = {
            "docs": list(self.docs.values()),
            "chunks": self.chunks,
            "doc_seq": self._doc_seq,
            "chunk_seq": self._chunk_seq,
        }
        tmp_meta = self.meta_path.with_name(self.meta_path.name + ".tmp")
        tmp_meta.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp_meta, self.meta_path)

    # ---------------------------------------------------------------- 写入
    def add_text(
        self,
        text: str,
        source: str = "manual",
        chunk_size: int = 400,
        overlap: int = 80,
        markdown: bool = False,
    ) -> dict:
        """把一段文本切块入库，返回 {doc_id, added, source}。"""
        pieces = (
            chunk_markdown(text, chunk_size, overlap)
            if markdown
            else chunk_text(text, chunk_size, overlap)
        )
        if not pieces:
            return {"doc_id": None, "added": 0, "note": "空内容"}
        self._doc_seq += 1
        doc_id = f"doc{self._doc_seq:05d}"
        self.docs[doc_id] = {
            "id": doc_id,
            "source": source,
            "created_at": time.time(),
            "n_chunks": len(pieces),
        }
        for p in pieces:
            self._chunk_seq += 1
            self.chunks.append(
                {
                    "id": f"c{self._chunk_seq:06d}",
                    "doc_id": doc_id,
                    "text": p,
                    "source": source,
                }
            )
        self._rebuild_and_persist()
        return {"doc_id": doc_id, "added": len(pieces), "source": source}

    def _rebuild_and_persist(self) -> None:
        corpus = [c["text"] for c in self.chunks]
        if not corpus:
            self.vectors = np.zeros((0, 1), dtype=np.float32)
            self._save()
            return
        self.embedder.fit(corpus)
        vecs = [self.embedder.transform(c) for c in corpus]
        self.vectors = np.vstack(vecs).astype(np.float32)
        self._save()

    # ---------------------------------------------------------------- 检索
    def search(
        self, query: str, top_k: int = 3, min_score: float = 0.0
    ) -> List[dict]:
        """返回 Top-K 相关片段（含 score 余弦相似度）。"""
        if self.vectors is None or len(self.chunks) == 0:
            return []
        q = self.embedder.transform(query)
        if q.shape[0] != self.vectors.shape[1]:
            # 维度不符（如嵌入器被替换）→ 用当前语料重建后再检索
            self.embedder.fit([c["text"] for c in self.chunks])
            q = self.embedder.transform(query)
        sims = self.vectors @ q  # 已 L2 归一，点积即余弦
        k = min(top_k, len(self.chunks))
        idx = np.argsort(-sims)[:k]
        results: List[dict] = []
        for i in idx:
            if sims[i] <= min_score:
                continue
            c = self.chunks[i]
            results.append({**c, "score": float(sims[i])})
        return results

    # ---------------------------------------------------------------- 维护
    def stats(self) -> dict:
        dim = (
            int(self.vectors.shape[1])
            if self.vectors is not None and len(self.vectors)
            else self.embedder.dim()
        )
        return {
            "docs": len(self.docs),
            "chunks": len(self.chunks),
            "embedding": type(self.embedder).__name__,
            "dim": dim,
            "path": str(self.root),
        }

    def clear(self) -> None:
        """清空整个知识库（删除目录后重建，谨慎调用）。"""
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.docs = {}
        self.chunks = []
        self.vectors = None
        self._doc_seq = 0
        self._chunk_seq = 0
