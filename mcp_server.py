"""
Hermes MCP Server — 把 Hermes 的情报工具独立成 MCP Server

用途：
  用官方 MCP SDK (FastMCP) 暴露 4 个无状态情报工具，
  任何支持 MCP 的 Host（pi / Claude Code / Cursor / 未来的 Hermes）都能
  通过标准协议（tools/list + tools/call）调用，无需复制胶水代码。

运行方式（stdio 传输）：
  python mcp_server.py

可视化调试（可选）：
  npx @modelcontextprotocol/inspector python mcp_server.py
"""

import sys

# Windows GBK 控制台下打印 emoji 会 UnicodeEncodeError 崩溃，
# 统一把标准流切到 UTF-8 + replace（与 server.py 同款处理）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import asyncio
import os
import certifi
import httpx
from urllib.parse import unquote
from dotenv import load_dotenv

# 读取项目 .env（GITHUB_TOKEN 等）；缺失不报错，工具退化为无认证模式
load_dotenv()

from mcp.server.fastmcp import FastMCP

# 创建 MCP Server 实例（名字会显示在 Host 的工具列表里）
mcp = FastMCP("hermes-tools")


@mcp.tool()
async def search_github(keyword: str, max_results: int = 5) -> str:
    """搜索 GitHub 开源项目（按 star 倒序）"""
    # 带 GITHUB_TOKEN 时限流从 60/hr 提升到 5000/hr，缺失则退化为无认证
    headers = {"Accept": "application/vnd.github+json"}
    gh_token = os.getenv("GITHUB_TOKEN")
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"
    try:
        async with httpx.AsyncClient(timeout=15, verify=certifi.where()) as cli:
            r = await cli.get(
                "https://api.github.com/search/repositories",
                params={"q": keyword, "sort": "stars", "order": "desc", "per_page": max_results},
                headers=headers,
            )
            if r.status_code != 200:
                return f"GitHub API {r.status_code}"
            data = r.json()
            items = data.get("items", [])
            if not items:
                return f"🔍 没找到「{keyword}」"
            lines = [f"🔍 GitHub: 「{keyword}」({data['total_count']} 个结果)\n"]
            for i, repo in enumerate(items, 1):
                lines.append(f"{i}. {repo['full_name']} ⭐{repo['stargazers_count']} | {repo.get('language', '?')}")
                lines.append(f"   {(repo.get('description') or '')[:100]}")
                lines.append(f"   {repo['html_url']}\n")
            return "\n".join(lines)
    except Exception as e:
        return f"GitHub 搜索失败: {e}"


@mcp.tool()
async def hackernews_top(count: int = 10) -> str:
    """获取 HackerNews 首页热门文章（全球开发者社区头条）"""
    try:
        async with httpx.AsyncClient(timeout=15, verify=certifi.where()) as cli:
            r = await cli.get("https://hacker-news.firebaseio.com/v0/topstories.json")
            ids = r.json()[:count]
            stories = []
            for sid in ids[:count]:
                sr = await cli.get(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json")
                if sr.status_code == 200:
                    s = sr.json()
                    stories.append({
                        "title": s.get("title", ""),
                        "url": s.get("url", f"https://news.ycombinator.com/item?id={sid}"),
                        "score": s.get("score", 0),
                        "comments": s.get("descendants", 0),
                    })
            lines = [f"🔥 HackerNews 热门 Top {len(stories)}\n"]
            for i, s in enumerate(stories, 1):
                lines.append(f"{i}. {s['title']} (↑{s['score']} 💬{s['comments']})")
                lines.append(f"   {s['url']}\n")
            return "\n".join(lines)
    except Exception as e:
        return f"HackerNews 获取失败: {e}"


@mcp.tool()
async def arxiv_latest(keyword: str = "AI agent", max_results: int = 5) -> str:
    """搜索 arXiv 最新 AI 论文（按提交时间倒序）"""
    import xml.etree.ElementTree as ET
    try:
        async with httpx.AsyncClient(timeout=20, verify=certifi.where()) as cli:
            r = await cli.get(
                "https://export.arxiv.org/api/query",
                params={
                    "search_query": f"all:{keyword}",
                    "start": 0,
                    "max_results": max_results,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                },
            )
            if r.status_code != 200:
                return f"arXiv API {r.status_code}"
        ns = {"a": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(r.text)
        entries = root.findall("a:entry", ns)
        lines = [f"📚 arXiv 最新: 「{keyword}」({len(entries)} 篇)\n"]
        for i, e in enumerate(entries, 1):
            title = (e.find("a:title", ns).text or "").strip().replace("\n", " ")
            url = e.find("a:id", ns).text or ""
            authors = [a.find("a:name", ns).text for a in e.findall("a:author", ns) if a.find("a:name", ns) is not None]
            published = (e.find("a:published", ns).text or "")[:10]
            lines.append(f"{i}. {title}")
            lines.append(f"   {', '.join(authors[:3])} | {published}")
            lines.append(f"   {url}\n")
        return "\n".join(lines)
    except Exception as e:
        return f"arXiv 搜索失败: {e}"


def _real_url(u: str) -> str:
    """DuckDuckGo HTML 的跳转链接形如 //duckduckgo.com/l/?uddg=<encoded_url>&...
    直接返回用户打不开，解出真实地址。"""
    if "duckduckgo.com/l/?uddg=" in u:
        try:
            return unquote(u.split("uddg=")[1].split("&")[0])
        except Exception:
            return u
    return u


@mcp.tool()
async def web_search(query: str, max_results: int = 5) -> str:
    """联网搜索网页（DuckDuckGo 免密钥，多源容错降级）"""
    import re
    try:
        async with httpx.AsyncClient(
            timeout=15, verify=certifi.where(), headers={"User-Agent": "Mozilla/5.0"}
        ) as cli:
            r = await cli.post("https://html.duckduckgo.com/html/", data={"q": query})
            if r.status_code != 200:
                return f"搜索失败: HTTP {r.status_code}"
        titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', r.text, re.DOTALL)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.DOTALL)
        links = re.findall(r'class="result__a"[^>]*href="([^"]+)"', r.text)
        if not titles:
            return "未获取到搜索结果（DuckDuckGo 可能临时限流）"
        lines = [f"🔎 搜索: 「{query}」\n"]
        for i in range(min(len(titles), max_results)):
            title = re.sub(r"<[^>]+>", "", titles[i]).strip()
            snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
            url = _real_url(links[i]) if i < len(links) else ""
            lines.append(f"{i + 1}. {title}")
            if snippet:
                lines.append(f"   {snippet[:140]}")
            if url:
                lines.append(f"   {url}")
        return "\n".join(lines)
    except Exception as e:
        return f"搜索失败: {e}"


if __name__ == "__main__":
    # stdio 传输：通过标准输入/输出与 Host 通信（本地子进程方式）
    mcp.run()
