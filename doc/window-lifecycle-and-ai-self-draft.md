# 窗口生命周期与注入面草稿

状态：草稿，未实现，不含代码改动。

依据：工作树当前状态的只读核对（HEAD `5a2e1a2`，另有 7 个已修改未提交文件）。行号以本次核对为准。

覆盖四条需求：

1. `prefetch` 不再注入知识路径块。
2. `prefetch` 不再注入显式召回块。
3. `## Minir layer state` 只注入 `ai_self_representation` 与 `window_summary` 两部分。
4. `window_summary` 在会话内持续累积，`compress()` 时校准为 `session_summary`；`ai_self_representation` 的六字段与 `window_summary.objective` 建立数据通路（压缩后同一目标集合进入 `session_summary`）。

## 1. 边界

范围：单个会话内部的窗口周期。会话之间只定义隔离不变量（§4.1），不定义继承；并行会话的调度与同一 session 的多进程并发不在本稿范围。

改：

- `prefetch()` 的组装与取数。
- base 刷新里 B2、B3 的输入、顺序与输出契约。
- `compress()` 的摘要来源与 schema。
- 会话收尾（24h 提取）的摘要输入。
- 相关提示词资产与测试。

不改：

- `minir_search`、`minir_kb_search` 两个显式通道。
- 知识页的写入与提取管线。
- `minir_todo` 的存储与渲染。
- advisories、dialectic 的评审调用与版本栅栏。
- 其他会话的窗口与 `is_latest`。新会话只写自己的行。
- 若走方案 A（见 §3），不改表结构、不做迁移。

## 2. 注入面

改后 `prefetch()` 只返回两块：layer state 与 todo。

| 段 | 现状 | 改后 |
| --- | --- | --- |
| 知识路径块 | `_knowledge_block`（`__init__.py:565-591`） | 删除调用与函数 |
| layer state | 读 `layer_state` 再 `project_artifact`（`__init__.py:742-800`） | 读活 base 行的两列，渲染两个子块 |
| todo | `_todo_block`（`__init__.py:818-823`） | 不变 |
| 显式召回 | `_search` + `_format_block`（`__init__.py:719-727`） | 删除注入路径 |

渲染形状：

```markdown
## Minir layer state
### AI self representation
<ai_self_representation 正文>
### Window summary
<window_summary 正文>
```

取数：

- 两列在写入前经 `json.dumps`，解码走 `layers.artifact_from_columns` 的同一套逻辑（`layers.py:416-440`）。读 prefetch 时按 `json.loads` 解出 markdown 字符串。
- 任一段为空则省略该子块；两段都空则整块不注入，与今天 `_layer_block` 返回空串的行为一致。

预算：

- 两段上限：ai_self ≤400、window ≤2000（现在共用一个 `_MAX_LAYER_CHARS = 1200`，`__init__.py:116`）。window 超限时先保 `objective` 与 `current_state` 全量，再截断其余六节并标 `[summary truncated]`。理由：目标账本不设条数上限，硬预算只能落在非目标字段上。

删除清单：

- `_knowledge_block`（565-591）、`queue_prefetch`（675-696）、`_take_cached`（`__init__.py:1854`）、`_format_block`（`__init__.py:1865`）、`_PREFETCH_KINDS`（`__init__.py:127`）、`_recent_knowledge` 状态。
- `knowledge.render_paths_block`（`knowledge.py:831`）在 provider 内失去唯一调用方；`select_paths` 仍被 `adviser_mode.py:1422` 使用，保留。

契约反转：这条需求推翻的是现有明确契约，不是遗漏。`prefetch()` 的 docstring 写着"No base-layer section is injected here"（`__init__.py:706-709`），`test_prefetch_does_not_inject_any_base_artifact` 把这句话钉成了回归测试。改动要同时改 docstring、该测试和"基础字段只走显式召回"的注释，否则代码与文档会长期相反。

保留：

- `_search`（`minir_search` 工具在用，`__init__.py:1624`）。
- `_todo_block` 与 `RecallStatus` 机制。

计数与指示器：

- 现在的 `count` 是四部分之和：缓存或召回的条数、layer 各段 bullet 数、todo 项数、知识路径数（`__init__.py:718-739`）。改后只剩 layer 与 todo 两部分。
- `recall_status()` 的文案若仍写 "recalled N memories"，语义与内容不符。两种处理：改文案为 "injected N items"，或让该指示器只反映 layer 段。需要选一个，见 §10。

