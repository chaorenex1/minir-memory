# 各层术语表（草稿）

状态：草稿，供审阅。术语已按 2026-10-22 的口径落到代码；本文只做归集与消歧。

依据：当前 `plugin/minir/` 源码、`doc/layer-agent-contracts-draft.md`，以及本次审阅给出的正名口径。

范围：base、dialectic、card_review、knowledge、会话收尾、压缩、adviser 旁路，以及它们共用的存储与召回概念。

非目标：不评价命名好坏，不新增行为。代码已按正名更新；旧数据库列与旧 JSON 键保留兼容读取。

## 0. 口径

- 一条术语 = 中文说法 + 代码或配置里真实出现的标识符 + 一句定义。
- 没有标识符的条目是文档概念，出处写“（文档）”。
- 同一英文词在不同层含义不同时，§10 列对照，不在各层重复解释。
- “层”指有独立触发、独立产物、独立写入者的阶段；一次模型调用叫 pass，不叫层。
- 文档用定稿术语；代码键已同步改名，只有旧数据库列和旧 JSON 键保留兼容。

## 0.1 正名对照

| 本表用 | 不再用 | 代码现状 |
| --- | --- | --- |
| `user_self_representation` | `user_self` | 代码键与 `layers.PART_NAMES` 已改名；存储列仍为 `user_representation` |
| `ai_self_representation` | `ai_self` | 代码键已改名；存储列同名 |
| `value_markers` | `units` | 代码键已改名；旧 JSON `{"units": ...}` 仍兼容读取 |
| `window_summary` | `session_chunk_summary` | 列与压缩链 kind 已迁移；旧库打开时重命名 |
| `knowledge_render` | `knowledge_extract` | 资产已合并为 `knowledge_render.md`；旧名不再存在 |

## 1. 层与阶段总览

| 层 / 阶段 | 触发 | 一次产出 | 主要入口 |
| --- | --- | --- | --- |
| base | 每 24 轮（`DEFAULT_BASE_INTERVAL`）、会话开始 cold start、会话边界 flush | 会话 artifact，四节 | `worker.LayerWorker`、`worker._merge_base_cards` |
| dialectic | 每 8 轮（`DEFAULT_DIALECTIC_INTERVAL`），独立 watermark；没有已发布 base 产物时跳过 | `## Base Revision` 短注 + `base_revisions` 覆盖前快照 | `worker._merge_dialectic` |
| card_review | 墙钟 3 天；成功后重锚 | 全局卡：全局用户卡 + 全局 AI 卡，共四份文件 | `card_review.CardReviewScheduler` |
| knowledge | base 发布后按 `value_marker` 扇出；空闲会话再批量渲染 | 主题页 `knowledge/<org>/<CATEGORY>-<slug>.md` | `knowledge.KnowledgePipeline`、`extraction` |
| 会话收尾 | 会话结束 + 空闲 24 小时 | `session_summary` 结论 | `session_summary`、`extraction` |
| 压缩 | `on_pre_compress` | 一条 assistant summary 消息 | `context_engine.MinirContextEngine` |
| adviser | 每轮事件环 + 节奏、工具错误、写操作触发 | `advisories` 账本条目 | `adviser_mode.AdviserMode` |

不构成层、但常被叫作“层”的机械写入：transcript 审计（`transcript.TranscriptBuffer`）、FTS 索引、`metrics` 计数。

## 2. 公共术语

