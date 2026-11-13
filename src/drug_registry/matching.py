"""章节匹配引擎：在同一药品的明确版本对之间建立章节对应关系。

设计要点
--------
1. 每条输出都带 ``rule_version``、分数和中文依据（rationale），审阅者可以
   对照规则版本解释结论，也可以重跑另一套规则比较前后差异。
2. 规则分两级：
   - 确认（confirmed）：标题完全相同、非空正文完全相同、拆分/合并的
     覆盖关系成立。确认组章节互不相交，唯一竞争旧/新章节。
   - 候选（candidate）：仅标题相似、或证据不足以达到确认阈值。候选不参与
     正式差异计算，只写入 ``section_links`` 供人工判断；多个候选争夺同一
     章节时用 ``competing=True`` 标记，互不静默覆盖。
3. 空正文：标题相同仍可确认（监管文件中标题本身就是章节标识），但只凭
   正文或相似度的匹配在正文为空时一律降级为候选，不得当作改名确认。
"""

from __future__ import annotations
from dataclasses import dataclass
from itertools import combinations, permutations

from .models import PairResult, RegulatoryNotice, SectionChange, SectionLinkGroup

RULE_VERSION_V1 = "rules-v1"
RULE_VERSION_V2 = "rules-v2"
DEFAULT_RULE_VERSION = RULE_VERSION_V1

CONFIRMED = "confirmed"
CANDIDATE = "candidate"


@dataclass(frozen=True)
class MatchRules:
    version: str
    # 仅标题相似、没有正文证据时，达到该相似度才产生候选（永远不会确认）
    title_candidate_threshold: float
    # 拆分/合并要求的正文覆盖度（旧正文是新正文的简单拼接，或反之）
    containment_threshold: float
    # v2 起，拆分/合并还要求标题对之间存在最低相似度，防止含义不同的章节
    # 仅凭文字拼接巧合被确认为结构变化
    structure_title_affinity: float
    require_title_affinity_for_structure: bool


RULES: dict[str, MatchRules] = {
    RULE_VERSION_V1: MatchRules(
        version=RULE_VERSION_V1,
        title_candidate_threshold=0.3,
        containment_threshold=0.99,
        structure_title_affinity=0.0,
        require_title_affinity_for_structure=False,
    ),
    RULE_VERSION_V2: MatchRules(
        version=RULE_VERSION_V2,
        title_candidate_threshold=0.6,
        containment_threshold=0.99,
        structure_title_affinity=0.5,
        require_title_affinity_for_structure=True,
    ),
}


def available_rule_versions() -> list[str]:
    return list(RULES)


def _char_ngrams(text: str, n: int = 2) -> set[str]:
    text = text.strip()
    if not text:
        return set()
    if len(text) < n:
        return {text}
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def title_similarity(left: str, right: str) -> float:
    """字符二元组 Jaccard 相似度，取值 [0, 1]；空标题返回 0。"""
    grams_left = _char_ngrams(left)
    grams_right = _char_ngrams(right)
    if not grams_left or not grams_right:
        return 0.0
    return len(grams_left & grams_right) / len(grams_left | grams_right)


def _coverage_ratio(container: str, parts: list[str]) -> float:
    """拆分/合并的覆盖度评分，取值 [0, 1]。

    单纯做字符包含会让短章节误命中（只要字符都在拼接串里出现即可），因此
    组合两个更严格的信号：
    1. 顺序消费率：按 parts 给定顺序在 container 上贪心匹配，实际连续消费的
       字符占 container 的比例——能识别“拼接关系”而不是“字符集合相似”；
    2. 二元组覆盖率：container 的相邻字符二元组有多少出现在 parts 拼接中，
       用来约束局部用词一致性。
    """
    joined = "".join(parts)
    if not container or not joined:
        return 0.0
    cursor = 0
    consumed = 0
    for part in parts:
        idx = container.find(part, cursor)
        if idx >= 0:
            consumed += len(part)
            cursor = idx + len(part)
    order_ratio = consumed / len(container)
    grams_container = _char_ngrams(container)
    grams_joined = _char_ngrams(joined)
    gram_ratio = (
        len(grams_container & grams_joined) / len(grams_container)
        if grams_container
        else 0.0
    )
    return min(1.0, 0.7 * order_ratio + 0.3 * gram_ratio)


