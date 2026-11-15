# -*- coding: utf-8 -*-
"""RAG 双后端一致性测试：同一接口，local / pgvector 结果对齐。

pgvector 用例连接真实 WSL Postgres（127.0.0.1:5432）；连不上自动跳过。
"""
import os
import sys

sys.path.insert(0, r"D:\31227\应用\Hermes Sentinel")
sys.stdout.reconfigure(encoding="utf-8")

import pytest


def test_fixed_hash_embedder_deterministic():
    from hermes_rag.embeddings import FixedHashEmbedder

    e = FixedHashEmbedder(dim=512)
    a = e.transform("数据开发工程师，负责 ETL 与数仓")
    b = e.transform("数据开发工程师，负责 ETL 与数仓")
    assert a.shape[0] == 512
    assert (a == b).all()
    assert abs(float(a @ b) - 1.0) < 1e-6  # 同文本自相似 = 1


def test_fixed_hash_semantic_weakness_is_expected():
    """哈希嵌入是词面匹配：同义词不同词 → 相似度低（与 TF-IDF 同级水平）。"""
    from hermes_rag.embeddings import FixedHashEmbedder

    e = FixedHashEmbedder(dim=512)
    exact = e.transform("AI 应用开发 岗位")
    syn = e.transform("人工智能应用 岗位")
    assert float(exact @ syn) < 0.6  # 词面不重叠 → 低相似（语义扩展是 dense 的事）


def test_local_store_add_search():
    import tempfile
    from hermes_rag.store import KnowledgeStore

    with tempfile.TemporaryDirectory() as tmp:
        st = KnowledgeStore(tmp)
        r = st.add_text("Hermes Sentinel 是一个多渠道 AI 情报助手，支持 RAG 长期记忆。", "测试")
        assert r["added"] >= 1
        hits = st.search("AI 情报助手", 3)
        assert hits and "Hermes" in hits[0]["text"]
        s = st.stats()
        assert s["docs"] == 1 and s["chunks"] == r["added"]


PG_DSN = os.environ.get(
    "HERMES_PG_DSN", "postgresql://hermes:hermes-pg-2026@127.0.0.1:5432/hermes_rag"
)


def _pg_available() -> bool:
    try:
        import psycopg
        with psycopg.connect(PG_DSN, autocommit=True, connect_timeout=3) as c:
            c.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _pg_available(), reason="WSL Postgres 不可达")
def test_pgvector_store_parity():
    """pgvector 后端与本地后端接口一致：add → search → stats → clear 语义对齐。"""
    from hermes_rag.pgstore import PgVectorStore

    st = PgVectorStore(dsn=PG_DSN)
    st.clear()
    r = st.add_text("公司情报卡：绿盟科技校招本科可投，加班强度中等。", "测试")
    assert r["added"] >= 1
    hits = st.search("绿盟科技 校招 本科", 3)
    assert hits and "绿盟" in hits[0]["text"]
    assert hits[0]["score"] > 0.3
    s = st.stats()
    assert s["backend"] == "postgres" or s["docs"] >= 1
    st.clear()
    assert st.stats()["chunks"] == 0