## 3. 数据源

`dialectic_refreshes` 不能当"最新发布"读。

- `base_refreshes` 是每次 base 刷新写入的活行，`is_latest` 按 `window_to_ts` 决定（`store.py:1426-1503`），`latest_base_refresh` 按会话读活行（`store.py:1505-1518`）。
- `dialectic_refreshes` 是 `apply_base_revision` 的覆写前快照：先整行拷入，再改 `base_refreshes`（`store.py:1658-1674`）。它只在评审真正改字段或空窗口身份刷新时写一行（`worker.py:1227-1260`、`worker.py:1386-1395`），最新一行的内容是旧值；首评之前为空。
- `base_revisions()` 与 FTS 也读这张表，定位是审计与历史检索（`store.py:1693-1708`）。

方案：

| | A（建议） | B |
| --- | --- | --- |
| 读取 | `base_refreshes WHERE session_id=? AND subject=? AND is_latest=1` 的两列 | `dialectic_refreshes` 的两列 |
| 写侧 | 不改 | 每次 publish 也写快照，或把两张表合并 |
| 代价 | 无迁移 | 快照语义消失或行数翻倍；两张表的 `is_latest` 唯一索引要重建（`store.py:163-165`、`314-317`） |
| 风险 | 无 | 评审纠正后注入的是纠正前内容；无评审时为空块 |

建议 A。若必须保留表名，B 需要单独一版迁移草稿。

## 4. 窗口周期

### 4.1 范围与隔离不变量

本稿只描述单个会话内部的窗口周期。会话之间只定义隔离，不定义继承；并行会话的调度、同一 session 的多进程并发不在本稿范围（写入由 `BEGIN IMMEDIATE` 串行化，见 `store.py:1426-1503` 的 `add_base_refresh`）。

- I1 会话隔离：窗口的读写一律带 `session_id`；`window_summary` 不跨会话继承。跨会话只允许 durable 的 `ai_identity` 与 user card 作为基线（`latest_base_refresh_for_subject` 的现有用途，`store.py:1520-1532`）。
- I2 只增不减：同一窗口内 coverage 的终点单调不减；刷新重试只延长窗口（`add_base_refresh` 的 `effective_to` 取 `max`，`store.py:1426-1503`）。
- I3 压缩不封口：`compress()` 压缩的是宿主的会话历史，不结束窗口、不重置基线、不清空 done；压缩后同一窗口继续从最新行拉长。
- I4 旧会话冻结：开启新会话，或在别的会话里 compress，都不改本会话的行与 `is_latest`。
- I5 收尾不重置：24h 的 final 摘要是同一窗口的最后一次校准，不是窗口边界。

### 4.2 一次完整的窗口生命周期

以会话 A 为例。同一会话内窗口只增不减；不同会话各自独立，A 的窗口不会因 B 的建立或压缩而改变。"窗口长度"指摘要的覆盖范围，不是每次把全部历史重喂模型：每次刷新只喂增量证据，状态靠 `previous_summary` 累积。

| 阶段 | 触发 | 读取 | 写入 | 窗口状态 |
| --- | --- | --- | --- | --- |
| T0 建立 | 宿主创建 session_id | 无 | 无 | coverage=[t0,t0)，window_summary 空 |
| T1 首轮 | 用户第一条消息 | 空基线 | 消息入库 | 开始累积 |
| T2 刷新 | base 24 轮 / 复盘 8 轮（首次 base 后计） | 本会话最近一行 + 增量证据 | 新 `base_refreshes` 行 | coverage=[t0,to_ts)，只增不减 |
| T3 阈值 | prompt ≥ 95% | 无 | 无 | 等待 compress |
| T4 压缩前刷新 | `on_pre_compress` | 尾部证据 | 强制刷新一行 | coverage 到 t4 |
| T5 校准 | `compress()` | T4 行的 window_summary + 上一份 session_summary | `session_summary:<n>`，作为压缩结果返回宿主 | 窗口不变，coverage=[t0,t5] |
| T6 继续 | 下一条消息 | 本会话最新 base 行 | 新行 | coverage=[t0,t6)，继续拉长 |
| T7 再次压缩 | 第二次 ≥95% | 同 T5 | `session_summary:<n+1>` | 窗口仍不变 |
| T8 收尾 | 空闲 24h | 最新 base 行 + 最新 session_summary | `session_summary:<sid>:final` | 本会话窗口结束 |
| T9 新会话 | 新 session_id | 仅 durable 身份 | 只写自己的行 | A 的窗口原样保留 |