def _best_title_affinity(old_titles: tuple[str, ...], new_titles: tuple[str, ...]) -> float:
    return max(
        (title_similarity(o, n) for o in old_titles for n in new_titles),
        default=0.0,
    )


def _structure_candidates(
    sections: list[str], body_map: dict[str, str], container_body: str
) -> list[tuple[str, ...]]:
    """枚举拆分/合并候选组合，并做长度护栏避免无意义的大组合爆炸。

    护栏：成员总长度不超过容器的 1.8 倍；组合成员数封顶 5 个。
    每个组合按所有排列返回——部件在正文中的先后顺序不一定等于标题排序，
    覆盖度评分会选出真正能按顺序消费容器正文的排列。护栏只做剪枝，
    最终判定仍由覆盖度阈值负责。
    """
    eligible = sorted(s for s in sections if body_map[s])
    limit = min(5, len(eligible))
    out: list[tuple[str, ...]] = []
    for width in range(2, limit + 1):
        for combo in combinations(eligible, width):
            total = sum(len(body_map[s]) for s in combo)
            if total <= max(len(container_body), 1) * 1.8:
                out.extend(permutations(combo))
    return out


@dataclass
class _Edge:
    """匹配过程中产生的一条候选边（可能单独成组，也可能与其他边合并成组）。"""

    old_sections: tuple[str, ...]
    new_sections: tuple[str, ...]
    relation: str
    score: float
    rationale: str
    competing: bool = False


def _make_group(seq: int, edge: _Edge, status: str) -> SectionLinkGroup:
    return SectionLinkGroup(
        group_id=f"g{seq:03d}",
        old_sections=edge.old_sections,
        new_sections=edge.new_sections,
        relation=edge.relation,
        status=status,
        score=round(edge.score, 4),
        rationale=edge.rationale,
        competing=edge.competing,
    )


