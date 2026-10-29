# base 刷新失败率与重试成本：诊断与改造草稿

状态：草稿（2026-10-28 23:15），未改任何代码，未重放任何 job。
依据：`minir-memory/memory.db` 的 `jobs` / `layer_failures` / `base_refreshes`、`hermes/state.db` 的 `session_model_usage`、`hermes/logs/agent.log`、`minir-memory/sessions/default/base/*.jsonl` 的 pass transcript，以及当前树的 `plugin/minir/passes/*.py`。下文每个数字都标了来源；没复现的写在「未验证」。
非目标：不改 dialectic 的判定内容，不动 b4 写页，不重放历史 job，不放宽 marker schema，不动 host-patches。

配图：[base 刷新的失败与重跑路径](base-refresh-retry-cost-diagram.html)。它画的是**当前**代码的路径（节点名即函数名），不是改造后的设计；卡片里的数字与本文 §1.1、§1.2 同源，线上一切以此文的表为准。

## 1. 实测

### 1.1 作业账本

来源：`select layer,status,finished_at-claimed_at from jobs`（480 行）。

| 层 | 状态 | n | 中位耗时 | p90 |
|---|---|---|---|---|
| base | done | 236 | 10.1 s | 229.4 s |
| base | failed | 66（有 claimed_at 的） | 213.3 s | 729.6 s |
| dialectic | done | 125 | 19.7 s | 289.4 s |
| dialectic | failed | 25（有 claimed_at 的） | 108.6 s | 798.4 s |

- 480 个 job，117 个 failed（24.4%）。
- **321 个 base job 里 136 个 attempts≥2（42.4%）**，其中 55 个重跑后成功、81 个两次后落 failed。
- 有可测耗时的失败合计 10.4 小时。
- 当前 `layer_failures` 未解决 25 条，今天新增 23 条。

结论：中位数 10 秒说明单轮并不慢；贵的是**第二次整轮重跑**，它把 10 秒的作业变成 210 秒以上。

### 1.2 失败分类

来源：`select substr(error,1,72), count(*) from jobs where status='failed' group by 1`，共 117 条。

| 次数 | 类别 | 归组 |
|---|---|---|
| 29 | `base refresh for user:default assembled an invalid artifact` | 契约 |
| 11 | `base summary child failed after one retry` | 契约 |
| 9 + 8 | `dialectic merge ... returned no usable JSON`（2 / 3 passes） | 契约 |
| 6 | `dialectic review ... returned empty cards` | 契约 |
| 5 + 5 | `base user pass returned an invalid object` / `invalid JSON cards` | 契约 |
| 2 + 2 | `base AI pass` 同类两条 | 契约 |
| 2 | `base value markers pass returned an invalid object` | 契约 |
| 1 + 1 | `summary is missing the '## Window Summary' heading after one retry` / 一条 `base user pass` | 契约 |
| 20 + 7 + 1 | `abandoned: ...` | 进程生命周期 |
| 4 + 1 + 1 | `OperationalError`（旧 schema 列、旧列名、无事务） | 迁移残留 |
| 2 | `AgentPromptError`（b3 资产读不到） | 部署 |

契约类合计 81 次，占失败 69%。生命周期 28 次、迁移残留 6 次、部署 2 次。

### 1.3 关键复现：三次失败与一次成功

我把每个 base job 的 b1 答案与紧随其后的 b2 答案（`sessions/default/base/*.jsonl`，每次 pass 一份 transcript）按真实合并顺序 `render_artifact` 重放，再套 `_valid_artifact` 的三条判据。10-28 晚上的四次运行与账本逐条对上：

| b1 时间 | b2 时间 | 组装后字符 | ≤12,000 | 账本结果 |
|---|---|---|---|---|
| 22:11:51 | 22:12:43 | 11,553 | 是 | 发布 user:default v143（日志 22:12:43） |
| 22:24:42 | 22:26:20 | **12,770** | 否 | job #480 第一次 `invalid artifact`（日志 22:26:20） |
| 22:35:39 | 22:37:46 | **12,539** | 否 | job #480 第二次，落 failed（日志 22:37:46） |
| 22:57:00 | 22:58:11 | **14,470** | 否 | job #483 `invalid artifact`（日志 22:58:11） |

四次的四个标题都在、顺序也对（`is_artifact` 为真），唯一的差别是**总长度越过 12,000 字符上限**。

### 1.4 已发布 artifact 的长度分布

来源：对 `base_refreshes` 全部 117 行用 `layers.render_artifact` 重渲染后量长度。

- min 125、中位 3,790、p90 9,806、max 13,403。
- 7 行在 ≥10,800（距上限 10% 以内）。
- 2 行已超过 12,000（13,393 / 13,403）却已发布——与当前校验矛盾，见「未验证」。

分布说明这不是偶发：卡体只增不减，p90 已贴着上限，越过之后每次刷新都失败。

### 1.5 重试为什么贵

1. `run_once` 取一个 job 调 `_process`（`passes/worker.py:1181-1211`）。
2. `_process` → `_merge_base`（`:1292`）→ `_merge_base_cards`（`:961-1097`）从 b1 开始整轮跑：b1 一次调用、b3/b4 两个子 agent、可能各重试一次、然后 b2 一次调用。
3. 任何异常走 `fail_job`（`store/store.py:2012-2037`），`attempts` 上限 2（`passes/worker.py:86`）。
4. 重排队的下一次 claim 重新进入 `_process`，**没有任何中间产物被复用**，b1 再从同一个窗口跑一遍。
5. 两次都失败后 `record_layer_failure` 开一条缺口，预取块据此渲染「Minir memory gaps」（`minir/__init__.py:797-819`）；本次会话开头那段缺口提示就是这条链的产物。

直接证据：job #480 的两次尝试分别留下 22:24:42 与 22:35:39 两份 b1 transcript，内容几乎一样，只是都越过了上限。

## 2. 根因

### 2.1 主因：两个上限互相矛盾（确定性失败）

| 位置 | 值 | 含义 |
|---|---|---|
| `passes/layers.py:28` | `CARD_TEXT_LIMIT = 8000` | 单个卡体的上限 |
| `passes/layers.py:231-240` | 同上，逐体校验 | 四个卡体各自合规 |
| `passes/worker.py:85` | `_MAX_STATE_CHARS = 12_000` | 整件 artifact 的上限 |
| `passes/worker.py:199-202` | `_valid_artifact` | 长度超限即 False |
| `passes/worker.py:1081-1087` | 渲染后判 False 就 raise | 整轮报废 |

四个卡体都能是 8,000 字符，整件上限却是 12,000：**「每个部分都合规」不蕴含「整件合规」**。这是校验层之间的算术缺口，不是模型的不听话。

