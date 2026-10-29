# adviser 与 omp 同构：完整方案（草稿 v2）

状态：已评审（2026-10-29，六项决定见 §10）。未改一行代码。
**本文件在 2026-10-29 被误删后按会话记录重建**：内容与删除前一致，行级排版可能略有差异。执行清单见 `docs/todo/adviser-parity-todo.md`。
基线：`main` `50831ae`。工作树另有 knowledge 页行列上限的未提交改动，与本稿无关。
本稿取代 v1（仅批次 1）。v1 的问题是把已定案的东西当缺口、并且没有覆盖"补丁级同构"与"超大事件"两条路线。

---

## 0. 沿革与依赖

| 文档 | 状态 | 已定下、本稿不重开 |
| --- | --- | --- |
| `docs/omp-adviser-hermes-integration.md` | §1–§9 落地 | 能力映射 §3、五通道 §4.6、发射层 §4.5、背压 §4.7、照搬不了 §5、切片 §6、风险 §7、宿主补丁 §9 |
| `docs/draft/adviser-runtime-persistent-draft.md` | 已实现 2026-10-22 | 常驻 `AdvisorRuntime`、history 回传、独立 host session id、**字段不截断**、24k 只丢整事件、turns_ago/deferred、turn-end 聚合、暂停 |
| `docs/draft/adviser-brain-to-hand-draft.md` | P0–P4 已实现 | claim 认领、重复抑制、blame 计数、回灌 card_review |
| `docs/draft/adviser-child-observation-draft.md` | 已落地 2026-10-28 | 后台 minir agent 的观察口径 |
| `docs/draft/adviser-knowledge-link-draft.md` | 契约部分落地 | 记忆引用后缀、检索映射、路径校验 |
| `docs/draft/adviser-prompt-draft.md`、`adviser-session-modes-draft.md` | 草稿/已落地 | 单一主 adviser、`adviser_consult` 与 review 两入口、research/implement 模式 |

不重开：五通道投递、EmissionGuard、常驻 runtime、claims/forks、turns_ago/deferred、roster、暂停、宿主补丁 0001/0005/0006/0010 的语义。

---

## 1. 同构的定义：六个控制点，不是"行为接近"

| # | 控制点 | omp 的做法 | 我们的对应物 | 现状 |
| --- | --- | --- | --- | --- |
| C1 | 输出通道 | `advise` 工具，schema 保证结构化，ack 回同一轮 | JSON 散文 + 解析 + 修补链 | 不同 |
| C2 | 裁决权 | 宿主 `resolveAdvisorDeliveryChannel`（terminalIdle/streaming/aborting/autoResumeSuppressed/immune） | `_deliverable_rows`（floor/immune/deferred） | 近似，缺状态输入 |
| C3 | 上下文策略 | 增量 + 超集历史 + **驱逐**（f=saved−rewrite），不截断新内容 | 增量 + 60 条/120k 窗口，不驱逐 | 不同 |
| C4 | 打断语义 | `host.abortInProgress()` 硬中止在飞工具 | `interrupt` 停止等待，工具被放弃不是杀死 | 不同 |
| C5 | 节拍 | 每回合末审（interval 1），单飞合批 | `cadence=3`，单飞合批 | 近似 |
| C6 | 失败收敛 | 去 thinking→换模型→halt；工具循环 abort | 暂停 + 连续失败清零；无换模型链 | 近似 |

判定标准：C1、C3、C4 是"不同"，其余是"近似"。**"真同构"= 把 C1/C3/C4 变成"相同"**，C2/C5/C6 是参数与补齐。

---

## 2. 基线（实测，不是估算）

口径：`minir-memory/memory.db` 全时段，延迟取 `latency_ms > 0` 的 2,596 行。

| 指标 | 值 |
| --- | --- |
| p50 / p90 / mean | 6,652 ms / 38,783 ms / 20,240 ms |
| 调用次数 | 0 次 49、1 次 2,143（82.6%）、2 次 265、≥3 次 139 |
| 1 / 2 / 3 / 5 次调用均值 | 11.7 s / 55.3 s / 89.0 s / 100.2 s |
| 按触发 | tool 30.2 s、scheduled 12.8 s、final 85.1 s |
| 结果 | silent 2,006（76.2%）、advised 514、failed 103（3.9%） |
| 失败 | non-JSON 58、process ended 36 |
| 投递 | delivered 376（62%）、injected 94（15.6%）、expired 94、dropped 22 |
| 严重度 | blocker 120、concern 478、nit 6 |

