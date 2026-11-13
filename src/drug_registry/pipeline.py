"""导入编排：sources -> normalize -> matching -> storage。

版本对的确定只发生在同一药品（药品名+批准文号）按修订日期排序后的相邻
公告之间，属于输入明确给出的版本对；旧数据库缺记录时按首次导入处理，
不会伪造旧版本。每次处理的规则版本随 diff_run 留痕，切换规则重跑会
新增/替换对应 rule_version 的结论，原始公告始终不受影响。
"""

from __future__ import annotations

from .matching import DEFAULT_RULE_VERSION, match_sections
from .models import PairResult, RegulatoryNotice
from .normalize import normalize_notice
from .sources import NoticeSource
from .storage import DrugStore


class DrugRegistryPipeline:
    def __init__(
        self,
        source: NoticeSource,
        store: DrugStore,
        rule_version: str = DEFAULT_RULE_VERSION,
        failure_hook=None,
    ) -> None:
        self.source = source
        self.store = store
        self.rule_version = rule_version
        # 测试钩子：在某个版本对的 links 写入后、changes 写入前制造失败
        self.failure_hook = failure_hook

    def run(self) -> list[PairResult]:
        previous_by_drug: dict[tuple[str, str], RegulatoryNotice] = {}
        results: list[PairResult] = []
        raws = sorted(self.source.read(), key=lambda item: (item["revision_date"], str(item["notice_id"])))
        for raw in raws:
            current = normalize_notice(raw)
            # 原始公告先独立落库；内容哈希冲突会直接拒绝，规则重算无法覆盖它
            self.store.save_notice(current)
            key = (current.drug_name, current.approval_number)
            previous = previous_by_drug.get(key)
            result = match_sections(previous, current, self.rule_version)
            self.store.save_pair_result(result, failure_hook=self.failure_hook)
            previous_by_drug[key] = current
            results.append(result)
        return results