它还是单调的：卡体只会越写越长，所以一旦越过 12,000，此后每次 base 刷新都会以同样方式失败，重试注定复现同一结果。这解释了 29 次 `invalid artifact` 与 25 条未解决缺口；81 个两次后落 failed 的 base 作业里，超限只是其中已复现的那部分（见「未验证」）。

### 2.2 次因：标题由模型写，结构由代码校验

- `render_artifact` 只负责按固定顺序拼四个 section（`passes/layers.py:491-504`），artifact 里的 `## Window Summary` 这一行却是 b3 子 agent 写出来的正文的一部分（`passes/worker.py:1007-1026`）。
- 代码只能事后检查（`summary/window_summary.py:117-126`），发现标题被翻译就**再让模型写一次**（`passes/worker.py:1023`）。
- 实测 19:40、19:44、19:54、20:00、20:10、20:16 六份 b1 答案的摘要首行都是 `## 窗口总结`，六份都没有 `## Window Summary` 这一行；对应作业落在 `invalid artifact` 与 `summary child failed after one retry` 两类里（日志 20:16:51 是后者）。

结构由模型产出、由代码拒绝（`_valid_artifact` 的注释原话是「rendered by code, never repaired」，`passes/worker.py:200`），中间只剩「重试」一个旋钮，重试又恰好是最贵的那条路。

### 2.3 与「性能」的关系

不是 SQLite、不是预取、不是调度器（本会话 `layers` 任务实测 0.01 ms，`layer_drain` 0.2 ms）。成本全部来自**确定会失败的重试**：每个重跑 4 次模型调用、平均 210 秒以上，而且结果不会变好。

## 3. 改造设计

四个设计互相独立。设计 1 与设计 2 各消掉一类失败；设计 4 改变材料进提示词的方式（prompt 只给任务与定位符，材料留在 `memory.db` 的行里，由 pass 按定位符读）；设计 3 按操作者的定法改为**分段重试**（失败只重跑一段，段落写进 `base_refreshes` 的草稿行）。批次见 §3.4。

### 设计 1：取消整件长度上限，卡体保持完整

状态：**开**（操作者 2026-10-28：「不裁卡，每个 agent 输出还是完整的」）。

**做什么**：删掉「整件 artifact ≤ 12,000 字符」这条发布判据，保留结构判据。每个 pass 的输出完整落库，不裁剪、不截断。

- `_valid_artifact`（`passes/worker.py:199-202`）只保留「非空」与 `is_artifact`（四个标题、顺序），去掉 `len(content) <= _MAX_STATE_CHARS`。
- `_MAX_STATE_CHARS` 不再参与发布判定。它现在还在两处把草稿切到 12,000（dialectic 的 draft / audit，`:1649-1651`），一并去掉——草稿完整输入（见 §3.6）。
- 单列的上限照旧：单卡体 8,000（`passes/layers.py:28`）；模板与规则由文件本身约束。

**机制核对**：29 次 `invalid artifact` 的成因是两个上限互相矛盾——四个卡体各可 8,000、整件只给 12,000——而 12,000 的来历在代码与文档里都没有记录。既然「卡体完整」是需求，被去掉的只能是那条整件上限；`is_artifact` 仍能挡住顺序错乱，`CARD_TEXT_LIMIT` 仍能挡住单列失控。

| 换来 | 放弃 |
|---|---|
| 每个 pass 的输出完整落库，卡不会逐轮变薄 | `layer_state` 的行会变大（当前中位 3,790、p90 9,806、最大 13,403；去掉上限后可能到三万字级） |
| 29 次 `invalid artifact` 直接消失，不需要「装得下」的算术，也不需要裁剪顺序与 `[trimmed]` 标记 | 读者必须分页：dialectic 读 artifact、预取读摘要都要走 `max_chars`，不能再假设一屏读完 |

**失效面**：
1. 无上限之后写侧没有闸门：某个 pass 若陷入复读，卡会一轮比一轮大。只能靠读侧 `max_chars` 与后续轮次的摘要收回；需要一条观测（连续两轮长度翻倍就告警），不是拦截。
2. `is_artifact` 只挡顺序错误，挡不住「标题齐全、内容空洞」。
3. 两行已发布的超限历史数据（13,393 / 13,403）来源仍未查清（§6）；去掉上限不会解释它们，只是让它们不再特殊。

### 设计 2：结构归代码（b3 只给正文）

**做什么**：`OUTPUT_B3` 从「整段摘要（含标题）」改为「不含 `## Window Summary` 的正文」，`render_artifact` 统一补标题；删除 `missing_section_heading` 的「再写一次」重试分支（`passes/worker.py:1007-1026`、`summary/window_summary.py:117-126`）。

**机制核对**：b1 的契约写明 `window_summary` 是「the B3 child's markdown, copied verbatim」（`passes/layers.py:121`），b3 的答案是 b1 答案的一部分，b1 本身不做任何结构加工——所以标题目前只由模型负责，代码只负责拒绝。把标题移到 `render_artifact`，等于把「结构由代码写」这条已经在卡片渲染上成立的原则延伸到摘要。

| 换来 | 放弃 |
|---|---|
| 标题漂移这一类失败归零（六份 `## 窗口总结` 的实测样本）；少一次模型调用 | b1 的 JSON 语义变了（整段 → 正文），dialectic 的 `OUTPUT_DIALECTIC` 里 `window_summary` 一栏要同步改（`:156`） |
| 「模型翻译标题」不再需要专门的检测器和重试分支，删代码而不是加代码 | 旧行仍含标题，重渲染需要一次「有标题则不再加」的兼容判断 |

**失效面**：
1. 兼容判断若写反，旧行会出现两个 `## Window Summary`，`is_artifact` 取第一个、位置仍有序，于是错行被静默接受。
2. 摘要正文若自己带一行 `## Window Summary`（模型爱复述结构），同一问题从「缺失」变成「重复」。
3. dialectic 的 `window_summary` 若继续要求整段，两条写 base 的路径会产出两种形状。

与设计 3 同批落地时，`OUTPUT_B1` 直接去掉 `window_summary` 一栏（b3 的答案不再经过 b1），上表「b1 的 JSON 语义变了」这一项随之消失；两条必须同批定稿，否则会出现「b1 转抄」与「worker 直取」两种来源。

### 设计 3：分段重试（失败只重跑那一段）

状态：**开**（操作者 2026-10-28 定为「分段重试」）。段落产物不写文件、不建新表，写进 `base_refreshes` 的草稿行。

**做什么**：把现在由 b1 顺手做的编排收回 worker，四段各自成为一次可重试的调用。

现状是反过来的：b1 被要求「拉起 b3/b4 两个子 agent，把答案抄进自己的 JSON」（`passes/worker.py:481-493`），所以 b1 一失败，四段的调用全部作废。而重试 b3/b4 时 worker 其实**已经能直接跑子 agent**（`_base_child` `:909`、`_retry_base_child` `:935-950`，b3/b4 的两次重试分别走 `:1023` 与 `:1031`）。分段重试就是把这个已经存在的直连路径变成唯一路径：

