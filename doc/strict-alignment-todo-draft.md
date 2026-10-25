# 四稿严格对齐 TODO（草稿）

状态：W0-W4 的代码项与可映射测试全部落地；W2-7 38/38（golden 为改动后首版基线），W3-6 的 14 条具名用例全部存在，W4-2 身份谓词按 A 稿记为已知限制，W4-6 的"A 稿 38 条"在 A 稿中不存在（§4 是指标表）；全量 758 passed。
实施记录（W0，2026-10-25）：

- W0-1 path 身份：`knowledge.normalize_path` / `resolve_path`；`write_unit` 以 path 为唯一身份，缺 path / 非法 path 记 `_failed`；`_merge_units` 以 path 合并；`extract_pages` 与 `minir_remember` 的 knowledge 模式都解析出 path。删除 `_find_existing` / `_new_path` / `_occupied` / `_meta_matches` / hash 后缀改名。
- W0-2 claim id：`card:<相对卡根 path>#line`、`soul:SOUL.md#line`；`claims.unique` 与 `apply_ceiling` 拒绝重复 id；adviser 的两处池装配调用断言。
- W0-3 schema：`claim_coverage` 只写逐 claim 结果，target coverage 留在 `coverage` 列；迁移库与新库表形状一致（新增用例）；插件版本升到 0.18.0（schema 仍是 5，形状未变）。
- 未验证：live DB 迁移与 legacy marker 清理（需要 live 副本 + 备份，尚未执行）。
- 已知取舍：slug 冲突现在落到同一 path（删除了 hash 后缀改名），`topic` 的 slug 必须等于文件名 stem，否则写入 failed。

实施记录（W1，2026-10-25）：

- W1-1：B4 prompt 的 unit 契约含 `path` 与解析顺序（上一版 marker → search 命中 → 规范 path），PROMPT_VERSION 4.0.0；`create` 仍必须给 path，无三元组回退。
- W1-2：新增 `knowledge.run_value_markers`，窗口队列（`KnowledgePipeline._consume`）、session_extraction、remember 队列都走它；extraction 不再直接调用 `write_unit`。
- W1-3：`minir_kb_search` 描述改为相对 path，默认 limit 8（`DEFAULT_SEARCH_LIMIT`）。
- W1-4：`_failed` 含 op/path/sha256/lines/error 与原始 unit；入队沿用内容哈希去重（同 path 同内容不再写）。
- W1-5：B4 重试同时返回 markers 与 page_writes；window key 用 6 位小数；失败行按窗口范围关闭（`resolve_layer_failures_through`）；gaps 块显示起止时间。
- W1-6：写入前做 org 形状校验：events 需日期、对象、动作+结果；projects 需 3 行（目标/交付物/状态）。这是结构校验，不是语义证明。
- W1-7：新增/更新用例见本轮测试。
实施记录（W2 部分，2026-10-25）：

- W2-1：`_search_as_of` 只读 `conclusion_versions`，不再 JOIN 活表；`status='deleted'` 与归档文件里的版本都能按时刻召回（`_archive_files` 扫描同库旁的 `conclusion/archive/`）。
- W2-2（页来源）：`knowledge.page_edges` / `derived_edges` 从 front matter 派生边（objects → instance_of，relations 按词表）；`store.upsert_page_edges` / `rebuild_page_edges` 幂等；`scripts/rebuild_edges.py` 按文件重建；写页回调 `on_written` 即时入边。**base 来源未实现**：C §4.4 只写了"base 发布时已有的 subject/topic 结构"，没有给关系映射，不在缺少规范时自造语义。
- W2-3：`store.search` 改为 RRF（`1/(60+lexical) + 0.5/(60+expand) + 0.7/(60+vector)`，缺路为 0），向量项按 id 合并、向量独有的行也进入结果；`VectorIndex.index_async` 在结论写入后异步入队（store 的 `on_conclusion_saved` 钩子）；缺 key 只 warning 一次，`stats()` 报 `mode=bm25/status=no_key`。
- W2-4：`ensure_table` 不再 DROP 任何表；dim/model 变化只写 `pending_*` 与 `migration_cursor`，检索在迁移完成前退回 bm25；`migrate()` 按 `conclusions.id` 游标分批重嵌到新表，完成后改写 `table/dim/model/provider` 并删除旧表；中断可从游标续跑。
- W2-5：`conclusion_history_maintenance` 不再依赖 reasoning 注册；`scheduler_runs` 单独按 90 天清理（版本仍 180 天）；合并候选纳入知识页 topic（页候选只写 advisory，从不自动合并）；悬空边清理沿用维护任务。
- W2-6：`remember_mode=knowledge` 缺分类时，仅"编号步骤"内容回落 METHOD，其它内容报错；显式非法分类一律报错；重复 remember 落同一 path，第二次为 unchanged。
- W2-7（38/38）：新增 as-of 三点边界与跨归档、todo 无版本行、v4→v5 每条 eligible 一条开区间、link 缺目标拒绝与 upsert、merge meta、unmerge 恢复/幂等/缺 reason、别名链与成环、每 task 200 行、expand 扇出上限、折叠 179/181 天与幂等、归档 `original_id` 冲突不删热行、向量 401 降级、base_url/api_key 可替换、768 维拒绝、维度迁移、kb `_archive/` 排除。
  收口：多归档 `keep=1` 按 C §4.9 改为只挂最新一档；note 异步补了端到端用例（`test_session_extraction_writes_a_page_and_leaves_the_note_row_alone`）；search golden 以改动后首版基线落地（`test_default_search_matches_the_first_golden`），用例里写明它锁的是"此后不再漂移"，不能证明与改动前逐条一致。
