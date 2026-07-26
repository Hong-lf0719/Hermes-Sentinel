"""Hermes RAG 命令行：灌库 / 检索 / 统计 / 清空。

示例：
  python -m hermes_rag.cli add --text "我是 2027 届数据科学本科生..." --source "简历"
  python -m hermes_rag.cli add --file resume.md
  python -m hermes_rag.cli add --url https://news.example.com/ai-agent
  python -m hermes_rag.cli search "Agent 框架 怎么学"
  python -m hermes_rag.cli stats
  python -m hermes_rag.cli clear
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

import httpx

from .rag import get_store, retrieve_context
from .store import KnowledgeStore


def _strip_html(html: str) -> str:
    """极简 HTML → 纯文本（去 script/style + 标签 + 折叠空白）。"""
    html = re.sub(
        r"<(script|style)[^>]*>.*?</\1>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;", " ", html)
    html = re.sub(r"\s+", " ", html)
    return html.strip()


async def _fetch_url(url: str) -> str:
    async with httpx.AsyncClient(
        timeout=20, verify=False, headers={"User-Agent": "Mozilla/5.0"}
    ) as c:
        r = await c.get(url)
        r.raise_for_status()
        return _strip_html(r.text)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="hermes-rag", description="Hermes 长期知识库 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("add", help="灌入知识（文本 / 文件 / 网页）")
    pa.add_argument("--text")
    pa.add_argument("--file")
    pa.add_argument("--url")
    pa.add_argument("--source", default="")
    pa.add_argument("--path", default=None)
    pa.add_argument("--markdown", action="store_true", help="按 Markdown 结构分块")

    ps = sub.add_parser("search", help="检索知识库")
    ps.add_argument("query")
    ps.add_argument("--top-k", type=int, default=3)
    ps.add_argument("--path", default=None)

    pst = sub.add_parser("stats", help="查看知识库统计")
    pst.add_argument("--path", default=None)

    pc = sub.add_parser("clear", help="清空知识库")
    pc.add_argument("--path", default=None)

    args = p.parse_args(argv)

    if args.cmd == "clear":
        store = KnowledgeStore(args.path) if args.path else get_store()
        store.clear()
        print("🗑️ 知识库已清空")
        return

    if args.cmd == "stats":
        store = KnowledgeStore(args.path) if args.path else get_store()
        print(store.stats())
        return

    if args.cmd == "add":
        store = KnowledgeStore(args.path) if args.path else get_store()
        if args.text:
            res = store.add_text(args.text, args.source or "文本", markdown=args.markdown)
        elif args.file:
            fp = Path(args.file).expanduser().resolve()
            if not fp.exists():
                print(f"文件不存在: {fp}")
                sys.exit(1)
            txt = fp.read_text("utf-8", errors="replace")
            res = store.add_text(txt, args.source or fp.name, markdown=args.markdown)
        elif args.url:
            try:
                txt = asyncio.run(_fetch_url(args.url))
            except Exception as e:
                print(f"抓取失败: {e}")
                sys.exit(1)
            res = store.add_text(txt, args.source or args.url, markdown=False)
        else:
            print("add 需要 --text / --file / --url 之一")
            sys.exit(1)
        print(f"✅ 已入库 doc={res['doc_id']} 新增 {res['added']} 片段 | {store.stats()}")
        return

    if args.cmd == "search":
        ctx = retrieve_context(args.query, top_k=args.top_k, path=args.path or None)
        print(ctx or "（知识库暂无相关内容）")


if __name__ == "__main__":
    main()