1. `b1` 只产出用户卡（`OUTPUT_B1` 现有五栏 `passes/layers.py:118-124`，去掉 `window_summary`、`value_markers`、`page_writes` 三栏——后两者本来就是 b4 答案的转抄）。
2. `b3` 产出摘要正文（与设计 2 合并后就是纯正文）。
3. `b4` 产出 markers。
4. `b2` 产出 AI 卡（输入是用户卡 + 摘要）。
5. dialectic 的 draft 与 audit 各算一段。

**段落载体就是库里的行**：`base_refreshes` 六列全部 `NOT NULL DEFAULT ''`（`store/store.py:230-246`），所以「只填已经产出的几列」天然合法；唯一索引 `(session_id, subject, window_from_ts)`（`:249-250`）让同一窗口的每次尝试落在**同一行**，不需要新表、不需要 scratch 文件、不需要清理策略。

- 每段写完 `UPDATE` 该行对应的列。
- 合并时先从行里取：列非空就用，空着或校验不过才调模型，写完再取。
- 发布时把该行 `is_latest` 翻成 1（旧行由现有发布逻辑置 0），再 publish conclusions。
- 段落不复用 digest：行的身份就是 `(session_id, subject, window_from_ts)`，输入变了窗口就变；模板与规则是随插件版本走的代码资产。

**窗口必须固定**：`_process` 现在每次把 `end` 拉到 live edge（`:1254-1262`），第二次尝试的窗口比第一次大，行的唯一键就变了、段落全部作废。首次认领时把 `(from_ts, to_ts)` 定下来，重试沿用同一窗口，下一轮 job 再取新内容。

| 换来 | 放弃 |
|---|---|
| 一次失败的价格从四段降到一段；11 次空摘要（b3）与 17 次 dialectic JSON 失败不再各烧 200 秒以上 | b1 不再自己编排：b3/b4 的调用、输入拼装、失败处理从 agent 内部挪到 worker，`OUTPUT_B1` 与 b1 提示词要同步收缩 |
| 一份材料一个来源：草稿行就是历史表里的那一行，没有副本 | 草稿行与已发布行同表，任何读者都必须按 `is_latest=1` 过滤 |
| 账本能区分「模型慢」与「模型白跑」（段名进 `jobs.error`） | 段落的复用键只有窗口，没有 prompt 版本；部署换 prompt 时正在重试的 job 会沿用旧段 |

**失效面**：
1. 草稿行被当成已发布：`is_latest=0` 的行会被至少三处现有读路径看到——`latest_base_refresh_for_subject`（`store/store.py:2425-2437`，**没有** `is_latest` 过滤，worker 用它取跨会话基线，`passes/worker.py:1375`）、`base_refreshes()` 历史列表（`:2439-2455`）、以及 external-content 的 `base_refreshes_fts`（草稿行一样会被索引）。其中第一条最危险：重试的草稿行窗口最新，会被当成下一次新会话的基线。批 2 必须把这几个查询补上 `is_latest=1`。
2. 并发：同一窗口的重试落在同一行，两个 worker 同时在跑就会后写覆盖先写。现有 `claim` 一次只跑一个 job（`_MAX_JOBS_PER_RUN`），但这条约束必须写死，不能靠默认。
3. 部分写后失败：行里已有 b1 的列、b2 的列还是空串；下一次尝试必须能识别「空串 = 未产出」，而不是把它当作「产出为空」。
4. 窗口漂移未固定之前，唯一索引会让新窗口另起一行，旧行永远停在草稿态——它不会污染发布，但会在表里堆积。

### 设计 4：用户提示词只给任务与定位符

状态：**开**（操作者 2026-10-28：「memory.db 不是有数据吗？给路径又不一定是文件路径」）。材料留在库里，prompt 里写定位符；只有插件资产与用户声明仍是文件。

**做什么**：把现在内联进 user prompt 的长文本换成定位符，并指明用哪个工具读。

材料本来就在库里有位置，逐条对得上（10-28 实测）：

| 材料 | 现在 | 定位符 | 读法 |
|---|---|---|---|
| 本窗口会话原文 | `<base_user_transcript>` ≤12,000 | `(session_id, from_ts..to_ts)` | `minir_hermes_session_search`（`_BASE_TOOL_HINT:108-120` 已教过） |
| 上一轮六段 / 本窗口已产出的段 | 内联全文 / 无 | `(session_id, subject, window_from_ts)` | 新读工具 + `base_refreshes` 行（唯一索引 `store.py:249-250`，另有 `base_refreshes_fts`） |
| 当前用户卡 / 同伴卡 | 内联 JSON ≤12,000 | `(kind=base.user_representation, key=user:default)`、`(kind=base.user_peer_card, …)` | 新读工具（`conclusions`，64 / 3 个 key） |
| 当前 AI 卡 | 内联 JSON | `(kind=base.ai_identity_card, key=agent:default:default)` | 同上（2 个 key） |
| 上一份窗口摘要 | 内联全文 | `(kind=window_summary, key=…)` | 同上（123 行） |
| 上一份 review | 内联全文 | `(kind=dialectic.review, key=…)` | 同上（65 行） |
| 已发布 artifact | 内联全文 13,403 | `(layer=base, subject=user:default)` | 新读工具（`layer_state`，每层每 subject 一行，只有当前版本） |
| 模板 / 两份 marker 规则 / agent 说明 | 内联全文 7,923 | 文件路径 | `file` 工具（这些是随包走的代码资产） |
| SOUL / identity / preference | 内联，SOUL 截 4,000 | 文件路径 | `file` 工具（用户声明文件） |
| `plugin.yaml` | 内联 1,465 | 版本号 + 2-3 个字段 | 不需要整份 |

**缺的能力（批 2 必须同批补）**：今天没有任何工具能按定位符取数。`minir_search` 是 FTS 排序检索——它能返回整行 `text`（实测取回了 2,378 字符的卡全文），但**不定键**：查 `user:default` 会同时返回 `user:5AB45AEF…` 与 `user:basic:zzh` 两行。`base_refreshes` 与 `layer_state` 完全没有读接口。所以需要三个按定位符精确取数的工具（`minir_get` / `minir_state` / `minir_window`，参数与返回见 §3.5）；这是新增能力，不是免费。

| 换来 | 放弃 |
|---|---|
| prompt 只剩任务与定位符；材料永远是库里的当前值，不再有「prompt 里的旧副本」 | 每个 pass 至少多 2-3 次工具调用；工具报错时 pass 无料可用 |
| 不用 scratch、不用清理策略、不用管副本生命周期 | 读到的可能是任务开始之后才发布的新值，同一 job 内前后不一致 |
| 一份材料一个来源，排障时定位符可复述、可回放 | 工具结果会留在对话里被后续每次调用重发，「不塞进 prompt」不等于「不占 context」 |