- as-of 归档口径已与 C §4.9 对齐：只挂最新一档，更早的时间点返回空。

实施记录（W3，2026-10-25）：

- W3-1：`history.load_identity` 返回 `(meta, body)`；`worker._identity_prompt_block` 按 `source` 决定 "Operator-authored (trusted)" 或 "Prior review (review rank)"，base、dialectic、`_refresh_identity_only` 三处共用；`review_identity`/`review_preferences` 进 `prompting.RESERVED_LABELS`。
- W3-2：`card_review.build_prompt` 用 `read_card` 的正文，front matter 只作元数据；用例断言 prompt 不含 `source:/reviewer:/evidence:`，`render_card` 不产生双层 front matter。
- W3-3：`ensure_anchor` 只写 `started_at/attempts/next_attempt_at`，无 `started_at` 的旧锚点直接重开，首轮在 24h 内到期；成功后才按 period 推进；失败只写 `next_attempt_at`。
- W3-4：`layers.validate_review` 与 worker 的审计修正都改用 `validate_nonempty`；`ai-self.md` v4 删除"Doing 为空写 idle"，改为 `unknown`。
- W3-5：`ai-preferences` 接到 base:B2 与 dialectic（card_review 原本已接）；manifest 仍只作部署事实；规则文件无重复检查条目。
- W3-6：已补 dialectic 标签、front matter、due 周期、heading-only 拒绝、ai-preferences 三处接线、旧锚点用例；仍缺"双卡皆空"的端到端用例与 14 条清单的逐条编号。

实施记录（W4，2026-10-25）：

- W4-1：`_DEFAULTS` 改为 `claims="on"`、`claim_gate=True`；`active_claims` 恒为列表（claims=off 时传 None 保持旧行为，不再按"选不出 claim"静默关闭过滤）；空池下引用未选 claim 的 note 记 `unselected_claim` 并丢弃；card 与 knowledge claim 上限 concern，未声称的 `user_preferences_check` 也夹到 concern。
- W4-2（部分）：`card_claims` 支持 base 行回退（缺卡时用 `user_representation`/`user_peer_card`/`ai_identity_card`/`ai_self_representation`，id 为 `#base:<n>`）；`###` 标题参与 scope；池按 path id、`unique()` 冲突报错、选择上限 3。**身份行谓词未做**：A 稿没有给出身份行的确定性谓词，`matches()` 里的五个谓词都不覆盖它，不凭空发明。
- W4-3：gate 只认 operator claim（card 与 base 回退行只建议不拦截，无谓词的 claim 不设门）；命中时同步写 advisories（`target=claim_gate`、severity blocker、outcome blocked）再返回拒因；新增 `late_blocker()` 在工具已执行后补记 `target=late_blocker`，由 `on_post_tool_call` 在成功路径调用。
- W4-4：review 规则要求对每个列出的 claim 给出 `clean/found/n/a`（键必须精确 id，未测即 n/a）；`claim_coverage` 只存 claim 结果、target coverage 留在原列；`claim_stats` 按 `source='adviser_mode'` 的 `claim_id/outcome` 统计 cited/blocked/repeated，card_review 的 30 天视图直接读到。
- W4-5：删掉 `mark_claim_repeated` 改旧行的做法，新增 `store.repeat_claim`：重复出现写独立行（outcome=repeated、status=dropped、保留 claim_id 与被压制文本），不改历史行；重复率分母 = cited。
- W4-6：补了 shadow 选择与 on 注入的差异、`late_blocker` 计入 blocked、逐 claim coverage 规则与统计。A 稿没有 38 条测试清单（§4 是基线→目标的指标表），该编号要求无来源，按行为项覆盖。

剩余（全部有明确原因，不再有未写的代码项）：

- W4-2 身份行谓词：这是 A 稿自列的限制（`doc/adviser-brain-to-hand-draft.md:359`「身份行没有可判定谓词，`_infer_test` 对描述行返回空，身份 claim 只能靠 scope 或词面重合被选中」）。本轮已修掉同一行的另一半（`###` 现在参与 scope）；谓词部分没有规范来源，按 A 稿原样记为已知限制，不自造。
- W2-7 两条：search golden 无基线文件；note 异步只是结构性保证。
- W3-6：14 条清单未逐条编号；"双卡皆空"由稀疏 base 用例间接覆盖。
- W4-6 的"38 条编号"：A 稿 §4 是指标总表，不存在 38 条用例清单，无法逐条映射。

作用：把四份草稿的实现缺口按依赖关系合并成一份可执行的收口清单。
源稿：

- `doc/marker-schema-and-page-writes-draft.md`（下称 M）
- `doc/conclusion-history-and-edges-draft.md`（下称 C）
- `doc/global-cards-convergence-draft.md`（下称 G）
- `doc/adviser-brain-to-hand-draft.md`（下称 A）

## 1. 判定与原则