T0 建立。会话 A 开始时没有 `base_refreshes` 行，也没有 session_summary。window_summary 为空，尚无 ai_self 持久行，prefetch 此时只剩 todo（若有）。

T1 到 T2 累积。第一次 base 刷新（满 24 轮，或 pre_compress 强制）时 B3 的 `previous_summary` 为空，输出第一版 8 字段 window_summary。之后每次刷新取"本会话内 `window_to_ts` 最大的一行"作为 `previous_summary`，再合并本次增量证据。证据是增量的，摘要是累积的，覆盖范围只向前延伸。

T3 到 T4 压缩前。prompt 达到 95% 时宿主调 compress。插件先跑 `on_pre_compress`：强制 base 与 dialectic 各一次、同步 drain、publish、计数器归零（`__init__.py:1111-1168`）。这是压缩前本窗口的最后一次拉长。若这次强制刷新失败或超时，用上一行 base 作为压缩输入并记录缺口，不阻断压缩。

T5 校准。compress 读 T4 行的 window_summary 与上一份 session_summary，产出 `session_summary:<n>`（8 字段、session 作用域），并把它作为本次压缩的结果返回宿主。窗口不封口：base 行不改写，done 不清空，也没有"下一个窗口"这回事。

T6 继续拉长。压缩之后的下一次刷新仍取本会话 `window_to_ts` 最大的一行作为 `previous_summary`，窗口覆盖继续向后延伸。宿主丢弃的是原始消息，记忆窗口的覆盖范围不缩短。

T7 再次压缩。第二次达到阈值时重复 T3 到 T5，写 `session_summary:<n+1>`；最新一份是 ai_self 的 `当前目标` 来源，此前几份保留为历史快照。

T8 收尾。会话不再有新消息，空闲 24h 后提取管线写 `session_summary:<sid>:final`（同一 8 字段模板）。若会话期间从未 compress，final 直接由当前窗口校准（窗口为空时不产出摘要，见下条）。收尾只是同一窗口的最后一次校准；同一 session 之后恢复使用，窗口继续拉长，不因空闲时间重置。

T9 新会话。新 session_id 建立自己的窗口：window_summary 为空，即使会话 A 已有若干轮压缩。B3 的 `previous_summary` 必须为空，而不是 A 的行。A 的窗口、行、`is_latest` 全部保持原状；B 的 compress 只写 B 的会话。

边界：会话短于首个刷新周期且从未触发压缩时，窗口始终为空。本稿不为这种会话补摘要，它只有原始消息。

### 4.3 窗口基线

- 窗口起点 = 会话开始，终点 = 会话结束；中间没有重置点。compress 只是窗口内的一次校准事件。
- 内层刷新节拍不变：base 24 轮、dialectic 8 轮（`turn_counter.py:44-45`）；dialectic 的 8 轮从首次 base 触发之后才开始计，base 未触发前不排期。
- B3 基线：

```
previous = 本会话 window_to_ts 最大的 base_refreshes 行（无则空）
```

- 不以 session_summary 作基线：它是压缩产物，不是窗口状态。
- 没有 session_summary 时，ai_self 的 `当前目标` 回落到 window_summary.objective 的 `active` 子集。

现状问题：

- B3 的 `previous_summary` 取自 `baseline["window_summary"]`（`worker.py:903-911`），`baseline` 来自 `latest_base_refresh_for_subject`，是跨会话最新行（`store.py:1520-1532`、`worker.py:1182-1198`）。新会话会继承上一个会话的窗口，违反 I1。窗口持续拉长本来就是要求，改动只需把它限制在本会话内。
- `latest_session_summary` 仍需要（ai_self 的 `当前目标` 来源），但不参与基线计算。

### 4.4 压缩时的校准

1. `_prepare_compression` 已把窗口刷到末尾（现状已有）。
2. `compress()` 读活 base 行的 `window_summary` 与上一份 `session_summary`，产出新的 `session_summary`（8 字段，session 作用域）。
3. 写入键 `session_summary:<store.next_session_summary_index(session)>`（`context_engine.py:270-274` 的写入路径不变），并把同一段摘要作为压缩结果返回宿主。
4. 校准是快照，不是状态转移：不改写窗口行、不清空 done、不改变基线。
5. 模型失败或超时：用确定性合并兜底（objective 取窗口的 `active` 条目，done 累加，其余按上限截断），仍要给宿主一段非空摘要，不阻断压缩。