**失效面**：
1. 键形状不统一：用户卡按 subject（`user:default`）、AI 卡按 session（`session:<sid>:user:default`，80 个 key）。模型拼错键就静默读到别人的卡；读工具必须精确匹配并对缺失报错。
2. `layer_state` 只有当前版本（每层每 subject 一行），但**历史在 `base_refreshes`**：上一版 artifact 用 `minir_window(before_ts=<当前窗口起点>, format="text")` 取那一行渲染即得（101 行历史）。`minir_state` 只用于取当前版本，不要拿它当历史用。
3. 两种读法混在一个 prompt 里（行 vs 文件），模型可能拿错工具；定位符的写法必须让类型一眼可辨。
4. 读不到就编：定位符解析失败时，模型仍会产出卡片。必须有「取不到就明说没有」的指令与校验。

### 3.4 组合与顺序

| 批次 | 内容 | 消掉什么 |
|---|---|---|
| 批 1 | 设计 1 | 29 次 `invalid artifact`；两行超限历史数据；重跑率的主要部分 |
| 批 2 | 设计 2 + 设计 4 + 设计 3 | 设计 2 改 b3 的返回契约，设计 4 把材料换成定位符并补一个按定位符取数的工具，设计 3 把编排收回 worker 并让每段写进草稿行；三者动的是同一批 prompt、`OUTPUT_*`、读接口与段落载体 |

批 1 是一处合并函数与一处渲染函数的改动，可以先落。批 2 的三个设计改的是同一处：b1 / b2 / b3 / b4 的提示词、输出契约、定位符清单与段落载体，分开做会三次改同一批文件。

### 3.5 工具契约

原则：pass 只能通过工具拿材料，每个工具的参数与返回在代码里定义一次，prompt 只引用名字。

**A. 现有工具**（签名按当前代码；批 2 落地后 pass 的 toolset 仍是 `file` + `minir`，`runtime/tools.py:276-291`）

| 工具 | 调用者 | 参数 | 返回 | 边界 |
|---|---|---|---|---|
| `minir_hermes_session_search` | b1 / b3 / b4 / dialectic | `mode`(search/messages/around/sessions/stats)、`query`、`session_id`、`role`、`source`、`after`、`before`、`limit`≤100、`max_chars`≤4,000 | `{mode, filters, count, total, matches[], has_more, next_offset}`，每条带 `session` | 默认只覆盖本平台来源；`after`/`before` 收 epoch 或 `2h` 这类相对值（`store/session_search.py:48-100`） |
| `minir_search` | 同上 | `query`、`scope`(session/all)、`type`、`session_id`、`as_of`、`expand`、`include_superseded`、`limit`≤20 | 结论行，含整段 `text` | **排序检索，不定键**；同一个词可能命中多个 subject |
| `minir_kb_search` | b3 / b4 / dialectic | `query`、`org`、`category`、`limit`≤25 | 页面摘要 + 命中行 | 只读知识目录 |
| `minir_kb_write` | b4 / dialectic | `path`、`op`(create/append/insert/replace/delete)、`content`、`line`/`end_line`、`old`、`position`、`expect_sha` 等 | `{status, path, sha256, verified, warnings, error}` | 写后校验，失败回滚字节（`knowledge/kb_write.py:43-70`） |
| `minir_layer_history` | 同上 | `mode`(messages/search/around/stats)、`scope`、`session_id`、`seq`、`window`、`cursor`、`last_n`、`since`、`until`、`role`、`kind`、`limit` | 本层 transcript 事件 + `coverage` / `source` | 只能读本层自己的流（`agentrun/agent_history.py:3-40`） |
| `minir_agents` | b1，批 2 后由 worker 调 | `agent_name`、`user_prompts`、`system_prompts`、`context_files`、`project_dir`、`tool_sets`、`model`、`background` | job handle 或最终答案 | 每 session 3 次并发上限 |
| `file`（宿主） | 全部 | 宿主定义（读 / 写 / 列目录） | 文件内容 | 只读传入的路径；插件资产与用户声明用它 |

**B. 新增三个读工具**（定位符方案的载体，S6）

| 工具 | 参数 | 返回 | 边界 |
|---|---|---|---|
| `minir_get(kind, key, projection, include_superseded)` | `kind` 如 `base.user_representation`；`key` 如 `user:default`；`projection` 取 `text`（默认，整段）或 `paths`（只把内容里的 md 路径/标题摘出来，给 b4 看已有页面用）；`include_superseded` 默认 false | `{found, kind, key, text, lines[], updated_at, status}`（`projection=paths` 时 `text` 为空、`lines` 有值） | 精确匹配，不排序、不模糊；`found=false` 时不返回空串 |
| `minir_state(layer, subject)` | `layer` 取 base / dialectic / card_review / identity_meta；`subject` | `{found, layer, subject, version, content, updated_at}` | 只有当前版本（`layer_state` 每层每 subject 一行）；没有 version 参数 |
| `minir_window(session_id, subject, window_from_ts, window_to_ts, before_ts, after_ts, latest, include_drafts, parts, order, limit, max_chars, format)` | 见 B.1 | `{found, count, truncated, rows[{session_id, subject, window_from_ts, window_to_ts, turns, is_latest, conclusions_published_at, parts{...}}]}` | 只读；草稿行必须显式 `include_drafts=true` 才返回，且每行回显 `is_latest` |

**B.1 `minir_window` 的参数**

一行 `base_refreshes` 的六列默认全是空串、且按窗口唯一，所以「取哪一行」和「取哪几列」要分开给。每个参数都有当前代码里的消费者：

| 参数 | 类型 / 默认 | 作用 | 谁用 |
|---|---|---|---|
| `session_id` | str / 当前会话 | 限定会话；与 `subject`、`window_from_ts` 一起构成唯一键 | 全部 |
| `subject` | str / `user:<profile>` | 限定 subject（`user:default`、`user:<hash>`、`user:basic:zzh` 三种都在库里） | 全部 |
| `window_from_ts` | float / 无 | 精确取某一窗口；配合上面两个参数命中唯一行 | b1 取本窗口草稿、dialectic 取 artifact 覆盖的那一窗 |
| `window_to_ts` | float / 无 | 校验终点：与行里的 `window_to_ts` 不符就按未命中返回（防窗口漂移） | b1 / b2 重试时自检 |
| `before_ts` | float / 无 | 取该时刻**之前最近**的一行，用来拿「上一轮」 | b1 的基线卡、b3 的上一份摘要 |
| `after_ts` | float / 无 | 取该时刻**之后最早**的一行 | dialectic 取 artifact 之后的第一窗 |
| `latest` | bool / false | true 时取 `is_latest=1` 的当前行（`idx_base_refreshes_latest` 保证唯一） | b1 / b2 的基线 |
| `include_drafts` | bool / false | 是否返回 `is_latest=0` 的草稿行 | 分段重试读自己写了一半的行 |
| `parts` | [str] / 全部 | 只取指定列：`user_representation`、`user_peer_card`、`ai_self_representation`、`ai_identity_card`、`window_summary`、`value_markers`；未知列名直接报错，不静默忽略 | b1 只要用户两列 + markers、b2 只要用户卡 + 摘要、b4 只要 markers |
| `order` | newest / oldest / newest | 多行时的顺序（`limit>1` 才有意义） | dialectic 列历史窗口 |
| `limit` | int ≤10 / 1 | 返回几行 | dialectic |
| `max_chars` | int ≤4,000 / 4,000 | 每列截断上限，0 表示不截；`value_markers` 实测可到 15,843，必须能封顶 | 全部 |
| `format` | parts / text / parts | `parts` 返回列字典（JSON 解码后）；`text` 返回按 `render_artifact` 顺序拼好的可读文本 | b1 / b2 要 `parts`，dialectic 要 `text` |

