from __future__ import annotations
from .diff import compare_versions
from .models import RegulatoryNotice, SectionChange
from .normalize import normalize_notice
from .sources import NoticeSource
from .storage import DrugStore


class DrugRegistryPipeline:
    def __init__(self, source: NoticeSource, store: DrugStore) -> None:
        self.source = source
        self.store = store

    def run(self) -> list[SectionChange]:
        previous_by_drug: dict[tuple[str, str], RegulatoryNotice] = {}
        all_changes: list[SectionChange] = []
        for raw in sorted(self.source.read(), key=lambda item: item["revision_date"]):
            current = normalize_notice(raw)
            key = (current.drug_name, current.approval_number)
            changes = compare_versions(previous_by_drug.get(key), current)
            self.store.save(current, changes)
            previous_by_drug[key] = current
            all_changes.extend(changes)
        return all_changes