合并规则：

- `decisions`、`constraints`、`verified_facts`：以窗口为准，冲突时新值覆盖旧值。
- `open_issues`、`next_actions`、`references`：窗口与上一份的并集，已解决的移除。
- `objective`：会话的目标账本，压缩时原样带过，不清空、不重置；完成的目标保留，放弃的目标移除。它是 ai_self 的 `当前目标` 来源。
- `current_state.done`：保留在窗口摘要里继续累积，不在压缩时清空。

### 4.5 24h 收尾

- 现在的 `session_summary.py` 输出 5 个标题（`session_summary.py:33-35`），与上一步的 8 字段不是一套。
- 改为同一 8 字段模板的 `final` 形态，键保持 `session_summary:<session>:final`（`__init__.py:1431-1436`）。handoff 的角色从"4 标题摘要"改为"校准 pass"，输出同一模板（`handoff.py:22`）。

## 5. 模板与解析器

已有：

- `templates/window_summary.md` 就是 8 个标题；`objective` 是目标列表（无编号），`current_state` 把每个目标放进 done、doing、todo 之一，其余 6 个是状态字段。
- B3 的 caps 只是提示词里的写作目标，代码不据此判失败。

缺：

- 不需要解析器，也不需要新增校验。`validate()` 现有行为（只检查是字符串，`window_summary.py:92-96`）保持不变；摘要按 markdown 原样传给 B2 与 compress。
- `window_summary.py:22` 的 `MAX_OBJECTIVE_CHARS` 从"硬上限"降级为提示词写作目标；不再有字段级或条数级失败。
- 两个作用域不再有字段语义差异：window_summary 是活视图，session_summary 是压缩时刻的快照；同一份目标集合与 done 在两者中一致。

输出约定：

- B3 的提示词要求按模板输出；没有内容的章节保留标题、留空。
- 代码不解析、不校验摘要正文，不因格式失败或重试。

## 6. ai_self_representation 六字段

错位根因：

- B2 与 B3 被要求写同一件事：B2 是 "where the task stands, what is known, what is not, the state"（`agents/b2_ai_card.md:27`），B3 是 "the task state at the end of this window"（`agents/b3_window_summary.md:2`）。两次独立生成，注入时并排出现。
- ai_self 没有字段契约：`validate_ai_self_representation` 只做字符串校验（`layers.py:244-245`）。
- 与 `session_summary` 没有通路：后者今天没有 objective 字段。

字段与来源：

| 字段 | 来源 |
| --- | --- |
| 自我定位 | 先写 `ai_identity`，`ai_self` 取其首行（同一 pass 内，先身份后状态） |
| 当前任务 | `window_summary.current_state.doing` 里的目标短句，最多 2 条 |
| 当前目标 | `window_summary.objective` 中未完成的目标（doing + todo）短句，最多 2 条；压缩后 `session_summary` 保留同一目标集合 |
| 自我认知 | `window_summary.verified_facts` 中与当前状态直接相关的 1-3 条（已知）+ `open_issues` 中的未知；完整清单留在窗口摘要 |
| 当前状态 | `current_state.doing/todo` 与 `open_issues` 映射：待命、执行、阻塞、完成 |
| 不确定性 | `open_issues` 中标为待验证的条目 |

`objective` 是目标列表：ai_self 只引用未完成的目标（doing + todo），按目标短句指代，不写编号。`current_state.doing` 为空时 `当前任务` 写"待命"；全部目标都在 Done 时写"完成"，`当前目标` 写"无进行中的目标"。`自我认知` 只取与当前状态直接相关的 1-3 条已验证事实，不复制 `verified_facts` 全表。

轮中途输入的捕获不额外加通道：steering 在 transcript 里就是一条 user 行，B3 的 `<window_summary_transcript>` 已声明覆盖 every role（`agents/b3_window_summary.md:17`），目标集合的更新靠 B3 的合并完成。

生成方式（按新编排）：

