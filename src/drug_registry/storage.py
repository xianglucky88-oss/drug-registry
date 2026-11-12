from __future__ import annotations
import sqlite3
from pathlib import Path
from .models import RegulatoryNotice, SectionChange


class DrugStore:
    def __init__(self, database: str | Path) -> None:
        self.connection = sqlite3.connect(database)
        self.connection.execute("""CREATE TABLE IF NOT EXISTS notices (
            notice_id TEXT PRIMARY KEY, drug_name TEXT NOT NULL, company TEXT NOT NULL,
            approval_number TEXT NOT NULL, revision_date TEXT NOT NULL, source_url TEXT NOT NULL
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS notice_sections (
            notice_id TEXT NOT NULL, section TEXT NOT NULL, text TEXT NOT NULL,
            PRIMARY KEY (notice_id, section)
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS section_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, notice_id TEXT NOT NULL, section TEXT NOT NULL,
            change_type TEXT NOT NULL, text TEXT NOT NULL
        )""")
        self.connection.commit()

    def save(self, notice: RegulatoryNotice, changes: list[SectionChange]) -> None:
        self.connection.execute("INSERT OR REPLACE INTO notices VALUES (?, ?, ?, ?, ?, ?)", (
            notice.notice_id, notice.drug_name, notice.company, notice.approval_number,
            notice.revision_date, notice.source_url,
        ))
        self.connection.execute("DELETE FROM notice_sections WHERE notice_id = ?", (notice.notice_id,))
        self.connection.executemany("INSERT INTO notice_sections VALUES (?, ?, ?)", [(notice.notice_id, key, value) for key, value in notice.sections.items()])
        self.connection.executemany("INSERT INTO section_changes (notice_id, section, change_type, text) VALUES (?, ?, ?, ?)", [(item.notice_id, item.section, item.change_type, item.text) for item in changes])
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