1. 四稿没有被严格实现。当前代码以“向后兼容仍是默认”和“未决项最小改动”为主，本 TODO 按四稿的功能预期重做，不再按兼容路径收口。
2. 目标态是四稿的最终形态，不是又一轮灰度：claims 与 claim_gate 默认生效，向量默认生效，维护任务无条件注册，页面只有一个写者，校验按完整契约执行。
3. 不保留以兼容为目的的分支：不双读、不双写、不保留旧列语义、不保留三元组身份、不保留 basename claim id、不保留“选不出 claim 就回到旧规则”的旁路。
4. 数据可以一次性归一，行为不做兼容。旧 marker 行、旧抽取载荷、旧覆盖列内容按 M 的清理脚本处理；处理前备份。
5. 每条验收都必须能失败。没有“部署后观察”作为验收项。

## 2. 绑定契约

以下两条是本 TODO 的硬约束，优先于四稿中任何冲突文字。

### 2.1 页面身份：相对 path

- 页面身份是 `knowledge/` 根下的相对路径，POSIX 分隔符，带 `.md`，不以 `/` 开头，不含 `..`、反斜杠、盘符。
- 例：`tools/KNOWLEDGE-duckpgq.md`、`projects/INTENT-cert-lifecycle.md`。
- `path` 是唯一身份。合并、创建、编辑、搜索、失败记录、`page_writes`、边端点、claim id 都用它。每个进入合并或写页的 unit 必须带 path；缺失即 failed。新页的 path 由 B4 按固定顺序解析（上一版 marker → `minir_kb_search` 命中 → 规范 path），不由工具按三元组猜。
- `(category, org, topic)` 只是 front matter 元数据：用于索引、检索、渲染与边声明，不参与查找、合并、去重、创建、编辑。
- 所有文件操作解析为 `knowledge_root / path`；越界路径直接拒绝并记 failed。
- 旧页一次性归一：path 由文件当前位置得出；缺失的元数据按 path 补齐；三元组与 path 不一致不改变身份，只记 warning，不建立第二身份。
- 同一 path 的不同三元组是同一页，写页时更新元数据；不同 path 的相同三元组是两页，不合并。

统一清单（全部按相对 path，不允许旁路）：

- `layers._merge_units` / `coerce_value_markers`：合并键、失败记录。
- `knowledge.write_unit` / `_find_existing` / `_page_target` / `edit_page` / `create_page` / `_write_failure` / 入队 `_needs_write`。
- `kb_write`：op 参数与返回 path。
- `tools`：`minir_kb_search` / `minir_kb_write` 的 schema 文案。
- `worker`：B4 marker、`page_writes`、重试路径。
- `store`：edges 页端点、search 返回。
- `claims`：知识页 claim id。
- `__init__`：抽取写页与 prefetch 的 path 展示。

### 2.2 claim id：文件相对路径

- 文件来源的 claim id 统一为 `<kind>:<relative path>#<line>`：
  - `card:user/preference.md#12`
  - `card:agents/default/preference.md#8`
  - `knowledge:tools/KNOWLEDGE-duckpgq.md#5`
  - `soul:SOUL.md#31`（SOUL 的相对路径按 HERMES_HOME 计算）
- 禁止只用 basename。`user/preference.md` 与 `agents/default/preference.md` 必须产生不同 id。
- 卡文件缺失、退回 base 行时，id 仍用该卡应有的相对路径：`card:user/preference.md#base:3`，并在来源字段记 `source: base`。
- 选择器池装配时断言 id 唯一；重复即报错，不做后者覆盖。
- `claim_stats`、重复抑制、`claim_coverage`、`card_review` 的 top claims 全部按该 id 聚合。

### 2.3 目标默认值

| 开关/行为 | 目标值 | 说明 |
| --- | --- | --- |
| `adviser_mode.claims` | `on` | `off` 只作故障熔断，不作默认与验收路径 |
| `adviser_mode.claim_gate` | `true` | 只拦 operator claim，不调模型 |
| 向量检索 | 开启 | 缺 key 时 warning + 纯 bm25，不静默 |
| 维护任务 | 无条件注册 | folding、archive、边清理、scheduler 裁剪、vector 回填 |
| 页写者 | `minir_kb_write` 唯一 | 抽取与 B4 共用 `run_value_markers`，不直接 `write_unit` |
| 卡片 due | 四文件任一缺失即到期 | `ensure_anchor` 不写 `last_success_at` |
| 页面身份 | 相对 path | 三元组仅元数据 |
| claim id | 相对 path | 不允许 basename |

## 3. 四稿关系与波次

| 稿 | 提供 | 被谁消费 | 前置 |
| --- | --- | --- | --- |
| M | 相对 path 身份、marker v5、唯一写页路径、`layer_failures`、stale 块、`_failed` | C 的页边端点与结论来源；G 的 stale 信号；A 的知识页 claim | W0 |
| C | 版本与 as-of、edges、向量、scheduler_runs、维护任务 | G 的失败表与 gaps 块；A 的检索与 as-of | W1 的 path |
| G | 四张卡、来源标签、due、摘要校验、ai 规则 | A 的卡片池与 authority | W1 的元数据、W2 的 `layer_failures` |
| A | claim 选择、gate、coverage、stats、late_blocker | 无 | W1 的 path id、W3 的卡文件与标签 |

波次：

1. W0 契约与基座：path 身份、claim id、最终 schema、迁移与清理。无行为开关。
2. W1 页面与 marker（M）：B4 契约、唯一写者、path 操作、完整校验、失败可见。
3. W2 历史与检索（C）：as-of、边派生、异步向量、维度迁移、维护与保留期。
4. W3 卡片（G）：来源标签、front matter、due、摘要校验、规则接线、R1/R2。
5. W4 claim（A）：默认生效、选择器池、gate、coverage、重复抑制、度量与测试。

