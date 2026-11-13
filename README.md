# Drug Registry

医药监管公开信息与药品说明书更新采集链路的离线实现。

## 模块职责

- `sources.py`：读取公告和说明书版本输入（JSON 公告 → 原始 dict）。
- `normalize.py`：统一药品名、企业名、批准文号和章节文本。
- `matching.py`：**章节对应关系引擎**。在同一药品的明确版本对之间，
  按带版本号的规则输出确认关联（same / rename / split / merge）与
  候选关联（candidate，含分数、依据、竞争标记），并据此生成正式差异。
- `diff.py`：兼容入口，返回匹配引擎产出的 `SectionChange` 列表。
- `storage.py`：SQLite 持久化，原始公告哈希保护、单事务原子写入、
  候选审核留痕、差异反查。
- `pipeline.py`：按药品（名称+批准文号）与修订日期编排版本对处理。

## 匹配规则的两级结论

| 级别 | 触发条件（rules-v1） | 是否产生正式差异 |
| --- | --- | --- |
| confirmed / same | 标题完全相同（空正文亦可，标题即章节标识） | 仅正文变化时 updated |
| confirmed / rename | 标题不同但**非空正文完全相同** | renamed / renamed_updated |
| confirmed / split | 1 个旧章节正文被多个新章节按顺序覆盖（覆盖度 ≥0.99） | split_from |
| confirmed / merge | 多个旧章节正文按顺序覆盖 1 个新章节 | merged_into |
| candidate | 仅标题相似、正文部分重叠、空正文无法验证、或 v2 下结构证据不足 | 不产生；正式结论仍为 added/removed |

候选之间允许竞争（`competing=1`），确认关联的章节端必须互不相交；
不确定的关联永远不会被算成已确认的监管变化。未进入任何确认组的章节
才是真正的新增（added）或删除（removed）。

### 规则版本

- `rules-v1`：结构匹配（拆分/合并）只看正文覆盖；标题候选阈值 0.3。
- `rules-v2`：结构匹配额外要求标题亲和度 ≥0.5，不满足则降级为候选；
  标题候选阈值提高到 0.6。

切换规则重跑不会覆盖另一套结论：`diff_runs` 以
`(old_notice_id, new_notice_id, rule_version)` 为键各自留痕；
原始公告带内容哈希，内容冲突直接拒绝，规则重算无法改写公告原文。

## 运行

```bash
PYTHONPATH=src python3 -m drug_registry.cli \
  --fixture fixtures/notices_rich.json \
  --database /tmp/drug-registry.sqlite3 \
  --rule-version rules-v1
```

## SQLite 查询示例

```sql
-- 一个版本对的全部章节关联（确认 + 候选）
SELECT group_id, old_section, new_section, relation, status,
       competing, score, rationale, adjudication
FROM section_links WHERE run_id = ? ORDER BY group_id, id;

-- 从任一正式差异反查它依据的两版章节
SELECT c.change_type, c.old_notice_id, c.old_section,
       c.new_notice_id, c.new_section, c.rule_version, c.link_group_id
FROM section_changes c WHERE c.id = ?;

-- 仍待人工判断的候选
SELECT r.new_notice_id, l.group_id, l.old_section, l.new_section, l.rationale
FROM section_links l JOIN diff_runs r ON l.run_id = r.id
WHERE l.status='candidate' AND l.adjudication='pending';
```

候选可通过 `DrugStore.adjudicate_candidate(run_id, group_id, 'accepted'|'rejected')`
审核；审核结论不可变，且不会自动改写 `section_changes`——规则结论与
人工判断各自留痕。旧版数据库（无章节关联列的 v1 schema）打开时直接报错，
不迁移、不伪造历史。

## 原子性

单个版本对的 links 与 changes 在一个 `BEGIN IMMEDIATE` 事务内写入，
中途任何异常整体回滚（只保留一条 `status='failed'` 的运行留痕）；
重新处理同一版本对采用先删后插的替换语义，不会留下互相矛盾的半套结论。

## 测试

```bash
python -m pytest          # 有 pytest 的环境
# 离线无 pytest 时使用仓库自带垫片：
PYTHONPATH=src python3 tools/pytest_shim.py tests
```
