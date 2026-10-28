# events org 变更草稿

状态：已实现（2026-10-23）。ORGS、markers-org v3、README、术语/规则草稿和 test_knowledge 已同步。

## 1. 目标

给 minir 的知识体系加一个 `events` org，让"单次带日期的发生"有独立归档位置，不再和 `experiences`（单次发生 + 得到的教训）挤在一个目录里。

## 2. 边界

- events：一次带日期的发生，有参与者、地点与结果，不承担可复用教训。部署、故障、发布、迁移、会议。
- experiences：一次发生，重点是它证明了什么、留下了什么可复用结论。
- trajectories：同一对象在两个时间点之间的状态变化。
- 判定顺序：身份 → 时间变化 → 单次发生（events）→ 单次教训（experiences）→ 能力 → 持续工作。两者都像时取"下个会话会去搜的那个对象"；教训归 experiences，纯发生归 events。

## 3. 变更清单

| 文件 | 位置 | 动作 |
| --- | --- | --- |
| `plugin/minir/layers.py` | ORGS | 加 `"events"`：`("entities", "trajectories", "experiences", "events", "tools", "projects")` |
| `plugin/minir/rules/markers-org.md` | 头部与 CONTRACT | version 2→3；ORGS 加 events 定义；SELECTION 加 events 一步；EXAMPLES 加 events 例；OBJECTS 的 "five orgs" 改为 "orgs"；REVIEW CHECKS 加 events/experiences 误分检查 |
| `plugin/minir/extraction.py` | `validate_pages` | `events` + `KNOWLEDGE` 强制 `epistemic = "fact"` |
| `plugin/minir/knowledge.py` | `_write_unit` | 同上，直接写页也强制 fact |
| `README.md` | Knowledge pages 节 | org 列表加 `events` |
| `docs/draft/layer-terminology-draft.md` | org 行 | 加 `events` |
| `docs/draft/rule-files-draft.md` | ORGS / SELECTION / EXAMPLES | 加 events 定义、选择顺序与示例 |
| `tests/test_knowledge.py` | 写页测试附近 | 新增 `test_events_org_is_a_valid_knowledge_page`（写页落在 `events/`、front-matter org 正确、kb_search 可见）与 `test_events_pages_are_facts`（`validate_pages` 强制 fact） |

## 4. 自动生效的路径

- `knowledge.py` 的路径与文件名校验直接用 `ORGS`，events 页落到 `knowledge/events/`。
- `tools.py` 的 `minir_kb_search` org 枚举来自 `ORGS`，自动出现 events。
- `extraction.py` 的标记 org 校验、`layers.validate_value_markers` 的 objects 类型校验都放宽到 events。
- `knowledge_render` 可以写 events 页，不需要第二份白名单。

## 5. 明确不动的部分

- `store._MARKER_KINDS` 与 `minir_remember` 的 kind 不加 events：events 是知识 org，不是记忆卡类型。
- `knowledge.write_unit` 的 2k 预算与"丢最旧证据再失败"不改。
- 不导入 lancedb-memory 数据，不写适配器；相关映射见 `docs/draft/lancedb-knowledge-adaptation-draft.md`。

## 6. 验收

1. `python -m pytest tests/test_knowledge.py tests/test_layers.py -q` 全绿（含新增 events 页测试）。
2. `python -m pytest -q` exit 0。
3. 手工写一页 `org=events`，确认路径为 `knowledge/events/KNOWLEDGE-<slug>.md`，`minir_kb_search` 的 org 过滤能看到 events。

## 7. 风险与回退

- 风险：events 与 experiences 的判定重叠，抽取 agent 可能把"有教训的事件"写成 events。缓解是 rules 里写死"教训归 experiences"并加 review check。
- 风险：文档里旧的"五个 org"说法漏改。已列在上表。
- 回退：撤销 ORGS 一行、rules 回 version 2、文档列表回退、删除新增测试；无数据迁移，因为现有知识目录没有 events 子目录。

## 8. 已定

- knowledge_render 遵循 markers-org 选择 org，可产出 events 页；没有第二份白名单。
- events 页的 `epistemic` 固定 `fact`：`extraction.validate_pages` 与 `knowledge._write_unit` 都强制。