口径说明：`trigger='scheduled'`（1,691 行）是 worker 取不到触发原因时的默认串（`adviser_mode.py:1662`），不是第三种触发；`nit` 只有 6 条是 `severity_floor="concern"`（`:104`）过滤的结果。

复现（只读）：

```sql
select api_calls, count(*) n from adviser_reviews group by api_calls;
select latency_ms from adviser_reviews where latency_ms > 0 order by latency_ms;
select sum(injected_at is not null) inj, sum(delivered_at is not null) del, count(*) n from advisories;
```

---

## 3. 已对齐的部分（不要动）

- 增量游标、per-adviser 常驻状态、独立 host session id（`:2241-2250`、`:2303`；persistent draft §1）。
- 单飞 + 合批：`_reviewing`/`_dirty`（`:1589-1603`、`:1650-1676`）。
- 发射闸门：`adviser_guard.py`（omp 逐行移植）——C1 的准入面（不是 C2）。
- 五通道投递、immune、continue、preserve（integration §4.6）。
- 思考可见：`include_reasoning: True`、`max_reasoning_chars: 0`（`:171-174`），宿主 `plugins.stream_reasoning_deltas: true`（`config.yaml:706`）已开——与 omp 的 `includeReasoning` 默认值等价。
- roster、成员覆写、暂停、审计（persistent draft §4）。

结论：结构上已完成九成；缺的是 C1、C3、C4 三条结构性差异，以及 C2 的状态输入。

---

## 4. 四个宿主补丁（C1、C3、C4、C2 的前提）

### P-0021 `extra_tools`：让嵌套 run 带一个只属于 reviewer 的工具（C1）

- 目的：输出由工具 schema 保证，ack 在同一轮返回——这就是 omp 的 `advise` 形状，也是删掉整条修补链的前提。
- 改动点：`AIAgent.__init__`（`run_agent.py:236-281`，现在只收 `enabled_toolsets`）新增 `extra_tools`；工具表构建在 registry 解析之后合并，同名覆盖；`run_conversation` 与 `agent_loop.run`（`:282-293`）透传。
- 接口：`extra_tools=[{name, description, parameters, handler}]`；handler 在嵌套 run 的线程里执行，调 `AdvisorRuntime.on_advice(note, severity)` → guard → `_deliverable_rows` → 投递，然后把 omp 那四句 ack 作为工具结果返回。
- 隔离要求：只对 nested run 开放（已定 §10.1），不进全局 registry；主 run 的 `enabled_toolsets` 输出必须与本补丁前逐字节一致（验收项）。
- 风险：与内建工具同名（用 `advise` 前缀避开）、schema 校验、多 run 并发（按 run 持有，不做全局注册）。
- 验收：嵌套 run 里 `advise` 可调用；`structured_review` 的调用次数归零；主 run 工具表不变。

### P-0022 硬取消：把"停止等待"升级为"终止批次"（C4）

- 目的：omp 的 steer 是 `abortInProgress()`；补丁 0005 写的是"停止等待、不轮询的工具被放弃、不是杀死"。要同强度就必须真的终止。
- 改动点：工具执行循环登记本批次子进程；`request_interrupt` 先置合作式 flag（保留现有语义），再对活着的子进程 `kill_process_tree`（`agent/deadline.py:422`、`hermes_cli/_subprocess_compat.py:738` 已有实现，`tools/environments/base.py:770` 已在用）。
- 必须保留：`abandoned` 与 `cancelled` 是两种终态，0005 的注释就是为这个区别写的；不要把"没轮询、被放弃"写成"被杀掉"。
- 风险：Windows 进程树、PTY、正在写盘的工具；需要"杀掉之后 turn 能正常收尾"的回归。
- 验收：打断后子进程数为 0（`ps`/`Get-CimInstance` 核对）；`abandoned`/`cancelled` 两态仍可区分。

