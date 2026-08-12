"""预置脚本区：模型可运行、不可写入（write_file 仅允许 data/outputs）。

用途：需要"写数据 → 跑分析"时，先把数据写入 data/，再运行本目录的脚本。
模板：脚本名用 snake_case，末尾 print 一行 JSON 便于模型读取结果。
"""
import json
import sys


def main():
    # 用法: python scripts/quick_stats.py data/xxx.csv [列名]
    if len(sys.argv) < 2:
        print(json.dumps({"error": "用法: python scripts/quick_stats.py <csv路径> [列名]"}))
        return
    fp = sys.argv[1]
    col = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        import csv
        with open(fp, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            print(json.dumps({"error": "空文件或无表头"}))
            return
        out = {"rows": len(rows), "columns": list(rows[0].keys())}
        if col and col in rows[0]:
            vals = [float(r[col]) for r in rows if _to_num(r[col]) is not None]
            if vals:
                out[col] = {
                    "n": len(vals), "min": min(vals), "max": max(vals),
                    "mean": round(sum(vals) / len(vals), 3),
                    "median": round(sorted(vals)[len(vals) // 2], 3),
                }
        print(json.dumps(out, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e)}))


def _to_num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":
    main()
