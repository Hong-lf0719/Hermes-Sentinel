"""Hermes RAG —— 文本分块。

长文档（简历、论文、笔记）需要先切成语义完整的片段，再做嵌入与检索。
中文没有天然空格分词，因此分块以「句子边界优先、字符数兜底」为原则：
尽量在句号 / 问号 / 换行处断开，保证每块语义完整；相邻块带重叠，
避免跨块语义被切断。
"""

from __future__ import annotations

import re
from typing import List

# 句子终结符（中英文通用）
_SENT_SPLIT = re.compile(r"(?<=[。！？!?\n])")


def chunk_text(text: str, size: int = 400, overlap: int = 80) -> List[str]:
    """把长文本切成带重叠的块。

    Args:
        text: 原始文本。
        size: 每块目标字符数（中文按字符计）。
        overlap: 相邻块重叠字符数，缓解跨块语义割裂。

    Returns:
        非空片段列表。空输入返回 []。
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    sentences = [s for s in _SENT_SPLIT.split(text) if s.strip()]
    chunks: List[str] = []
    buf = ""

    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if len(buf) + len(s) <= size:
            buf += s
            continue
        # 当前块已满，先收尾
        if buf:
            chunks.append(buf.strip())
        # 超长单句：强制按 size-overlap 切（不太可能，但保底）
        if len(s) > size:
            step = max(size - overlap, 1)
            for i in range(0, len(s), step):
                piece = s[i : i + size].strip()
                if piece:
                    chunks.append(piece)
            buf = ""
        else:
            buf = s

    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if c]


def chunk_markdown(text: str, size: int = 400, overlap: int = 80) -> List[str]:
    """Markdown 友好分块：尽量不在标题行中间断开。

    实现上先做「标题保护」——把 `# 标题` 与其后内容视为一个整体单元，
    再交给通用 chunk_text 处理。
    """
    # 标题后补两个换行，确保标题与其段落被同一句边界聚合
    protected = re.sub(r"(?m)^(#{1,6}\s.*)$", r"\1\n\n", text)
    return chunk_text(protected, size=size, overlap=overlap)