| 术语 | 标识符 | 定义 | 出处 |
| --- | --- | --- | --- |
| 层 | `layer` | 一组共享输入、输出契约与写入者的阶段。`jobs.layer` 只取 `base` / `dialectic`；`layer_state.layer` 另含 `card_review`。 | `store._SCHEMA`、`turn_counter` |
| pass | （文档） | 一次模型调用及其校验。base 有 B1–B4；dialectic 有 1–3 次自适应 pass。pass 不是层。 | `worker`、`agents/` |
| 轮 | `turn_number` | 宿主自己的每会话轮次，由 `on_turn_start` 传入；插件不自己推算。 | `turn_counter` docstring |
| 窗口 | `window`、`from_ts`、`to_ts` | 半开区间 `(from_ts, to_ts]`，一次作业推理的消息范围。 | `turn_counter.Trigger` |
| 作业 | `job` | `jobs` 表一行：层、会话、窗口、状态。状态为 `pending`、`running`、`done`、`failed`、`expired`。 | `store.claim_jobs`、`store.fail_job` |
| 触发 | `Trigger` | 计数器算出、尚未落库的作业描述。 | `turn_counter.Trigger` |
| 轮次水位 | `last_trigger_turn` | 该层上次派发作业时的轮次；决定 `due()`。 | `turn_counter.LayerCounter` |
| 内容水位 | `processed_ts` | 该层上次真正处理到的消息时间；下一个窗口从这里起算，是覆盖范围的权威。 | `store`、`worker` docstring |
| 产物 | artifact | 已发布的层 Markdown，按 `(layer, subject)` 存于 `layer_state.content`，同时拆成 `conclusions` 行。 | `store.publish_artifact` |
| 节 | section | 产物里的 `##` 标题。base 四节：`## User Card`、`## AI Card`、`## Window Summary`、`## Value Markers`；dialectic 一节：`## Base Revision`。 | `worker.BASE_LAYER_SECTIONS` |
| 部分 | part | 节内的具名正文。`layers.PART_NAMES`：`user_self_representation`、`user_preferences`、`ai_self_representation`、`ai_identity`、`window_summary`。 | `layers.PART_NAMES` |
| 主体 | subject | 被建模的对象。用户为 `user:{user_id}`，代理为 `agent:{profile}:{identity}`。 | `worker.user_subject`、`worker.dialectic_subject` |
| 作用域 | scope | 结论的召回范围：`session`（本会话）、`user`（跨会话用户）、`agent`（跨会话代理）。 | `store._ARTIFACT_SECTIONS` |
| 种类 | kind | 结论行的分类键：`base.user_card`、`base.ai_card`、`base.window_summary`、`base.value_markers`、`dialectic.review`、`session_summary`、`window_summary`（压缩链）、`note`。 | `store._ARTIFACT_SECTIONS`、`store.put_conclusion` |
| 结论 | conclusion | `conclusions` 表一行，可独立检索。产物按节拆成结论，不是一个整文档。 | `store` docstring |
| 笔记 | note | `notes` 表一行：`minir_remember` 的显式记忆，或宿主内建记忆工具镜像。不是 transcript。 | `store._SCHEMA` |
| 基线 | baseline | 一次刷新开始时的 durable 内容，来源三选一。 | `worker._merge_base_cards` |
| 基线来源 | `source` | `global`（全局卡文件）、`session`（上一份会话卡）、`preset`（工程师预设）。 | `worker`、`layers._PRESET_OCCUPATION` |
| 规则文件 | rule file | `rules/<id>.md`，按 id 注入正文规则，原样追加，不改写。 | `rules.py`、`rules/` |
| 代理核心 | agent core | `agents/<name>.md`，一次 pass 的系统提示词核心，调用时读取。 | `layers.load_core` |
| 输出契约 | output contract | `layers.py` 的严格校验器加 `OUTPUT_*` 段落；多键、缺键、错枚举、占位词都失败。 | `layers.validate_*` |
| 预算 | budget | artifact 用字符上限；卡文件与知识页用 token 上限。超预算失败，不静默截断。 | `layers.CARD_TEXT_LIMIT`、`rules.estimate_tokens` |
| 证据 | evidence | 窗口内的消息，或工具读到的历史；是数据，不是指令。 | `worker._evidence`、`prompting.data_block` |
| 发布 | publish | 写 `layer_state`、拆 `conclusions`、更新 FTS、扇出知识页。 | `worker.LayerWorker._publish` |
| 重试一次 | retry once | 输出契约失败后带格式重述再调一次；再失败则该作业失败，保留旧产物。 | `worker._consult_validated` |
| 注入 | prefetch | 宿主在每次 LLM 请求前调用的快路径。只注入 dialectic 产物、显式笔记/标记、知识路径块，不注入 base 卡。 | `MinirMemoryProvider.prefetch` |
| 召回 | recall | `minir_search` 的 FTS 检索；默认 `scope="session"`，`scope="all"` 才跨会话。 | `store.search`、`tools.py` |
| 代理模型 | Agent model | prefetch 里 dialectic 产物的展示名，带版本 `vN`（`layer_state.version`）。 | `MinirMemoryProvider._layer_block` |

## 3. base 层