| | A 代码投影 | B B2 生成（推荐） |
| --- | --- | --- |
| 做法 | B3 之后由代码从 window_summary 投影 | B2 读 B1 的 user_self / user_preferences / window_summary 写六字段 |
| 校验 | 六标题齐备即可 | 六标题齐备 + 字段上限 |
| 成本 | 0 次调用 | 0 额外调用（B2 本来就要跑） |
| 风险 | 不确定性只能复述 open_issues | 模型复述任务账本，提示词里要明令不复述 |

新编排下 B2 在 B1 之后（B1 含 B3/B4 子 agent），所以 `ai_self` 天然读得到本轮 `window_summary`，不需要再拆 B2a/B2b。B2 的输入里没有 `session_summary`：它是同一目标集合在压缩时刻的快照，目标不变，`当前目标` 直接引用窗口集合即可。

去重边界（两条路线都适用）：

- ai_self 只写指针与状态，不复制 `next_actions`、`decisions`、`verified_facts` 的条目。
- `当前任务` 与 `当前目标` 各一句，各不超过 120 字符。

## 7. 改动清单

| 文件 | 位置 | 动作 |
| --- | --- | --- |
| `plugin/minir/__init__.py` | `prefetch` 698-740、`_layer_block` 742-800 | 删知识块与召回注入；layer 块换源 |
| `plugin/minir/__init__.py` | 565-591、675-696、1854、1865、127 | 删除对应函数与常量 |
| `plugin/minir/worker.py` | `_merge_base_cards` 848 起 | B3/B4 改由 B1 调度；B2 移到 B1 之后；B3 基线改为本会话最新行 |
| `plugin/minir/worker.py` | `_merge_base` 1167-1225 | baseline 不再把跨会话 window_summary 灌进新会话 |
| `plugin/minir/worker.py` | `_merge_dialectic` 1367-1375 | 纠正后的 window_summary 写回 chunk 的路径随契约调整 |
| `plugin/minir/layers.py` | 244-245、336-343 | 不新增 md 校验；value_markers 仍按 JSON 校验 |
| `plugin/minir/window_summary.py` | 22、92-96 | 不新增 `parse` / `render`；caps 只作提示词写作目标 |
| `plugin/minir/context_engine.py` | `compress` 226-290 | 摘要来源改为 window_summary 校准 |
| `plugin/minir/handoff.py` | 22、72-82 | 输出改为 8 字段校准结果 |
| `plugin/minir/session_summary.py` | 33-35 | 五标题改为同一模板 |
| `plugin/minir/agents/b2_ai_card.md` | 27、33-38 | 六字段定义，禁止复述任务账本 |
| `plugin/minir/agents/b3_window_summary.md` | 2、22-47 | previous_summary 只取本会话最新行；objective 改为完整目标列表（无编号）；MERGE 不再删 objective |
| `plugin/minir/agents/b1_user_card.md` | 全 | 加 ORCHESTRATION；输出四键 |
| `plugin/minir/agents/b4_value_markers.md` | 全 | info_type / why / relations / render_plan / anchor；kb_search 去重 |
| `plugin/minir/agents/knowledge_render.md` | 4-24 | marker 当索引；正文从会话历史提炼 |
| `plugin/minir/extraction.py` | 31、160-188 | marker 与 anchor 传入；队列顺序不变 |
| `plugin/minir/adviser_mode.py` | `_rulers` 1392-1403、`_digest` 1370-1377 | 准绳改读 live 行；加 window_summary；`### Preferences` 标题对齐 |
| `plugin/minir/rules/advice_target.md` | 39、42-43、81 | 引用文案；false_claim / missing_work / scope_change 分属三张卡 |
| `plugin/minir/rules/advice_judgment.md` | 39-41、107 | ai_self 的 stale_card；扫描信号改 base 行版本 |
| `plugin/minir/store.py` | 1426-1532 | 方案 A 只加查询；方案 B 才动表 |

顺序建议：

1. prefetch（独立，可单独验收）。
2. B3 的会话内基线与提示词。
3. compress 校准与 24h 收尾契约。
4. ai_self 六字段。
5. 测试重写。

先改提示词也能独立验证；这个顺序只为减少返工。

## 8. 验收

