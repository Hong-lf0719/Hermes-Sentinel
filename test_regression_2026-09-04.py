# -*- coding: utf-8 -*-
"""
Hermes 改动回归测试（2026-09-04）
覆盖本次两个核心改动：
  方案一：工具调用并行化（_run_one_tool + asyncio.gather）
  方案二：结果校验层（_verify_result）

设计原则：不启动整个服务、不依赖真实 DeepSeek API key。
通过 ast 从 server.py 提取目标函数源码，注入 mock 依赖后执行，
验证「校验逻辑」和「并行语义 + 顺序对齐」两个关键行为。
"""
import ast
import re
import json
import time
import asyncio
import sys
from pathlib import Path

SERVER = Path(__file__).parent / "server.py"


def extract_function(name: str) -> str:
    """从 server.py 提取单个函数（或 async 函数）的源码。"""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(SERVER.read_text(encoding="utf-8"), node)
    raise RuntimeError(f"未找到函数 {name}")


def build_ns() -> dict:
    """构造测试命名空间：注入 mock 依赖。"""
    ns = {"re": re, "json": json, "time": time, "asyncio": asyncio}
    # mock：TOOL_MAP / _execute_tool_with_retry / _save_trace / logger
    ns["TOOL_MAP"] = {}
    ns["logger"] = type("L", (), {"warning": lambda *a, **k: None})()
    ns["_save_trace"] = lambda *a, **k: None
    ns["_execute_tool_with_retry"] = None  # 测试里再按需覆盖
    ns["_verify_result"] = None  # 先占位
    ns["_RETRY_MARK"] = "可重试|"  # 模块级常量，_verify_result 会引用
    return ns


def test_verify_result() -> int:
    """方案二：结果校验逻辑。"""
    ns = build_ns()
    exec(extract_function("_verify_result"), ns)
    verify = ns["_verify_result"]

    # 注意：当前 server.py 语义是——
    #   exit≠0 = 命令执行完毕的「否定结论」（grep 无匹配/pytest 失败），非工具故障 → 通过
    #   空搜索结果 = 合法空结果，非故障 → 通过
    cases = [
        ("execute_command", {}, "[exit=0]\nhello", True),
        ("execute_command", {}, "[exit=1]\nfail", True),   # 否定结论，非故障
        ("execute_command", {}, "⛔ 已拦截: 危险命令", False),
        ("execute_command", {}, "超时 (30s)", False),
        ("execute_command", {}, "错误: 某个异常", False),
        ("write_file", {}, "✅ 已写入: /tmp/x.txt (10 字符)", True),
        ("write_file", {}, "拒绝访问: 路径越界", False),
        ("read_file", {}, "不存在: /tmp/nope", False),
        ("search_github", {}, "🔍 GitHub: keyword (5 个结果)", True),
        ("search_github", {}, "GitHub API 403", False),
        ("search_github", {}, "GitHub API 500", False),
        ("web_search", {}, "", True),   # 合法空结果，非故障
        ("web_search", {}, "超时", False),
        ("kb_search", {}, "whatever", True),
    ]
    passed = 0
    for fn, args, res, expect in cases:
        got, note = verify(fn, args, res)
        if got == expect:
            passed += 1
        else:
            print(f"  [FAIL] {fn}: 期望={expect} 实际={got} ({note})")
    print(f"[方案二] _verify_result: {passed}/{len(cases)} 通过")
    return passed == len(cases)


def test_parallel_semantics() -> bool:
    """方案一：验证并行执行 + 结果顺序与输入对齐。"""
    ns = build_ns()
    exec(extract_function("_run_one_tool"), ns)
    run_one = ns["_run_one_tool"]

    # 记录每个工具的实际执行时间点，验证「是否并行」
    exec_log = []

    async def fake_execute(fn_name, fn, fn_args, direction):
        exec_log.append(fn_name)
        if fn_name == "slow":
            await asyncio.sleep(0.2)
        else:
            await asyncio.sleep(0.1)
        return f"{fn_name}-result", "ok", ""

    ns["_execute_tool_with_retry"] = fake_execute

    tcs = [
        {"function": {"name": "slow", "arguments": "{}"}},
        {"function": {"name": "fast_a", "arguments": "{}"}},
        {"function": {"name": "fast_b", "arguments": "{}"}},
    ]

    async def run():
        t0 = time.perf_counter()
        results = await asyncio.gather(
            *[run_one(tc, "s1", "数据科学") for tc in tcs]
        )
        elapsed = time.perf_counter() - t0
        # 并行：3 个工具总耗时 ≈ max(0.2, 0.1, 0.1) ≈ 0.2s，而非串行 0.4s
        return results, elapsed

    results, elapsed = asyncio.run(run())

    # 顺序对齐检查：结果顺序 == 输入顺序
    order_ok = [r[1] for r in results] == ["slow-result", "fast_a-result", "fast_b-result"]
    parallel_ok = elapsed < 0.35  # 串行会 >0.4s，并行约 0.2s
    print(f"[方案一] 并行耗时 {elapsed:.2f}s (串行约 0.40s)，结果顺序对齐={order_ok}")
    return order_ok and parallel_ok


if __name__ == "__main__":
    print("=" * 50)
    print("Hermes 改动回归测试")
    print("=" * 50)
    r1 = test_verify_result()
    r2 = test_parallel_semantics()
    print("-" * 50)
    if r1 and r2:
        print("✅ 全部通过：方案一并行化 + 方案二校验层均工作正常")
        sys.exit(0)
    else:
        print("❌ 存在失败项，请检查上面 [FAIL] 输出")
        sys.exit(1)