### P-0023 `live_state`：把宿主状态交给插件（C2）

- 目的：omp 的路由函数直接读活对象（terminalIdle/streaming/aborting）。我们现在靠推断，推错就投错级。
- 改动点：`plugin_runtime` 增加 `live_state(session_id) -> {streaming, aborting, tool_batch, terminal_idle}`。
- 风险：低。验收：`_deliverable_rows` 的四个条件与宿主状态一一对应。

### P-0024 `cache surface`：把缓存身份与命中率交给插件（C3 的输入）

- 现状（比原先判断的好）：宿主已有 `agent/prompt_cache_scope.py`（rotation-stable 逻辑 scope：压缩 lineage 的根、fork 隔离、`/new` 换 scope），并且已有测试断言 background review 与父 agent 共享同一 scope（`tests/agent/test_background_review_cache_parity.py:514-519`）——所以 adviser 大概率已经和主会话同一分片。缺三样：① `prompt_cache_key` 只在 Codex/Responses 辅助路径落请求（`auxiliary_client.py:1535-1547`，取值是 `(instructions, tools, scope)` 的内容哈希），chat/completions 路径不送；② `AgentRun` 不带用量（`agent_loop.py:67-79` 只有 answer/api_calls/model/provider/toolsets/messages），驱逐无法定价；③ system/tools 不参与驱逐只是约定，代码没有强制。
- 改动点三件：
  1. `run(...)` 接受 `cache_scope`，透传给 AIAgent；provider 支持时落 `prompt_cache_key`（chat/completions 与 Responses 共用一处派生）。scope 传 `f"{session_id}--{adviser}"`（已定 §10.6），最终 key 仍由宿主按 `(instructions, tools, scope)` 算。
  2. `AgentRun` 增 `usage: {prompt_tokens, cache_read_tokens, cache_write_tokens, completion_tokens}`，取本 run 的增量（宿主 `turn_usage` 已在记这些字段）。
  3. 声明前缀边界：system 段、tools 段与"最近一次 review 的结果"永不参与驱逐，驱逐只发生在 user/tool 消息上。
- 为什么叫"开放"而不是"新增缓存"：缓存本来在 provider 侧；宿主缺的是把 scope 交出去、把命中率交回来。这两样正是驱逐定价的输入。
- 风险：scope 选错会让 adviser 与主 agent 抢同一分片（互相驱逐）；provider 不支持 `prompt_cache_key` 时退化为按前缀命中（DeepSeek 这类自动缓存），此时 scope 只影响观测。
- 验收：同一 adviser 的连续 review 落在同一 scope（日志可查）；`AgentRun.usage.cache_read_tokens` 非零且随会话增长；不同 adviser 的 scope 不同。


---

## 5. 三处插件改造（C1、C3、C6）

### 5.1 `advise` 工具 + ack（删修补链）

- `prompts/adviser/review.md` 的 OUTPUT CONTRACT 整段删除，换成"用 `advise` 工具表达；不要输出 JSON"。
- handler 复用现有链路：guard（`adviser_guard.py:107-160`，`admit`）→ `_deliverable_rows`（`adviser_mode.py:2680`）→ `handle_llm_request`（`:3207`）/`_deliver_host`（`:2751`）。
- ack 文案照 omp 四句，便于对照：`Delivered.` / `Queued for the end of the turn. Do not re-raise.` / `Dropped: empty|noise|duplicate|rate-limit`。
- 这一步同时删掉 `structured_review`（`:557-585`）与 `:2308-2316` 的修补分支：58 次 non-JSON 失败与 265 次二次调用直接归零；解析不出对象时记 `error="no_object"`，不再重问。

### 5.2 上下文策略：驱逐，且用实测缓存定价（C3）

