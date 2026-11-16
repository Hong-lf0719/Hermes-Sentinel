# -*- coding: utf-8 -*-
"""matcher 打分引擎单测 —— 2026-11-16 固化。

覆盖两大核心：
  1. score_job 岗位级 11 维打分（方向/城市/时效/薪资/性质/专业/学历/降权…）
  2. company_match 公司级五维打分（岗位 40 / 学历友好 25 / 福利 15 / 偏好 10 / 历史回应 10）
每个维度至少一例：命中、边界、中性/不编造三种情形。

注意：company_match 接收 dict 岗位行（与 DB 行结构一致），score_job 接收 Job 对象。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_sentinel.matcher import (  # noqa: E402
    city_score, company_match, days_to_deadline, deadline_urgency, direction_match,
    edu_score, education_friendliness, hard_filter, major_score, nature_score,
    recruit_type_score, salary_k, salary_score, score_job, score_tier, success_signal,
    tech_relevance, canon_enterprise_type,
)
from job_sentinel.models import Job  # noqa: E402

# ── 通用画像/岗位构造器 ──────────────────────────────────────────

PROF = {
    "regions": ["武汉"],
    "directions": ["数据开发/分析", "AI/大模型/算法", "软件开发(后端)"],
    "education": "本科",
    "major": ["计算机类"],
    "nature": ["央企", "国企"],
    "batches": ["秋招", "提前批", "春招", "补录", "实习"],
    "salary_min": 0,
    "recruit_type": [],
    "exclude_keywords": [],
    "downgrade_keywords": [],
}


def mkjob(**kw) -> Job:
    base = dict(company="测试公司", title="数据开发工程师", city="武汉",
                education="本科及以上", batch="秋招", deadline="2099-12-31",
                url="https://x", link_type="官方网申", source="国聘网",
                nature="国有企业", enterprise_type="国企", industry="软件",
                major="计算机类", salary="12K")
    base.update(kw)
    return Job(**base)


def jd(**kw) -> dict:
    """company_match 的岗位行（DB dict 结构）。"""
    base = dict(company="测试公司", title="数据开发工程师", city="武汉",
                education="本科及以上", enterprise_type="国企", industry="软件",
                salary="12K", deadline="2099-12-31", url="https://x",
                status="新", match_score=70, batch="秋招")
    base.update(kw)
    return base


# ══ 1. 方向匹配（0-30）══════════════════════════════════════════

def test_direction_canonical_hit():
    # 标题"数据挖掘"命中 数据开发/分析 类别词表 → 满分 30
    s, hits = direction_match(mkjob(title="数据挖掘工程师"), PROF)
    assert s == 30 and "数据开发/分析" in hits


def test_direction_no_hit():
    s, hits = direction_match(mkjob(title="机械设计工程师"), PROF)
    assert s == 0 and hits == []


def test_direction_custom_fallback():
    # 非 canonical 方向按字面匹配
    s, hits = direction_match(mkjob(title="GIS开发工程师"), PROF | {"directions": ["gis"]})
    assert s == 30 and hits == ["gis"]


# ══ 2. 城市分层（0-15）══════════════════════════════════════════

def test_city_target_first():
    s, note = city_score(mkjob(city="武汉, 北京"), PROF)
    assert s == 15 and note == "地区匹配"


def test_city_target_not_first():
    # 2026-09-02 修正：目标城市在后续位 → 8（与一线同档，不再霸占首屏）
    s, note = city_score(mkjob(city="深圳,武汉"), PROF)
    assert s == 8 and "武汉" in note


def test_city_tier_a():
    s, note = city_score(mkjob(city="北京"), PROF)
    assert s == 8 and note == "一线/新一线"


def test_city_small_and_unknown():
    assert city_score(mkjob(city="孝感"), PROF)[0] == 2
    assert city_score(mkjob(city=""), PROF)[0] == 4


# ══ 3. 时效紧急度（0-10）════════════════════════════════════════

def test_deadline_buckets():
    ref = "2026-11-16"
    j3 = mkjob(deadline="2026-11-19")       # 3 天
    j7 = mkjob(deadline="2026-11-23")       # 7 天
    j15 = mkjob(deadline="2026-12-01")      # 15 天
    jfar = mkjob(deadline="2027-03-01")     # 远
    jpast = mkjob(deadline="2026-11-01")    # 过期
    junk = mkjob(deadline="以官网为准")
    assert deadline_urgency(j3, ref)[0] == 10
    assert deadline_urgency(j7, ref)[0] == 8
    assert deadline_urgency(j15, ref)[0] == 6
    assert deadline_urgency(jfar, ref)[0] == 3
    assert deadline_urgency(jpast, ref) == (0, "已过期")
    assert deadline_urgency(junk, ref)[0] == 4  # 未知不惩罚


def test_days_to_deadline_formats():
    assert days_to_deadline(mkjob(deadline="2026年11月19日"), "2026-11-16") == 3
    assert days_to_deadline(mkjob(deadline="招满即止")) is None
    assert days_to_deadline(mkjob(deadline="2026-11-19")) == 3


# ══ 4. 薪资折算与打分（0-5，可负）═══════════════════════════════

def test_salary_k_unit_conversion():
    # 日薪不能当月薪：150-300元/天 ≈ 3.3K，而不是 150K
    k, _ = salary_k("150-300元/天")
    assert 3.0 < k < 3.6
    k, _ = salary_k("年薪15万")
    assert 12.0 < k < 13.0
    k, _ = salary_k("12000以上")
    assert k == 12.0
    assert salary_k("面议")[0] is None
    assert salary_k("10-18K")[0] == 10.0


def test_salary_score_buckets():
    assert salary_score(mkjob(salary="25K"), PROF)[0] == 5
    assert salary_score(mkjob(salary="8K"), PROF)[0] == 2
    assert salary_score(mkjob(salary="面议"), PROF)[0] == 2  # 中性不惩罚


def test_salary_below_user_min():
    # 低于画像门槛 -8：8K 岗 base 2 → 2-8 = -6
    s, note = salary_score(mkjob(salary="8K"), PROF | {"salary_min": 10})
    assert s == -6 and "<10K" in note


# ══ 5. 单位性质（0-6）═══════════════════════════════════════════

def test_nature_prestige_and_select():
    s, note = nature_score(mkjob(nature="国有企业"), PROF)
    assert s == 6 and "国企" in note          # 声望3 + 勾选4 → cap 6
    s, _ = nature_score(mkjob(nature="外商独资"), PROF)
    assert s == 2                             # 外企声望 2，未勾选
    assert nature_score(mkjob(nature=""), PROF)[0] == 1  # 无标注 → 底分


def test_canon_enterprise_type_priority():
    assert canon_enterprise_type("民营上市") == "民企"   # 优先级表内民企在前
    assert canon_enterprise_type("中央企业") == "央企"
    assert canon_enterprise_type("不知道啥") == ""
    assert canon_enterprise_type("上市股份") == "其他"


# ══ 6. 专业/招聘类型（0-6 / 0-7）═══════════════════════════════

def test_major_exact_and_contains():
    s, note = major_score(mkjob(major="计算机类"), PROF)
    assert s == 6 and note == "专业匹配"
    s, note = major_score(mkjob(major="计算机类、电子信息类"), PROF)
    assert s == 6  # canon 归一后精确命中
    assert major_score(mkjob(major=""), PROF)[0] == 0     # 空字段不惩罚


def test_recruit_type_filter():
    p = PROF | {"recruit_type": ["校招"]}
    s, note = recruit_type_score(mkjob(nature="国有企业 校招"), p)
    assert s == 7 and "校招" in note
    assert recruit_type_score(mkjob(nature="国有企业"), p)[0] == 0


# ══ 7. 学历可达性（+4/-8）══════════════════════════════════════

def test_edu_reachable_and_blocked():
    assert edu_score(mkjob(education="本科及以上"), PROF) == (4, "")
    assert edu_score(mkjob(education="硕士及以上"), PROF)[0] == -8
    assert edu_score(mkjob(education="不限"), PROF)[0] == 4


def test_edu_enum_bug_fixed():
    # 经典坑：「本科,硕士」= 都招，不是仅硕士（曾把本科可投岗全沉底）
    assert edu_score(mkjob(education="本科,硕士"), PROF)[0] == 4


# ══ 8. 技术相关度与硬过滤 ═══════════════════════════════════════

def test_tech_relevance_levels():
    assert tech_relevance(mkjob(title="大数据平台开发工程师")) >= 14   # 标题命中
    # major 置空，避免默认"计算机类"蹭出技术分
    assert tech_relevance(mkjob(title="行政专员", industry="行政", major="")) == 0
    # 标题无技术词但行业是科技行业 → 弱信号 8（聚合站"公司+行业标签"条目）
    assert tech_relevance(mkjob(title="2026秋季校园招聘", industry="半导体", major="")) == 8


def test_hard_filter_rejects():
    from job_sentinel.matcher import PLACEHOLDER_COMPANIES
    ph = next(iter(PLACEHOLDER_COMPANIES))
    assert hard_filter(mkjob(company=ph), PROF) is False            # 占位公司
    # 非技术岗（major 置空，标题/行业无技术词 → tech_relevance 0 → 拒）
    assert hard_filter(mkjob(title="客户经理", industry="金融", major=""), PROF) is False
    assert hard_filter(mkjob(), PROF | {"exclude_keywords": ["测试公司"]}) is False


def test_hard_filter_passes():
    assert hard_filter(mkjob(), PROF) is True


# ══ 9. score_job 集成与降权 ═════════════════════════════════════

def test_score_job_positive_reasons():
    score, reason = score_job(mkjob(), PROF, today="2026-11-16")
    assert 60 <= score <= 100
    assert "方向" in reason and "地区匹配" in reason


def test_score_job_expired_sinks():
    good, _ = score_job(mkjob(), PROF, today="2026-11-16")
    expired, r = score_job(mkjob(deadline="2026-11-01"), PROF, today="2026-11-16")
    assert good - expired == 3          # 时效差恰为 远期3 vs 过期0
    assert "已过期" in r


def test_score_job_downgrade_keyword():
    p = PROF | {"downgrade_keywords": ["算法"]}
    a, ra = score_job(mkjob(title="算法工程师"), p, today="2026-11-16")
    b, rb = score_job(mkjob(title="算法工程师"), PROF, today="2026-11-16")
    assert a == b - 12 and "避开:算法" in ra


def test_score_tier_labels():
    assert score_tier(80) == "优先投"
    assert score_tier(65) == "值得投"
    assert score_tier(50) == "备选"
    assert score_tier(10) == "观察"


# ══ 10. 学历友好度（company_match 的信号 1）══════════════════════

def test_education_friendliness_values():
    assert education_friendliness("硕士及以上") == (-6.0, "仅硕士可投（本科无门）")
    assert education_friendliness("本科及以上")[0] == 4.0
    assert education_friendliness("") == (0.0, "学历要求未标注")
    assert education_friendliness("大专及以上")[0] == 0.0   # 无法判断不编造


# ══ 11. company_match 五维 ══════════════════════════════════════

def test_cm_dims_shape_and_sum():
    m = company_match([jd(match_score=70)], PROF, None, None)
    assert set(m["dims"]) == {"job", "edu", "welfare", "pref", "hist"}
    assert m["score"] == round(sum(m["dims"].values()), 1)


def test_cm_job_dim():
    # best100/avg100 → 30+10=40；best60/avg45 → 18+4.5=22.5
    assert company_match([jd(match_score=100)], PROF, None, None)["dims"]["job"] == 40
    m = company_match([jd(match_score=60), jd(match_score=30, title="另一个岗")],
                      PROF, None, None)
    assert m["dims"]["job"] == 22.5


def test_cm_edu_full_evidence():
    # 本科可投(+4) + 门槛宽松(+12) + 双非友好(+13)，全部 ×0.4：
    # 12.5 + 1.6 + 4.8 + 5.2 = 24.1
    intel = {"school_barrier": "宽松", "confidence": "高",
             "school_tier_friendly": "双非友好"}
    m = company_match([jd(education="本科及以上")], PROF, intel, None)
    assert m["dims"]["edu"] == 24.1
    assert m["school_barrier"] == "宽松" and m["school_tier_friendly"] == "双非友好"


def test_cm_edu_master_only_no_intel():
    # 仅硕士 -6×0.4 = -2.4 → 10.1；无情报 → 门槛/态度=未知
    m = company_match([jd(education="硕士及以上")], PROF, None, None)
    assert m["dims"]["edu"] == 10.1
    assert m["school_barrier"] == "未知" and m["school_tier_friendly"] == "未知"


def test_cm_edu_low_confidence_neutral():
    # 低置信情报不参与打分（不把"未检索到"当结论）→ 只剩本科可投 +1.6
    intel = {"school_barrier": "宽松", "confidence": "低",
             "school_tier_friendly": "双非友好"}
    m = company_match([jd(education="本科及以上")], PROF, intel, None)
    assert m["dims"]["edu"] == 14.1


def test_cm_edu_negative_clamp():
    # 仅硕士 + 门槛严格 + 看重院校：12.5 -2.4 -4.8 -5.2 = 0.1 → clamp ≥0
    intel = {"school_barrier": "严格", "confidence": "高",
             "school_tier_friendly": "看重院校"}
    m = company_match([jd(education="硕士及以上")], PROF, intel, None)
    assert 0.0 <= m["dims"]["edu"] <= 25.0
    assert m["dims"]["edu"] == 0.1


def test_cm_welfare_hits_and_caps():
    P = PROF | {"welfare_pref": ["包住宿", "餐补"]}
    intel = {"welfare": ["包住宿", "餐补", "六险一金", "班车"]}
    m = company_match([jd()], P, intel, None)          # 命中偏好2项 → 10
    assert m["dims"]["welfare"] == 10.0
    intel4 = {"welfare": ["包住宿", "餐补", "房补", "班车"]}   # 偏好含房补时命中3项
    m4 = company_match([jd()], P | {"welfare_pref": ["包住宿", "餐补", "房补"]},
                       intel4, None)                            # 3×5 → cap 15
    assert m4["dims"]["welfare"] == 15.0


def test_cm_welfare_neutral_no_data():
    assert company_match([jd()], PROF, None, None)["dims"]["welfare"] == 7.5
    # 有数据但没配偏好 → 底分 3（丰富度），不按 0 罚
    m = company_match([jd()], PROF | {"welfare_pref": []}, {"welfare": ["班车"]}, None)
    assert m["dims"]["welfare"] == 3.0


def test_cm_pref_nature_industry_overtime():
    # 勾选国企命中 +6 → cap 10
    m = company_match([jd(enterprise_type="国企")], PROF, None, None)
    assert m["dims"]["pref"] == 10.0
    # 加班冲突：性质置空排除勾选干扰，口碑提加班 + 用户低接受度 → 5-6 clamp 0
    intel = {"employee_voice": ["加班强度大"]}
    m2 = company_match([jd(enterprise_type="")],
                       PROF | {"accept_overtime": "低", "nature": []}, intel, None)
    assert m2["dims"]["pref"] == 0.0
    # 行业命中 +4（性质未勾选）→ 9
    m3 = company_match([jd(industry="网络安全")],
                       PROF | {"nature": [], "industry_pref": ["网络安全"]}, None, None)
    assert m3["dims"]["pref"] == 9.0


def test_cm_hist_sample_guard():
    # 样本 ≥3 才计权：0.4 × 10 = 4.0；样本不足 → 中性 5
    assert company_match([jd()], PROF, None, {"applied": 5, "response_rate": 0.4})["dims"]["hist"] == 4.0
    assert company_match([jd()], PROF, None, {"applied": 2, "response_rate": 0.5})["dims"]["hist"] == 5.0


def test_cm_levels_and_reasons():
    m = company_match([jd(match_score=100)], PROF, None, None)
    assert m["level"] in ("高", "中", "低")
    m2 = company_match([jd(match_score=90, education="本科及以上")],
                       PROF, {"school_barrier": "宽松", "confidence": "高",
                              "school_tier_friendly": "双非友好",
                              "welfare": ["包住宿"]},
                       {"applied": 6, "response_rate": 0.5})
    # 40*0.9+9=36+... job=36? best90: 27+avg90*10=36; edu 24.1; welfare 5; pref 10(国企勾选); hist 5
    # 36+24.1+5+10+5 = 80.1 ≥75 → 高
    assert m2["level"] == "高"
    assert any("福利" in r for r in m2["reasons"])
    assert any("学历门槛" in r for r in m2["reasons"])


def test_cm_mini_jobs_and_more_count():
    rows = [jd(match_score=90 - i, title=f"岗{i}") for i in range(30)]
    m = company_match(rows, PROF, None, None)
    assert len(m["mini_jobs"]) == 20
    assert m["more_count"] == 30 - 1 - 20
    assert m["mini_jobs"][0]["match_score"] == 89      # 按分数排序，跳过 best


def test_cm_empty_welfare_list_noise():
    # welfare 列表含空串 → 过滤后视为无数据 → 中性 7.5
    m = company_match([jd()], PROF, {"welfare": ["", ""]}, None)
    assert m["dims"]["welfare"] == 7.5 and m["welfare"] == []


# ══ 12. success_signal 三路信号 ═════════════════════════════════

def test_success_signal_all_paths():
    j = mkjob(education="本科及以上", enterprise_type="国企")
    # 宽松+高置信 + 高回应率：4 + 5 + 3 = 12 → 高
    r = success_signal(j, {"测试公司": {"school_barrier": "宽松", "confidence": "高"}},
                       {"国企": {"applied": 4, "response_rate": 0.5}})
    assert r["level"] == "高" and r["add"] == 12.0
    # 低置信情报走中性文案，不计分
    r2 = success_signal(j, {"测试公司": {"school_barrier": "宽松", "confidence": "低"}}, {})
    assert r2["add"] == 4.0 and any("证据不足" in x for x in r2["reasons"])
    # 低回应率扣分：4 - 3 = 1 → 中
    r3 = success_signal(j, {}, {"国企": {"applied": 5, "response_rate": 0.05}})
    assert r3["level"] == "中" and r3["add"] == 1.0
