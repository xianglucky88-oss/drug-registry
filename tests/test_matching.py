"""匹配引擎规则测试：改名、相似标题、拆分/合并、空正文、竞争候选。"""

from __future__ import annotations

import pytest

from drug_registry.matching import (
    CANDIDATE,
    CONFIRMED,
    RULE_VERSION_V1,
    RULE_VERSION_V2,
    match_sections,
    title_similarity,
)
from drug_registry.normalize import normalize_notice


def notice(notice_id, date, sections, drug="测试药", approval="T-1"):
    return normalize_notice(
        {
            "notice_id": notice_id,
            "drug_name": drug,
            "company": "示例企业",
            "approval_number": approval,
            "revision_date": date,
            "source_url": "fixture://" + notice_id,
            "sections": sections,
        }
    )


def groups_by_relation(result, relation, status=CONFIRMED):
    return [
        g for g in result.groups if g.relation == relation and g.status == status
    ]


def change_index(result):
    # section 取新版本章节；removed 没有新章节时退回旧章节，与旧链路语义一致
    return {
        (c.new_section or c.old_section or c.section, c.change_type)
        for c in result.changes
    }


# ------------------------------------------------------------- 基础与首次导入
def test_baseline_without_previous_is_all_added_and_no_fake_links():
    new = notice("n1", "2026-01-01", {"适应症": "x", "禁忌": "y"})
    result = match_sections(None, new, RULE_VERSION_V1)
    assert result.baseline is True
    assert result.old_notice_id is None
    assert result.groups == []
    assert {c.change_type for c in result.changes} == {"added"}
    assert all(c.old_notice_id is None and c.old_section is None for c in result.changes)


def test_same_title_even_with_empty_body_is_confirmed_and_no_change():
    old = notice("o", "2026-01-01", {"备注": ""})
    new = notice("n", "2026-02-01", {"备注": ""})
    result = match_sections(old, new)
    assert len(groups_by_relation(result, "same")) == 1
    assert result.changes == []  # 两侧都为空，不制造差异


# ------------------------------------------------------------- 改名：正文不变
def test_rename_identical_body_confirmed_with_unchanged_body():
    old = notice("o", "2026-01-01", {"注意事项": "肝肾功能不全者慎用。"})
    new = notice("n", "2026-02-01", {"警示与注意": "肝肾功能不全者慎用。"})
    result = match_sections(old, new)
    renames = groups_by_relation(result, "rename")
    assert len(renames) == 1
    group = renames[0]
    assert group.old_sections == ("注意事项",) and group.new_sections == ("警示与注意",)
    assert "规则2" in group.rationale
    assert change_index(result) == {("警示与注意", "renamed")}
    change = result.changes[0]
    assert change.old_text == change.text == "肝肾功能不全者慎用。"
    assert change.old_section == "注意事项" and change.new_section == "警示与注意"


def test_unrelated_title_with_body_change_is_not_even_a_candidate():
    old = notice("o", "2026-01-01", {"注意事项": "慎用。"})
    new = notice("n", "2026-02-01", {"禁忌": "禁用。"})
    # 标题相似度低于阈值、正文不同：连候选都不产生
    result = match_sections(old, new)
    assert groups_by_relation(result, "rename") == []
    assert groups_by_relation(result, "rename", CANDIDATE) == []
    # 正式结论只能是新增/删除
    assert change_index(result) == {("禁忌", "added"), ("注意事项", "removed")}
    assert all(c.link_group_id is None for c in result.changes)


# ------------------------------------------------------------- 相似标题内容不同
def test_similar_title_but_different_body_stays_candidate():
    old = notice("o", "2026-01-01", {"不良反应": "恶心。"})
    new = notice("n", "2026-02-01", {"不良反应-观察": "恶心、头晕、皮疹，出现黄疸应立即就诊。"})
    result = match_sections(old, new, RULE_VERSION_V1)
    assert title_similarity("不良反应", "不良反应-观察") >= 0.3
    candidates = groups_by_relation(result, "rename", CANDIDATE)
    assert len(candidates) == 1
    assert candidates[0].competing is False
    # 正式差异仍是新增/删除，候选没有被算成确认的监管变化
    assert change_index(result) == {
        ("不良反应-观察", "added"),
        ("不良反应", "removed"),
    }


