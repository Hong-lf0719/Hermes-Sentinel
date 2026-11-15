"""Hermes RAG —— PGVector 后端（Postgres + pgvector 向量库）。

``PgVectorStore`` 与本地 ``KnowledgeStore`` 暴露完全相同的接口
（add_text / search / stats / clear），由 ``rag.get_store`` 按
``HERMES_RAG_BACKEND`` 环境变量切换。

适用场景：
  - 知识库规模增长到本地 JSON + numpy 不便管理
  - 需要 Postgres 运维经验 / 多实例共享同一个知识库
  - 演示「同一接口、双后端切换」的架构能力

工程说明：
  - 嵌入用 FixedHashEmbedder（固定 512 维，确定性、零模型下载），
    也可传 DenseEmbedder（bge，768 维——建表维度需一致）
  - 连接按操作建立（本地 Postgres 建连成本低，且天然线程安全）
  - 相似度用 pgvector 的余弦距离算符 ``<=>``，score = 1 - distance
"""

from __future__ import annotations

import os
import time
from typing import List, Optional

from .chunking import chunk_text, chunk_markdown
from .embeddings import FixedHashEmbedder
from .exceptions import StoreError

from pgvector import Vector

_DIM = 512


class PgVectorStore:
    def __init__(self, dsn: Optional[str] = None, embedder=None) -> None:
        self.dsn = dsn or os.getenv("HERMES_PG_DSN", "")
        if not self.dsn:
            raise StoreError(
                "pgvector 后端需要 HERMES_PG_DSN（如 postgresql://hermes:密码@127.0.0.1:5432/hermes_rag）"
            )
        self.embedder = embedder or FixedHashEmbedder()
        if self.embedder.dim() != _DIM:
            raise StoreError(
                f"嵌入维度 {self.embedder.dim()} 与建表维度 {_DIM} 不一致"
            )
        self._ensure_tables()

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row
        from pgvector.psycopg import register_vector

        conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
        register_vector(conn)
        return conn

    def _ensure_tables(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            # 扩展已由 DBA 以超级用户预建（hermes 用户非超级用户无法建扩展）
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except Exception:  # noqa: BLE001
                pass
            cur.execute(
                "CREATE TABLE IF NOT EXISTS kb_docs ("
                "doc_id TEXT PRIMARY KEY, source TEXT, "
                "created_at DOUBLE PRECISION, n_chunks INT)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS kb_chunks ("
                "chunk_id TEXT PRIMARY KEY, doc_id TEXT, source TEXT, text TEXT, "
                f"embedding vector({_DIM}))"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS kb_chunks_hnsw "
                "ON kb_chunks USING hnsw (embedding vector_cosine_ops)"
            )

    # ---------------------------------------------------------------- 写入
    def add_text(
        self,
        text: str,
        source: str = "manual",
        chunk_size: int = 400,
        overlap: int = 80,
        markdown: bool = False,
    ) -> dict:
        pieces = (
            chunk_markdown(text, chunk_size, overlap)
            if markdown
            else chunk_text(text, chunk_size, overlap)
        )
        if not pieces:
            return {"doc_id": None, "added": 0, "note": "空内容"}
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(SUBSTRING(doc_id FROM 4)::int), 0) AS mx FROM kb_docs"
            )
            doc_seq = cur.fetchone()["mx"] + 1
            doc_id = f"doc{doc_seq:05d}"
            cur.execute(
                "INSERT INTO kb_docs (doc_id, source, created_at, n_chunks) VALUES (%s,%s,%s,%s)",
                (doc_id, source, time.time(), len(pieces)),
            )
            chunk_seq = cur.execute(
                "SELECT COALESCE(MAX(SUBSTRING(chunk_id FROM 2)::int), 0) AS mx FROM kb_chunks"
            ).fetchone()["mx"]
            for p in pieces:
                chunk_seq += 1
                chunk_id = f"c{chunk_seq:06d}"
                vec = self.embedder.transform(p)
                cur.execute(
                    "INSERT INTO kb_chunks (chunk_id, doc_id, source, text, embedding) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (chunk_id, doc_id, source, p, Vector(vec)),
                )
        return {"doc_id": doc_id, "added": len(pieces), "source": source}

    # ---------------------------------------------------------------- 检索
    def search(
        self, query: str, top_k: int = 3, min_score: float = 0.0
    ) -> List[dict]:
        q = self.embedder.transform(query)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, doc_id, text, source, "
                "1 - (embedding <=> %s) AS score "
                f"FROM kb_chunks ORDER BY embedding <=> %s LIMIT {int(top_k)}",
                (Vector(q), Vector(q)),
            )
            rows = cur.fetchall()
        results: List[dict] = []
        for r in rows:
            score = float(r["score"]) if not isinstance(r, dict) else float(r["score"])
            rec = {
                "id": r["chunk_id"] if not isinstance(r, dict) else r["chunk_id"],
                "doc_id": r["doc_id"],
                "text": r["text"],
                "source": r["source"],
                "score": score,
            }
            if score <= min_score:
                continue
            results.append(rec)
        return results

    # ---------------------------------------------------------------- 维护
    def stats(self) -> dict:
        with self._connect() as conn, conn.cursor() as cur:
            docs = cur.execute("SELECT COUNT(*) AS docs FROM kb_docs").fetchone()["docs"]
            chunks = cur.execute("SELECT COUNT(*) AS chunks FROM kb_chunks").fetchone()["chunks"]
        return {
            "docs": docs,
            "chunks": chunks,
            "embedding": type(self.embedder).__name__,
            "dim": self.embedder.dim(),
            "path": "postgres://hermes_rag（pgvector）",
            "backend": "pgvector",
        }

    def clear(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE kb_chunks, kb_docs")
