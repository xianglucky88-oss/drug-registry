from drug_registry.diff import compare_versions
from drug_registry.normalize import normalize_notice


def notice(notice_id, date, sections):
    return normalize_notice({"notice_id": notice_id, "drug_name": "药", "company": "企", "approval_number": "A-1", "revision_date": date, "source_url": "fixture://" + notice_id, "sections": sections})


def test_section_diff_finds_update_and_addition():
    old = notice("old", "2026-01-01", {"风险": "轻微。"})
    new = notice("new", "2026-11-10", {"风险": "常见。", "禁忌": "过敏禁用。"})
    changes = compare_versions(old, new)
    assert {(item.section, item.change_type) for item in changes} == {("风险", "updated"), ("禁忌", "added")}