def test_rule_v2_raises_candidate_threshold_and_drops_weak_title_match():
    old = notice("o", "2026-01-01", {"备注": "旧备注内容。"})
    new = notice("n", "2026-02-01", {"备注说明": "新备注内容。"})
    sim = title_similarity("备注", "备注说明")
    assert 0.3 <= sim < 0.6
    v1 = match_sections(old, new, RULE_VERSION_V1)
    v2 = match_sections(old, new, RULE_VERSION_V2)
    assert len(groups_by_relation(v1, "rename", CANDIDATE)) == 1
    assert groups_by_relation(v2, "rename", CANDIDATE) == []


# ------------------------------------------------------------- 拆分与合并
def test_split_one_old_into_multiple_new_confirmed_v1():
    old = notice("o", "2026-01-01", {"风险警示": "本品可能引起严重过敏反应；需立即停药并就医。"})
    new = notice(
        "n",
        "2026-02-01",
        {
            "过敏反应": "本品可能引起严重过敏反应；",
            "急救处置": "需立即停药并就医。",
        },
    )
    result = match_sections(old, new, RULE_VERSION_V1)
    splits = groups_by_relation(result, "split")
    assert len(splits) == 1
    assert splits[0].old_sections == ("风险警示",)
    assert set(splits[0].new_sections) == {"过敏反应", "急救处置"}
    types = {c.change_type for c in result.changes}
    assert types == {"split_from"}
    assert all(c.old_section == "风险警示" for c in result.changes)


def test_split_demoted_to_candidate_under_v2_when_titles_unrelated():
    old = notice("o", "2026-01-01", {"风险警示": "本品可能引起严重过敏反应；需立即停药并就医。"})
    new = notice(
        "n",
        "2026-02-01",
        {
            "过敏反应": "本品可能引起严重过敏反应；",
            "急救处置": "需立即停药并就医。",
        },
    )
    result = match_sections(old, new, RULE_VERSION_V2)
    assert groups_by_relation(result, "split", CONFIRMED) == []
    candidates = groups_by_relation(result, "split", CANDIDATE)
    assert len(candidates) == 1
    assert "降级为候选" in candidates[0].rationale
    # 未确认 => 正式差异回到真实新增/删除
    assert change_index(result) == {
        ("急救处置", "added"),
        ("过敏反应", "added"),
        ("风险警示", "removed"),
    }


def test_merge_multiple_old_into_one_new_confirmed():
    old = notice(
        "o",
        "2026-01-01",
        {"用法用量": "口服，一次一片。", "给药途径": "用温水送服。"},
    )
    new = notice("n", "2026-02-01", {"用法与给药": "口服，一次一片。用温水送服。"})
    result = match_sections(old, new, RULE_VERSION_V1)
    merges = groups_by_relation(result, "merge")
    assert len(merges) == 1
    assert set(merges[0].old_sections) == {"用法用量", "给药途径"}
    assert merges[0].new_sections == ("用法与给药",)
    assert {c.change_type for c in result.changes} == {"merged_into"}


def test_partial_overlap_below_threshold_does_not_confirm_merge():
    old = notice(
        "o",
        "2026-01-01",
        {"药理毒理": "急性毒性试验未见异常。", "包装": "铝塑泡罩包装。"},
    )
    # 新正文只覆盖了旧内容的一部分，还引入了旧版没有的句子 -> 覆盖度不足 0.99
    new = notice(
        "n",
        "2026-02-01",
        {"毒理与包装": "急性毒性试验未见异常。新增临床试验摘要。"},
    )
    result = match_sections(old, new, RULE_VERSION_V1)
    assert groups_by_relation(result, "merge", CONFIRMED) == []
    # 存在部分重叠候选，但正式结论只能是新增/删除
    assert change_index(result) == {
        ("毒理与包装", "added"),
        ("包装", "removed"),
        ("药理毒理", "removed"),
    }


