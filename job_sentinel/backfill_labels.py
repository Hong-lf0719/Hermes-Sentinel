"""一次性回填：为存量 jobs 计算 enterprise_type / exam_free。

历史 nature 字段杂乱（社招/校招/国有企业/外商投资企业…混存），
新增 enterprise_type 列后需按 canon_enterprise_type 回填；
exam_free 从标题/JD 检测（存量 jd_text 很少，主要靠标题/行业）。
幂等：只更新空值，可重复运行。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_sentinel.matcher import canon_enterprise_type, detect_exam_free  # noqa: E402
from job_sentinel.store import get_db  # noqa: E402

DB = Path(__file__).resolve().parent.parent / "hermes.db"


def main() -> None:
    with get_db(str(DB)) as c:
        rows = c.execute(
            "SELECT raw_hash, nature, title, industry, major, jd_text, "
            "enterprise_type, exam_free FROM jobs"
        ).fetchall()
        ent_up = exam_up = 0
        for r in rows:
            if not (r["enterprise_type"] or "").strip():
                et = canon_enterprise_type(r["nature"] or "")
                if et:
                    c.execute("UPDATE jobs SET enterprise_type=? WHERE raw_hash=?",
                              (et, r["raw_hash"]))
                    ent_up += 1
            if not (r["exam_free"] or "").strip():
                ef = detect_exam_free(
                    f"{r['title'] or ''} {r['industry'] or ''} {r['major'] or ''} {r['jd_text'] or ''}"
                )
                if ef:
                    c.execute("UPDATE jobs SET exam_free=? WHERE raw_hash=?",
                              (ef, r["raw_hash"]))
                    exam_up += 1
        c.commit()
        # 汇总
        print(f"总岗位: {len(rows)}")
        print(f"回填 enterprise_type: {ent_up} 条")
        print(f"回填 exam_free: {exam_up} 条")
        print("\n=== enterprise_type 分布 ===")
        for r in c.execute(
            "SELECT COALESCE(NULLIF(enterprise_type,''),'未标注') t, COUNT(*) n "
            "FROM jobs GROUP BY t ORDER BY n DESC"
        ):
            print(f"  {r['t']}: {r['n']}")
        print("\n=== exam_free 分布 ===")
        for r in c.execute(
            "SELECT COALESCE(NULLIF(exam_free,''),'未知') t, COUNT(*) n "
            "FROM jobs GROUP BY t ORDER BY n DESC"
        ):
            print(f"  {r['t']}: {r['n']}")


if __name__ == "__main__":
    main()
