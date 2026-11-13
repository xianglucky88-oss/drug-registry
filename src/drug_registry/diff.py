"""版本比较入口。

历史上本模块只按标题输出 added/updated/removed；现在差异统一由
``matching.match_sections`` 产生，每条差异都带双端章节引用和规则版本。
``compare_versions`` 保留旧签名，返回结果中的 SectionChange 仍含
notice_id/section/change_type/text 四个旧字段。
"""

from __future__ import annotations

from .matching import DEFAULT_RULE_VERSION, match_sections
from .models import RegulatoryNotice, SectionChange


def compare_versions(
    previous: RegulatoryNotice | None,
    current: RegulatoryNotice,
    rule_version: str = DEFAULT_RULE_VERSION,
) -> list[SectionChange]:
    return match_sections(previous, current, rule_version).changes
