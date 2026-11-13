from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RegulatoryNotice:
    notice_id: str
    drug_name: str
    company: str
    approval_number: str
    revision_date: str
    sections: dict[str, str]
    source_url: str


@dataclass(frozen=True)
class SectionChange:
    # 前四个字段保持旧链路兼容：notice_id/section 始终指向新版本一侧
    notice_id: str
    section: str
    change_type: str
    text: str
    old_notice_id: str | None = None
    old_section: str | None = None
    new_notice_id: str | None = None
    new_section: str | None = None
    old_text: str | None = None
    rule_version: str | None = None
    link_group_id: str | None = None


@dataclass(frozen=True)
class SectionLinkGroup:
    """同一版本对内部的一组章节关联（确认或候选）。

    relation 取值：same（标题相同的 1:1）、rename（改名 1:1）、
    split（1 个旧章节 -> 多个新章节）、merge（多个旧章节 -> 1 个新章节）。
    status 取值：confirmed（规则确认）、candidate（待人工判断）。
    候选组允许与其他组争夺同一章节，确认组之间章节必须互不相交。
    """

    group_id: str
    old_sections: tuple[str, ...]
    new_sections: tuple[str, ...]
    relation: str
    status: str
    score: float
    rationale: str
    competing: bool = False


@dataclass(frozen=True)
class PairResult:
    old_notice_id: str | None  # None 表示该药品首次导入，没有可匹配的旧版本
    new_notice_id: str
    drug_name: str
    approval_number: str
    rule_version: str
    baseline: bool
    groups: list[SectionLinkGroup] = field(default_factory=list)
    changes: list[SectionChange] = field(default_factory=list)