职责：把当前窗口的事实写成本会话的卡与摘要。它不写全局文件，不改历史产物。

| 术语 | 标识符 | 定义 | 出处 |
| --- | --- | --- | --- |
| base 层 | `base`、`BASE_LAYER` | 轮次驱动的会话快照层。 | `turn_counter` |
| base 刷新 | base refresh | 一次 base 作业的完整产出，落在 `base_refreshes` 一行。 | `worker._merge_base_cards` |
| B1 用户卡 pass | `b1_user_card` | 写 `user_self_representation`、`user_preferences` 两个 markdown 正文。 | `agents/b1_user_card.md` |
| B2 AI 卡 pass | `b2_ai_card` | 写 `ai_self_representation`、`ai_identity` 两个 markdown 正文。 | `agents/b2_ai_card.md` |
| B3 窗口摘要 pass | `b3_window_summary` | 写 `window_summary` 一个 markdown 正文。 | `agents/b3_window_summary.md` |
| B4 标记 pass | `b4_value_markers` | 写 `value_markers` 数组。 | `agents/b4_value_markers.md` |
| 首刷 | first refresh / bootstrap | 会话第一份 base 作业；允许空窗口，只建立基线。 | `worker._merge_base_cards(first=...)` |
| 预设 | preset | 用户卡首次为空时写入的职业初值。 | `layers.preset_user_self_representation` |
| 空卡 | empty card | 空字符串是合法卡；不是缺失，也不是占位。 | `layers.validate_card_body` |
| 稀疏窗口 | sparse window | 四个 pass 都合法但没有任何 durable 内容；不发布产物。 | `worker._merge_base_cards` |
| 用户卡 | user card | 两部分：`user_self_representation`（这个人是谁）与 `user_preferences`（助手该怎么和他说话）。 | `layers.USER_PARTS` |
| AI 卡 | AI card | 两部分：`ai_self_representation`（本窗口的状态）与 `ai_identity`（durable 身份）。 | `layers.AI_PARTS` |
| 窗口摘要 | `window_summary` | 任务状态 markdown。列与压缩链 kind 都是 `window_summary`；旧名 `session_chunk_summary` 在打开旧库时迁移。模板是形状提示，代码只校验是字符串。 | `window_summary.py` |
| 标记数组 | `value_markers` | B4 输出的知识单元数组，不是日记行。 | `layers.validate_value_markers` |
| 标记 | `value_marker` | `value_markers` 数组的一条；中文叫标记，代码里仍写 unit。 | `layers.validate_value_markers` |
| 标记 id | `id` | 窗口内唯一；重复即失败。 | `layers.validate_value_markers` |
| 语义分类 | `category` | `KNOWLEDGE`、`VALUE`、`INTENT`、`METHOD`、`GOVERNANCE`，每条标记恰好一个。 | `layers.CATEGORIES` |
| 组织类型 | `org` | `entities`、`trajectories`、`experiences`、`events`、`tools`、`projects`，每条标记恰好一个；单次发生且无可复用教训归 `events`，带教训的归 `experiences`。 | `layers.ORGS` |
| 认识状态 | `epistemic` | `fact`、`inference`、`hypothesis`、`prediction`；只有 `KNOWLEDGE` 携带。 | `layers.EPISTEMIC` |
| 主题 | `topic` | 标记的业务身份之一，2–40 字符。 | `layers.MAX_UNIT_TOPIC` |
| 关系 | `relations` | 最多 3 条，类型为 `supersedes`、`caused_by`、`part_of`、`contradicts`、`updates`、`instance_of`。 | `layers.RELATION_TYPES` |
| 关联对象 | `objects` | 最多 5 个，形如 `tools:Docker`、`projects:minir-memory`。 | `layers.MAX_UNIT_OBJECTS` |

### 3.1 列名与旧名对照

`base_refreshes` 的旧列名在打开旧库时迁移；`user_*` 兼容列保留。本表规定文档用词。