返回：`{found, count, truncated, rows[]}`；每行带 `session_id / subject / window_from_ts / window_to_ts / turns / is_latest / conclusions_published_at / parts{}`。`truncated` 表示有列被 `max_chars` 截断——调用者必须知道它读到的不是全文。

三条共同约定：返回带 `found`，找不到就是找不到，不返回空字符串冒充内容；返回带来源字段（`kind/key`、`layer/subject`、`window_from_ts`），pass 引用材料时能复述出处；只读，不写。

### 3.6 每个 agent 的 user prompt 材料（落地后）

短结构在所有段都一样：`Target` / `Session` / `Window` / `REFRESH KIND` / `language` / 一句输出格式。下面是每段另外拿到的东西。规则与输出契约留在 system prompt，不在这张表里。

**B1 用户卡**

| 材料 | 定位符 | 工具与参数 | 上限 |
|---|---|---|---|
| 本窗口会话原文 | `session=<sid>, after=<from>, before=<to>` | `minir_hermes_session_search(mode="messages", role="user,assistant", limit=100, max_chars=4000)` | 工具自带上限，不再有 12,000 的 digest |
| 基线用户卡（上一轮 / 当前） | `latest=true` 或 `before_ts=<from>` | `minir_window(session_id, subject, parts=["user_representation","user_peer_card"], format="parts")` | 每列 ≤4,000 |
| 本窗口草稿（重试时） | `window_from_ts=<from>, window_to_ts=<to>` | `minir_window(..., include_drafts=true, parts=[...])` | 同上 |
| 首条用户消息（仅首刷） | 无 | 仍内联，≤2,000 | 唯一允许的窗口外证据，短，保留内联 |

不再进 prompt：模板（转给 b3）、上一份摘要（转给 b3）、两份 marker 规则（转给 b4）——b1 本来就不读它们。

**B2 AI 卡**

| 材料 | 定位符 | 工具与参数 | 上限 |
|---|---|---|---|
| 本窗口用户卡 | `window_from_ts=<from>` | `minir_window(..., parts=["user_representation","user_peer_card"])` | 每列 ≤4,000 |
| 本窗口摘要 | `window_from_ts=<from>` | `minir_window(..., parts=["window_summary"], format="text")` | ≤4,000 |
| 基线 AI 卡 | `(base.ai_identity_card, agent:<profile>:<id>)` + `(base.ai_self_representation, session:<sid>:user:<profile>)` | `minir_get(kind, key)` 两次 | 各自原文 |
| 本窗口会话原文（需要时） | 同 B1 | `minir_hermes_session_search` | 工具自带上限 |
| SOUL / identity / preference | 文件路径 | `file` | 待 A.4 第 3 条查清后定 |
| manifest | 一行：版本号 + 2-3 个部署事实 | worker 生成，不读整份 | 一行 |

不再进 prompt：整份 `plugin.yaml`（1,465 字符）、被 `_json_block` 包成 JSON 的基线卡。

**B3 窗口摘要**

| 材料 | 定位符 | 工具与参数 | 上限 |
|---|---|---|---|
| 模板 | `plugin/minir/templates/window_summary.md` | `file`（代码资产） | 188 字符 |
| 上一份摘要 | `before_ts=<from>` | `minir_window(..., parts=["window_summary"], format="text")` | ≤4,000 |
| 本窗口用户卡 | `window_from_ts=<from>` | `minir_window(..., parts=["user_representation"])` | ≤4,000 |
| 本窗口会话原文（全角色） | 同 B1 的窗口 | `minir_hermes_session_search(mode="messages", role="all")` | 工具自带上限 |

不再进 prompt：`<ai_card>`（A.4 决定删除，b3 运行时它不存在）。

**B4 价值标记**

| 材料 | 定位符 | 工具与参数 | 上限 |
|---|---|---|---|
| 两份规则 | `rules/markers-category.md`、`rules/markers-org.md` | `file` | 4,382 / 4,532 字符，按需读 |
| 上一份 markers（只给索引：路径 + 标题 + 日期，不给正文） | `(base.value_markers, session:<sid>:user:<profile>)` | `minir_get(kind, key, projection="paths")` | 全部路径；正文按需 `minir_kb_search` 查 |
| 本窗口会话原文（含 tool 结果） | 同窗口 | `minir_hermes_session_search(mode="messages", role="all")` | 工具自带上限 |
| 知识页检索 | 无 | `minir_kb_search(query, org, category)`，b4 自己查 | — |

不再进 prompt：两份规则全文内联、上一份 markers 的正文。

**dialectic（draft / audit / reconcile）**

三个 pass 共用同一份材料清单，差别只在「自己上一遍的产物」：

| 材料 | 定位符 | 工具与参数 | 上限 |
|---|---|---|---|
| base artifact（当前版） | `(base, user:default)` | `minir_state(layer="base", subject="user:default")` | 原文（当前 13,403） |
| 上一版 artifact | `before_ts=<当前窗口起点>` | `minir_window(session_id, subject, before_ts=…, format="text")` | 原文 |
| 上一份 review | `(dialectic.review, agent:<profile>:<id>)` | `minir_get(kind, key)` | 原文 |
| 窗口之后的会话 | `after=<artifact 的 window_to_ts>` | `minir_hermes_session_search(mode="messages")` | 工具自带上限 |
| chunk 摘要 | `type=window_summary` | `minir_search(...)`，或按 key 列表逐个 `minir_get` | ≤3,000 |
| identity / preference | 文件路径 | `file` | 原文 |
| 自己的草稿（audit / reconcile） | 无 | 仍内联：是本遍的产物，不是库里的材料 | **完整，不截断** |

audit = 材料清单 + 1 份**完整**草稿；reconcile = 材料清单 + 2 份**完整**草稿。草稿不设截断，也不设长度策略——所谓「审残稿」这个场景不成立（操作者 2026-10-28）。`_MAX_DIALECTIC_PROMPT_CHARS` 与 `_fit_window` 的让位式裁剪一并退场：evidence 本身就是定位符，没有可让的东西。