- 有活 base 行时，`prefetch()` 只含两个子块与 todo；断言 `"[knowledge" not in block`、`"## Minir recall" not in block`。
- 注入文本与 `json.loads(row["ai_self_representation"])`、`json.loads(row["window_summary"])` 逐字节相等。
- 一条评审都没跑过时块仍非空（这条在方案 B 下会失败，是选 A 的直接理由）。
- 两个 session 的 fixture：B 会话首个 `window_summary` 不含 A 的 objective，B 的 `previous_summary` 为空。
- 旧会话冻结：B 的刷新与 compress 前后，A 的 `base_refreshes` 行数、`is_latest` 指向与窗口状态全不变。
- 单调性：同一窗口内连续两次刷新，后一行 `window_to_ts` 不小于前一行；重试不缩短。
- 恢复：同一 session 空闲后再活跃，窗口继续拉长，done 继续累积。
- 压缩后：`session_summary` 的 objective 目标集合与窗口一致；下一次刷新的 `previous_summary` 仍是本会话最新 base 行，窗口覆盖不回退，done 不清空。
- 两次压缩：`session_summary:<1>` 与 `<2>` 都在，最新一份保留同一目标集合；base 行不被改写。
- 轮中途输入：窗口内注入一条 steering 消息，下一次刷新的 `objective` 多出一行新目标，或对应目标那一行被改写（不标来源）。
- 目标集合稳定：连续两次刷新，同一目标的文本不被无故改写；完成的目标留在集合里，并出现在 Done。
- 状态划分：`current_state` 三组覆盖 `objective` 的目标，不写进展，不重复。
- dialectic 计数：base 未触发前 8 轮节拍不启动；首次 base 之后从该轮起算，第 8 轮触发。
- 压缩保留集合：compress 后 `session_summary.objective` 的目标集合与 `window_summary` 一致，未完成目标未被清空。
- ai_self 的 `当前任务` 来自 `current_state.doing`，`当前目标` 取自 doing 与 todo；`自我认知` 只列 1-3 条与状态相关的事实，不整表复述。
- `minir_search`、`minir_kb_search` 结果与改动前一致。
- 24h 收尾写入的 final 摘要与压缩摘要同模板。

## 9. 迁移与测试影响

- 方案 A 无数据库迁移。旧 `dialectic_refreshes` 行继续作为审计历史存在。
- 旧 `session_summary` 行（4 标题或 5 标题）不重渲染；新代码读取时按"无 objective"处理，下一次压缩覆盖为 8 字段。是否回填见 §10。
- 跨会话继承的旧 `window_summary` 会在新会话首个窗口被忽略，无需清理。已存在的旧会话在升级后按自己最新的 base 行继续拉长；新会话的建立不改变它的行、`is_latest` 或窗口状态。
- 需要重写的测试（按名）：`test_queue_prefetch_feeds_next_prefetch`、`test_prefetch_falls_back_to_inline_search`、`test_prefetch_never_returns_stale_recall`、`test_prefetch_kind_filter_survives_derived_crowding`、`test_recall_block_is_bounded`、`test_prefetch_injects_only_the_dialectic_artifact`、`test_prefetch_does_not_inject_another_sessions_review_or_summary`、`test_prefetch_never_injects_durable_base_cards_from_fts`、`test_prefetch_does_not_inject_any_base_artifact`（这条是新契约的反面）、`test_todo_renders_in_prefetch_and_in_the_answer`；`test_integration_layers.py` 中关于知识路径注入的用例；删除 `render_paths_block` 时另含 `test_knowledge.py:190-210`、`338`。
- adviser 准绳测试：第二次 base 刷新后，`_rulers` 读到的是第二版卡而不是第一次的 conclusion；卡旧于事件时给 `stale_card`，不给 `false_claim`。
- 需要新增：compress 校准的字段继承测试、跨会话窗口隔离测试、旧会话冻结测试（B 的动作不改 A 的行）、dialectic 计数起点测试。不做 md 格式测试。

## 10. 待定

1. 表源：A `base_refreshes.is_latest`（建议）还是 B 改造 `dialectic_refreshes`。
2. ai_self：B2 生成（新编排已定）还是保留代码投影作兜底。
3. 旧 `session_summary` 行是否回填（模板已定 8 字段）。
4. `recall_status` 的文案：改成 "injected N items"，还是只统计 layer 段。

## 11. 与现有草稿的关系

- 沿用 `doc/window-summary-template-draft.md` 的字段与上限，不重复定义。
- 修正 `doc/conclusions-only-draft.md` 中把 `dialectic_refreshes` 当作发布面的读法：它只是覆写前快照。
- 与 `doc/base-dialectic-layers-draft.md` 的 base/dialectic 双节拍不冲突；本稿只加一个窗口概念，compress 只是窗口内的校准事件。