| 列名 / 旧名 | 定稿术语 | 说明 |
| --- | --- | --- |
| `user_representation` | `user_self_representation` | 兼容列，比术语少一个 `self`；存的是 JSON 字符串，内容是 markdown 正文。 |
| `user_peer_card` | `user_preferences` | 兼容列，与“peer card”无关。 |
| `ai_self_representation` | `ai_self_representation` | 列名与术语同名。 |
| `ai_identity_card` | `ai_identity` | 内容为 markdown 正文。 |
| `window_summary` | `window_summary` | 当前列名；旧库的 `session_chunk_summary` 在打开时重命名。 |
| `value_markers` | `value_markers` | JSON 形如 `{"value_markers": [...]}`；旧键 `units` 兼容读取。 |

## 4. dialectic 层

职责：在两次 base 刷新之间校订最新已发布的 base 产物。它不改写整个产物，只把有证据的部分写回，并留一条修订注。

| 术语 | 标识符 | 定义 | 出处 |
| --- | --- | --- | --- |
| dialectic 层 | `dialectic`、`DIALECTIC_LAYER` | 常驻校订层。 | `turn_counter` |
| 被审产物 | base artifact | 该会话最新的 base 刷新，不是本层自己窗口对应的那一份。 | `worker._merge_dialectic` |
| 证据窗口 | `evidence` | 本层作业窗口内的消息。设计稿的正名是 `[artifact.window_to_ts, now]`；独立调度后代码按本层水位取窗，两者边界不总相同。 | `worker._merge_dialectic` |
| 校订三件事 | correct / fill / advance | 纠正冲突字段、补齐空字段、把窗口摘要推进到当前。 | `agents/dialectic.md` |
| 变更字段 | `base_changed.fields` | 真正变化的 part 名；`changed` 为真时不得为空。 | `agents/dialectic.md` |
| 模型变更清单 | `changes` `[草案]` | 设计稿要求模型输出的审计数组；当前模型回复里的该键不被读取、不被校验，变更由代码按 `base_changed` 计算。 | `doc/layer-agent-contracts-draft.md` |
| 剪枝 | `prunings` `[草案]` | 只有 card_review 允许删；dialectic 恒为空。当前代码无此输出。 | 同上 |
| 版本栅栏 | version fence | 写回前确认被审 base 行仍是 latest 且 `window_to_ts` 未变；否则整份丢弃。 | `worker._merge_dialectic` |
| 过期基线 | `stale_base` | 版本栅栏失败时的返回标记与日志事件。 | `worker._merge_dialectic` |
| 无基线 | `no_base` `[草案]` | 设计稿的降级标记；当前代码直接跳过，不写事件。 | `doc/base-dialectic-layers-draft.md` |
| 修订注 | `## Base Revision` | dialectic 产物正文，只有被改字段的短注；无改动写 `no change this window`。 | `worker._render_revision_note` |
| 覆盖前快照 | `base_revisions` | 每次写回前保存的被覆盖字段与 changed_fields，供回滚与检索。 | `store.apply_base_revision` |
| 自适应深度 | `dialectic_depth` | 证据越长 pass 越多：单次、加自审、再加对账。 | `worker._merge_dialectic_depth` |

## 5. card_review 层

职责：把多会话的会话卡合并成全局卡。全局卡由全局用户卡与全局 AI 卡组成，共四份文件。它是唯一允许剪枝、也是唯一写全局卡的角色。

