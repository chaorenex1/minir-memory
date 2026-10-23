# lancedb 记忆与 minir 知识体系适配草稿

状态：规则已定；**不导入 lancedb-memory 数据**，不写导入器，不做迁移，不双写。

依据：`hermes-lancedb-memory` 的 `README.md`、`docs/filesystem-memory-contract.md`、`plugin/lancedb/prompt_templates.py`、`memory_types.py`；minir 的 `knowledge.py`、`layers.py`、`rules/markers-org.md`。

## 1. 决定

1. minir 拟新增 `events` org；变更草稿见 `doc/events-org-change-draft.md`，当前代码未改。
2. 适配时 category 用固定映射，不跑分类模型。
3. 超过 2k token 的页面拆子页，不丢内容。
4. 同一 (category, org, topic) 的多个来源合并成一页，不加后缀。
5. 不导入 lancedb-memory 的现有数据；本稿只固化规则与 org。

## 2. 为什么只适配规则

- lancedb 的叶文件是纯语义 markdown，`document_id`、版本、`source_refs`、`evidence_refs`、hash 都在 SQLite；minir 的页面靠页内 front-matter 做身份与检索。两边对"一页是什么"的定义不同。
- 根目录不能共用：lancedb 会把 minir 的 front-matter 当正文，minir 会把 lancedb 的无 front-matter 文件当无效页。
- 本机 `$HERMES_HOME/lancedb-memory` 为空；没有数据要迁，先定规则，避免为一次不存在的迁移写代码。

## 3. 固定映射

| lancedb 路径 | org | category | topic 来源 |
| --- | --- | --- | --- |
| `entities/<subtype>/<slug>.md` | `entities` | `KNOWLEDGE` | H1 实体名 |
| `events/YYYY/MM/DD/<slug>.md` | `events` | `KNOWLEDGE` | H1 事件名 |
| `experiences/<slug>.md` | `experiences` | `METHOD` | H1 |
| `tools/<slug>.md` | `tools` | `METHOD` | H1 |
| `trajectories/<slug>.md` | `trajectories` | `INTENT` | H1 |
| `projects/<project>/<role>.md` | `projects` | `KNOWLEDGE` | `<project> <role>` |

- subtype 与日期不进制目录，写进 `objects`：`entities:person`、`events:2026-10-18`。
- `epistemic` 只在 category 为 `KNOWLEDGE` 时给：默认 `fact`；lancedb 的弱验证强度用 `observation` 不在 minir 枚举里，改用 `inference`。
- 一张 lancedb 页可能同时含事实与方法；固定映射只取一个 category，另一层内容留在正文，不拆 category。

## 4. front-matter 合成

| minir 键 | 来源 |
| --- | --- |
| `id` | lancedb `document_id` |
| `revisions` | lancedb `version` |
| `content_hash` | lancedb 语义 hash |
| `time` | events 的 `Occurred At`；其余用文件 mtime 或 source ref 时间 |
| `summary` | `detailed-summary.md` 里该路径的条目，裁到 120 字；没有则首段 |
| `source_session` | `source_refs` 里的 session ref |
| `source_artifact` | `source_refs` 里的 artifact ref |
| `objects` | subtype、日期、正文里的对象引用 |
| `relations` | lancedb 的 Related/Links 段；`source_refs` 剩余项用 `type: source`，`evidence_refs` 用 `type: evidence` |

正文里的相对链接要按"旧路径 → 新页面"重写；映射不到的原文保留，并在报告里计数。

## 5. 超预算拆页

- 预算按 minir 的 2k token，含 front-matter。
- 先按 H2 分组累加；单组超预算时，把该组再按 H3 拆。
- 拆出的页 topic 为 `<原 topic> (1/n)`；每页 front-matter 的 `relations` 加 `{type: part_of, ref: <原 topic>}`。
- 不丢行、不截断；超到无法拆（单段超预算）时判失败并报告，不写半页。
- 当前 `knowledge.write_unit` 仍是"丢最旧证据再失败"，拆页属于适配器规则，运行时不改。

## 6. 同名合并

两个来源映射到同一 (category, org, topic) 时合并，不加后缀：

- 正文按 H2 合并；同名 H2 下追加不重复行（按行归一化去重）。
- 标量字段取 `time` 较新的一侧；`revisions` 取较大值 + 1。
- `objects`、`relations`、`source_refs` 取并集；`summary` 取较新的非空值。
- 合并后仍要过 2k 预算，超了就按 §5 拆。
- 报告里记录"merged from N sources"。

## 7. 不导入

- 不写 `import_lancedb_knowledge.py`，不跑迁移，不改 lancedb 文件。
- `identity.md`/`preference.md` 属于用户卡/AI 身份，`contexts/*` 属于会话卡与会话摘要，都不进知识库。
- `summary.md`/`detailed-summary.md` 只作为 §4 的 `summary` 来源，文件本身不导入。
- 若将来要导入，上表与 §4-§6 是合同；先按 `doc/events-org-change-draft.md` 落地 `events` org。

## 8. 与现有机制的接口

- 页面落在 `knowledge/events/<CATEGORY>-<topic>.md`；`minir_kb_search` 的 org 枚举来自 `layers.ORGS`，落地后自动包含 `events`。
- `minir_remember` 的 kind 不变：`events` 是知识 org，不是记忆卡 kind；`store._MARKER_KINDS` 不加。
- 标记校验、对象引用、知识页写入都用同一份 `ORGS`，不需要第二份白名单。