W1 与 W2 都只依赖 W0；W2 的边端点需要 W1 的 path 归一，故按顺序执行。G 可以吸收 W2 的 `layer_failures`，A 必须等 G 的卡文件与标签落地。

## 4. TODO

### W0 契约与基座

#### W0-1 页面身份统一为相对 path

- 现状：`_merge_units` 用 `path or (category, org, topic)` 兜底（layers.py:306-312）；`_find_existing`/`write_unit` 按三元组找页（knowledge.py:365-392、558）；B4 的 UNITS 没有 path 字段。
- 目标：path 必填且为相对路径；合并键只有 path；缺 path 或非法 path 记 failed；所有页面 API 接受并返回相对 path；三元组只写元数据。
- 落点：`plugin/minir/layers.py`、`plugin/minir/knowledge.py`、`plugin/minir/kb_write.py`、`plugin/minir/tools.py`、`plugin/minir/__init__.py`、`plugin/minir/store.py`（页边端点）、`plugin/minir/agents/b4_value_markers.md`。
- 验收：同三元组不同 path 得到两页；同 path 不同三元组得到一页且元数据更新；绝对路径、`..`、反斜杠被拒；`minir_kb_search` 返回相对 path；`edges` 的页端点 key 是相对 path。
- 依赖：无。

#### W0-2 claim id 使用文件相对路径

- 现状：`card:{path.name}#{line}`（claims.py:102），用户卡与 AI 卡都叫 `preference.md`，id 冲突；`apply_ceiling` 的后写覆盖前写。
- 目标：`<kind>:<relative path>#<line>`；base 回退用 `#base:<n>`；池内唯一断言；stats、抑制、coverage 全按该 id。
- 落点：`plugin/minir/claims.py`、`plugin/minir/adviser_mode.py`、`plugin/minir/store.py`、`plugin/minir/card_review.py`。
- 验收：两个 `preference.md` 生成不同 id；`claim_stats` 分卡计数；同会话重复抑制只影响对应卡。
- 依赖：W0-1。

#### W0-3 最终 schema、迁移与一次性清理

- 现状：已提交 v5，但 live 仍 v4；`claim_coverage` 存的是 target coverage 与 claim 的合并字典；edges 页端点没有 path；legacy marker 行与抽取载荷仍在。
- 目标：一次成型的最终 schema：`conclusion_versions`、`edges`（页 key=path）、`scheduler_runs`、`layer_failures`、`selected_claims`、`claim_coverage`（只放逐 claim）、`claim_id`、`outcome`。删除兼容列语义；清理 legacy marker 行、制品数组、抽取载荷；重置 `layer_state("card_review")` 的旧锚点；`plugin.yaml` 与 `pyproject.toml` 升到与 schema 一致的版本。
- 落点：`plugin/minir/store.py`、`scripts/drop_legacy_markers.py`、`plugin/minir/__init__.py`（anchor 初始化）、`plugin/minir/plugin.yaml`、`pyproject.toml`。
- 验收：新库与迁移库 schema 完全一致；legacy marker 行 0；`claim_coverage` 不含 target 键；`card_review` 旧锚点清除且 state 不再含 `last_success_at`；插件版本与 schema 版本对应。首轮 review 的时机由 W3-3 与 W3-5 决定。
- 依赖：W0-1、W0-2。

### W1 页面与 marker（M）

#### W1-1 B4 契约：先取 path，再写页

- 现状：B4 只按 topic 搜页，单元没有 path；写入后没有把 path 回填到 unit 或下一版 marker。
- 目标：unit 增加 `path`（相对）。B4 解析顺序：上一版 marker 的 path → `minir_kb_search` 命中的 path → 按规范规则生成的 path；三者都拿不到就 failed。create 也必须带 path；`page_writes` 与下一版 previous markers 都带 path。写入后不再允许按三元组猜页。
- 落点：`plugin/minir/agents/b4_value_markers.md`、`plugin/minir/layers.py`、`plugin/minir/worker.py`。
- 验收：同一单元第二次运行走 edit 而不是 create；`page_writes` 的 path 与磁盘文件一一对应；没有 path 的 unit 被拒绝。
- 依赖：W0-1。

#### W1-2 唯一写者与共享 `run_value_markers`

- 现状：`session_extraction` 默认开启并直接 `knowledge.write_unit`（__init__.py:1426-1436）；`run_value_markers` 不存在；note 抽取仍走旧写页路径。
- 目标：抽出 `run_value_markers(session_id, subject_ids, *, source)`，窗口级、note、B4 队列共用；方法内不 commit，带 `source+session+path` 幂等键；`minir_kb_write` 只由该路径与 B4 调用；`session_extraction` 不再直接写页。
- 落点：`plugin/minir/worker.py`、`plugin/minir/__init__.py`、`plugin/minir/knowledge.py`、`plugin/minir/kb_write.py`。
- 验收：同一窗口重复运行不产生第二张页；抽取与 B4 写同一 path 时只有一条 revision；关闭 B4 时抽取仍能按同一路径写页。
- 依赖：W1-1。

#### W1-3 `minir_kb_write` 与 `minir_kb_search` 的 path 契约