| 术语 | 标识符 | 定义 | 出处 |
| --- | --- | --- | --- |
| 全局卡 | global card | 全局用户卡与全局 AI 卡的合称；四份 profile 级文件是当前状态，数据库只记历史。 | `card_review.card_paths` |
| 全局用户卡 | global user card | `user/identity.md` 与 `user/preference.md` 两份文件。 | `card_review.card_paths` |
| 全局 AI 卡 | global AI card | `agents/<profile>/identity.md` 与 `agents/<profile>/preference.md` 两份文件。 | `card_review.card_paths` |
| 会话卡 | session card | 会话级用户卡与会话级 AI 卡的合称；base artifact 的 User Card 与 AI Card 两节，按会话存。 | `worker` |
| 用户半程 | user half-pass | 只写全局用户卡的一个文件：`user_identity` 或 `user_preference`。 | （文档）、`CARD_SCOPE_PARTS["user_identity"]` |
| AI 半程 | ai half-pass | 只写全局 AI 卡的一个文件：`ai_identity` 或 `ai_preference`。 | （文档）、`CARD_SCOPE_PARTS["ai_identity"]` |
| 半程规则 | `CARD_SCOPE_RULES` | 每个单文件半程注入一份规则：`user-self`、`user-preferences`、`ai-identity`；两个 AI 文件共用 `ai-identity`。 | `card_review.py` |
| 四份文件 | `PARTS` | `user/identity.md`、`user/preference.md`、`agents/<profile>/identity.md`、`agents/<profile>/preference.md`。 | `card_review.card_paths` |
| 文件头 | front-matter | `schema`、`source`、`generated_at` 等元数据；模型不写，代码框。 | `card_review._render_card` |
| 卡版本 | `CARD_SCHEMA` | 当前为 `minir-card/v1`。 | `card_review.py` |
| 锚点 | anchor | `layer_state("card_review", subject)` 里的 `last_success_at`、`next_attempt_at`、`attempts`。 | `CardReviewScheduler` |
| 周期 | `PERIOD_SECONDS` | 3 天；失败重试 30 分钟、2 小时、6 小时。 | `card_review.py` |
| 手改检测 | `operator_edit` | 文件 hash 与上一条 `after_hash` 不一致时，先记一条手改 revision。 | `card_review.write_cards` |
| 卡修订 | `card_revisions` | 每次写入的前后 hash、changed_fields、source、evidence。 | `store._SCHEMA` |
| 剪枝 | prune | 从全局卡删除不再成立的行；只有本层允许。 | `card_review` |
| 内容语言 | `content_language` | 文件层输出语言；与对话语言分开。 | `card_review`、`knowledge` |

## 6. knowledge 层

职责：把一条标记渲染成一个主题页，并在 prefetch 时只给路径。它不判断知识该不该存在。

| 术语 | 标识符 | 定义 | 出处 |
| --- | --- | --- | --- |
| 知识页 | knowledge page | `knowledge/<org>/<CATEGORY>-<slug>.md`。 | `knowledge.write_unit` |
| 页身份 | `(category, org, topic)` | 一页一个身份；同名不同主题不是同一页。 | `knowledge._find_existing` |
| slug | `slugify` | topic 的文件名干；CJK 与字母数字保留。 | `knowledge.slugify` |
| 页头 | front-matter | 索引来源，含 `schema`、`topic`、`summary`、`revisions`、`content_hash` 等。 | `knowledge.FRONT_MATTER_KEYS` |
| 页版本 | `revisions` | 该身份被重写的次数。 | `knowledge.write_unit` |
| 预算 | `knowledge_token_budget` | 默认 2k tokens；超预算先丢最旧证据再重试，仍超则失败。 | `knowledge.write_unit` |
| 失败页 | `_failed/` | 写不进去的页落在这里，旧页保持完整。 | `knowledge.FAILED_DIRNAME` |
| 知识渲染 | `knowledge_render` | 把标记写成主题页的 pass。会话收尾时批量读整场会话；`agents/knowledge_render.md` 是当前唯一资产。 | `agents/knowledge_render.md` |
| 知识管道 | `KnowledgePipeline` | 每 profile 一个守护线程 + 有界队列；发布不等页面。 | `knowledge.py` |
| 路径块 | path block | `[knowledge — retrieve on demand]` 到 `[end knowledge]`，只有路径与摘要。 | `knowledge.render_paths_block` |
| 路径选择 | `select_paths` | 先用 `rg` 收窄候选页（`facts.json` 定位二进制），再按 front-matter 选最多 6 条、700 字符；无 `rg` 时退回目录扫描。 | `knowledge.select_paths` / `rgtool.search` |
| 阶段号 | “stage P6” `[草案]` | 模块 docstring 的说法；仓库没有 P1–P5 的定义。 | `knowledge.py` docstring |

## 7. 会话收尾

职责：会话空闲后产出最终摘要，再据摘要与标记渲染知识页。

| 术语 | 标识符 | 定义 | 出处 |
| --- | --- | --- | --- |
| 空闲窗口 | `knowledge_idle_hours` | 默认 24 小时；期间再次输入则取消本次收尾。 | `extraction.IDLE_SECONDS` |
| 收尾行 | `session_extractions` | 每个会话一行，记录 due_at、状态、标记快照、页数、摘要长度。 | `store._SCHEMA` |
| 会话记录 | session record | 压缩期 B3 校准出的最新窗口总结，键 `session_summary:<n>`；终稿与召回的第一输入。 | `compress_pass.py`、`store.latest_compression_summary` |
| 会话摘要 | `session_summary` | 最终 durable 摘要，自带模板：Request、Outcome、Decisions、Constraints、Verified Facts、Lessons、Open Threads、References。 | `session_summary.TEMPLATE_PATH` |
| 记录优先 | record first | 摘要 agent 的用户消息第一块是会话记录，缺则最新 base 窗口摘要。 | `__init__._session_record` |
| 用量 | token usage | 每轮最终 token 用量，随收尾行保存。 | `store.session_usage` |