**三段共同的变化**：`_MAX_PROMPT_CHARS`（16,000）退场——prompt 里只剩短结构与定位符，长度由结构决定，不再由材料决定。



## 4. 决策槽位

| 槽 | 问题 | 已定 / 待定 | 理由 | 放弃 |
|---|---|---|---|---|
| S1 | 整件上限怎么办 | 已定：**取消整件长度上限**，保留结构判据（操作者 2026-10-28：不裁卡，输出完整） | 卡体完整是需求，被去掉的只能是「装得下」这条判据；12,000 的来历在代码与文档里都没有记录 | layer_state 行变大，读侧必须分页 |
| S2 | 摘要小节能否裁剪 | 已作废：设计 1 取消了裁剪，卡体与摘要都完整（见 S1） | — | — |
| S3 | 标题由谁写 | 已定：**代码写** | 模型写结构、代码只判结构，是当前唯一可确定性收敛的分工 | 旧行要兼容判断 |
| S4 | 重试以什么粒度 | 已定：**分段重试**（操作者 2026-10-28） | 失败只重跑失败那一段；不新建表，段落文件与设计 4 的材料同目录 | b1 不再自己编排，四段的调用与失败处理挪到 worker |
| S5 | 哪些内容留内联、哪些走定位符 | 已定：**短结构内联，材料给定位符**（操作者 2026-10-28） | 内联只保留任务、窗口边界、语言、输出格式一句话、trust 声明与定位符；规则与输出契约留在 system prompt。材料留在库里（行键）或文件里（插件资产与用户声明），按需读 | prompt 里看不出一条完整材料，排障要按定位符回查 |
| S6 | 是否新增按定位符取数的工具 | 已定：**是**，批 2 内落地 | 今天 `minir_search` 是排序检索（能取全文但不定键），`base_refreshes` 与 `layer_state` 没有读接口；定位符方案没有它无法执行 | 多一个要维护的读接口，且必须守住 `is_latest=1` 与精确键匹配 |
| S7 | locator 的 subject 取哪个 | 已定：**`user:default`**（操作者 2026-10-28） | 库里另有 `user:5AB45AEF…`、`user:basic:zzh` 两套 key，属于其它入口的历史数据 | 那两套 key 不参与 base 刷新的读写，直到有人明确要合并 |

## 5. 验收

| 观察 | 判据 | 证据来源 |
|---|---|---|
| 完整性不变式（批 1） | 四个卡体各取 `CARD_TEXT_LIMIT`、摘要与 markers 各取上限，组合后 `render_artifact` 的产出与输入**逐字一致**且发布成功（不再有长度拒绝） | 单元/属性测试，不调模型 |
| 三次失败回放 | 把三组 b1+b2 产物按 `render_artifact` → `_valid_artifact` 重放（本次脚本已能做到），应发布而不是 raise | 复用本次的 transcript 重放脚本 |
| 该失败类归零 | 7 天内 `assembled an invalid artifact` 计数为 0 | `jobs.error` 直方图 |
| 重跑率下降 | base 的 `attempts≥2` 占比从 42.4% 下降 | `jobs` 聚合 |
| 缺口收敛 | `layer_failures` 未解决数从 25 下降 | `layer_failures.resolved_at is null` |
| 提示词形状（批 2） | b1 / b2 的 user prompt 里不再出现任何材料正文，只有定位符（行键或资产文件路径）；prompt 字符数落在一个固定的数量级 | pass transcript 的 user 消息 |
| 无材料内联（批 2） | B1 / B2 的 prompt 里没有任何材料正文，只有短结构与定位符（≤2,000 字符）；dialectic 的 prompt 里只有定位符与本遍的完整草稿 | pass transcript 按块累加 |
| 定位符失效时降级（批 2） | 定位符取不到时不静默继续：pass 明确报"该材料不存在"，输出里不留编造内容 | 单元测试，构造缺失的行 key |
| 草稿行不可见（批 2） | `is_latest=0` 的草稿行不出现在预取块与任何读路径的结果里 | 预取块快照 + 读工具单测 |
| 标题归代码（批 2） | b3 只返回正文时，`render_artifact` 生成的摘要段带且只带一行 `## Window Summary`；六份 `## 窗口总结` 样本重放应发布 | 单元测试 + 本次六份 transcript 重放 |
| 分段重试（批 2） | 构造一次 b3 失败后重跑：该 job 只多一次 b3 调用，b1 / b2 / b4 不再出现；`jobs.error` 带 `segment=b3` | `session_model_usage` 按 job 聚合 + `jobs.error` |

注：第 1、2 条是批 1 的通过条件；第 6-10 条是批 2 的通过条件；第 3-5 条是上线后 7 天的观察项，不是任何一批的验收。

## 6. 未验证

- 29 次 `invalid artifact` 里只逐条复现了 10-28 的三次；更早的 26 次没有可回放的 pass transcript（`minir-memory/sessions` 已轮换），假定同因但未证实。
- 11 次 `base summary child failed after one retry` 与标题漂移是否同因未证实：六份 `## 窗口总结` 样本对应的作业落在 `invalid artifact` 与 `empty summary` 两类里，样本量不足以分离。
- 两行已发布的超限 artifact（13,393 / 13,403 字符）与当前 `_valid_artifact` 矛盾。可疑路径是 dialectic 侧写 base（`passes/worker.py:1525` 的 `_render_base_artifact`），未验证。
- 12,000 这个数最初的来历在代码与文档里都没有记录；它已不再作为发布判据（设计 1），来历不明不影响执行。
- 设计 2 改变 `window_summary` 的语义（设计 3 同批后 b1 不再转抄，改由 worker 直取 b3），dialectic 侧的影响面未逐行核算。
- 设计 3 的改动面未逐行核算：把 b3/b4 从 b1 内部挪到 worker 后，b1 的提示词与 `OUTPUT_B1` 要收缩到什么程度、`_base_scratch` 要不要扩成段清单，都只定了方向。
- 窗口固定在 `_process` 的具体落点未定：写进 job 行还是段清单里的 `(from_ts, to_ts)`，两种都要动 `store` 或 scratch 的写入时机。

## 7. 决策记录（原未决）

三项都在 2026-10-28 由操作者答定，已从待决表移出：

| 编号 | 原问题 | 决定 | 代价 |
|---|---|---|---|
| U1 | 批 2 怎么组成 | **设计 2 + 设计 4 + 分段重试**（设计 3 的新形态） | b1 的编排职责收回 worker，`OUTPUT_B1` 与 b1 提示词要收缩 |
| U2 | 25 条历史缺口要不要重放补写 | **不动**，作为缺口留在预取块 | 预取块会继续显示这些窗口的缺口提示，直到新窗口覆盖 |
| U3 | 整件上限怎么办 | **取消**（不再作发布判据），卡体保持完整 | layer_state 行变大，读侧分页；12,000 的来历不再需要 |