# ------------------------------------------------------------- 空正文
def test_empty_body_match_is_never_confirmed_by_body_rules():
    old = notice("o", "2026-01-01", {"用药说明": "", "适应症": "缓解疼痛。"})
    new = notice("n", "2026-02-01", {"用药说明": "", "适应症": "缓解疼痛。"})
    result = match_sections(old, new)
    same = groups_by_relation(result, "same")
    assert {g.old_sections[0] for g in same} == {"用药说明", "适应症"}
    assert result.changes == []


def test_empty_body_renamed_title_only_remains_candidate():
    old = notice("o", "2026-01-01", {"备注": ""})
    new = notice("n", "2026-02-01", {"备注说明": ""})
    result = match_sections(old, new, RULE_VERSION_V1)
    assert groups_by_relation(result, "rename", CONFIRMED) == []
    candidates = groups_by_relation(result, "rename", CANDIDATE)
    assert len(candidates) == 1
    assert candidates[0].score < 0.6
    assert change_index(result) == {("备注", "removed"), ("备注说明", "added")}


# ------------------------------------------------------------- 竞争候选
def test_competing_candidates_are_flagged_and_never_confirm():
    old = notice(
        "o",
        "2026-01-01",
        {"孕妇用药": "孕妇用药请咨询医生。", "妊娠用药": "妊娠期用药请咨询医生。"},
    )
    new = notice("n", "2026-02-01", {"孕妇妊娠用药": "孕妇及妊娠期用药请咨询医生。"})
    result = match_sections(old, new, RULE_VERSION_V1)
    # 合并阈值 0.99 不满足（正文不是严格拼接），不能确认 merge
    assert groups_by_relation(result, "merge", CONFIRMED) == []
    candidates = groups_by_relation(result, "rename", CANDIDATE)
    assert len(candidates) == 2
    assert all(g.competing for g in candidates)
    assert all("竞争" in g.rationale for g in candidates)
    # 两个旧章节都只能算 removed，新章节算 added
    assert change_index(result) == {
        ("孕妇妊娠用药", "added"),
        ("孕妇用药", "removed"),
        ("妊娠用药", "removed"),
    }


def test_confirmed_groups_are_disjoint_but_candidates_may_overlap():
    old = notice(
        "o",
        "2026-01-01",
        {
            "适应症": "x",
            "孕妇用药": "孕妇用药请咨询医生。",
            "妊娠用药": "妊娠期用药请咨询医生。",
        },
    )
    new = notice(
        "n",
        "2026-02-01",
        {
            "适应症": "x",
            "孕妇妊娠用药": "孕妇及妊娠期用药请咨询医生。",
        },
    )
    result = match_sections(old, new)
    confirmed = [g for g in result.groups if g.status == CONFIRMED]
    old_titles = [s for g in confirmed for s in g.old_sections]
    new_titles = [s for g in confirmed for s in g.new_sections]
    assert len(old_titles) == len(set(old_titles))
    assert len(new_titles) == len(set(new_titles))


def test_every_result_carries_rule_version_and_traceable_refs():
    old = notice("o", "2026-01-01", {"适应症": "a"})
    new = notice("n", "2026-02-01", {"适应症": "b", "禁忌": "c"})
    result = match_sections(old, new, RULE_VERSION_V2)
    assert result.rule_version == RULE_VERSION_V2
    for change in result.changes:
        assert change.rule_version == RULE_VERSION_V2
        assert change.new_notice_id == "n"
        assert change.old_notice_id == "o"


def test_unknown_rule_version_rejected():
    old = notice("o", "2026-01-01", {"a": "x"})
    new = notice("n", "2026-02-01", {"a": "x"})
    with pytest.raises(KeyError):
        match_sections(old, new, "rules-nope")