## 8. 压缩

职责：在宿主压缩边界把窗口中段折成一条 assistant 摘要，保住头尾原文。

| 术语 | 标识符 | 定义 | 出处 |
| --- | --- | --- | --- |
| 上下文引擎 | context engine、`minir` | 实现 `ContextEngine` 的压缩引擎。 | `context_engine.ENGINE_NAME` |
| 折叠 | fold | 用一条摘要替换窗口中段。 | `foldable_slice` |
| 折叠片 | foldable slice | 不碰系统提示与头部保护、不碰尾部保护、不拆工具组的中段。 | `foldable_slice` |
| 摘要消息 | assistant summary message | 折叠后插入的 assistant 消息，带 `[minir summary #<id>]` 标记。 | `context_engine.SUMMARY_MARKER` |
| 提示语 | notice | `[minir memory notice` 开头的说明，告诉模型摘要是派生记录。 | `context_engine.NOTICE_TEXT` |
| 压缩闸 | `compression_gates` | 防止同一 run 重复压缩的会话级闸门。 | `store._SCHEMA` |
| 预备块 | prepared chunk | base 为本窗口准备的摘要块，是压缩 agent 的输入之一。 | `context_engine` |
| chunk 链 | `window_summary` | 每个窗口一条，串成压缩链；kind 与列名都已迁移为 `window_summary`。 | `store.add_chunk_summary` |

## 9. adviser 旁路

职责：在实时工具循环里给主 agent 提建议。它不写卡、不写 revision、不写层状态。

| 术语 | 标识符 | 定义 | 出处 |
| --- | --- | --- | --- |
| 主 adviser | `auxiliary.adviser` | 一条模型路由，同时服务 consult 与 review。 | `adviser.py` |
| 评审 | `review_once` | 看事件环，产出 advice；只写 `advisories`。 | `adviser_mode` |
| 咨询 | `adviser_consult` | 同步提问入口；共享同一人格核心，不写账本。 | `adviser.TOOL_NAME` |
| 事件环 | event ring | 有界内存队列，收 `post_tool_call`、`post_api_request`、`on_interim_message`。 | `adviser_mode.MAX_RING` |
| 建议 | advice | 一条带 target 与 severity 的提示。 | `prompting.ADVICE_TARGETS` |
| 建议目标 | target | 7 个：`tool_choice`、`current_step`、`final_answer`、`user_intent`、`general`、`output_surface`、`code_economy`。 | `prompting.ADVICE_TARGETS` |
| 严重度 | severity | `blocker`、`concern`、`nit`。 | `prompting.ADVICE_SEVERITIES` |
| 严重度下限 | `severity_floor` | 低于下限的建议不走中途通道；默认 `concern`。 | `adviser_mode` |
| 发射守卫 | emission guard | 去重、限流、拒绝不合规的建议。 | `adviser_guard.EmissionGuard` |
| 账本 | `advisories` | 建议的持久化与投递状态：`pending`、`injected`、`staged`、`delivered`、`expired`。 | `store._SCHEMA`、`store.mark_advisories_injected` |
| 投递 | delivery | `llm_request` 中间件把 pending 建议注入下一次请求；`post_api_request` 确认。 | `adviser_mode` |
| 有效期 | `ADVICE_TTL` | 建议一小时过期。 | `adviser_mode` |
| 只看建议模式 | `adviser_only` | 只允许建议、拒绝变更工具的配置模式。 | `plugin.yaml`、`pre_tool_call` |
| 会话检索 | `adviser_session_search` | 只读多形态检索，属 `adviser` toolset。 | `session_search.py` |

## 10. 易混对照