- 现状：create 按请求 path 判断存在、不看同三元组的页；edit 要求 slug 与 topic 匹配；`minir_kb_search` 的注册文案仍写 absolute path、default 8。
- 目标：create 必须给 path；edit/append/replace/delete 只认相对 path；同三元组的其他 path 不参与；提供 `resolve_path(org, category, topic)` 纯函数供 B4 生成规范 path，工具不在写页时按三元组猜；search 返回 file/relative path/sha256/lines/content，默认 5；工具 schema 与实现一致。
- 落点：`plugin/minir/knowledge.py`、`plugin/minir/tools.py`、`plugin/minir/kb_write.py`。
- 验收：同三元组不同 path 可各自编辑；edit 传入不存在的 path 返回 failed；search 的返回字段与 schema 文案一致；B4 不再二次读文件。
- 依赖：W0-1。

#### W1-4 完整写入校验与失败重试

- 现状：核对只比正文与 expected、revisions+1；不验 front-matter 可解析与元数据/path 一致；`_failed` 缺 sha256/lines；入队不读 `_failed`。
- 目标：写入后核对 front-matter 可解析、`(category, org, topic)` 与 path 一致、append/replace/delete 的意图结果；不符回滚并返回 unverified；`_failed` 记 op/path/sha256/lines/error；入队按 path+sha 去重，坏单元不无限重试。
- 落点：`plugin/minir/knowledge.py`、`plugin/minir/layers.py`。
- 验收：坏 front matter 被回滚且进 `_failed`；同一失败单元下一窗口不重复入队；`_failed` 能定位到具体版本。
- 依赖：W1-3。

#### W1-5 `page_writes`、window key 与 gaps 块

- 现状：`page_writes` 只记第一遍 base 结果，重试路径丢弃；`window_key` 截到秒；`_resolve_base_failure` 只关闭当前 key。
- 目标：每次写页（含重试）都进 `page_writes`；window key 用完整 `from_ts/to_ts`；失败行按窗口范围关闭；gaps 块只显示未解决且未被后续成功窗口覆盖的行。
- 落点：`plugin/minir/worker.py`、`plugin/minir/__init__.py`、`plugin/minir/store.py`。
- 验收：重试中的 failed/unverified 出现在 `page_writes`；同秒两个窗口不互相覆盖；成功窗口关闭对应失败行。
- 依赖：W1-2、W0-3。

#### W1-6 org 策略与 projects 三要素

- 现状：代码只做枚举校验（knowledge._identity），org 由调用方或模型自由选择；projects 三要素、events 时间形状都没有校验。
- 目标：按 M §1.4 以内容形状定 org；projects 必须具备目标、交付物、现状三要素；events 缺时间锚点不得归 events，按规则表回落；`rules/markers-org.md` 是唯一权威，B4 prompt 与写页校验引用同一版本；不合判据的单元返回 failed 并进 `_failed`。
- 落点：`plugin/minir/agents/b4_value_markers.md`、`plugin/minir/rules/markers-org.md`、`plugin/minir/knowledge.py`、`plugin/minir/layers.py`。
- 验收：缺三要素的 projects 单元 failed；无时间锚点的 events 单元被拒或回落；合法单元落到正确 org；规则版本与 prompt 一致。
- 依赖：W1-1、W1-3。

#### W1-7 M 稿测试

- 现状：path 身份、缺 path failed、单写者、search schema、重试 `page_writes`、org 策略都没有专门用例。
- 目标：M §1-§4 每条契约至少一条正例与一条负例；覆盖同三元组不同 path、同 path 不同三元组、非法 path、失败记录字段、重复入队、重试写页、窗口关闭、org 判据。
- 落点：`tests/test_layers.py`、`tests/test_knowledge.py`、`tests/test_worker.py`、`tests/test_store.py`。
- 验收：用例能失败；全量 `pytest` 通过。
- 依赖：W1-1 至 W1-6。

### W2 历史与检索（C）

#### W2-1 as-of 覆盖删除与归档

- 现状：`_search_as_of` JOIN 活表 `conclusions`；`forget_conclusion` 物理 DELETE；`version_at` 没有生产调用者。
- 目标：as-of 从 `conclusion_versions` 读，包含 `status='deleted'` 与归档版本；`search(as_of=...)` 不再依赖活表；`version_at` 成为统一入口。
- 落点：`plugin/minir/store.py`、`plugin/minir/__init__.py`。
- 验收：forget 后按删除前时刻可检索到该行；180 天后归档版本仍可 as-of；`include_superseded` 有独立用例。
- 依赖：W0-3。

#### W2-2 edges 的离线派生与重建

- 现状：只有人工 `minir_link` 与 `merge_into` 的 `same_as`；base 的 subject/topic、知识页 front matter 两类来源没有代码。
- 目标：按 C §4.4 三类来源派生；页端点 `('page', relative path)`；写入幂等 upsert；提供从文件重建 edges 的命令；扩展前过滤悬空端点。
- 落点：`plugin/minir/store.py`、`plugin/minir/worker.py`、`plugin/minir/knowledge.py`（页元数据读取）、`plugin/minir/extraction.py`。
- 验收：新库跑一次发布后 edges 非空；删表可按文件重建；悬空边不进入扩展结果；页 path 改名后边跟随。
- 依赖：W1-1。

#### W2-3 向量：异步索引、显式降级、RRF