- 成本函数 f(i)=saved(i)−rewrite(i)，其中 rewrite(i) **不靠估算**：用 P-0024 的 `cache_read_tokens` 前后差与 `cache_write_tokens` 直接算——驱逐位置 i 之后，下一次请求里从 i 起的前缀就是重写成本。
- 只驱逐已完成工具的 `result`/`args`；`error`/`status`、system、tools、最近一次 review 的结果不参与。驱逐针对历史；本回合的新内容按 §6 的单事件上限处理（>24k 只进骨架），除此之外不截断。
- 没有 P-0024 时退化为按 token 估算定价（今天的做法）；P-0024 落地后同一函数切到实测，不改调用点。
- `_trim_history`（`:1423-1447`）的"移动起点"只在实测判定"不移动就爆预算"时使用，`cache_epoch` 记在账本列（已定 §10.5）。
- 验收：`prompt_chars` 中位降 ≥40%；`cache_read_tokens / prompt_tokens` 不降；p50 <3 s；同一 note 被驱逐后不再出现（漏报率不升）。

### 5.3 失败收敛补齐（C6）

- 模型回退：宿主没有 omp 那种按角色键的 `retry.fallbackChains`（那是 omp 的配置；宿主侧是 `fallback_providers`/delegation 的 fallback）。两条路：把 `auxiliary.adviser` 指到带 fallback 的 provider（零代码），或在插件内实现"换第二个 route 重试一次"。
- 工具循环：宿主已有 `tool_loop_guardrails`（`cli-config.yaml.example:645`，`agent_init.py:1259` 注入），但嵌套 run 构造时没传 `agent_cfg`（`agent_loop.py:333-351`）——先确认是否继承；不继承就在 `run(...)` 里显式传。
- 验收：`process ended` 之外的失败有一半被回退吃掉；`halt` 后需要显式 reset。

---

## 6. 当前回合的超大事件（原"检索式输入"的缩减版）

### 6.1 结论：拉取那半套不需要，单事件上限不能删

原提案有两只手：一只手砍当前 delta 里的大结果（给指针），一只手给拉取工具把人拉回来。缓存开放 + 驱逐之后，第二只手确实边际收益很低——驱逐管历史，缓存让重复前缀不再重复付费，专门再加一个 `minir_transcript_read` + 分页 + 拉取预算，收益配不上复杂度。

但第一只手不能一起删，因为它挡的不是成本，是**失败**。

### 6.2 实测（state.db，只读）

| 事实 | 值 |
| --- | --- |
| `role='tool'` 行 | 82,709 |
| 中位行字符数 | 944 |
| >24,000 字符（单条就超过 review 的整个 prompt 预算） | 1,511 行（1.83%），74 MB |
| >100,000 字符 | 19 行（0.02%） |
| >400,000 字符 | 3 行；最大 529,008 字符 ≈ 132k token |

现在的 `_render_event` 对 `result`/`args` 零截断，`_prompt` 还写明"一个超大事件整块保留"（`:2174-2176`，注释在 `:89-91`）——于是这 1,511 行里的任何一条进了 review，prompt 就超出 24k 预算；最大那条约 132k token，直接越过多数模型窗口。这不是"贵一点"，是 review 根本跑不出来。

### 6.3 保留的最小集

- 事件超 `REVIEW_EVENT_CHARS`（24,000，已定 §10.3，与 `MAX_PROMPT_CHARS` 同量级）时，只推骨架：工具名、`status`、`args` 摘要（≤400 字符）、结果首 400 + 尾 200 字符、`original_chars`、`tool_call_id`。
- `tool_call_id` 是逃生口：`post_tool_call` 已经提供它，而 `message_id` 要等宿主 append 之后才存在，钩子里拿不到。给 `minir_hermes_session_search` 加一个 `tool_call_id` 过滤（一处 WHERE），reviewer 用它取全文——不新增工具、不新增分页、不新增预算。
- `error` 全文保留；≤24,000 字符的事件照旧整段推送（占 98.17%）。
- 提示加一句："被截断不等于缺失；要全文用 tool_call_id 读"。

### 6.4 与 omp 的关系

omp 是 `expandToolIO: true` 全量推送 + 驱逐；它在单条超大输出面前同样会爆上下文，只是它的工具结果本身有上限（read/grep 都是摘要式返回）。我们保留单事件上限，是为了在"工具能吐 529k 字符"的现实里活得下去，不是为了省钱。

**这是本稿唯一有意偏离 omp 的一处**，评审时应单独确认：换来的是"review 永不超窗"，代价是 reviewer 对超大输出只看得到骨架。


---

## 7. 落地顺序与验收