| A | B | 差别 |
| --- | --- | --- |
| 产物 artifact | 结论 conclusion | 产物是 `(layer, subject)` 的整篇 Markdown；结论是拆开后可按节检索的行。 |
| base 刷新 refresh | base 修订 revision | refresh 是 base 自己的一次窗口产出；revision 是 dialectic 对它的覆盖，带覆盖前快照。 |
| 会话卡 session card | 全局卡 global card | 会话卡 = 会话级用户卡 + 会话级 AI 卡；全局卡 = 全局用户卡 + 全局 AI 卡（四份文件）。 |
| `user_self_representation` | `user_identity` | 前者是会话卡正文；后者是全局用户卡文件的对应 part。 |
| `user_preferences` | `user_preference` | 前者是会话卡正文（复数）；后者是全局文件的 part 名（单数）。 |
| `window_summary` | `session_summary` | 前者是 base 每个窗口的任务状态；后者是会话空闲后的最终摘要。 |
| `window_summary` | `session_chunk_summary` | 旧名，已迁移；文档不再区分两者。 |
| 压缩记录 | 终稿 | 前者是压缩期 B3 校准出的最新窗口总结，八节；后者是会话空闲后的最终记录，自带八节（Request/Outcome/Decisions/Constraints/Verified Facts/Lessons/Open Threads/References）。 |
| 标记 `value_marker` | 笔记 note | 标记是 `value_markers` 的一条知识单元；笔记是 `notes` 表的一行。两者都进 FTS，但生命周期不同。 |
| `value_markers` | 显式标记 | 前者随 base 刷新合并；后者由 `minir_remember` 写入，按 type 删除。 |
| dialectic review | card_review | 前者校订一份 base 产物，轮次驱动；后者合并多会话到全局文件，墙钟驱动。 |
| adviser review | dialectic review | 前者评实时 turn，产出 advice；后者评记忆产物，产出 revision。 |
| 层 layer | pass | 层有独立触发与产物；pass 是一次模型调用。 |
| prefetch | recall | prefetch 是宿主注入的自动快路径；recall 是显式 `minir_search`。 |
| `knowledge_render` | `knowledge_extract` | 旧资产名；代码已合并为一个 `knowledge_render` pass。 |

## 11. 命名冲突与待裁定

1. **base 是三个 pass 还是四个。**
   `doc/layer-agent-contracts-draft.md` §1.1 的表格写“3 个 pass（B1 用户卡、B2 AI 卡、B3 摘要与 markers）”，但同一文件 §1.3 又列出了 B4 的 markers 输出；当前代码是 B1–B4 四个 pass。默认：以代码为准，把设计稿改成四个 pass。

2. **`## Base Revision` 与 `base_revisions` 表不是一回事。**
   前者是 dialectic 的产物正文（短注）；后者是写回前的字段快照（表）。默认：文档把前者叫“修订注”，后者叫“覆盖前快照”。

3. **`user_peer_card` 是兼容列名。**
   它现在存 `user_preferences`，和“peer card”没有关系。默认：文档只写 `user_preferences`，列名放 §3.1。

4. **marker 有三个所指。**
   `value_markers` 数组、`minir_remember` 的 `notes.marker_type`、`session_extractions.markers` 的快照。默认：分别叫“标记数组”“显式标记”“收尾快照”。

5. **review 有三个所指。**
   dialectic review、card_review、adviser review。默认：永远带限定词。

6. **“stage P6”没有 P1–P5。**
   `knowledge.py` docstring 自述是“stage P6 of the layer pipeline”，仓库没有其余阶段定义。默认：删掉阶段号，或补一张完整的 P1–P6 表。

7. **`base` 产物的三种存储形态。**
   `layer_state` 是当前投影，`base_refreshes` 是历史行，`conclusions` 是检索行。默认：分别叫“当前产物”“刷新历史”“检索结论”。

8. **`card` 一词覆盖多处。**
   全局卡、会话卡、用户卡、AI 卡、卡文件、卡 part。默认：按 §5 与 §10 的限定词写。

## 12. 审阅清单

1. §11 的 8 条默认处理，哪些接受，哪些要改？
2. base 的 pass 数按代码（4）还是按设计稿（3）定稿？
3. 是否把兼容列 `user_representation` 一并迁移为 `user_self_representation`？
4. 是否需要在本文之外，另出一份给模型看的术语片段（只含 `ADVICE_TARGETS` 与 `MARKER_TYPES` 这类注册表）？
