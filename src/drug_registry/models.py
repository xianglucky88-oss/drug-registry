from __future__ import annotations
from dataclasses import dataclass


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
    notice_id: str
    section: str
    change_type: str
    text: str
