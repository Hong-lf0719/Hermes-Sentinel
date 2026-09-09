"""Hermes 方案一/方案二 打桩端到端回归（不依赖 DeepSeek 网络）。

用假 HTTP 客户端替代 api.deepseek.com，直接驱动真实的 process_chat()。
运行：D:/Anaconda/python.exe e2e_mock_test.py   （必须用 Anaconda python，托管版缺 numpy）

覆盖：
  1. 一轮内多个独立工具是否真的并行（执行区间重叠）
  2. 工具消息是否按 tool_calls 顺序、且每个 call 都有对应消息（否则 API 400）
  3. 单工具彻底失败时，同轮其他工具结果是否保留、整轮是否不崩
  4. _run_one_tool 双层兜底（trace 落库抛异常不外泄）
  5. _verify_result 新语义：exit≠0 不误杀、空结果不误杀、瞬时故障可重试
  6. 重试策略：瞬时故障消耗重试次数，确定性失败不重试
"""
import sys, json, time, asyncio

sys.path.insert(0, ".")
import server

PASS, FAIL = "✅", "❌"
results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"{PASS if cond else FAIL} {name}" + (f"\n      → {detail}" if detail else ""))


# ---------- 假 HTTP 客户端 ----------
class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._p


class FakeClient:
    """第1次返回 2 个 tool_calls，之后返回纯文本（结束工具循环）。"""

    def __init__(self, *a, **k):
        self.n = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None, **kw):
        self.n += 1
        if self.n == 1:
            return FakeResp({"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [
                    {"id": "call_hn", "type": "function",
                     "function": {"name": "hackernews_top", "arguments": "{}"}},
                    {"id": "call_ax", "type": "function",
                     "function": {"name": "arxiv_latest", "arguments": "{}"}},
                ]}}]})
        return FakeResp({"choices": [{"message": {
            "role": "assistant", "content": "最终回答：两路都查完了"}}]})


spans, saved = {}, []


async def fake_hn(**kw):
    spans["hackernews_top"] = [time.perf_counter(), None]
    await asyncio.sleep(0.6)
    spans["hackernews_top"][1] = time.perf_counter()
    return "HN热榜: A / B / C"


async def fake_ax(**kw):
    spans["arxiv_latest"] = [time.perf_counter(), None]
    await asyncio.sleep(0.6)
    spans["arxiv_latest"][1] = time.perf_counter()
    return "arXiv: 论文1 / 论文2"


def fake_save_message(session_id, role, content, tool_name=None, tool_result=None):
    saved.append((role, tool_name))


def fake_save_trace(*a, **k):
    pass


async def _coro(v):
    return v


def install():
    server.httpx.AsyncClient = FakeClient
    server.save_message = fake_save_message
    server._save_trace = fake_save_trace
    server.self_diagnose = lambda *a, **k: _coro(None)
    server.TOOL_MAP["hackernews_top"] = fake_hn
    server.TOOL_MAP["arxiv_latest"] = fake_ax


# ============================================================
print("=" * 64)
print("测试 1：并行执行 —— 两个独立工具是否真的同时跑")
print("=" * 64)
install()
spans.clear(); saved.clear()
t0 = time.perf_counter()
reply = asyncio.run(server.process_chat("查 HN 和 arXiv", session_id="mock_1"))
elapsed = time.perf_counter() - t0
a, b = spans.get("hackernews_top"), spans.get("arxiv_latest")
check("两个工具都被执行", bool(a and b), f"spans={list(spans)}")
if a and b:
    overlap = a[0] < b[1] and b[0] < a[1]
    check("执行区间重叠 → 确认并行（非串行）", overlap,
          f"单工具各~0.6s；工具段并行耗时 {(max(a[1],b[1])-min(a[0],b[0])):.2f}s，"
          f"串行应为 {(a[1]-a[0])+(b[1]-b[0]):.2f}s（总 {elapsed:.2f}s 含 RAG/DB 等开销）")
check("回复正常返回", isinstance(reply, str) and "出错了" not in reply, str(reply)[:60])

print()
print("=" * 64)
print("测试 2：每个 tool_call 都有对应 tool 消息（缺一条就会 API 400）")
print("=" * 64)
tool_msgs = [t for r, t in saved if r == "tool"]
check("落库 2 条 tool 消息（与 2 个 tool_calls 一一对应）", len(tool_msgs) == 2, f"实际={tool_msgs}")
check("顺序与 tool_calls 一致（hackernews_top → arxiv_latest）",
      tool_msgs == ["hackernews_top", "arxiv_latest"], f"实际={tool_msgs}")