三项都没有「代码自己定」的余地：U1 是批次组成，U2 是数据修复，U3 是容量取舍。无遗留未决。

## 8. 附录 A：每个 agent 的 user prompt 材料

按「谁写这条 user prompt」分两组：B1 / B2 / dialectic 由代码拼，B3 / B4 第一次由 B1 自己写、失败后由代码兜底重试。系统提示词里的规则不在表内。

### A.1 实测三份真实 prompt

从 `sessions/default/{base,dialectic}/*.jsonl` 的 user 消息里按 `<tag>` 块量出来的，日期 10-28。

B1（21:00，`base-1792760321197`）共 **11,742 字符**

| 块 | 字符 | 谁用 |
|---|---|---|
| `markers_org` | 4,323 | 转给 b4，B1 自己不用 |
| `markers_category` | 3,600 | 转给 b4，B1 自己不用 |
| `baseline_user_card` | 2,145 | B1 的工作输入 |
| `base_user_transcript` | 306 | B1 的证据 |
| `window_template` | 187 | 转给 b3，B1 自己不用 |
| `previous_markers_path` / `window_digest_path` | 121 / 112 | 转给子 agent |
| 块外（指令、标签） | 948 | — |

B2（21:00，`base-1792760334610`）共 **6,761 字符**：`user_card` 2,251、`baseline_ai_card` 1,813、`plugin_yaml` 1,465、`base_ai_transcript` 306、`window_summary` 218、块外 708。

dialectic reconcile pass（23:26，`dialectic-1793201202755`）共 **48,306 字符**

| 块 | 字符 |
|---|---|
| `base_artifact` | 13,403 |
| `initial_draft` | 11,999 |
| `self_audit_correction` | 11,999 |
| `review_preferences` | 4,000 |
| `dialectic_transcript` | 2,219 |
| `chunk_summaries` | 2,213 |
| `review_identity` | 596 |
| `previous_review` | 542 |
| 块外 | 1,335 |

材料文件的原始大小：`window_summary.md` 188、`markers-category.md` 4,382、`markers-org.md` 4,532、`plugin.yaml` 1,544、`SOUL.md` 6,384（进 prompt 前截 4,000）。

dialectic 那份实测 48,306 字符，是声明上限 24,000 的两倍：`_fit_window` 只裁窗口块，非窗口块自己超限就原样返回（`passes/worker.py:413-433`）。两份草稿都正好 11,999，说明各被 `_MAX_STATE_CHARS` 砍掉尾部。落地后这一段的材料改由定位符给，草稿完整输入，不再有长度裁断（§3.6）。

### A.2 代码拼的三段

| agent | 进 user prompt 的材料 | 上限 / 截断 | 位置 |
|---|---|---|---|
| B1 | Target / Session / Window / Refresh kind / Baseline source / Language | 短结构，不裁 | `passes/worker.py:464-472` |
| B1 | `<first_user_message>` 首条用户消息 | 2,000（查询参数 `max_chars`） | `:473-476`、`:774-791` |
| B1 | `<window_digest_path>` / `<previous_markers_path>` | 只有路径 | `:477-482` |
| B1 | `<window_template>` 摘要模板全文 | 不裁 | `:483-485` |
| B1 | `<previous_summary>` 上一份窗口摘要全文 | 不裁 | `:486-488` |
| B1 | `<markers_category>` / `<markers_org>` 两份规则全文 | 不裁 | `:489-493` |
| B1 | `<baseline_user_card>` 基线用户卡 JSON | `_json_block` 截 12,000 | `:444-449`、`:494-495` |
| B1 | `<base_user_transcript>` 本窗口摘要 | 摘要按行累加到 12,000，再被 16,000 软顶收敛 | `:83`、`:287`、`:496-499` |
| B2 | Target / Session / Window / kind / Language | 短结构，不裁 | `:526-532` |
| B2 | `<user_card>` 本窗口用户卡 JSON | `_json_block` 12,000 | `:533` |
| B2 | `<window_summary>` 本窗口摘要全文 | 不裁 | `:534-535` |
| B2 | `<baseline_ai_card>` 基线 AI 卡 JSON | `_json_block` 12,000 | `:536` |
| B2 | `<soul_identity>`、identity / preference 文件、`<plugin_yaml>` | SOUL 截 4,000；manifest 截 8,000；两份文件不裁；空的不下发 | `:538-549`、`:600-608` |
| B2 | `<base_ai_transcript>` 本窗口摘要 | 同 B1 | `:550-553` |
| dialectic | 短结构 + `<previous_review>` | 短结构，不裁 | `:565-573` |
| dialectic | `<base_artifact>` 整件 artifact | 不裁 | `:574-575` |
| dialectic | `<review_identity>` / `<review_preferences>` 全文（`"review_" + tag`） | 不裁 | `:515`、`:577-580` |
| dialectic | `<chunk_summaries>` 分块摘要 | 3,000 | `:581-583` |
| dialectic | `<dialectic_transcript>` 窗口之后的对话摘要 | 12,000；总软顶 24,000 | `:584-587`、`:556` |
| dialectic audit（depth≥2） | 上面整份 + `<draft_to_audit>` 草稿 | 草稿截 12,000 | `:1638-1645` |
| dialectic reconcile（depth≥3） | 上面整份 + `<initial_draft>` + `<self_audit_correction>` | 两份草稿各截 12,000 | `:1647-1652` |

### A.3 模型写、代码兜底的两段

| agent | 资产声明的输入 | 首次尝试实际收到（B1 写） | 重试实际收到（代码写） |
|---|---|---|---|
| B3 `b3_window_summary` | `<window_template>`、`<user_card>`、`<ai_card>`、`<previous_summary>`、`<window_summary_transcript>`、`<language>`（`agents/b3_window_summary.md:13-20`） | 模板、上一份摘要、Window/Session 边界；窗口摘要以 `context_files` 给文件；**没有两张卡**（`agents/b1_user_card.md:43-46`） | 指令 + 标题规则 + language + 模板 + 上一份摘要；窗口摘要文件（`worker.py:1012-1025`） |
| B4 `b4_value_markers` | `<value_markers_transcript>`、`<previous_value_markers>`（units 正文）、`<markers-category>`、`<markers-org>`、`<language>`（`agents/b4_value_markers.md:14-21`） | 两份规则全文 + Window/Session 边界；窗口摘要与上一份 markers 都以 `context_files` 给文件（`agents/b1_user_card.md:47-49`） | 「Record this window's value markers.」+ 两份规则；两个文件（`worker.py:1031-1034`） |

### A.4 三处不一致与办法

**1. B3 声明了 `<user_card>` / `<ai_card>`，实际一张都没有**

