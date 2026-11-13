"""持久化与导入链路测试：原子写入、规则切换、审核留痕、历史保护。"""

from __future__ import annotations

import sqlite3

import pytest

from drug_registry.matching import (
    CANDIDATE,
    CONFIRMED,
    RULE_VERSION_V1,
    RULE_VERSION_V2,
    match_sections,
)
from drug_registry.normalize import normalize_notice
from drug_registry.pipeline import DrugRegistryPipeline
from drug_registry.sources import JsonNoticeSource
from drug_registry.storage import (
    ADJUDICATION_ACCEPTED,
    ADJUDICATION_PENDING,
    ADJUDICATION_REJECTED,
    ContentConflictError,
    DrugStore,
    StorageError,
)

FIXTURE = "fixtures/notices_rich.json"


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


@pytest.fixture()
def store(tmp_path):
    db = DrugStore(tmp_path / "registry.sqlite3")
    yield db
    db.close()


def _load_raw():
    return JsonNoticeSource(FIXTURE).read()


# ----------------------------------------------------------- 原始公告保护
def test_raw_notice_is_immutable_and_content_conflict_rejected(store):
    first = notice("n1", "2026-01-01", {"适应症": "原文"})
    changed = notice("n1", "2026-01-01", {"适应症": "被改写的原文"})
    store.save_notice(first)
    with pytest.raises(ContentConflictError):
        store.save_notice(changed)
    # 库里的原始章节没有被覆盖
    assert store.get_notice_sections("n1") == {"适应症": "原文"}


def test_save_same_notice_twice_is_idempotent(store):
    item = notice("n1", "2026-01-01", {"适应症": "原文"})
    store.save_notice(item)
    store.save_notice(item)
    assert store.get_notice_sections("n1") == {"适应症": "原文"}


def test_missing_old_notice_is_not_fabricated(store):
    new = notice("n2", "2026-02-01", {"适应症": "x"})
    store.save_notice(new)
    # 手工构造一个引用不存在旧公告的结果
    result = match_sections(notice("ghost", "2026-01-01", {"适应症": "y"}), new)
    with pytest.raises(StorageError, match="缺少历史记录"):
        store.save_pair_result(result)
    assert store.list_runs("n2") == []


# ----------------------------------------------------------- 原子写入/失败重跑
def test_failure_mid_write_rolls_back_and_rerun_succeeds(store):
    old = notice("o", "2026-01-01", {"注意事项": "肝肾功能不全者慎用。"})
    new = notice("n", "2026-02-01", {"警示与注意": "肝肾功能不全者慎用。"})
    store.save_notice(old)
    store.save_notice(new)
    result = match_sections(old, new, RULE_VERSION_V1)

    calls = {"count": 0}

    def hook(con, run_id):
        calls["count"] += 1
        raise RuntimeError("模拟磁盘写入失败")

    with pytest.raises(RuntimeError, match="模拟磁盘写入失败"):
        store.save_pair_result(result, failure_hook=hook)

    # 半截 links/changes 已随事务回滚，库里没有互相对不上的结论
    run_rows = store.list_runs("n")
    assert all(row["status"] == "failed" for row in run_rows)
    for row in run_rows:
        assert store.list_links(row["id"]) == []
        assert store.list_changes(row["id"]) == []

    # 不带故障重新处理同一版本对：得到唯一一套完整结论
    run_id = store.save_pair_result(result)
    links = store.list_links(run_id)
    changes = store.list_changes(run_id)
    assert {row["status"] for row in links} == {CONFIRMED}
    assert len(changes) == 1
    assert changes[0]["change_type"] == "renamed"
    # 失败历史保留留痕，completed 结论全库唯一且完整
    final_rows = store.list_runs("n")
    assert [row["status"] for row in final_rows].count("completed") == 1
    completed = [row for row in final_rows if row["status"] == "completed"][0]
    assert completed["id"] == run_id
    assert len(store.list_changes(run_id)) == 1


