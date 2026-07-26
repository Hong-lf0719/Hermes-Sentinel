"""
Hermes 日报 HTML 渲染模块

把 ai_daily_report 产出的结构化数据（dict）渲染成：
  1. 手机 QQ 邮箱兼容的响应式 HTML（table 布局 + 全内联样式）
  2. 纯文本兜底（不支持 HTML 的渠道用）

设计约定（来自用户确认）：
- 简洁大气、深蓝头部、配色编码：蓝=必看 / 金=行动 / 绿=项目 / 灰=资讯
- 除信息罗列外，必须含「可行性速判」与「本周落地步骤」区块，方便用户针对性追问
- 求职方向罗盘 / 可行性 / 步骤为针对用户画像（2027届数据科学 → AI Agent）的固定增值区块
"""

import html as _html


def _e(s):
    """HTML 转义，防止描述/标题里的特殊字符破坏布局。"""
    return _html.escape(str(s if s is not None else ""), quote=True)


def _stars(n):
    """把 star 数字格式化成 ⭐N。"""
    try:
        return f"⭐{int(n)}"
    except (TypeError, ValueError):
        return _e(n)


def render_report_html(data: dict, direction: str = "数据科学") -> tuple:
    """
    渲染日报。

    :param data: ai_daily_report / ai_job_report 返回的结构化数据，字段：
        date, title, intro, top3[ {title, stars, desc, value, url} ],
        projects[ {name, stars, lang, desc, tag} ],
        bigtech[ {domestic[str], international[str]} ]（求职专题版可选）,
        industry[ str ], learning[ str ], one_liner
    :param direction: 用户方向（用于头部定制文本）
    :return: (text_fallback, html)
    """
    date = _e(data.get("date", ""))
    title = _e(data.get("title", "AI 日报"))
    intro = _e(data.get("intro", ""))
    top3 = data.get("top3", []) or []
    projects = data.get("projects", []) or []
    industry = data.get("industry", []) or []
    learning = data.get("learning", []) or []
    one_liner = _e(data.get("one_liner", ""))

    # ---------- TOP3 卡片 ----------
    top3_html = ""
    for i, item in enumerate(top3[:3], 1):
        url = item.get("url", "") or ""
        value = _e(item.get("value", ""))
        link_html = (
            f'<a href="{_e(url)}" style="font-size:12px; color:#2563eb; text-decoration:none;">🔗 查看原文</a>'
            if url else ""
        )
        value_html = (
            f'<div style="margin-top:8px; font-size:12px; color:#2563eb; background:#eff6ff; '
            f'display:inline-block; padding:3px 10px; border-radius:20px; margin-right:6px;">'
            f'🎯 {value}</div>' if value else ""
        )
        top3_html += f"""
          <tr>
            <td style="padding:10px 30px;">
              <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc; border-left:4px solid #2563eb; border-radius:8px;">
                <tr>
                  <td style="padding:14px 16px;">
                    <div style="font-size:16px; font-weight:800; color:#0f172a; margin-bottom:4px;">{i} · {_e(item.get('title',''))} <span style="color:#f59e0b; font-size:13px; font-weight:700;">{_stars(item.get('stars',''))}</span></div>
                    <div style="font-size:13px; color:#475569; line-height:1.6;">{_e(item.get('desc',''))}</div>
                    <div style="margin-top:8px;">{value_html}{link_html}</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>"""

    # ---------- 项目表格 ----------
    proj_rows = ""
    shade = False
    for p in projects:
        tag = p.get("tag", "")
        tag_html = (
            f' <span style="color:#10b981; font-weight:700;">🔥{_e(tag)}</span>' if tag else ""
        )
        bg = ' style="background:#fafcff;"' if shade else ""
        proj_rows += f"""
                <tr{bg}>
                  <td style="padding:8px; border-bottom:1px solid #f1f5f9;"><span style="font-weight:700; color:#0f172a;">{_e(p.get('name',''))}</span><br><span style="color:#94a3b8; font-size:11px;">{_e(p.get('lang',''))}</span></td>
                  <td style="padding:8px; border-bottom:1px solid #f1f5f9; text-align:center; color:#f59e0b; font-weight:800;">{_stars(p.get('stars',''))}</td>
                  <td style="padding:8px; border-bottom:1px solid #f1f5f9; color:#475569;">{_e(p.get('desc',''))}{tag_html}</td>
                </tr>"""
        shade = not shade

    # ---------- 行业信号 ----------
    industry_html = "".join(
        f'<br>· {_e(x)}' for x in industry
    )
    if industry_html:
        industry_html = industry_html[4:]  # 去掉首个 <br>

    # ---------- 学习建议 ----------
    learning_html = "".join(
        f'<br>· {_e(x)}' for x in learning
    )
    if learning_html:
        learning_html = learning_html[4:]

    # ---------- 动态增值区块（LLM 按方向生成）----------
    roadmap = data.get("roadmap", []) or []
    actions = data.get("actions", []) or []
    weekly_focus = data.get("weekly_focus") or {}
    interview_tips = data.get("interview_tips", []) or []

    # 方向罗盘
    if roadmap:
        roadmap_rows = ""
        shade = False
        for item in roadmap:
            bg = ' style="background:#fafcff;"' if shade else ""
            roadmap_rows += f"""
                <tr{bg}>
                  <td style="padding:9px 8px; border-bottom:1px solid #f1f5f9;"><span style="font-weight:700; color:#0f172a;">{_e(item.get('name',''))}</span></td>
                  <td style="padding:9px 8px; border-bottom:1px solid #f1f5f9; text-align:center; color:#ef4444; font-weight:800; font-size:11px;">{_e(item.get('heat',''))}</td>
                  <td style="padding:9px 8px; border-bottom:1px solid #f1f5f9; text-align:center; color:#475569;">{_e(item.get('difficulty',''))}</td>
                  <td style="padding:9px 8px; border-bottom:1px solid #f1f5f9; color:#475569;">{_e(item.get('reason',''))}</td>
                </tr>"""
            shade = not shade
        roadmap_html = f"""
          <!-- 🧭 求职方向罗盘 -->
          <tr>
            <td style="padding:14px 30px 4px;">
              <div style="font-size:15px; font-weight:800; color:#0f172a; border-left:4px solid #f59e0b; padding-left:10px;">🧭 求职方向罗盘 · 该学什么</div>
            </td>
          </tr>
          <tr>
            <td style="padding:10px 30px;">
              <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse; font-size:12px;">
                <tr style="color:#94a3b8; font-size:11px; text-transform:uppercase; letter-spacing:.5px;">
                  <td style="padding:6px 8px; border-bottom:2px solid #eef1f5; text-align:left;">方向</td>
                  <td style="padding:6px 8px; border-bottom:2px solid #eef1f5; text-align:center; width:96px;">热度</td>
                  <td style="padding:6px 8px; border-bottom:2px solid #eef1f5; text-align:center; width:70px;">难度</td>
                  <td style="padding:6px 8px; border-bottom:2px solid #eef1f5; text-align:left;">推荐理由</td>
                </tr>
                {roadmap_rows}
              </table>
            </td>
          </tr>
          <tr><td style="padding:6px 30px;"><div style="border-top:1px solid #eef1f5;"></div></td></tr>"""
    else:
        roadmap_html = ""

    # 可行性速判
    if actions:
        action_rows = ""
        shade = False
        for item in actions:
            bg = ' style="background:#fafcff;"' if shade else ""
            hint = item.get("hint", "")
            hint_html = f'<br><span style="color:#94a3b8; font-size:11px;">{_e(hint)}</span>' if hint else ""
            action_rows += f"""
                <tr{bg}>
                  <td style="padding:9px 8px; border-bottom:1px solid #f1f5f9;"><span style="font-weight:700; color:#0f172a;">{_e(item.get('name',''))}</span>{hint_html}</td>
                  <td style="padding:9px 8px; border-bottom:1px solid #f1f5f9; text-align:center; color:#10b981; font-weight:800;">{_e(item.get('feasible',''))}</td>
                  <td style="padding:9px 8px; border-bottom:1px solid #f1f5f9; text-align:center; color:#475569;">{_e(item.get('cycle',''))}</td>
                  <td style="padding:9px 8px; border-bottom:1px solid #f1f5f9; text-align:center; color:#f59e0b; font-weight:800;">{_e(item.get('leverage',''))}</td>
                </tr>"""
            shade = not shade
        actions_html = f"""
          <!-- 📋 可行性速判 -->
          <tr>
            <td style="padding:14px 30px 4px;">
              <div style="font-size:15px; font-weight:800; color:#0f172a; border-left:4px solid #f59e0b; padding-left:10px;">📋 可行性速判 · 值不值得做</div>
            </td>
          </tr>
          <tr>
            <td style="padding:10px 30px;">
              <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse; font-size:12px;">
                <tr style="color:#94a3b8; font-size:11px; text-transform:uppercase; letter-spacing:.5px;">
                  <td style="padding:6px 8px; border-bottom:2px solid #eef1f5; text-align:left;">候选动作</td>
                  <td style="padding:6px 8px; border-bottom:2px solid #eef1f5; text-align:center; width:64px;">可行性</td>
                  <td style="padding:6px 8px; border-bottom:2px solid #eef1f5; text-align:center; width:70px;">周期</td>
                  <td style="padding:6px 8px; border-bottom:2px solid #eef1f5; text-align:center; width:88px;">求职杠杆</td>
                </tr>
                {action_rows}
              </table>
            </td>
          </tr>
          <tr><td style="padding:6px 30px;"><div style="border-top:1px solid #eef1f5;"></div></td></tr>"""
    else:
        actions_html = ""

    # 本周落地步骤
    if weekly_focus and weekly_focus.get("steps"):
        steps_html = "<br>".join(f'<b style="color:#92400e;">{_e(s)}</b>' for s in weekly_focus["steps"])
        weekly_html = f"""
          <!-- 🪜 本周落地步骤 -->
          <tr>
            <td style="padding:14px 30px 4px;">
              <div style="font-size:15px; font-weight:800; color:#0f172a; border-left:4px solid #f59e0b; padding-left:10px;">🪜 {_e(weekly_focus.get('title','本周落地步骤'))}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 30px 2px;">
              <table width="100%" cellpadding="0" cellspacing="0" style="background:#fffbeb; border-radius:8px;">
                <tr><td style="padding:14px 16px; font-size:13px; color:#713f12; line-height:1.95;">
                  {steps_html}
                </td></tr>
              </table>
            </td>
          </tr>
          <tr><td style="padding:6px 30px;"><div style="border-top:1px solid #eef1f5;"></div></td></tr>"""
    else:
        weekly_html = ""

    # 面试话术
    if interview_tips:
        tips_html = "<br>".join(f'· {_e(t)}' for t in interview_tips)
    else:
        tips_html = '· "精通多 Agent 系统架构与上下文工程"<br>· "深入理解知识图谱增强的 Agent 记忆机制"<br>· "具备 Agent + 垂直领域落地经验"'

    # ---------- 大厂动态（求职专题版可选） ----------
    bigtech = data.get("bigtech")
    bigtech_html = ""
    if isinstance(bigtech, dict):
        def _bt(items):
            items = items or []
            s = "".join(f'<br>· {_e(x)}' for x in items)
            return s[4:] if s else "暂无"
        domestic = bigtech.get("domestic", []) or []
        international = bigtech.get("international", []) or []
        bigtech_html = f"""
          <!-- 🏢 大厂动态 -->
          <tr>
            <td style="padding:14px 30px 4px;">
              <div style="font-size:15px; font-weight:800; color:#0f172a; border-left:4px solid #64748b; padding-left:10px;">🏢 大厂 AI 动态 · 求职信号</div>
            </td>
          </tr>
          <tr>
            <td style="padding:10px 30px;">
              <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse; font-size:12px;">
                <tr><td style="padding:9px 10px; background:#f1f5f9; font-weight:800; color:#0f172a; border-radius:8px 8px 0 0;">🇨🇳 国内大厂</td></tr>
                <tr><td style="padding:8px 10px 12px; color:#475569; line-height:1.85;">{_bt(domestic)}</td></tr>
                <tr><td style="padding:9px 10px; background:#f1f5f9; font-weight:800; color:#0f172a;">🌐 国际动态</td></tr>
                <tr><td style="padding:8px 10px 6px; color:#475569; line-height:1.85;">{_bt(international)}</td></tr>
              </table>
            </td>
          </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} | {date}</title>
</head>
<body style="margin:0; padding:0; background:#eef1f5; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif; -webkit-font-smoothing:antialiased;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f5;">
    <tr>
      <td align="center" style="padding:18px 12px;">

        <table width="600" cellpadding="0" cellspacing="0" style="width:600px; max-width:100%; background:#ffffff; border-radius:14px; overflow:hidden; border:1px solid #e5e9f0;">

          <!-- 头部 -->
          <tr>
            <td style="background:#0f172a; padding:26px 30px;">
              <div style="color:#64748b; font-size:12px; letter-spacing:2px; font-weight:600;">HERMES · AI 情报官</div>
              <div style="color:#ffffff; font-size:23px; font-weight:800; margin-top:6px; letter-spacing:.5px;">{title}</div>
              <div style="color:#94a3b8; font-size:13px; margin-top:8px;">{date} &nbsp;·&nbsp; 为你定制：{direction} 方向</div>
            </td>
          </tr>

          <!-- 引言 -->
          {"".join(f'''          <tr>
            <td style="padding:18px 30px 6px; color:#475569; font-size:14px; line-height:1.6;">
              {intro}
            </td>
          </tr>
''' if intro else '''          <tr>
            <td style="padding:18px 30px 6px; color:#475569; font-size:14px; line-height:1.6;">
              本周 AI 动态精选。<span style="color:#0f172a; font-weight:700;">已按「对你的求职价值」排序</span>。
            </td>
          </tr>
''')}

          <!-- ⭐ TOP3 -->
          <tr>
            <td style="padding:14px 30px 4px;">
              <div style="font-size:15px; font-weight:800; color:#0f172a; border-left:4px solid #2563eb; padding-left:10px;">⭐ 重磅推荐 · 必看 TOP 3</div>
            </td>
          </tr>
          {top3_html}

          <!-- 分隔 -->
          <tr><td style="padding:6px 30px;"><div style="border-top:1px solid #eef1f5;"></div></td></tr>

          {roadmap_html}

          <!-- 📦 项目速递 -->
          <tr>
            <td style="padding:14px 30px 4px;">
              <div style="font-size:15px; font-weight:800; color:#0f172a; border-left:4px solid #10b981; padding-left:10px;">📦 开源项目速递 · {len(projects)} 个</div>
            </td>
          </tr>
          <tr>
            <td style="padding:10px 30px;">
              <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse; font-size:12px;">
                <tr style="color:#94a3b8; font-size:11px; text-transform:uppercase; letter-spacing:.5px;">
                  <td style="padding:6px 8px; border-bottom:2px solid #eef1f5; text-align:left;">项目</td>
                  <td style="padding:6px 8px; border-bottom:2px solid #eef1f5; text-align:center; width:58px;">Stars</td>
                  <td style="padding:6px 8px; border-bottom:2px solid #eef1f5; text-align:left;">一句话 / 标签</td>
                </tr>
                {proj_rows}
              </table>
            </td>
          </tr>

          {bigtech_html}

          <!-- 分隔 -->
          <tr><td style="padding:6px 30px;"><div style="border-top:1px solid #eef1f5;"></div></td></tr>

          <!-- 📰 行业信号 -->
          <tr>
            <td style="padding:14px 30px 4px;">
              <div style="font-size:15px; font-weight:800; color:#0f172a; border-left:4px solid #64748b; padding-left:10px;">📰 行业信号</div>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 30px 2px;">
              <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc; border-radius:8px;">
                <tr><td style="padding:14px 16px; font-size:13px; color:#475569; line-height:1.8;">{industry_html or "· 暂无"}</td></tr>
              </table>
            </td>
          </tr>

          <!-- 分隔 -->
          <tr><td style="padding:6px 30px;"><div style="border-top:1px solid #eef1f5;"></div></td></tr>

          {actions_html}
          {weekly_html}

          <!-- 🎯 行动清单 -->
          <tr>
            <td style="padding:14px 30px 4px;">
              <div style="font-size:15px; font-weight:800; color:#0f172a; border-left:4px solid #f59e0b; padding-left:10px;">🎯 本周行动清单</div>
            </td>
          </tr>
          <tr>
            <td style="padding:10px 30px 4px;">
              <table width="100%" cellpadding="0" cellspacing="0" style="background:#fffbeb; border-radius:8px;">
                <tr><td style="padding:14px 16px; font-size:13px; color:#713f12; line-height:1.9;">
                  <b style="color:#92400e;">📚 学习建议</b><br>
                  {learning_html or "· 暂无"}<br>
                  <b style="color:#92400e; display:block; margin-top:8px;">💡 面试话术参考（可直接抄）</b><br>
                  {tips_html}<br>
                  <b style="color:#92400e; display:block; margin-top:8px;">⚡ 本周建议</b><br>
                  · 结合本周 top3 和项目速递，选一个感兴趣的开源项目动手跑起来<br>
                  · 把日报里的学习建议拆解成具体可执行的小任务<br>
                  · 关注与你方向相关的技术趋势，在面试中能聊出深度
                </td></tr>
              </table>
            </td>
          </tr>

          <!-- 页脚 -->
          <tr>
            <td style="padding:20px 30px; background:#f8fafc;">
              <div style="font-size:12px; color:#94a3b8; line-height:1.6;">
                数据来源：GitHub API · HackerNews · arXiv<br>
                Hermes AI 情报官 · 让信息找人，而不是人找信息
              </div>
            </td>
          </tr>

        </table>

        <div style="font-size:11px; color:#b8c0cc; text-align:center; padding-top:14px;">
          你收到这封邮件是因为订阅了 Hermes 每日 AI 情报 · 回复即可对话
        </div>

      </td>
    </tr>
  </table>
</body>
</html>"""

    # ---------- 纯文本兜底 ----------
    text_lines = [f"{title} | {date}", f"为你定制：{direction} 方向", ""]
    if intro:
        text_lines.append(intro)
        text_lines.append("")
    if top3:
        text_lines.append("⭐ 重磅推荐 TOP3")
        for i, item in enumerate(top3[:3], 1):
            text_lines.append(f"{i}. {item.get('title','')} ({item.get('stars','')})")
            if item.get("value"):
                text_lines.append(f"   🎯 {item.get('value')}")
            if item.get("url"):
                text_lines.append(f"   🔗 {item.get('url')}")
        text_lines.append("")
    if projects:
        text_lines.append("📦 开源项目速递")
        for p in projects:
            tag = f" 🔥{p.get('tag')}" if p.get("tag") else ""
            text_lines.append(f"- {p.get('name','')} {p.get('stars','')} [{p.get('lang','')}] {p.get('desc','')}{tag}")
        text_lines.append("")
    if industry:
        text_lines.append("📰 行业信号")
        for x in industry:
            text_lines.append(f"- {x}")
        text_lines.append("")
    bigtech = data.get("bigtech")
    if isinstance(bigtech, dict):
        text_lines.append("🏢 大厂 AI 动态")
        for x in (bigtech.get("domestic", []) or []):
            text_lines.append(f"- [国内] {x}")
        for x in (bigtech.get("international", []) or []):
            text_lines.append(f"- [国际] {x}")
        text_lines.append("")
    if learning:
        text_lines.append("🎓 学习建议")
        for x in learning:
            text_lines.append(f"- {x}")
        text_lines.append("")
    if roadmap:
        text_lines.append("🧭 求职方向罗盘")
        for item in roadmap:
            text_lines.append(f"- {item.get('name','')} | 热度:{item.get('heat','')} | 难度:{item.get('difficulty','')} | {item.get('reason','')}")
        text_lines.append("")
    if actions:
        text_lines.append("📋 可行性速判")
        for item in actions:
            text_lines.append(f"- {item.get('name','')} | 可行性:{item.get('feasible','')} | 周期:{item.get('cycle','')} | 杠杆:{item.get('leverage','')}")
        text_lines.append("")
    if weekly_focus and weekly_focus.get("steps"):
        text_lines.append(f"🪜 {weekly_focus.get('title','本周落地步骤')}")
        for s in weekly_focus["steps"]:
            text_lines.append(f"  {s}")
        text_lines.append("")
    if interview_tips:
        text_lines.append("💡 面试话术参考")
        for t in interview_tips:
            text_lines.append(f"- {t}")
        text_lines.append("")
    if one_liner:
        text_lines.append(f"💡 {one_liner}")
    text_lines.append("")
    text_lines.append("（本邮件含 HTML 精排版，建议在手机 QQ 邮箱中查看）")
    text = "\n".join(text_lines)

    return text, html