- 现象：资产里写着两张卡（`agents/b3_window_summary.md:13-20`），B1 的编排指令没让给（`agents/b1_user_card.md:43-46`），重试 prompt 也没有（`passes/worker.py:1012-1025`）；`<ai_card>` 在 b3 运行时根本不存在——AI 卡由 B2 产出，B2 在 B1 之后（`:1051`）。
- 办法：删掉 `<ai_card>`；`<user_card>` 保留，但改成定位符 `minir_get(kind=base.user_representation, key=<subject>)`——b1 在 b3 之前已经把用户卡写进草稿行，读得到。
- 落点：`agents/b3_window_summary.md` 的 INPUTS 两行；`passes/worker.py:1012-1025` 的清单；`passes/layers.py:121-124` 里 OUTPUT 的注释。

**2. B4 的材料形态对不上，重试还丢了 `language`**

- 现象：资产说 `<previous_value_markers>` 是「已经在 artifact 里的 units」、`<value_markers_transcript>` 是正文（`agents/b4_value_markers.md:14-21`）；实际两样都是文件路径，重试 prompt 只有一句话加两份规则（`:1031-1034`），没有 `language`。
- 办法：声明改成定位符——上一份 markers 用 `minir_get(kind=base.value_markers, key=session:<sid>:user:default)`，窗口用 `minir_hermes_session_search(mode=messages, after/before=窗口边界)`；重试 prompt 补 `<language>`，与 b3 重试的写法对齐（`:1012-1015` 已有）。
- 落点：`agents/b4_value_markers.md`；`passes/worker.py:1029-1035`。

**3. B2 收到一份它没声明的 `<soul_identity>`，而且是空的**

- 现象：代码有 `<soul_identity>` 分支（`:538-541`），b2 的 INPUTS 里没有这一项（`agents/b2_ai_card.md:12-24`）；本次实测的 B2 prompt 里这块是空的，而 `SOUL.md` 在 `hermes_home` 根下存在（6,384 字符）。
- 办法：先查 `_read_soul_identity(hermes_home)` 收到的 `hermes_home` 到底是什么（`:600-608`），与 `SOUL.md` 的实际位置对齐；查清后二选一——路径修正后保留该块并写进 b2 的 INPUTS，或判定 SOUL 不属于 AI 卡则删块。**在查清之前不要把 SOUL 当成 b2 的已生效输入。**
- 落点：`passes/worker.py:538-541`、`:600-608`；`agents/b2_ai_card.md`。

**三处共用一个修法：材料清单只能有一个来源。** 今天有三份清单——资产的 INPUTS、b1 的 CHILD CALLS、worker 的重试 prompt——各自为准，才会漂移。批 2 之后由 worker 为每段生成一份 locator 清单（材料 + 工具名 + 参数），agent 资产的 INPUTS 从这份清单派生，重试 prompt 也用它；§3.5 的工具定义是这份清单的取值域。

## 9. 修订记录

- 一稿（2026-10-28）：实测与三个设计，S1-S3 已定，U1/U2 留给操作者。
- 二稿（2026-10-28，操作者指示）：加设计 4「用户提示词不塞长文本，材料落文件由 pass 自读」；批 2 改为设计 4 + 设计 3 同批；S1 的论据随之更新并新增 U3，S5、验收两行与三条未验证同步补上。
- 三稿（2026-10-28，操作者答复 u1/u2/u3）：批 1 只留设计 1，批 2 改为设计 2 + 设计 4，设计 3 不开；U2 不重放、U3 不调；S4 改为「不做」，§7 由待决表改为决策记录。
- 四稿（2026-10-28，操作者「u1 分段重试」）：设计 3 从「检查点表」改写为「分段重试」，状态由不开改为开并进批 2；S4 改为「分段重试」，U1 的记录同步，批次表恢复三行。
- 五稿（2026-10-28，操作者「每个 agent 用户提示词输入的材料」）：新增 §8 附录 A，逐个 agent 列出 user prompt 的材料、形态与上限，并列出资产声明与实际交付的三处不一致；原修订记录改为 §9。
- 六稿（2026-10-28，操作者「怎么这么多全文？」）：附录 A 改写为实测版——量了三份真实 prompt 的块级大小，补出 dialectic 的 audit / reconcile 两个附加块，归出三条机制（转发材料、整份搬入、上限只裁窗口块），并补一条批 2 验收「上限成立」。
- 七稿（2026-10-28，操作者「memory.db 不是有数据吗？给路径又不一定是文件路径」）：设计 4 从「材料落 scratch 文件」改为「材料留在库里、prompt 给定位符」，补出材料与行键的对照表、按定位符取数工具的缺口，新增 S6 与两条验收；设计 3 的段落载体从文件改为 `base_refreshes` 草稿行。
- 八稿（2026-10-28，操作者「A.4 给出办法，A.5 删了不属于草稿，分析里的工具在草稿定义出来」）：A.4 改为「三处不一致与办法」，逐处给做法与落点；删除 A.5（全文成因）与 A.6（对设计的含义），48,306 超限的事实并进 A.1；新增 §3.5 工具契约，定义 pass 可用的 7 个现有工具与 3 个新读工具。
- 九稿（2026-10-28，操作者「按照草稿每个 agent 用户提示词输入的材料」）：新增 §3.6，按设计落地后的形态逐段列出材料、定位符、工具参数与上限，并写明每段不再进 prompt 的东西；`minir_window` 的参数按实际消费者展开成 13 个。
- 十稿（2026-10-28，操作者「还有什么问题？」）：把已核实的两个缺口写进文档——设计 3 的草稿行会被三处不过滤 `is_latest` 的读路径看到（含跨会话基线），b4 的 markers 改为路径投影（正文 15,843 字符，截断会重复建页）；`minir_get` 增加 `projection` 参数。
- 十一稿（2026-10-28，操作者答复 3-9）：设计 1 从「份额裁剪」改为「取消整件长度上限、卡体完整」，S1/S2 随之改写，验收第一条改为完整性不变式；草稿不截断，「审残稿」策略删除；subject 定为 `user:default`（S7）；历史在 `base_refreshes`，上一版 artifact 用 `minir_window(before_ts)` 取；删掉工具形态、成本、回退、过期四处过度推演。
- 十二稿（2026-10-29，操作者「将草稿中计划内容落地」）：批 1 + 批 2 落地。整件上限与存储截断删除；b3 只写正文、标题由 `render_section` 生成；base 改为 b1/b3/b4/b2 四段，段落写进 `base_refreshes` 草稿行并在重试时复用；prompt 改为定位符 + 三个新读工具（`minir_get`/`minir_state`/`minir_window`）；单次尝试内的修复重试与 one-shot 兜底删除。与草稿的唯一偏差：草稿行用新增的 `published_at` 列标记（`is_latest=0` 同时覆盖「已发布但被新窗口降级」的历史行，单靠它无法区分草稿）。
