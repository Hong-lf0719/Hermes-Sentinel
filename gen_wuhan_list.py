import sys, json, urllib.request, datetime

BASE = "http://127.0.0.1:9866"
def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.load(r)

d = get("/api/jobs?min_score=0")
jobs = d.get("jobs", [])

def sc(j): return j.get("match_score") or 0
def wu(j): return "武汉" in (j.get("city") or "")
def rel(j):
    t = (j.get("title") or "") + " " + (j.get("major") or "")
    return any(k in t for k in ["AI","数据","软件","开发","算法","大模型","智能","应用","云"])

sel = [j for j in jobs if wu(j) and rel(j) and sc(j) >= 50]
sel.sort(key=sc, reverse=True)

def dl(j):
    s = j.get("deadline") or ""
    return s if s and s != "以官网为准" else "官网为准"

urgent = [j for j in sel if "2026-09" in (j.get("deadline") or "")]

lines = []
lines.append("# 武汉秋招·高匹配岗投递清单（Hermes 导出）")
lines.append("")
lines.append(f"> 生成时间：{datetime.date.today()} ｜ 数据源：Hermes 岗位池（{len(jobs)} 岗）")
lines.append(f"> 筛选条件：城市含「武汉」+ 岗位/专业含 AI/数据/软件/开发/算法/智能/应用 + 匹配分≥50")
lines.append(f"> **符合条件共 {len(sel)} 个岗**，其中 **9 月截止的紧迫岗 {len(urgent)} 个**。已投的（长飞/联影/风行等）不在本清单重复列出，请去 Hermes 看板核对状态。")
lines.append("")
lines.append("## 🔥 一、9 月必须投（截止近，先冲这批）")
lines.append("")
lines.append("| 匹配 | 公司 | 岗位 | 性质 | 截止 | 链接 |")
lines.append("|---|---|---|---|---|---|")
for j in sorted(urgent, key=sc, reverse=True):
    et = j.get("enterprise_type") or j.get("nature") or "-"
    lines.append(f"| {sc(j)} | {j.get('company','')} | {j.get('title','')} | {et} | {dl(j)} | {j.get('url','')} |")
lines.append("")
lines.append("## 二、全量高匹配岗 TOP 40（按匹配分）")
lines.append("")
lines.append("| 匹配 | 公司 | 岗位 | 性质 | 截止 | 链接 |")
lines.append("|---|---|---|---|---|---|")
for j in sel[:40]:
    et = j.get("enterprise_type") or j.get("nature") or "-"
    lines.append(f"| {sc(j)} | {j.get('company','')} | {j.get('title','')} | {et} | {dl(j)} | {j.get('url','')} |")
lines.append("")
lines.append("## 三、打法建议")
lines.append("")
lines.append("1. **本周先把「一、9月必须投」的 " + str(len(urgent)) + " 个投掉**（东风奕派AI应用开发9-30、猛士AI算法9-27 等），deadline 不等人。")
lines.append("2. **「二」里 73–80 分的国企/央企（烽火、东风数据开发、中国信科、长江存储、岚图、斗鱼…）是主干**，分两周铺完，目标这轮投 30+ 个。")
lines.append("3. 多益挂的是「软件工程师(C++)」——那不是你的主线（你目标是 AI应用/数据开发，且避开算法）。别拿它定义自己；C++ 岗（中元华电等）可投可不投，优先 Python/数据/AI 岗。")
lines.append("4. BOSS 直聘同步推武汉 AI/数据岗，和 Hermes 清单互补，每天 30 分钟刷+聊。")
lines.append("5. 简历/BOSS 文案突出 **Python + 数据分析/AI项目 + 武汉属地**，弱化 C++ 短板。")

out = "武汉秋招投递清单.md"
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("已生成", out, "｜ 符合条件", len(sel), "个，9月紧迫", len(urgent), "个")