def match_sections(
    previous: RegulatoryNotice | None,
    current: RegulatoryNotice,
    rule_version: str = DEFAULT_RULE_VERSION,
) -> PairResult:
    """对同一药品的一个明确版本对执行章节匹配。"""
    rules = RULES[rule_version]
    if previous is None:
        # 首次导入：数据库中没有旧版本，不能伪造历史，全部按真实新增处理
        changes = [
            SectionChange(
                current.notice_id,
                section,
                "added",
                text,
                old_notice_id=None,
                old_section=None,
                new_notice_id=current.notice_id,
                new_section=section,
                old_text=None,
                rule_version=rule_version,
                link_group_id=None,
            )
            for section, text in current.sections.items()
        ]
        return PairResult(
            old_notice_id=None,
            new_notice_id=current.notice_id,
            drug_name=current.drug_name,
            approval_number=current.approval_number,
            rule_version=rule_version,
            baseline=True,
            groups=[],
            changes=changes,
        )

    old_map = dict(previous.sections)
    new_map = dict(current.sections)
    used_old: set[str] = set()
    used_new: set[str] = set()
    groups: list[SectionLinkGroup] = []
    seq = 0

    def add_group(edge: _Edge, status: str) -> None:
        nonlocal seq
        seq += 1
        groups.append(_make_group(seq, edge, status))

    # --- 规则 1：标题完全相同（1:1）。标题是章节标识，空正文也可确认。 ---
    for title in sorted(set(old_map) & set(new_map)):
        used_old.add(title)
        used_new.add(title)
        if old_map[title] == new_map[title]:
            relation, score = "same", 1.0
            rationale = "规则1：新旧章节标题完全相同且正文一致，判定为同一章节，无内容变化。"
        else:
            relation, score = "same", 0.95
            rationale = "规则1：新旧章节标题完全相同，正文存在差异，判定为同一章节的内容更新。"
        add_group(_Edge((title,), (title,), relation, score, rationale), CONFIRMED)

    free_old = [s for s in old_map if s not in used_old]
    free_new = [s for s in new_map if s not in used_new]

    # --- 规则 2：非空正文完全相同但标题不同（1:1 改名，正文不变）。 ---
    body_to_old: dict[str, list[str]] = {}
    for section in free_old:
        if old_map[section]:
            body_to_old.setdefault(old_map[section], []).append(section)
    for new_section in sorted(free_new):
        body = new_map[new_section]
        if not body:
            continue
        candidates = body_to_old.get(body, [])
        if len(candidates) == 1:
            old_section = candidates[0]
            if old_section in used_old:
                continue
            sim = title_similarity(old_section, new_section)
            used_old.add(old_section)
            used_new.add(new_section)
            add_group(
                _Edge(
                    (old_section,),
                    (new_section,),
                    "rename",
                    0.9 + 0.05 * sim,
                    "规则2：标题不同但非空正文完全一致，判定为章节改名且正文不变；"
                    f"标题相似度 {sim:.2f} 仅作记录。",
                ),
                CONFIRMED,
            )

    free_old = [s for s in old_map if s not in used_old]
    free_new = [s for s in new_map if s not in used_new]

    # --- 规则 3：拆分（1 旧 -> 多新），按 (覆盖度, 组合大小, 名称) 确定性排序。 ---
    for old_section in sorted(free_old):
        old_body = old_map[old_section]
        if not old_body:
            continue
        available_new = [s for s in free_new if s not in used_new and new_map[s]]
        possible: list[tuple[float, int, tuple[str, ...]]] = []
        for members in _structure_candidates(available_new, new_map, old_body):
            ratio = _coverage_ratio(old_body, [new_map[s] for s in members])
            if ratio >= rules.containment_threshold:
                possible.append((ratio, -len(members), members))
        if possible:
            possible.sort(reverse=True)
            ratio, _, members = possible[0]
            affinity = _best_title_affinity((old_section,), members)
            rationale = (
                "规则3：新章节正文按顺序覆盖旧章节正文"
                f"（覆盖度 {ratio:.2f}），判定为一拆多。"
            )
            if rules.require_title_affinity_for_structure and affinity < rules.structure_title_affinity:
                rationale += (
                    f" 但规则 {rules.version} 要求标题相似度≥"
                    f"{rules.structure_title_affinity:.2f}，实际 {affinity:.2f}，"
                    "证据不足，降级为候选。"
                )
                add_group(
                    _Edge((old_section,), members, "split", ratio * 0.5, rationale),
                    CANDIDATE,
                )
                continue
            if rules.require_title_affinity_for_structure:
                rationale += f" 标题相似度 {affinity:.2f} 满足规则 {rules.version} 要求。"
            used_old.add(old_section)
            used_new.update(members)
            add_group(_Edge((old_section,), members, "split", ratio, rationale), CONFIRMED)

    # --- 规则 4：合并（多旧 -> 1 新）。 ---
    for new_section in sorted(s for s in free_new if s not in used_new):
        new_body = new_map[new_section]
        if not new_body:
            continue
        available_old = [s for s in free_old if s not in used_old and old_map[s]]
        possible: list[tuple[float, int, tuple[str, ...]]] = []
        for members in _structure_candidates(available_old, old_map, new_body):
            ratio = _coverage_ratio(new_body, [old_map[s] for s in members])
            if ratio >= rules.containment_threshold:
                possible.append((ratio, -len(members), members))
        if possible:
            possible.sort(reverse=True)
            ratio, _, members = possible[0]
            affinity = _best_title_affinity(members, (new_section,))
            rationale = (
                "规则4：旧章节正文按顺序覆盖新章节正文"
                f"（覆盖度 {ratio:.2f}），判定为多合一。"
            )
            if rules.require_title_affinity_for_structure and affinity < rules.structure_title_affinity:
                rationale += (
                    f" 但规则 {rules.version} 要求标题相似度≥"
                    f"{rules.structure_title_affinity:.2f}，实际 {affinity:.2f}，"
                    "证据不足，降级为候选。"
                )
                add_group(
                    _Edge(members, (new_section,), "merge", ratio * 0.5, rationale),
                    CANDIDATE,
                )
                continue
            if rules.require_title_affinity_for_structure:
                rationale += f" 标题相似度 {affinity:.2f} 满足规则 {rules.version} 要求。"
            used_old.update(members)
            used_new.add(new_section)
            add_group(_Edge(members, (new_section,), "merge", ratio, rationale), CONFIRMED)

    # --- 规则 5：剩余章节两两收集候选证据（永不确认）。 ---
    raw_edges: dict[tuple[str, str], _Edge] = {}

    def remember(old_section: str, new_section: str, edge: _Edge) -> None:
        key = (old_section, new_section)
        existing = raw_edges.get(key)
        if existing is None or edge.score > existing.score:
            raw_edges[key] = edge

    for old_section in sorted(s for s in old_map if s not in used_old):
        for new_section in sorted(s for s in new_map if s not in used_new):
            old_body = old_map[old_section]
            new_body = new_map[new_section]
            sim = title_similarity(old_section, new_section)
            if not old_body or not new_body:
                # 至少一侧为空：只保留标题相似候选，且分数压低，明确不确认
                if sim >= rules.title_candidate_threshold:
                    remember(
                        old_section,
                        new_section,
                        _Edge(
                            (old_section,),
                            (new_section,),
                            "rename",
                            0.2 + 0.3 * sim,
                            f"规则5：至少一侧正文为空，无法用正文验证；仅标题相似度 {sim:.2f}，"
                            "保留为候选，不得视为已确认改名。",
                        ),
                    )
                continue
            if old_body == new_body:
                # 正文相同却走到这里，通常是多对一竞争未被规则 2 消歧
                remember(
                    old_section,
                    new_section,
                    _Edge(
                        (old_section,),
                        (new_section,),
                        "rename",
                        0.8 + 0.1 * sim,
                        f"规则5：非空正文完全一致但存在同名竞争，无法唯一确认改名；标题相似度 {sim:.2f}。",
                    ),
                )
                continue
            if sim >= rules.title_candidate_threshold:
                remember(
                    old_section,
                    new_section,
                    _Edge(
                        (old_section,),
                        (new_section,),
                        "rename",
                        0.3 + 0.4 * sim,
                        f"规则5：标题相似（{sim:.2f}）但正文不同，含义是否一致存疑，保留候选待判断。",
                    ),
                )
            ratio_old = _coverage_ratio(old_body, [new_body])
            ratio_new = _coverage_ratio(new_body, [old_body])
            # 字符覆盖在短文本上极易偶然命中（如“慎用。”vs“禁用。”），
            # 只有较长正文才允许把部分重叠作为候选证据
            if max(ratio_old, ratio_new) >= 0.6 and max(len(old_body), len(new_body)) >= 12:
                remember(
                    old_section,
                    new_section,
                    _Edge(
                        (old_section,),
                        (new_section,),
                        "rename",
                        0.15 + 0.35 * max(ratio_old, ratio_new),
                        f"规则5：正文部分重叠（覆盖度 {max(ratio_old, ratio_new):.2f}）但不满足拆分/合并阈值，保留候选。",
                    ),
                )

    # 竞争检测：同一章节被多条候选边引用即标记 competing
    old_degree: dict[str, int] = {}
    new_degree: dict[str, int] = {}
    for edge in raw_edges.values():
        for section in edge.old_sections:
            old_degree[section] = old_degree.get(section, 0) + 1
        for section in edge.new_sections:
            new_degree[section] = new_degree.get(section, 0) + 1
    for edge in raw_edges.values():
        edge.competing = any(old_degree.get(s, 0) > 1 for s in edge.old_sections) or any(
            new_degree.get(s, 0) > 1 for s in edge.new_sections
        )
        if edge.competing:
            edge.rationale += " 该章节同时存在多个竞争候选，候选之间不得互相当作确认结论。"
    for edge in sorted(raw_edges.values(), key=lambda item: (-item.score, item.old_sections, item.new_sections)):
        add_group(edge, CANDIDATE)

    changes = _build_changes(previous, current, rule_version, groups, old_map, new_map)
    return PairResult(
        old_notice_id=previous.notice_id,
        new_notice_id=current.notice_id,
        drug_name=current.drug_name,
        approval_number=current.approval_number,
        rule_version=rule_version,
        baseline=False,
        groups=groups,
        changes=changes,
    )


