from __future__ import annotations
import re
from .models import RegulatoryNotice


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def normalize_notice(raw: dict) -> RegulatoryNotice:
    sections = {compact(key): compact(value) for key, value in raw.get("sections", {}).items()}
    return RegulatoryNotice(
        notice_id=compact(str(raw["notice_id"])),
        drug_name=compact(str(raw["drug_name"])).upper(),
        company=compact(str(raw["company"])).upper(),
        approval_number=re.sub(r"\s+", "", str(raw["approval_number"]).upper()),
        revision_date=compact(str(raw["revision_date"])),
        sections=sections,
        source_url=compact(str(raw["source_url"])),
    )