| 步 | 动作 | 归属 | 验收（账本口径） |
| --- | --- | --- | --- |
| 1 | P-0021 + `advise` 工具 + 删修补链 | 补丁+插件 | `api_calls` 恒 1；`no_object` 归零；ack 四句可查 |
| 2 | P-0023 + 裁决状态对齐 | 补丁+插件 | 注入率、误打断率；分级与 omp 对照一致 |
| 3 | P-0024 缓存面 + 驱逐定价（5.2） | 补丁+插件 | 同一 adviser 同 scope；`usage.cache_read_tokens` 非零；`prompt_chars` 中位降 ≥40%；p50 <3 s |
| 4 | 单事件上限 + `tool_call_id` 逃生口（6.3） | 插件 | >24k 事件只进骨架；review 不再出现超窗 prompt；逃生口使用率可查 |
| 5 | P-0022 硬取消（真同构，已定） | 补丁 | 打断后子进程 0；`abandoned`/`cancelled` 可区分 |
| 6 | cadence 3 → 1 | 配置 | 注入率 >50% |

执行顺序建议：4 → 1 → 2 → 3 → 5 → 6（4 是唯一不依赖补丁的一步）。
每步独立回滚：补丁接在 0010 之后编号，按编号停用；插件按 `event_cap`/`eviction` 开关停用。

---

## 8. 不能同构的三处（写实）

1. **缓存键的形状**：omp 是每个 advisor 一个显式 `promptCacheKey`；我们按 §10.6 传 `f"{session_id}--{adviser}"` 作为 scope 交给宿主解析（`prompt_cache_scope.py`），最终 key 由宿主按 `(instructions, tools, scope)` 算成内容哈希（`auxiliary_client.py:1535-1547`）。分片独立、跨轮稳定都能做到，但 key 的取值不是我们能任写的字符串——行为等价，取值不等价。
2. **活对象 vs 状态中继**：omp 的 advisor 与 harness 共享内存，路由读的是活对象；我们即使有 P-0023 也多一层间接。
3. **工具执行器是全局的**：omp 的 advisor 用独立 ToolSession 做隔离；我们靠工具集裁剪 + `read_only`，"只读"不是执行器强制。

---

## 9. 风险

1. **超大事件的漏判**：只看到骨架时最容易说"你没验证"。缓解：骨架带 `original_chars` 与首尾、提示写死"截断≠缺失"、逃生口使用率纳入验收。
2. **逃生口被反复调用**：同一条 529k 结果可能被连读几次。缓解：同一 review 内同一 `tool_call_id` 只允许读一次，使用率进验收。
3. **硬取消误伤**：kill 进程树可能带走正在写盘的工具。缓解：先 flag 后 kill，`cancelled` 只用于真正被中止的批次，测试覆盖"被放弃"与"被杀"两种。
4. **补丁跟版**：P-0021/P-0022 改的是宿主内部结构，宿主升级要跟着改。缓解：与 0001/0005 同样的编号与测试方式，`scripts/install.py --check` 里加对应能力探测。
5. **观测偏差**：`scheduled` 占 64%，按触发分组时必须与 `tool` 合并。

---

## 10. 已定（2026-10-29 评审）

| # | 决定 | 落到哪 |
| --- | --- | --- |
| 1 | P-0021 的 `extra_tools` 只对 nested run 开放，主 run 一律不变 | §4 P-0021 隔离要求 |
| 2 | `advise` 一次只带一条 note（与 omp 一致） | §5.1 handler 签名 |
| 3 | 单事件阈值 24,000，先跑一周看逃生口使用率再调 | §6.3 `REVIEW_EVENT_CHARS` |
| 4 | 硬取消要做——目标是真同构，不是够用；P-0022 列入范围 | §4 P-0022、§7 步 5 |
| 5 | 驱逐阈值与 `cache_epoch` 记在账本（`adviser_reviews`），不记 transcript | §5.2、store DDL |
| 6 | P-0024 的 scope 用 `f"{session_id}--{adviser}"` 派生，不继承父会话 | §4 P-0024 |

后果：§7 六步全部在范围内，没有"可选"项；p50 目标与硬取消验收都要跑满。