print()
print("=" * 64)
print("测试 3：单工具彻底失败 —— 同轮其他工具结果是否保留、整轮是否不崩")
print("=" * 64)


async def boom(**kw):
    raise RuntimeError("模拟工具彻底挂掉")


install()
server.TOOL_MAP["arxiv_latest"] = boom
spans.clear(); saved.clear()
reply3 = asyncio.run(server.process_chat("查 HN 和 arXiv", session_id="mock_3"))
msgs3 = [t for r, t in saved if r == "tool"]
check("整轮未崩溃（返回正常回复而非「出错了」）",
      isinstance(reply3, str) and "出错了" not in reply3, str(reply3)[:60])
check("失败工具也补了 tool 消息（缺消息会 400）", len(msgs3) == 2, f"实际={msgs3}")
check("另一个成功的工具结果被保留（不被连带作废）",
      "hackernews_top" in msgs3, f"实际={msgs3}")

print()
print("=" * 64)
print("测试 4：_run_one_tool 双层兜底 —— trace 落库抛异常不外泄")
print("=" * 64)
install()


def boom_trace(*a, **k):
    raise RuntimeError("模拟 SQLite 写失败")


server._save_trace = boom_trace
tc = {"id": "call_x", "function": {"name": "hackernews_top", "arguments": "{}"}}
try:
    out = asyncio.run(server._run_one_tool(tc, session_id="mock_4", direction="数据科学"))
    check("trace 落库抛异常时仍正常返回（不炸整轮）",
          isinstance(out, tuple) and out[0] is tc, f"返回={str(out)[:70]}")
except Exception as e:
    check("trace 落库抛异常时仍正常返回（不炸整轮）", False, f"抛出了 {type(e).__name__}: {e}")

print()
print("=" * 64)
print("测试 5：_verify_result 新语义（不误杀合法结果）")
print("=" * 64)
vf = server._verify_result
MARK = server._RETRY_MARK
ok1, n1 = vf("execute_command", {}, "输出\n[exit=0]")
check("exit=0 通过", ok1 is True, n1)
ok2, n2 = vf("execute_command", {}, "grep 无匹配\n[exit=1]")
check("exit=1 不再判失败（grep无匹配/pytest失败 属合法否定结论）", ok2 is True, n2)
ok3, n3 = vf("execute_command", {}, "⛔ 已拦截: rm -rf")
check("安全拦截仍判失败", ok3 is False, n3)
ok4, n4 = vf("execute_command", {}, "超时")
check("超时判失败且标记为可重试", ok4 is False and n4.startswith(MARK), n4)
ok5, _ = vf("read_file", {}, "拒绝访问 /etc/shadow")
check("read_file 拒绝访问判失败", ok5 is False)
ok6, n6 = vf("web_search", {}, "   ")
check("搜索空结果不再判失败（合法空结果）", ok6 is True, n6)
ok7, n7 = vf("web_search", {}, "错误: GitHub API 429 rate limit")
check("搜索 API 4xx/5xx 判失败且可重试", ok7 is False and n7.startswith(MARK), n7)
ok8, _ = vf("web_search", {}, "结果1\n结果2")
check("搜索正常内容放行", ok8 is True)
ok9, _ = vf("unknown_tool", {}, "任意")
check("未知工具默认放行", ok9 is True)

print()
print("=" * 64)
print("测试 6：重试策略 —— 瞬时故障重试、确定性失败不重试")
print("=" * 64)


def _attempts_with(fake_result, max_retries=1):
    n = {"c": 0}

    async def f(**kw):
        n["c"] += 1
        return fake_result
    res, status, err = asyncio.run(
        server._execute_tool_with_retry("web_search", f, {}, "数据科学", max_retries))
    return n["c"], status, err


c1, s1, e1 = _attempts_with("错误: GitHub API 429")
check("瞬时故障（429）消耗重试次数 → 共尝试 2 次", c1 == 2, f"实际 {c1} 次，status={s1}")
check("重试后仍失败才标记 error", s1 == "error" and "429" in e1, e1[:60])
c2, s2, _ = _attempts_with("⛔ 已拦截 xxx")
check("确定性失败（拦截）不重试 → 仅尝试 1 次", c2 == 1, f"实际 {c2} 次")

print()
print("=" * 64)
ok = sum(1 for _, c in results if c)
print(f"汇总：{ok}/{len(results)} 项通过" + ("  🎉 全绿" if ok == len(results) else "  ⚠️ 有失败项"))
print("=" * 64)