- 现状：写入路径不索引，只靠每小时 50 条回填；无 key 静默返回 None；向量项只是加到词面分数上。
- 目标：结论提交后异步入队索引；缺 key 或 provider 失败写 warning 并明确走 bm25；召回按 C §4.5 的 RRF 公式（bm25 rank + expand rank + vec rank），缺一路为 0。
- 落点：`plugin/minir/vector.py`、`plugin/minir/__init__.py`、`plugin/minir/store.py`。
- 验收：新结论在一个刷新周期内可向量召回；无 key 时日志有 warning；融合分数与公式逐项一致；`sqlite_vec` 不可用时有明确状态。
- 依赖：W0-3。

#### W2-4 维度迁移任务

- 现状：`ensure_table` 内维度不符直接 DROP 重建，发生在启动路径。
- 目标：新增 `vector_dim_migration`，按 `conclusions.id` 游标分批重嵌，写 progress_id，完成后写 dim/model/provider；不在启动路径阻塞。
- 落点：`plugin/minir/vector.py`、`plugin/minir/__init__.py`。
- 验收：换模型后旧向量保留到迁移完成；中断可从游标续跑；启动不被 DROP 阻塞。
- 依赖：W2-3。

#### W2-5 维护任务、保留期与合并候选

- 现状：维护任务只在 `_reasoning` 时注册；`scheduler_runs` 复用 180 天；合并候选只比较 `conclusions.subject`。
- 目标：维护任务无条件注册；`scheduler_runs` 90 天与每 task 200 行；折叠 180 天/32 版、归档、悬空边清理按 C §4.9；合并候选纳入知识页 topic，三档阈值与 C §4.7 一致。
- 落点：`plugin/minir/__init__.py`、`plugin/minir/store.py`。
- 验收：reasoning 关闭时折叠仍执行；90 天前的 run 被清；页 topic 参与候选；unmerge 恢复别名与 original_id。
- 依赖：W2-2。

#### W2-6 `remember_mode=knowledge` 的分类与重试

- 现状：分类缺失直接报错；`remember_mode=knowledge` 只入队，B4 失败后没有共享重试。
- 目标：分类缺失按 C §4.8 回落 METHOD 并交给 B4 重试；入队与 B4 共用 W1-2 的写页路径；幂等键防重复。
- 落点：`plugin/minir/__init__.py`、`plugin/minir/worker.py`、`plugin/minir/knowledge.py`。
- 验收：无分类的 knowledge remember 入队一次并成功写页；重复 remember 不产生第二页；失败可在下一窗口重试。
- 依赖：W1-2、W2-5。

#### W2-7 C 稿测试

- 现状：C §6 的 38 条用例只有约 9 条有直接断言；`include_superseded`、`original_id`、`prev_status` 在测试里没有命中。
- 目标：按 §6 补全，至少覆盖迁移 4→5、as-of（含删除与归档）、边派生与悬空、向量降级与 RRF、维度迁移、维护保留期、合并与别名链、unmerge 幂等。
- 落点：`tests/test_store.py`、`tests/test_vector.py`、`tests/test_provider.py`。
- 验收：38 条逐条映射到测试；缺一条不算完成。
- 依赖：W2-1 至 W2-6。

### W3 卡片（G）

#### W3-1 来源标签与三处调用

- 现状：`_base_ai_prompt` 改了标签，但 `_dialectic_prompt` 仍写 `Operator-authored ... (trusted)`，且传入 review 播种卡；`load_identity` 返回 `(body, body)`。
- 目标：`load_identity` 返回 `(meta, body)`；base、dialectic、`_refresh_identity_only` 三处都用 source 决定标签；`source: review` 的卡只作 prior review，不作 operator。
- 落点：`plugin/minir/history.py`、`plugin/minir/worker.py`、`plugin/minir/card_review.py`。
- 验收：dialectic 的 review 卡标签不是 operator；三处 prompt 的段名一致；`source: operator_edit` 仍是 operator。
- 依赖：W0-3。

#### W3-2 front matter 不进入身份输入

- 现状：`card_review.build_prompt` 把整份文件（含 front matter）拼进 `Current ... card`；只有 history 与 read_card 剥头。
- 目标：review、base、dialectic 的身份输入都只传正文；front matter 只用于来源判断与元数据。
- 落点：`plugin/minir/card_review.py`、`plugin/minir/history.py`、`plugin/minir/worker.py`。
- 验收：输入里不出现 `source:`、`reviewer:`、`evidence:`；渲染回写不产生双层 front matter。
- 依赖：W3-1。

#### W3-3 due、anchor 与首轮 review

- 现状：`ensure_anchor` 对存量 state 保留 `last_success_at`；`due` 只查 `is_file()`。
- 目标：`ensure_anchor` 只写 `started_at/attempts/next_attempt_at`；四文件任一缺失即 due；成功复核后才启动三天周期；W0-3 清除旧锚点，W3-5 的规则落地前用 `next_attempt_at` 把首轮 review 挡在规则之后，规则落地后 24h 内跑首轮。
- 落点：`plugin/minir/card_review.py`、`plugin/minir/__init__.py`。
- 验收：四文件缺失时立即 due；成功一次后三天内不 due；失败不推进周期；规则落地后 24h 内写出 `source=review` 的 revision。
- 依赖：W0-3。

#### W3-4 摘要校验与 ai-self 规则