def _build_changes(
    previous: RegulatoryNotice,
    current: RegulatoryNotice,
    rule_version: str,
    groups: list[SectionLinkGroup],
    old_map: dict[str, str],
    new_map: dict[str, str],
) -> list[SectionChange]:
    changes: list[SectionChange] = []
    linked_old: set[str] = set()
    linked_new: set[str] = set()

    for group in groups:
        if group.status != CONFIRMED:
            continue  # 候选不产生正式监管差异
        linked_old.update(group.old_sections)
        linked_new.update(group.new_sections)
        if group.relation == "same":
            section = group.new_sections[0]
            if old_map[section] != new_map[section]:
                changes.append(
                    SectionChange(
                        current.notice_id,
                        section,
                        "updated",
                        new_map[section],
                        previous.notice_id,
                        section,
                        current.notice_id,
                        section,
                        old_map[section],
                        rule_version,
                        group.group_id,
                    )
                )
        elif group.relation == "rename":
            old_section = group.old_sections[0]
            new_section = group.new_sections[0]
            change_type = "renamed" if old_map[old_section] == new_map[new_section] else "renamed_updated"
            changes.append(
                SectionChange(
                    current.notice_id,
                    new_section,
                    change_type,
                    new_map[new_section],
                    previous.notice_id,
                    old_section,
                    current.notice_id,
                    new_section,
                    old_map[old_section],
                    rule_version,
                    group.group_id,
                )
            )
        elif group.relation == "split":
            old_section = group.old_sections[0]
            for new_section in group.new_sections:
                changes.append(
                    SectionChange(
                        current.notice_id,
                        new_section,
                        "split_from",
                        new_map[new_section],
                        previous.notice_id,
                        old_section,
                        current.notice_id,
                        new_section,
                        old_map[old_section],
                        rule_version,
                        group.group_id,
                    )
                )
        elif group.relation == "merge":
            new_section = group.new_sections[0]
            for old_section in group.old_sections:
                changes.append(
                    SectionChange(
                        current.notice_id,
                        new_section,
                        "merged_into",
                        new_map[new_section],
                        previous.notice_id,
                        old_section,
                        current.notice_id,
                        new_section,
                        old_map[old_section],
                        rule_version,
                        group.group_id,
                    )
                )

    # 未进入任何确认组的章节才是真正的新增/删除；候选不改变这一结论
    for section in sorted(set(new_map) - linked_new):
        changes.append(
            SectionChange(
                current.notice_id,
                section,
                "added",
                new_map[section],
                previous.notice_id,
                None,
                current.notice_id,
                section,
                None,
                rule_version,
                None,
            )
        )
    for section in sorted(set(old_map) - linked_old):
        changes.append(
            SectionChange(
                current.notice_id,
                section,
                "removed",
                old_map[section],
                previous.notice_id,
                section,
                current.notice_id,
                None,
                old_map[section],
                rule_version,
                None,
            )
        )

    changes.sort(key=lambda item: (item.new_section or "", item.old_section or "", item.change_type))
    return changes
