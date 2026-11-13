"""SQLite 持久化层。

可追溯性约束
------------
* ``notices``/``notice_sections`` 保存原始公告与章节正文，并记录内容哈希；
  重复导入同一 notice_id 但内容不同时直接报错——匹配规则重算只能追加
  diff_runs/links/changes，绝不覆盖原始公告。
* ``diff_runs`` 记录每个明确版本对在某条规则版本下的一次处理；
* ``section_links`` 同时承载 confirmed 关联与 candidate 候选（含人工审核状态）；
* ``section_changes`` 的每条正式差异都引用双端章节和 link 分组，
  可以从任一差异反查依据的两版章节。
* 一个版本对的写入在单事务内完成，中途失败整体回滚；同版本对 + 同规则
  重跑采用“先删后插”的替换语义，不会留下互相矛盾的半套结论。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from .matching import CANDIDATE, CONFIRMED
from .models import PairResult, RegulatoryNotice, SectionChange, SectionLinkGroup

SCHEMA_VERSION = "2"

# 候选审核状态
ADJUDICATION_PENDING = "pending"
ADJUDICATION_ACCEPTED = "accepted"
ADJUDICATION_REJECTED = "rejected"

_RUN_PENDING = "pending"
_RUN_COMPLETED = "completed"
_RUN_FAILED = "failed"


class StorageError(RuntimeError):
    pass


class ContentConflictError(StorageError):
    """同一 notice_id 导入了与库中不一致的原始内容。"""


def _content_hash(notice: RegulatoryNotice) -> str:
    payload = "\u001f".join(
        f"{key}\u001e{notice.sections[key]}" for key in sorted(notice.sections)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DrugStore:
    def __init__(self, database: str | Path) -> None:
        self.connection = sqlite3.connect(database)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    # ------------------------------------------------------------------ schema
    def _create_schema(self) -> None:
        con = self.connection
        # 旧库（schema v1）的 section_changes 没有双端章节列，检测出来明确报错，
        # 而不是静默写坏历史库。
        legacy = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='section_changes'"
        ).fetchone()
        if legacy:
            columns = {row[1] for row in con.execute("PRAGMA table_info(section_changes)")}
            if "old_section" not in columns:
                raise StorageError(
                    "数据库使用的是旧版 section_changes 结构（缺少章节关联列），"
                    "请用新数据库重新导入；本工具不会迁移或重写历史记录。"
                )
        con.execute("""CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )""")
        row = con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        if row is None:
            con.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
        elif row[0] != SCHEMA_VERSION:
            raise StorageError(f"不支持的 schema 版本: {row[0]}")

        con.execute("""CREATE TABLE IF NOT EXISTS notices (
            notice_id TEXT PRIMARY KEY, drug_name TEXT NOT NULL, company TEXT NOT NULL,
            approval_number TEXT NOT NULL, revision_date TEXT NOT NULL,
            source_url TEXT NOT NULL, content_hash TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS notice_sections (
            notice_id TEXT NOT NULL, section TEXT NOT NULL, text TEXT NOT NULL,
            PRIMARY KEY (notice_id, section),
            FOREIGN KEY (notice_id) REFERENCES notices(notice_id)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS diff_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            old_notice_id TEXT, new_notice_id TEXT NOT NULL,
            drug_name TEXT NOT NULL, approval_number TEXT NOT NULL,
            rule_version TEXT NOT NULL, baseline INTEGER NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (old_notice_id) REFERENCES notices(notice_id),
            FOREIGN KEY (new_notice_id) REFERENCES notices(notice_id)
        )""")
        # old_notice_id 可能为 NULL（首次导入），普通 UNIQUE 无法约束 NULL。
        # 唯一键含 status：同一版本对+规则只允许一条 completed/pending 结论，
        # 多次失败留痕（failed）互不冲突，重跑成功后失败历史仍保留。
        con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_pair_rule
            ON diff_runs(COALESCE(old_notice_id, ''), new_notice_id, rule_version,
                         CASE WHEN status='failed' THEN 'failed' || id ELSE 'active' END)""")
        con.execute("""CREATE TABLE IF NOT EXISTS section_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            group_id TEXT NOT NULL,
            old_notice_id TEXT, new_notice_id TEXT NOT NULL,
            old_section TEXT, new_section TEXT,
            relation TEXT NOT NULL, status TEXT NOT NULL,
            score REAL NOT NULL, rationale TEXT NOT NULL,
            competing INTEGER NOT NULL DEFAULT 0,
            adjudication TEXT NOT NULL DEFAULT 'pending',
            adjudicated_at TEXT, adjudicated_note TEXT,
            FOREIGN KEY (run_id) REFERENCES diff_runs(id)
        )""")
        # 同一 run 内确认关联的章节端唯一：拆分/合并靠 run+group 允许多行共享，
        # 单端唯一性在写入事务里用应用级校验保证（SQLite 无法表达跨行互斥）。
        con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_links_run_group_section
            ON section_links(run_id, group_id, COALESCE(old_section, ''), COALESCE(new_section, ''))""")
        con.execute("""CREATE INDEX IF NOT EXISTS idx_links_run_status
            ON section_links(run_id, status, adjudication)""")
        con.execute("""CREATE TABLE IF NOT EXISTS section_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            old_notice_id TEXT, new_notice_id TEXT NOT NULL,
            old_section TEXT, new_section TEXT,
            change_type TEXT NOT NULL,
            old_text TEXT, new_text TEXT NOT NULL,
            rule_version TEXT NOT NULL, link_group_id TEXT,
            FOREIGN KEY (run_id) REFERENCES diff_runs(id),
            FOREIGN KEY (old_notice_id) REFERENCES notices(notice_id),
            FOREIGN KEY (new_notice_id) REFERENCES notices(notice_id)
        )""")
        con.execute("""CREATE INDEX IF NOT EXISTS idx_changes_run
            ON section_changes(run_id)""")
        con.execute("""CREATE INDEX IF NOT EXISTS idx_changes_new_notice
            ON section_changes(new_notice_id)""")
        con.commit()

    # ------------------------------------------------------------- raw notices
    def save_notice(self, notice: RegulatoryNotice) -> None:
        """写入原始公告；内容冲突时拒绝覆盖。"""
        con = self.connection
        digest = _content_hash(notice)
        row = con.execute(
            "SELECT content_hash FROM notices WHERE notice_id = ?", (notice.notice_id,)
        ).fetchone()
        if row is not None and row[0] != digest:
            raise ContentConflictError(
                f"公告 {notice.notice_id} 的原始内容与库中记录不一致，"
                "拒绝覆盖；原始公告不允许被规则重算改写。"
            )
        if row is not None:
            return  # 已存在且内容一致：幂行跳过，不动任何历史数据
        con.execute(
            "INSERT INTO notices VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                notice.notice_id,
                notice.drug_name,
                notice.company,
                notice.approval_number,
                notice.revision_date,
                notice.source_url,
                digest,
            ),
        )
        con.executemany(
            "INSERT INTO notice_sections VALUES (?, ?, ?)",
            [(notice.notice_id, key, value) for key, value in notice.sections.items()],
        )
        con.commit()

    def get_notice_sections(self, notice_id: str) -> dict[str, str] | None:
        row = self.connection.execute(
            "SELECT 1 FROM notices WHERE notice_id = ?", (notice_id,)
        ).fetchone()
        if row is None:
            return None
        return dict(
            self.connection.execute(
                "SELECT section, text FROM notice_sections WHERE notice_id = ? ORDER BY section",
                (notice_id,),
            ).fetchall()
        )

    def latest_notice_for_drug(
        self, drug_name: str, approval_number: str, before_notice_id: str | None = None
    ) -> str | None:
        """返回同一药品（名称+批准文号）最新一条已入库公告 id。

        只基于库里真实存在的记录；缺历史时返回 None，由调用方按首次导入处理，
        不会伪造旧版本。``before_notice_id`` 用于把候选限制在当前版本之前。
        """
        sql = (
            "SELECT notice_id FROM notices WHERE drug_name = ? AND approval_number = ?"
        )
        params: list[str] = [drug_name, approval_number]
        if before_notice_id is not None:
            sql += " AND notice_id != ?"
            params.append(before_notice_id)
        sql += " ORDER BY revision_date DESC, notice_id DESC LIMIT 1"
        row = self.connection.execute(sql, params).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------- pair import
    def save_pair_result(
        self,
        result: PairResult,
        failure_hook=None,
    ) -> int:
        """在单事务内写入一次版本对处理的全部产物，返回 run_id。

        ``failure_hook`` 在 links 写完、changes 写之前调用（仅测试用），
        抛异常会触发整体回滚，验证重跑不会留下半套结论。
        """
        con = self.connection
        try:
            con.execute("BEGIN IMMEDIATE")
            # 两个公告必须已作为原始数据入库；不存在则报错而不是补造记录
            if con.execute(
                "SELECT 1 FROM notices WHERE notice_id = ?", (result.new_notice_id,)
            ).fetchone() is None:
                raise StorageError(f"新版本公告 {result.new_notice_id} 尚未入库，拒绝写入差异")
            if result.old_notice_id is not None and con.execute(
                "SELECT 1 FROM notices WHERE notice_id = ?", (result.old_notice_id,)
            ).fetchone() is None:
                raise StorageError(f"旧版本公告 {result.old_notice_id} 缺少历史记录，拒绝伪造关联")

            # 重跑同一 (版本对, 规则版本)：先清掉旧产物再插新产物，保证唯一结论
            old_run = con.execute(
                "SELECT id FROM diff_runs WHERE COALESCE(old_notice_id,'')=COALESCE(?,'') "
                "AND new_notice_id=? AND rule_version=?",
                (result.old_notice_id, result.new_notice_id, result.rule_version),
            ).fetchone()
            if old_run is not None:
                run_id = old_run[0]
                con.execute("DELETE FROM section_changes WHERE run_id = ?", (run_id,))
                con.execute("DELETE FROM section_links WHERE run_id = ?", (run_id,))
                con.execute(
                    "UPDATE diff_runs SET status=?, baseline=?, created_at=datetime('now') WHERE id=?",
                    (_RUN_PENDING, int(result.baseline), run_id),
                )
            else:
                cur = con.execute(
                    "INSERT INTO diff_runs (old_notice_id, new_notice_id, drug_name, "
                    "approval_number, rule_version, baseline, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        result.old_notice_id,
                        result.new_notice_id,
                        result.drug_name,
                        result.approval_number,
                        result.rule_version,
                        int(result.baseline),
                        _RUN_PENDING,
                    ),
                )
                run_id = cur.lastrowid

            self._validate_disjoint_confirmed(result.groups)
            self._insert_links(con, run_id, result)

            if failure_hook is not None:
                failure_hook(con, run_id)

            self._insert_changes(con, run_id, result)
            con.execute(
                "UPDATE diff_runs SET status=? WHERE id=?", (_RUN_COMPLETED, run_id)
            )
            con.commit()
            return run_id
        except BaseException:
            con.rollback()
            # 记录一次失败运行（独立事务），不保留任何半截 links/changes
            self._record_failure(result)
            raise

    def _record_failure(self, result: PairResult) -> None:
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO diff_runs (old_notice_id, new_notice_id, drug_name, "
                    "approval_number, rule_version, baseline, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        result.old_notice_id,
                        result.new_notice_id,
                        result.drug_name,
                        result.approval_number,
                        result.rule_version,
                        int(result.baseline),
                        _RUN_FAILED,
                    ),
                )
        except sqlite3.DatabaseError:
            pass

    @staticmethod
    def _validate_disjoint_confirmed(groups: list[SectionLinkGroup]) -> None:
        old_seen: dict[str, str] = {}
        new_seen: dict[str, str] = {}
        for group in groups:
            if group.status != CONFIRMED:
                continue  # 候选允许竞争，确认组必须互斥
            for section in group.old_sections:
                if section in old_seen:
                    raise StorageError(
                        f"确认关联冲突：旧章节 {section} 同时属于 {old_seen[section]} 和 {group.group_id}"
                    )
                old_seen[section] = group.group_id
            for section in group.new_sections:
                if section in new_seen:
                    raise StorageError(
                        f"确认关联冲突：新章节 {section} 同时属于 {new_seen[section]} 和 {group.group_id}"
                    )
                new_seen[section] = group.group_id

    def _insert_links(self, con: sqlite3.Connection, run_id: int, result: PairResult) -> None:
        rows: list[tuple] = []
        for group in result.groups:
            old_sections = group.old_sections or (None,)
            new_sections = group.new_sections or (None,)
            for old_section in old_sections:
                for new_section in new_sections:
                    rows.append(
                        (
                            run_id,
                            group.group_id,
                            result.old_notice_id,
                            result.new_notice_id,
                            old_section,
                            new_section,
                            group.relation,
                            group.status,
                            group.score,
                            group.rationale,
                            int(group.competing),
                        )
                    )
        con.executemany(
            "INSERT INTO section_links (run_id, group_id, old_notice_id, new_notice_id, "
            "old_section, new_section, relation, status, score, rationale, competing) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    def _insert_changes(self, con: sqlite3.Connection, run_id: int, result: PairResult) -> None:
        rows = [
            (
                run_id,
                item.old_notice_id,
                item.new_notice_id or result.new_notice_id,
                item.old_section,
                item.new_section,
                item.change_type,
                item.old_text,
                item.text,
                item.rule_version or result.rule_version,
                item.link_group_id,
            )
            for item in result.changes
        ]
        con.executemany(
            "INSERT INTO section_changes (run_id, old_notice_id, new_notice_id, "
            "old_section, new_section, change_type, old_text, new_text, rule_version, link_group_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    # -------------------------------------------------------------- adjudication
    def adjudicate_candidate(
        self,
        run_id: int,
        group_id: str,
        decision: str,
        note: str = "",
    ) -> None:
        """人工对候选组下结论（accepted/rejected），审核结果不可变。

        只允许作用于 candidate 组；已经审核过的候选不能改判，审核动作也不会
        自动改写正式 section_changes——规则结论与人工判断各自留痕。
        """
        if decision not in (ADJUDICATION_ACCEPTED, ADJUDICATION_REJECTED):
            raise StorageError(f"未知审核结论: {decision}")
        with self.connection:
            row = self.connection.execute(
                "SELECT status, adjudication FROM section_links WHERE run_id=? AND group_id=? LIMIT 1",
                (run_id, group_id),
            ).fetchone()
            if row is None:
                raise StorageError(f"run {run_id} 中不存在分组 {group_id}")
            status, adjudication = row
            if status != CANDIDATE:
                raise StorageError(f"分组 {group_id} 是规则确认关联，不属于候选审核范围")
            if adjudication != ADJUDICATION_PENDING:
                raise StorageError(
                    f"分组 {group_id} 已审核为 {adjudication}，审核结论不可变更"
                )
            self.connection.execute(
                "UPDATE section_links SET adjudication=?, adjudicated_at=datetime('now'), "
                "adjudicated_note=? WHERE run_id=? AND group_id=?",
                (decision, note, run_id, group_id),
            )

    # ------------------------------------------------------------------ queries
    def list_runs(self, new_notice_id: str | None = None) -> list[sqlite3.Row]:
        con = self.connection
        con.row_factory = sqlite3.Row
        sql = "SELECT * FROM diff_runs"
        params: list[str] = []
        if new_notice_id is not None:
            sql += " WHERE new_notice_id=?"
            params.append(new_notice_id)
        sql += " ORDER BY id"
        return con.execute(sql, params).fetchall()

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        self.connection.row_factory = sqlite3.Row
        return self.connection.execute(
            "SELECT * FROM diff_runs WHERE id=?", (run_id,)
        ).fetchone()

    def list_links(self, run_id: int, status: str | None = None) -> list[sqlite3.Row]:
        self.connection.row_factory = sqlite3.Row
        sql = "SELECT * FROM section_links WHERE run_id=?"
        params: list[object] = [run_id]
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY group_id, id"
        return self.connection.execute(sql, params).fetchall()

    def list_changes(self, run_id: int) -> list[sqlite3.Row]:
        self.connection.row_factory = sqlite3.Row
        return self.connection.execute(
            "SELECT * FROM section_changes WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()

    def trace_change(self, change_id: int) -> dict | None:
        """从任一正式差异反查它依据的两版章节、规则版本与关联依据。"""
        self.connection.row_factory = sqlite3.Row
        change = self.connection.execute(
            "SELECT * FROM section_changes WHERE id=?", (change_id,)
        ).fetchone()
        if change is None:
            return None
        links = []
        if change["link_group_id"] is not None:
            links = self.connection.execute(
                "SELECT * FROM section_links WHERE run_id=? AND group_id=? ORDER BY id",
                (change["run_id"], change["link_group_id"]),
            ).fetchall()
        return {"change": change, "links": [dict(row) for row in links]}

    def pending_candidates(self, new_notice_id: str | None = None) -> list[sqlite3.Row]:
        self.connection.row_factory = sqlite3.Row
        sql = (
            "SELECT l.* FROM section_links l JOIN diff_runs r ON l.run_id = r.id "
            "WHERE l.status='candidate' AND l.adjudication='pending'"
        )
        params: list[str] = []
        if new_notice_id is not None:
            sql += " AND l.new_notice_id=?"
            params.append(new_notice_id)
        sql += " ORDER BY l.run_id, l.group_id, l.id"
        return self.connection.execute(sql, params).fetchall()

    def close(self) -> None:
        self.connection.close()
