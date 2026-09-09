"""武大源熔断器验证：站点半死（列表 200 / 详情 502）时是否会狂发请求。

复现真实故障形态：getToken 与列表接口正常，只有 ajax_show（详情）连续 502。
无熔断时对 DETAIL_LIMIT(230) 条简章逐个发请求逐个失败 → 日志刷屏 + 空耗配额。
运行：D:/Anaconda/python.exe test_whu_breaker.py
"""
import sys, asyncio, logging

sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(message)s")

from job_sentinel.sources import whu_jobs  # noqa: E402

PASS, FAIL = "✅", "❌"
results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(f"{PASS if cond else FAIL} {name}" + (f"\n      → {detail}" if detail else ""))


stat = {"token": 0, "list": 0, "detail": 0}


class Resp:
    def __init__(self, payload, code=200):
        self._p, self.status_code = payload, code

    def json(self):
        if self.status_code >= 400:
            raise ValueError("bad gateway html")
        return self._p


class HalfDeadClient:
    """getToken / 列表正常，详情(ajax_show) 恒定 502 —— 复现线上真实故障形态。"""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **k):
        stat["token"] += 1
        return Resp({"data": "fake-token"})

    async def post(self, url, **k):
        if "ajax_show" in url:
            stat["detail"] += 1
            return Resp(None, 502)          # 详情：恒定 502
        stat["list"] += 1
        # 每页返回 30 条简章，足以触发「逐个拉详情」的洪水
        items = [{"id": f"id{i}", "corporationName": f"公司{i}"} for i in range(30)]
        return Resp({"object": {"list": items, "totalPage": 1}})


whu_jobs.httpx.AsyncClient = HalfDeadClient


def reset_counters():
    """只清请求计数，不影响熔断器状态。"""
    for k in stat:
        stat[k] = 0


def reset_all():
    """连熔断器一起复位（仅在需要「从干净状态开始」时使用）。"""
    whu_jobs._fail_streak = 0
    whu_jobs._open_until = 0.0
    reset_counters()


profile = {"regions": ["武汉"], "cities": []}

print("=" * 64)
print("场景：列表接口正常，详情接口恒定 502")
print("=" * 64)
reset_all()
src = whu_jobs.WhuJobsSource()
jobs = asyncio.run(src.fetch(profile))

print(f"\n请求统计：getToken={stat['token']}  列表={stat['list']}  详情={stat['detail']}")
check("详情请求被熔断截断（远小于 DETAIL_LIMIT=230）",
      stat["detail"] <= whu_jobs.FAIL_THRESHOLD + 5,
      f"实际发出 {stat['detail']} 次详情请求；无熔断时应接近 230 次")
check("熔断器已断开", whu_jobs._breaker_open(),
      f"冷却至 {whu_jobs._open_until:.0f}（当前 {__import__('time').time():.0f}）")
check("本轮返回空结果但不抛异常", jobs == [], f"返回 {len(jobs)} 条岗位")

print()
print("=" * 64)
print("冷却期内再次扫描：应整源跳过，0 请求")
print("=" * 64)
reset_counters()          # 注意：不清熔断器，保持上一轮已断开的状态
jobs2 = asyncio.run(src.fetch(profile))
total2 = sum(stat.values())
check("冷却期内不发任何请求", total2 == 0, f"实际发出 {total2} 次请求")
check("直接返回空列表", jobs2 == [], f"返回 {len(jobs2)} 条")

print()
print("=" * 64)
print("冷却结束后：应自动恢复探测")
print("=" * 64)
reset_counters()
whu_jobs._open_until = 0.0          # 模拟冷却已过
asyncio.run(src.fetch(profile))
check("冷却结束后恢复探测（重新发起请求）", stat["token"] + stat["list"] > 0,
      f"getToken={stat['token']} 列表={stat['list']} 详情={stat['detail']}")

print()
print("=" * 64)
ok = sum(results)
print(f"汇总：{ok}/{len(results)} 项通过" + ("  🎉 全绿" if ok == len(results) else "  ⚠️ 有失败项"))
print("=" * 64)
