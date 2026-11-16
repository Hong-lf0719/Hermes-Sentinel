"""Hermes 检索层（2026-11-16 从 server.py 拆出）——外部信息源统一入口。

GitHub / HackerNews / arXiv / 多源容错网页搜索。纯外部调用，无 DB 依赖，
被工具层（server.py）与情报层（hermes_intel.py）共用。
"""
from __future__ import annotations

import asyncio
import os

import certifi
import httpx

from hermes_common import GITHUB_TOKEN, logger


async def search_github(keyword: str, max_results: int = 5) -> str:
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=15, verify=certifi.where()) as cli:
            r = await cli.get("https://api.github.com/search/repositories", params={
                "q": keyword, "sort": "stars", "order": "desc", "per_page": max_results
            }, headers=headers)
            if r.status_code != 200:
                return f"GitHub API {r.status_code}"
            data = r.json()
            items = data.get("items", [])
            if not items:
                return f"🔍 没找到「{keyword}」"
            lines = [f"🔍 **GitHub: 「{keyword}」** ({data['total_count']} 个结果)\n"]
            for i, repo in enumerate(items, 1):
                lines.append(f"{i}. **{repo['full_name']}** ⭐{repo['stargazers_count']} | {repo.get('language','?')}")
                lines.append(f"   {(repo.get('description') or '')[:100]}")
                lines.append(f"   🔗 {repo['html_url']}\n")
            return "\n".join(lines)
    except Exception as e:
        return f"GitHub 搜索失败: {e}"


async def hackernews_top(count: int = 10) -> str:
    """获取 HackerNews 热门"""
    try:
        async with httpx.AsyncClient(timeout=15, verify=certifi.where()) as cli:
            # 获取 top story IDs
            r = await cli.get("https://hacker-news.firebaseio.com/v0/topstories.json")
            ids = r.json()[:count]

            # 逐条获取详情
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

            lines = [f"🔥 **HackerNews 热门 Top {len(stories)}**\n"]
            for i, s in enumerate(stories, 1):
                lines.append(f"{i}. **{s['title']}** (↑{s['score']} 💬{s['comments']})")
                lines.append(f"   🔗 {s['url']}\n")
            return "\n".join(lines)
    except Exception as e:
        return f"HackerNews 获取失败: {e}"


async def arxiv_latest(keyword: str = "AI agent", max_results: int = 5) -> str:
    """搜索 arXiv 最新论文（带超时退避重试，缓解限流/偶发连接失败）"""
    last_err = "arXiv 搜索失败"
    r = None
    for _attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=20, verify=certifi.where()) as cli:
                r = await cli.get("https://export.arxiv.org/api/query", params={
                    "search_query": f"all:{keyword}",
                    "start": 0,
                    "max_results": max_results,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                })
            if r.status_code == 200:
                last_err = ""
                break
            last_err = f"arXiv API {r.status_code}"
        except Exception as e:
            last_err = f"arXiv 请求异常: {e}"
        if _attempt < 2:
            await asyncio.sleep(2 * (_attempt + 1))
    if last_err:
        return last_err

    # 简单解析 XML
    import xml.etree.ElementTree as ET
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(r.text)
    entries = root.findall("a:entry", ns)

    lines = [f"📚 **arXiv 最新: 「{keyword}」** ({len(entries)} 篇)\n"]
    for i, e in enumerate(entries, 1):
        title = (e.find("a:title", ns).text or "").strip().replace("\n", " ")
        url = e.find("a:id", ns).text or ""
        authors = [a.find("a:name", ns).text for a in e.findall("a:author", ns) if a.find("a:name", ns) is not None]
        published = (e.find("a:published", ns).text or "")[:10]
        lines.append(f"{i}. **{title}**")
        lines.append(f"   👤 {', '.join(authors[:3])}{'...' if len(authors)>3 else ''} | 📅 {published}")
        lines.append(f"   🔗 {url}\n")
    return "\n".join(lines)


# ============================================================
# 网页搜索（多源容错：Tavily → Bing 可选 → DuckDuckGo → 优雅降级）
# ============================================================
def _parse_bing_cn_html(html_text: str, max_results: int) -> str:
    """从 Bing 中国站结果页解析 标题/链接/摘要（<li class="b_algo"> 结构，无外部依赖）"""
    import re
    out = []
    blocks = re.findall(r'<li class="b_algo".*?</li>', html_text, flags=re.DOTALL | re.IGNORECASE)
    for b in blocks[:max_results]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, flags=re.DOTALL | re.IGNORECASE)
        if not m:
            continue
        url = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        snip = ""
        ms = re.search(r"<p[^>]*>(.*?)</p>", b, flags=re.DOTALL | re.IGNORECASE)
        if ms:
            snip = re.sub(r"<[^>]+>", "", ms.group(1)).strip()
        if not title:
            continue
        out.append(f"{len(out) + 1}. **{title[:80]}**\n   {snip[:140]}\n   🔗 {url}\n")
    return "\n".join(out)