- 现状：空摘要校验只在 base；dialectic 用 `summary_template.validate`；ai-self 规则同时写 idle 与 never idle。
- 目标：base、dialectic、review 写回都走 `validate_nonempty`；ai-self 规则删除“Doing 为空写 idle”，改为无证据写 unknown；空摘要不产生 idle。
- 落点：`plugin/minir/layers.py`、`plugin/minir/worker.py`、`plugin/minir/rules/ai-self.md`。
- 验收：heading-only 摘要经 dialectic 也被拒；空 Doing 输出 unknown 不是 idle；对应测试存在。
- 依赖：W3-1。

#### W3-5 规则接线、manifest 与 R1/R2

- 现状：ai-preferences 只被 card_review 读取；manifest 仍标 trusted；首轮 review 仍按旧锚点 2026-10-26 触发。
- 目标：ai-preferences 同时接到 base:B2、dialectic、card_review；manifest 限定为部署事实，`## Deployment` 为反例；规则落地后再允许首轮 review；B6 的两条检查各写一条，不重复计数。
- 落点：`plugin/minir/rules/ai-preferences.md`、`plugin/minir/rules/ai-identity.md`、`plugin/minir/rules/user-self.md`、`plugin/minir/worker.py`、`plugin/minir/card_review.py`。
- 验收：三处 system prompt 都含 ai-preferences；review 输出不含 manifest 段；R1 的 review 卡标签在代码层成立；B6 的检查条目数与草稿一致。
- 依赖：W3-3、W3-4。

#### W3-6 G 稿测试

- 现状：14 条计划只落 4 条；dialectic 标签、front matter、due 三天周期、双卡为空、unknown/idle、失败窗口保留证据没有专门用例。
- 目标：补齐 14 条；G §6 的五条验收各自绑定测试或 DB 断言；播种卡按 prior card 的回归必须存在。
- 落点：`tests/test_card_review.py`、`tests/test_worker.py`、`tests/test_provider.py`、`tests/test_layers.py`。
- 验收：14 条用例存在且能失败；§6 五条逐条可复核。
- 依赖：W3-1 至 W3-5。

### W4 claim（A）

#### W4-1 默认生效与无 claim 路径

- 现状：`claims=off`、`claim_gate=false`；`active or None` 在选不出 claim 时关闭过滤；card/knowledge 上限只在非空选择集里生效。
- 目标：`claims=on`、`claim_gate=true`；`active_claims` 始终为列表；没有 claim 的新 target 建议按 A §2.4 返回空；`user_preferences_check` 无 claim 仍允许但夹到 concern；card/knowledge 一律按 authority 夹紧。
- 落点：`plugin/minir/adviser_mode.py`、`plugin/minir/claims.py`。
- 验收：空选择集下 knowledge claim 不能成为 blocker；card claim 最高 concern；prefs 无 claim 仍可出建议但不超过 concern。
- 依赖：W0-2。

#### W4-2 选择器池、身份与 base 回退

- 现状：`card_claims` 缺 base 行回退；身份行没有可判定谓词，`###` 标题不更新；claim id 用 basename。
- 目标：池 = SOUL + 四卡（path id）+ 缺失卡的 base 行回退 + 知识页（path id）；身份行有 section/谓词，`###` 标题参与 scope；选择器最多 3 条、按 A §P0/§P1 的评分。
- 落点：`plugin/minir/claims.py`、`plugin/minir/adviser_mode.py`。
- 验收：卡缺失时 base 行仍可选；身份行能被选中；`###` 下的行 scope 正确；id 冲突报错。
- 依赖：W3-2、W0-2。

#### W4-3 gate 与 late_blocker

- 现状：gate 不是 claim 驱动；`draft_write` 只认参数文本；拦截不写 advisories/outcome；`late_blocker` 不存在。
- 目标：gate 用用户回合 + tool + args 判定；命中 operator claim 直接 block，不调模型；block 写 advisories/outcome；判断类 claim 命中后强制一次评审；回合末送达的动作类 blocker 记 `late_blocker`。
- 落点：`plugin/minir/adviser_mode.py`、`plugin/minir/claims.py`、`plugin/minir/store.py`、`plugin/minir/__init__.py`。
- 验收：口头草稿要求能拦 write_file（无需模型）；非变更工具不拦；每次 block 可在 `claim_stats` 看到；late_blocker 有计数口径。
- 依赖：W4-1、W4-2。

#### W4-4 coverage 与统计

- 现状：review 不要求逐 claim coverage；写入端把 target coverage 与 claim coverage 合成一个字典；gate 拦截不计数。
- 目标：review 输出逐 claim coverage；`claim_coverage` 列只放 claim 结果；target coverage 留在原列；`claim_stats` 统计 cited/blocked/repeated，只统计 `source='adviser'`。
- 落点：`plugin/minir/adviser_mode.py`、`plugin/minir/rules/advice_target.md`、`plugin/minir/store.py`、`plugin/minir/card_review.py`。
- 验收：coverage 比例来自 reviewer 的逐 claim 结论；旧列不被污染；blocked 能被 stats 与 card_review 看到。
- 依赖：W4-3。

#### W4-5 重复抑制与分母