if __name__ == "__main__":
    # 自测：用一份示例数据渲染并打印长度
    sample = {
        "date": "2026-07-26",
        "title": "AI 日报",
        "top3": [
            {"title": "梁文锋 4 小时投资人会议实录", "stars": 86, "desc": "DeepSeek 创始人闭门发言。", "value": "面试谈 AGI 战略用得上", "url": "https://github.com/x"},
            {"title": "中国大模型行业全景", "stars": 134, "desc": "国内 LLM 全模型对比。", "value": "国内 AI 格局一键补齐", "url": "https://github.com/y"},
            {"title": "Karpathy 编码行为准则", "stars": 170, "desc": "LLM 写代码容易犯的错。", "value": "理解 LLM 局限=AI 工程师第一步", "url": "https://github.com/z"},
        ],
        "projects": [
            {"name": "caspian-sdk", "stars": 191, "lang": "Python/TS", "desc": "一套 API 让 Agent 接入全渠道", "tag": "你的方向"},
            {"name": "pi-coding-agent", "stars": 191, "lang": "Kotlin", "desc": "全栈 AI 编程 Agent", "tag": "你的方向"},
            {"name": "VinvAI", "stars": 28, "lang": "Python", "desc": "MCP 编码助手", "tag": "MCP"},
        ],
        "industry": ["Coding Agent 赛道白热化", "MCP 成事实标准"],
        "learning": ["读梁文锋会议实录", "学 MCP 协议并写 Server"],
        "one_liner": "Agent 正从 Demo 进入工程化阶段。",
    }
    # 求职专题版（含大厂动态）
    job = dict(sample)
    job["title"] = "AI 岗位情报"
    job["bigtech"] = {
        "domestic": ["DeepSeek：梁文锋会议透露 AGI 路线图与开源战略", "字节：豆包 Seedance 2.0 视频模型全面接入"],
        "international": ["Coding Agent 赛道白热化，本周新增 5+ 编程 Agent 项目", "MCP 成事实标准，新项目几乎都集成 MCP"],
    }
    t, h = render_report_html(sample)
    tj, hj = render_report_html(job)
    print(f"daily : text={len(t)}, html={len(h)}")
    print(f"job   : text={len(tj)}, html={len(hj)}")
    print("daily 关键区块:", all(k in h for k in ["重磅推荐", "求职方向罗盘", "可行性速判", "本周落地步骤", "本周行动清单"]))
    print("job 大厂动态区块:", ("大厂 AI 动态" in hj) and ("国内大厂" in hj) and ("国际动态" in hj))
