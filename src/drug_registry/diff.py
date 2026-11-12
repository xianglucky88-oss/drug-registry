from __future__ import annotations
from .models import RegulatoryNotice, SectionChange


def compare_versions(previous: RegulatoryNotice | None, current: RegulatoryNotice) -> list[SectionChange]:
    if previous is None:
        return [SectionChange(current.notice_id, section, "added", text) for section, text in current.sections.items()]
    changes: list[SectionChange] = []
    for section in sorted(set(previous.sections) | set(current.sections)):
        old = previous.sections.get(section)
        new = current.sections.get(section)
        if old == new:
            continue
        if old and new:
            changes.append(SectionChange(current.notice_id, section, "updated", new))
        elif new:
            changes.append(SectionChange(current.notice_id, section, "added", new))
        else:
            changes.append(SectionChange(current.notice_id, section, "removed", old or ""))
    return changes