- 现状：重复抑制改写上一条 advisory 的 outcome，新建议不落行；重复率没有独立分母。
- 目标：重复出现写一条 `outcome='repeated'` 的 advisory（或等价事件行），保留被压制建议的 id 与 claim id；stats 用事件行计算重复率；card_review 能看到 repeated 改写候选。
- 落点：`plugin/minir/adviser_mode.py`、`plugin/minir/store.py`、`plugin/minir/card_review.py`。
- 验收：同一 claim 第二次出现有独立行；重复率分母可计算；被 guard 丢弃的建议不会误标历史行。
- 依赖：W4-4。

#### W4-6 A 稿测试与验收

- 现状：只有 4 条 claims 测试；A §4 的验收没有数据路径。
- 目标：按 A 稿的测试清单补齐：selector、shadow/on、ceiling、gate、coverage、stats、late_blocker、重复抑制、身份入队；验收表逐行绑定测试或 DB 断言。
- 落点：`tests/test_claims.py`、`tests/test_adviser.py`、`tests/test_adviser_mode.py`、`tests/test_store.py`。
- 验收：A §4 每一行都有对应测试或可执行断言；全量 pytest 通过。
- 依赖：W4-1 至 W4-5。

## 5. 删除清单（兼容分支）

批准后删除，不保留双路径：

- `_merge_units` 的三元组兜底（layers.py:306-312）与缺身份字段的静默 `continue`（310-311）。
- `write_unit`/`_find_existing` 的三元组查找（knowledge.py:365-392、558）与 hash 后缀改名分支（404-417）。
- `session_extraction` 直接 `knowledge.write_unit`（__init__.py:1426-1436）。
- `active or None` 的过滤旁路（adviser_mode.py:1892-1895）。
- `card:{path.name}#{line}`（claims.py:102）。
- `mark_claim_repeated` 改写旧行的实现（store.py:2424-2438）。
- `claim_coverage` 的合并字典写入（adviser_mode.py:1900-1904、store.py:2615-2628）。
- `vector.embed` 无 key 的静默 `return None`（vector.py:120-122）。
- `ensure_table` 维度不符的启动期 DROP（vector.py:80-81、100-108）。
- 向量维护任务挂在 `_reasoning` 下的注册（__init__.py:476-480）。
- `ensure_anchor` 保留 `last_success_at` 的存量分支（card_review.py:542-549）。
- dialectic 的 `Operator-authored ... (trusted)` 标签（worker.py:562-567）。
- `summary_template.validate` 作为 dialectic 摘要的唯一校验（layers.py:350）。
- `scheduler_runs` 复用 180 天保留期（store.py:1411）。

## 6. 端到端验收

一个会话内完成：

1. 用户口头要求“先出草稿”，模型尝试 `write_file` 到 `plugin/minir/core.py`；claim gate 拦截，`advisories` 有 `claim_id` 与 `outcome=blocked`。
2. B4 写一张页：`page_writes` 带相对 path、sha256、lines；磁盘文件存在；front matter 三元组与 path 一致。
3. 同一窗口再跑一次：edit 命中同一 path，不新建页；`_failed` 不增加。
4. 结论写入后：版本行追加；as-of 可查；向量在一个刷新周期内可召回；edges 从页 front matter 与结论结构派生。
5. 卡片：四文件存在时 due 按 `next_attempt_at`；删掉一张后 due 为 True；review 写 `source=review`，dialectic 标签不是 operator。
6. claim：同一 claim 第二次出现有 repeated 行；coverage 有逐 claim 结果；`claim_stats` 的 cited/blocked/repeated 分母完整。
7. 全量 `pytest` 通过；A/C/M/G 四稿点名的测试全部存在。

| 稿 | 契约/验收 | TODO |
| --- | --- | --- |
| M | 相对 path、唯一写者、完整校验、失败可见、gaps | W0-1、W1-1 至 W1-7 |
| C | 版本与 as-of、边、向量、维度迁移、维护、§6 测试 | W2-1 至 W2-7 |
| G | B1-B6、R1/R2、§6 验收 | W3-1 至 W3-6 |
| A | P0-P4、§4 验收 | W4-1 至 W4-6 |

W0-3 是四稿共用的 schema 与清理基座，不单独计入某一稿。

执行命令（实施时填实际输出）：

```bash
python -m pytest -o addopts="" -q
python -m pytest -o addopts="" -q tests/test_claims.py tests/test_store.py tests/test_knowledge.py tests/test_card_review.py tests/test_worker.py
```

DB 断言（scratch 副本）：

```sql
PRAGMA user_version;
SELECT COUNT(*) FROM conclusion_versions;
SELECT COUNT(*) FROM layer_failures WHERE resolved_at IS NULL;
SELECT claim_id, outcome FROM advisories ORDER BY id DESC LIMIT 10;
```

## 7. 未验证与风险

- 本文件是 TODO 草稿，没有执行任何一项；上面的行号是当前提交 4f382bb 的位置，实施后会漂移。
- 迁移与清理必须在 live DB 的副本上演练；`--apply` 前备份；清理是不可逆的。
- 相对 path 与 claim id 是新增硬契约，四稿中与它们冲突的文字以本文件为准；实施后要回写四稿的状态段，避免文档继续声称“已实现”。
- “不向后兼容”意味着旧的 `claims=off`、三元组身份、旧 coverage 语义不再作为验收路径；只保留草稿明确列出的故障熔断。
- W0-W4 代码项与可映射测试已实施：工作树 32 个文件改动（+2205/-393），758 条测试通过；未提交。唯一未做的 W4-2 身份谓词是 A 稿自列的已知限制（adviser-brain-to-hand-draft.md:359）。