def test_rerun_same_pair_and_rule_replaces_conclusions_without_duplicates(store):
    old = notice("o", "2026-01-01", {"适应症": "a"})
    new = notice("n", "2026-02-01", {"适应症": "b", "禁忌": "c"})
    store.save_notice(old)
    store.save_notice(new)
    result = match_sections(old, new, RULE_VERSION_V1)
    first_id = store.save_pair_result(result)
    second_id = store.save_pair_result(result)
    assert first_id == second_id  # 同一版本对+规则复用 run 行
    assert len(store.list_changes(run_id=first_id)) == 2
    assert len(store.list_links(run_id=first_id)) == 1


def test_pipeline_failure_hook_then_full_reimport_is_consistent(tmp_path):
    db_path = tmp_path / "p.sqlite3"

    def flaky_hook(con, run_id):
        raise RuntimeError("链路中断")

    store = DrugStore(db_path)
    with pytest.raises(RuntimeError):
        DrugRegistryPipeline(
            JsonNoticeSource(FIXTURE), store, RULE_VERSION_V1, failure_hook=flaky_hook
        ).run()
    store.close()

    # 重开库重新导入：公告仍在，且不存在任何已完成 run 的半截关联
    store = DrugStore(db_path)
    results = DrugRegistryPipeline(JsonNoticeSource(FIXTURE), store, RULE_VERSION_V1).run()
    store.close()

    con = sqlite3.connect(db_path)
    try:
        completed = con.execute(
            "SELECT id FROM diff_runs WHERE status='completed'"
        ).fetchall()
        assert len(completed) == 5  # 5 条公告各一次处理
        for (run_id,) in completed:
            # 每个 completed run 都有完整结果：基线 run 允许零 links
            change_count = con.execute(
                "SELECT COUNT(*) FROM section_changes WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            assert change_count >= 1
        # 没有任何 change 引用不存在的 link 分组
        orphans = con.execute(
            "SELECT COUNT(*) FROM section_changes c WHERE c.link_group_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM section_links l WHERE l.run_id=c.run_id "
            "AND l.group_id=c.link_group_id)"
        ).fetchone()[0]
        assert orphans == 0
    finally:
        con.close()
    assert len(results) == 5


# ----------------------------------------------------------- 规则切换对照
def test_rule_switch_keeps_both_versions_and_changes_conclusions(store):
    raws = {raw["notice_id"]: raw for raw in _load_raw()}
    d12 = normalize_notice(raws["D1-2"])
    d13 = normalize_notice(raws["D1-3"])
    store.save_notice(d12)
    store.save_notice(d13)

    v1 = match_sections(d12, d13, RULE_VERSION_V1)
    v2 = match_sections(d12, d13, RULE_VERSION_V2)
    run_v1 = store.save_pair_result(v1)
    run_v2 = store.save_pair_result(v2)
    assert run_v1 != run_v2

    # v1 确认了拆分；v2 因标题亲和度不足把拆分区分为候选，正式差异变为新增/删除
    v1_types = {row["change_type"] for row in store.list_changes(run_v1)}
    v2_types = {row["change_type"] for row in store.list_changes(run_v2)}
    assert "split_from" in v1_types
    assert "split_from" not in v2_types
    assert {"added", "removed"} <= v2_types

    v1_links = store.list_links(run_v1)
    v2_links = store.list_links(run_v2)
    assert any(row["status"] == CONFIRMED and row["relation"] == "split" for row in v1_links)
    assert all(
        not (row["status"] == CONFIRMED and row["relation"] == "split")
        for row in v2_links
    )
    assert any(row["status"] == CANDIDATE for row in v2_links)

    # 两条 run 都可按规则版本解释，原始公告未被重算覆盖
    runs = {row["rule_version"]: row for row in store.list_runs("D1-3")}
    assert set(runs) == {RULE_VERSION_V1, RULE_VERSION_V2}
    assert store.get_notice_sections("D1-3") == d13.sections


def test_end_to_end_rich_fixture_contains_all_scenario_types(store):
    results = DrugRegistryPipeline(
        JsonNoticeSource(FIXTURE), store, RULE_VERSION_V1
    ).run()
    all_groups = [group for item in results for group in item.groups]
    all_changes = [change for item in results for change in item.changes]

    relations = {(g.relation, g.status) for g in all_groups}
    assert ("rename", CONFIRMED) in relations          # D1-1->D1-2 改名正文不变
    assert ("split", CONFIRMED) in relations           # D1-2->D1-3 拆分
    assert ("merge", CONFIRMED) in relations           # D2-1->D2-2 合并
    assert ("rename", CANDIDATE) in relations          # 相似标题/竞争/空正文候选

    # 竞争候选确实带 competing 标记
    competing = [g for g in all_groups if g.competing]
    assert competing and all(g.status == CANDIDATE for g in competing)

    change_types = {c.change_type for c in all_changes}
    assert {"added", "removed", "renamed", "split_from", "merged_into"} <= change_types
    # 真正新增（D1-2 的禁忌）仍然保留；候选不会把新增吞掉
    added_sections = {c.new_section for c in all_changes if c.change_type == "added"}
    assert "禁忌" in added_sections


# ----------------------------------------------------------- 差异可追溯
def test_every_change_traces_back_to_two_version_sections(store):
    raws = {raw["notice_id"]: raw for raw in _load_raw()}
    old = normalize_notice(raws["D1-1"])
    new = normalize_notice(raws["D1-2"])
    store.save_notice(old)
    store.save_notice(new)
    run_id = store.save_pair_result(match_sections(old, new, RULE_VERSION_V1))

    for row in store.list_changes(run_id):
        traced = store.trace_change(row["id"])
        assert traced is not None
        change = traced["change"]
        assert change["new_notice_id"] == "D1-2"
        if change["change_type"] in {"renamed", "updated"}:
            assert change["old_notice_id"] == "D1-1"
            assert change["old_section"] and change["new_section"]
            assert len(traced["links"]) >= 1
            assert all(link["rationale"] for link in traced["links"])
        if change["change_type"] == "added":
            # 真正的新增没有旧章节，也没有伪关联
            assert change["old_section"] is None and change["link_group_id"] is None


def test_pending_candidates_query_lists_only_undecided(store):
    raws = {raw["notice_id"]: raw for raw in _load_raw()}
    old = normalize_notice(raws["D2-1"])
    new = normalize_notice(raws["D2-2"])
    store.save_notice(old)
    store.save_notice(new)
    run_id = store.save_pair_result(match_sections(old, new, RULE_VERSION_V1))
    pending = store.pending_candidates("D2-2")
    assert pending and all(row["adjudication"] == ADJUDICATION_PENDING for row in pending)

    target = pending[0]
    store.adjudicate_candidate(run_id, target["group_id"], ADJUDICATION_ACCEPTED, "审阅人确认")
    remaining = {row["group_id"] for row in store.pending_candidates("D2-2")}
    assert target["group_id"] not in remaining

    # 已审核不可改判；确认组不允许走审核入口
    with pytest.raises(StorageError, match="不可变更"):
        store.adjudicate_candidate(run_id, target["group_id"], ADJUDICATION_REJECTED)
    confirmed_group = next(
        row["group_id"] for row in store.list_links(run_id) if row["status"] == CONFIRMED
    )
    with pytest.raises(StorageError, match="规则确认关联"):
        store.adjudicate_candidate(run_id, confirmed_group, ADJUDICATION_ACCEPTED)

    # 审核只改候选状态，不自动改写正式差异
    assert all(
        row["rule_version"] == RULE_VERSION_V1 for row in store.list_changes(run_id)
    )


def test_legacy_database_is_rejected_not_silently_migrated(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE section_changes (id INTEGER PRIMARY KEY, notice_id TEXT, "
        "section TEXT, change_type TEXT, text TEXT)"
    )
    con.commit()
    con.close()
    with pytest.raises(StorageError, match="旧版"):
        DrugStore(path)