def _parse_ddg_html(html_text: str, max_results: int) -> str:
    """从 DuckDuckGo HTML 结果页解析 标题/链接/摘要（无外部依赖）"""
    import re
    titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html_text, re.DOTALL)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html_text, re.DOTALL)
    links = re.findall(r'class="result__a"[^>]*href="([^"]+)"', html_text)
    if not titles:
        return ""
    lines = []
    for i in range(min(len(titles), max_results)):
        title = re.sub(r"<[^>]+>", "", titles[i]).strip()
        snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
        url = links[i] if i < len(links) else ""
        lines.append(f"{i+1}. **{title}**")
        if snippet:
            lines.append(f"   {snippet[:140]}")
        if url:
            lines.append(f"   🔗 {url}")
    return "\n".join(lines)


async def web_search(query: str, max_results: int = 5) -> str:
    """
    多源容错的网页搜索。

    数据源（按优先级自动降级）：
      0. Tavily Search API —— 为 AI Agent 设计的搜索 API（免费额度 1000 次/月），
         注册 tavily.com 取 key 后设 TAVILY_API_KEY 即启用；返回干净正文，国内可达
      1. Bing Web Search v7 —— 若环境变量 BING_SEARCH_KEY 已配置
      2. DuckDuckGo HTML —— 免密钥（大陆网络常被墙）
      3. Bing 中国站 HTML 抓取 —— 免密钥国内直连（注意：连续自动化查询会被
         Bing 软封锁——返回不相关的词典类结果，此时应改用 Tavily）
      4. 全部失败 → 返回友好提示（不抛异常、不中断主流程）

    设计目标：任一搜索源抖动都不应导致 Agent 整体失败 —— 这是
    "多源容错降级"工程能力的体现。
    """
    # 数据源 0：Tavily（推荐：AI Agent 专用搜索 API，免费额度充足）
    tavily_key = os.getenv("TAVILY_API_KEY")
    if tavily_key:
        try:
            async with httpx.AsyncClient(timeout=20, verify=certifi.where()) as cli:
                r = await cli.post("https://api.tavily.com/search", json={
                    "api_key": tavily_key, "query": query,
                    "max_results": max_results, "search_depth": "advanced",
                    "include_answer": False,
                })
                if r.status_code == 200:
                    items = r.json().get("results", [])
                    if items:
                        lines = [f"🔎 **Tavily 搜索: 「{query}」**\n"]
                        for i, it in enumerate(items[:max_results], 1):
                            lines.append(f"{i}. **{it.get('title', '')}**")
                            content = (it.get("content", "") or "")[:160]
                            if content:
                                lines.append(f"   {content}")
                            lines.append(f"   🔗 {it.get('url', '')}\n")
                        return "\n".join(lines)
        except Exception as e:
            logger.warning(f"Tavily 搜索失败，降级下一数据源: {e}")

    # 数据源 1：Bing（可选）
    bing_key = os.getenv("BING_SEARCH_KEY")
    if bing_key:
        try:
            async with httpx.AsyncClient(timeout=15, verify=certifi.where()) as cli:
                r = await cli.get(
                    "https://api.bing.microsoft.com/v7.0/search",
                    headers={"Ocp-Apim-Subscription-Key": bing_key},
                    params={"q": query, "count": max_results, "mkt": "zh-CN"},
                )
                if r.status_code == 200:
                    items = r.json().get("webPages", {}).get("value", [])
                    if items:
                        lines = [f"🔎 **Bing 搜索: 「{query}」**\n"]
                        for i, it in enumerate(items[:max_results], 1):
                            lines.append(f"{i}. **{it.get('name', '')}**")
                            lines.append(f"   {(it.get('snippet', '') or '')[:140]}")
                            lines.append(f"   🔗 {it.get('url', '')}\n")
                        return "\n".join(lines)
        except Exception as e:
            logger.warning(f"Bing 搜索失败，降级 DuckDuckGo: {e}")

    # 数据源 2：DuckDuckGo HTML（免密钥）
    try:
        async with httpx.AsyncClient(timeout=15, verify=certifi.where(), headers={"User-Agent": "Mozilla/5.0"}) as cli:
            r = await cli.post("https://html.duckduckgo.com/html/", data={"q": query})
            if r.status_code == 200:
                parsed = _parse_ddg_html(r.text, max_results)
                if parsed:
                    return f"🔎 **DuckDuckGo 搜索: 「{query}」**\n{parsed}"
    except Exception as e:
        logger.warning(f"DuckDuckGo 搜索失败: {e}")

    # 数据源 2.5：Bing 中国站 HTML（免密钥 · 国内直连可用——DDG 被墙时的主兜底）
    try:
        async with httpx.AsyncClient(timeout=15, verify=certifi.where(),
                                     headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
                                              "Accept-Language": "zh-CN,zh;q=0.9"}) as cli:
            r = await cli.get("https://cn.bing.com/search", params={"q": query, "count": max_results})
            if r.status_code == 200 and "b_results" in r.text:
                parsed = _parse_bing_cn_html(r.text, max_results)
                if parsed:
                    return f"🔎 **Bing(国内) 搜索: 「{query}」**\n{parsed}"
    except Exception as e:
        logger.warning(f"Bing 国内站搜索失败: {e}")

    # 数据源 3：优雅降级
    return f"⚠️ 网页搜索暂时不可用（Bing/DuckDuckGo 均失败），已跳过联网检索。"


async def fetch_news(query: str, max_results: int = 5) -> str:
    """新闻检索便捷封装，report 函数共用"""
    return await web_search(query, max_results)
