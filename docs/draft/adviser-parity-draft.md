# adviser 与 omp 同构：完整方案（草稿 v2）

状态：已评审（2026-10-29，六项决定见 §10）。第 4 项读源码后改判为"不硬取消，同 omp 语义"（§11.2.1、§11.4、§10 #4）。实现状态见 §11 末。
**本文件在 2026-10-29 被误删后按会话记录重建**：内容与删除前一致，行级排版可能略有差异。执行清单见 `docs/todo/adviser-parity-todo.md`。
基线：`main` `50831ae`；第二轮核对在 `63c4b25`（补丁 0021–0024 已入树）。

源码位置写法（只写文件名与相对路径，不写家目录、用户名或绝对路径；行号按当前检出，代码优先于行号）：

- 本仓库：`adviser_mode.py`、`adviser_guard.py` 在 `plugin/minir/adviser/`，review 提示词在 `plugin/minir/prompts/adviser/`，补丁在 `plugin/minir/host-patches/`。
- omp：`advise-tool.ts`/`runtime.ts`/`tool-result-eviction.ts` 等在 `hermes-omp-ui/vendor/oh-my-pi/packages/coding-agent/src/advisor/`；`session-*.ts` 在 `…/src/session/`；`agent-loop.ts` 在 `packages/agent/src/`；带 `tui/` 前缀的按包内路径。
- 宿主：已安装的 hermes-agent 检出（`agent/…`、`run_agent.py`、`config.yaml`），本次核对的宿主行号来自该检出。
- 行号漂移以代码为准：标"已删"的引用指向删除前的代码，不再存在。
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
| C1 | 输出通道 | `advise` 工具，schema 保证结构化，ack 回同一轮（`advise-tool.ts:16-21`、`advise-tool.ts:192-201`） | `advise` 工具已落（`adviser_mode.py:745-747`、补丁 0021）；`structured_review` 与 JSON 修补链已删 | 已相同 |
| C2 | 裁决权 | `resolveAdvisorDeliveryChannel`（`advisor/advise-tool.ts:118`）：入参 `severity`/`continueSeverity`/`streaming`/`aborting`/`autoResumeSuppressed`/`terminalAnswerNoQueuedWork`/`interruptImmuneTurnActive`/`preserveOnly`，其中 `terminalIdle = terminalAnswerNoQueuedWork && !streaming && !aborting`（`advise-tool.ts:130`）；出参 `aside`/`steer`/`preserve` | `_deliverable_rows`（floor/immune/deferred）＋`host_bridge.live_state`（`adviser_mode.py:2936`、`adviser_mode.py:3028`） | 近似：状态输入已接，`aborting` 在用，另三项的消费未核 |
| C3 | 上下文策略 | 增量 + 超集历史 + **驱逐**：取 `saved−rewrite` 最大的切点（不是最深的合格切点），只裁 `read`/`grep`/`glob` 的 toolResult，保护最近一次 review（最新非合成 user 起），按 token 计且单条 ≥ `MIN_PRUNE_TOKENS=50`，默认 `evictStaleResults=true` 每次 review 前跑（`tool-result-eviction.ts:10,21-25,59-93`；`packages/agent/src/compaction/pruning.ts:292`；`session-advisors.ts:2309`）；切点之后连签名 thinking 一起失效（`session-advisors.ts:2305-2308`） | 增量 + 60 条/120k 窗口；`_evict_tool_results` 同名，但取**最浅**的合格切点、按字符计价、无工具名白名单、无 review 边界、只在 history > 120k 时触发 | 不同（公式相同；选点、单位、候选集、边界、触发都不同构） |
| C4 | 打断语义 | concern/blocker 走 `agent.steer()`：批次内轮询发现排队后，只对声明 `interruptible` 的纯等待工具硬中止，其余工具只收合作式 `steeringSignal`（可自行转后台），消息在下一个边界注入，**从不杀正在产生副作用的前台工具**（`agent-loop.ts:3298-3312`、`agent-loop.ts:3423-3437`）；`#abortInProgress` 是用户 Esc 的旗标，不在 advisor 路径上 | `interrupt` 停止等待，工具被放弃不是杀死（0005）；P-0022 已撤消，行为保持不变 | 同一形状；P-0022 已定撤消，保留合作式中断 |
| C5 | 节拍 | `reviewMode=turn` + `reviewInterval` 数的是 eligible primary update（每 N 个审一次，跳过的并入下次），单飞合批 | `cadence` 数的是本次 `fresh` 事件条数（`adviser_mode.py:2383-2385`），单飞合批 | 近似：计数单位不同，`cadence=3` ≠ `reviewInterval=3` |
| C6 | 失败收敛 | 去 thinking→换模型→halt；工具循环 abort | 暂停 + 连续失败清零；无换模型链 | 近似 |

判定标准：C3 仍是"不同"，C1 已相同（§5.1 已落），C2/C4/C5/C6 是"近似"。**"真同构"只剩 C3 一条**（选点、单位、候选集、边界、触发）；C4 与 omp 同形，P-0022 已撤消、保留合作式中断（§10 #4）。

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

口径说明：`trigger='scheduled'`（1,691 行）是 worker 取不到触发原因时的默认串（`adviser_mode.py:1784`），不是第三种触发；`nit` 只有 6 条是 `severity_floor="concern"`（`adviser_mode.py:113`）过滤的结果。 全时段计数随流量增长，21:30 复测为 2,623 条延迟行、p50 6,816 ms / p90 40,260 ms——口径不变，快照不同。

复现（只读）：

```sql
select api_calls, count(*) n from adviser_reviews group by api_calls;
select latency_ms from adviser_reviews where latency_ms > 0 order by latency_ms;
select sum(injected_at is not null) inj, sum(delivered_at is not null) del, count(*) n from advisories;
```

---

## 3. 已对齐的部分（不要动）

- 增量游标、per-adviser 常驻状态、独立 host session id（`adviser_mode.py:2368`、`adviser_mode.py:895`；persistent draft §1）。
- 单飞 + 合批：`_reviewing`/`_dirty`（`adviser_mode.py:1780-1798`）。
- 发射闸门：`adviser_guard.py`（omp 逐行移植）——C1 的准入面（不是 C2）。
- 五通道投递、immune、continue、preserve（integration §4.6）。
- 思考可见：`include_reasoning: True`、`max_reasoning_chars: 0`（`adviser_mode.py:180`），宿主 `plugins.stream_reasoning_deltas: true`（宿主 `config.yaml:711`）已开。
  **第二轮更正（第一轮写的"omp 输入不含推理"是错的）**：omp 的 advisor 默认就吃主 agent 的推理——`#includeThinking = true`（`runtime.ts:314`），渲染成 `_thinking:_ …`（`session-history-format.ts:573`），系统提示写着 "Receive incremental agent transcript, including thoughts"（`prompts/advisor/system.md:24`）；只有分类器拒绝（内容/政策拦截）时剥一次推理再重试，换模型身份时重新打开（`runtime.ts:1436-1455`、`runtime.ts:753-758`）。真正的差异是**作用域**：omp 把推理放在本次 delta 里随事件推送，本插件的 `<recent_reasoning>` 按会话累计、无上限（10-29 峰值 132,039 字符），是 prompt 超预算的直接原因。
- roster、成员覆写、暂停、审计（persistent draft §4）。

结论：C1 已落成工具（§5.1）；C3 仍是结构性差异；C2 的四项宿主状态已接、代码只消费了 `aborting`；C4 与 omp 同形，P-0022 已撤消。

---

## 4. 四个宿主补丁（C1、C4、C2、C3 的前提；C4 的 0022 已撤消）

**已落**：`plugin/minir/host-patches/0021`、`0023`、`0024` 在树中并随 `2b2dd36` 装入 live host（`plugin_runtime_v3`）；`0022` 已定撤消（§10 #4），撤消步骤见 P-0022 段。下面保留设计依据与验收，代码位置以补丁文件为准。

### P-0021 `extra_tools`：让嵌套 run 带一个只属于 reviewer 的工具（C1）

- 目的：输出由工具 schema 保证，ack 在同一轮返回——这就是 omp 的 `advise` 形状，也是删掉整条修补链的前提。
- 改动点（实落，见补丁 0021）：`extra_tools` 经 `run_agent.py` 进入 `agent/agent_init.py` 的构造，工具表在 `agent/tool_executor.py` 与 `agent/inline_tool_executors.py` 合并，同名覆盖；`agent_loop.run` 透传。
- 接口：`extra_tools=[{name, description, parameters, handler}]`；handler 在嵌套 run 的线程里执行，调 `AdvisorRuntime.on_advice(note, severity)` → guard → `_deliverable_rows` → 投递，然后把 ack（六条原文，§5.1）作为工具结果返回。
- 隔离要求：只对 nested run 开放（已定 §10.1），不进全局 registry；主 run 的 `enabled_toolsets` 输出必须与本补丁前逐字节一致（验收项）。
- 风险：与内建工具同名（用 `advise` 前缀避开）、schema 校验、多 run 并发（按 run 持有，不做全局注册）。
- 验收：嵌套 run 里 `advise` 可调用；`structured_review` 的调用次数归零；主 run 工具表不变。

### P-0022（已撤消）硬取消：不移植，回到 0005 的合作式语义（C4）

- 决定（2026-10-29）：**不硬取消，同 omp 语义**。omp 的 steer 只对声明 `interruptible` 的纯等待工具（`wait`/`vibe`）硬中止，其余工具拿合作式 `steeringSignal`、在下一个边界注入，注释明写 "neither queued steering nor a peer IRC ever hard-kills a partially side-effecting foreground tool (e.g. `bash`)"（`agent-loop.ts:3304-3308`）。0005 的"停止等待、被放弃不是杀死"就是与它同形的那个语义。
- 为什么不移植窄硬中止：Hermes 没有 `interruptible` 这种工具声明（宿主只有 `tools/interrupt.py` 的合作式 `is_interrupted()`，`process_manage` 的等待也不声明可中断）。没有声明面就没有"哪些可以硬中止"的判据，只能全杀——那正是撤消掉的这条。
- 撤消清单（落地时执行）：宿主 `git apply --reverse` 0022（两个 hunk：`_kill_live_tool_processes()` 与 `request_interrupt` 里的调用及 `killed` 返回）；删补丁文件与 `manifest.json` 条目；从 `host-patches.json` 的 `applied` 摘掉 0022——安装器需要 `--drop NAME`，否则下次 `--revert` 会因"state 里有、series 里没有"报错（`scripts/install_host_patches.py:477-480`）。插件的 `host_interrupt` 路由不动，那是 0005。
- 保留：`kill_live_foreground_processes()` 仍在宿主 `tools/environments/base.py:95`，供退出路径使用；撤消只断开它与 `request_interrupt` 的连接。
- 验收：打断后轮询工具看到 `is_interrupted()`；不轮询的前台命令继续跑完、结果被放弃（abandoned），进程不被杀；`abandoned`/`cancelled` 两态仍可区分；turn 正常收尾。

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

## 5. 四处插件改造（C1、C3、C6）

### 5.1 `advise` 工具 + ack（删修补链）

- `prompts/adviser/review.md` 的 OUTPUT CONTRACT 整段删除，换成"用 `advise` 工具表达；不要输出 JSON"。
- handler 复用现有链路：guard（`adviser_guard.py:107-160`，`admit`）→ `_deliverable_rows`（`adviser_mode.py:2923`）→ `handle_llm_request`（`adviser_mode.py:3469`）/`_deliver_host`（`adviser_mode.py:2999`）。
- ack 照 omp 的原文六条：`Delivered.` / `Queued for the end of the turn. Do not re-raise.`，抑制四条是整句 `Dropped: empty note.` / `Dropped: nothing actionable.` / `Dropped: already raised.` / `Dropped: this update's advice budget is spent.`（`advise-tool.ts:192-201`）。本插件现在是模板 `Dropped: {reason}`（`adviser_mode.py:747`），要对齐得换成整句。
- 这一步同时删掉 `structured_review`（原 `adviser_mode.py:557-585`，已删）与 `adviser_mode.py:2308-2316` 的修补分支（已删）：58 次 non-JSON 失败与 265 次二次调用直接归零；解析不出对象时记 `error="no_object"`，不再重问。

### 5.2 上下文策略：驱逐，且用实测缓存定价（C3）

- 成本函数 f(i)=saved(i)−rewrite(i)，其中 rewrite(i) **不靠估算**：用 P-0024 的 `cache_read_tokens` 前后差与 `cache_write_tokens` 直接算——驱逐位置 i 之后，下一次请求里从 i 起的前缀就是重写成本。
- 只驱逐已完成工具的 `result`/`args`；`error`/`status`、system、tools、最近一次 review 的结果不参与。驱逐针对历史；本回合的新内容按 §6.3 的字段级截断处理（args 一行、结果 8 KiB / 80 行），除此之外不截断。
- 没有 P-0024 时退化为按 token 估算定价（今天的做法）；P-0024 落地后同一函数切到实测，不改调用点。
- `_trim_history` 的"移动起点"随总预算层一起删除（T4.7）：omp 不丢整条事件，历史靠驱逐；`cache_epoch` 仍记在账本列（已定 §10.5）。
- 验收：`prompt_chars` 中位降 ≥40%；`cache_read_tokens / prompt_tokens` 不降；p50 <3 s；同一 note 被驱逐后不再出现（漏报率不升）。

### 5.3 失败收敛补齐（C6）

- 模型回退：宿主没有 omp 那种按角色键的 `retry.fallbackChains`（那是 omp 的配置；宿主侧是 `fallback_providers`/delegation 的 fallback）。两条路：把 `auxiliary.adviser` 指到带 fallback 的 provider（零代码），或在插件内实现"换第二个 route 重试一次"。
- 工具循环：宿主有 `tool_loop_guardrails`，但嵌套 run 不继承——`plugin/minir/agentrun/agent_loop.py:335` 构造 `AIAgent(...)` 时不传 `agent_cfg`（已核，todo「先查再动」）。omp 的对应物是 advisor 自己的 loop guard：复用主会话 `model.toolCallLoopGuard.*`，先注入一次纠正、再犯就 abort 本条 update（`loop-guard.ts:27-113`）。补齐方式：在 `run(...)` 里显式传 guardrail 配置，或在插件内做同样的 redirect→abort。
- 验收：`process ended` 之外的失败有一半被回退吃掉；`halt` 后需要显式 reset。

### 5.4 system prompt 的五段组成（C3 的装配面）

omp 的 adviser system prompt 不是一段文本，而是五段按固定顺序拼进 system 消息（`session-advisors.ts:1314-1319`）：`system.md` 恒定，其余各段有内容才注入。把它拆成五段看时，第 5 段（advice 的规则和判定）在 omp 里属于 `system.md` 内部，本插件把它拆成独立文件。段与段的差别不只是内容，还有成本归属：system 消息在同一 session 内逐字不变（配置不变时；父/子 run 的 child brief 除外），落在前缀缓存里；review 的 user 消息每次都重写，受 `MAX_PROMPT_CHARS=24,000` 约束。`soul_constraints` 这类治理内容之所以"不占 review 预算"，原因在这里，不在它的长度。

| # | 段 | omp 的出处与形状 | 本插件今天 | 实测 |
| --- | --- | --- | --- | --- |
| 1 | `system.md` | 角色、workflow、communication、`<critical>`、`<completeness>`，带 `max_notes_per_update` 占位（`prompts/advisor/system.md:1-95`） | `core.md` + `review.md` + `review_budget.md`，由 `_review_brief` 拼（`adviser_mode.py:198-228`） | 7,223 字符 |
| 2 | `context-files.md` | `<project-context>` 内嵌每个 context file 的 path 与全文（`prompts/advisor/context-files.md:1-8`、`advisor/watchdog.ts:27-32`、`sdk.ts:4753`） | `_project_context` 只返回路径，且挂在 review 的 user 消息（`adviser_mode.py:278-318`、`:2281-2283`） | 路径清单（T7.1 实测 47 字符；本机当前 cwd 只命中 1 个 AGENTS.md） |
| 3 | `memory-context` | 包住记忆后端给主 agent 的同一份 instructions，并写明"工具列表里没有就别用"（`prompts/advisor/memory-context.md:1-4`、`advisor/watchdog.ts:41-44`、`sdk.ts:4765`） | 无；同源物是 provider 的 `system_prompt_block()`（`plugin/minir/__init__.py:721-730`） | 8,315 字符（memory 契约 3,283 + 两张用户卡 5,030） |
| 4 | `active-repo-watchdog.md` | cwd 不在任何 repo 内、且恰好一个直接子目录是 git 仓库根时注入 `<attention>`；它与 WATCHDOG.md 的块拼成同一个 watchdog prompt（`utils/active-repo-context.ts:129-139`、`advisor/watchdog.ts:12-18`、`sdk.ts:4745-4749`） | 无 | 模板 258 字节 |
| 5 | advice 的规则和判定 | 就在 `system.md` 的 `<critical>`（`:30-56`）与 `<completeness>`（`:58-93`）；工具描述另在 `prompts/advisor/advise-tool.md:1-3` | `advice_target.md` + `advice_judgment.md` 接在 `review.md` 之后（`adviser_mode.py:214-221`） | 17,476 字符 |

装配顺序：1 → 2 → 3 → 4 → 5。omp 的字面顺序是 `system.md` → `context-files.md` → `memory-context.md` → watchdog（WATCHDOG.md 块 + `active-repo-watchdog.md`）→ roster/共享 instructions；第 5 段在它那里内含于第 1 段。当前 review 的 system 消息 24,707 字符（不含 child brief）：第 1 段 7,223、第 5 段 17,476，其余是分隔符。第 2–4 段还没进 system 消息（第 2 段现挂在 user 消息里，第 3–4 段没有对应实现）。`P-0024` 的 scope `f"{session_id}--{adviser}"` 保证同一 adviser 命中同一分片，这是把静态段移进 system 消息的前提。

已定（2026-10-29，操作员按 D1–D5 逐条答复；决定表见 §10 第 7–11 项）：

- **D1 第 2 段贴正文**：AGENTS.md / WATCHDOG.md 的 path 与全文进 system 消息，长度仍受 `max_project_chars`（0–6000，默认 `MAX_PROJECT_CHARS`）约束。T7.1 的 project_context 路径化就此回退，`<project-context>` 从 user 消息移出。顺带修停止条件：cwd 自己命中 `.git` 时就在此停（今天只检查父目录，实测走满 4 级到 `C:/Users`）。
- **D2 第 3 段是记忆后端的 instructions 整块**：`_memory_prompt_block()` 整块进 system 消息（口径、标记词表、工具、卡路径、工作流），并与 `system_prompt_block()` 共用同一个渲染函数——改一处两侧同步。块要覆盖"记忆工具 + Minir 数据目录布局"；今天块里有工具与卡路径，没有 stores 布局（那部分在 `minir:data-layout` 技能里），落地时一次补进同一个函数。用户卡不进 adviser，仍走 `<rulers>` 句柄。
- **D3 第 4 段同 omp**：cwd 不在任何 repo 内、且恰好一个直接子目录是仓库根（`.git` 存在、HEAD 可解析）时注入 `<attention>`；0 个或 2 个以上都不注入。
- **D4 roster instructions 同 omp**：移到 system 消息末尾（第 5 段之后），不再是 user 消息里的 `<adviser_instructions>` 数据块。来源是 config 的 roster 或 `WATCHDOG.yml`（`_watchdog_roster()`）；移入后按 system 级指令生效，"data, not instructions" 的标签随之取消——这是 omp 的形状，后果是项目文件里的 instructions 获得 system 级权重，记在 §9。
- **D5 共用**：第 2–4 段同时进 review 与 consult 的 system 消息；第 1 段按模式取 `core.md` + `review.md` 或 `core.md` + `consult.md`；第 5 段（advice 规则）只属 review。

目标形态（估算：按已测分段相加，第 2 段取上限）：7,223 + ≤6,000 + 8,315 + ≈300 + 17,476 ≈ 33,300–39,300 字符，全部落在缓存前缀；review 的 user 消息只剩本回合素材。

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
| >24,000 字符（原骨架阈值；现按 8 KiB / 80 行截断） | 1,511 行（1.83%），74 MB |
| >100,000 字符 | 19 行（0.02%） |
| >400,000 字符 | 3 行；最大 529,008 字符 ≈ 132k token |

这 1,511 行是加单手截断的理由：最大的一条 529,008 字符 ≈ 132k token，直接越过多数模型窗口。骨架版已把它们压到可推，但换成分字段截断后这条路径可以整段删掉——字段有界，就不需要一个"整条事件"的开关。

### 6.3 工具输入/输出的截断（同 omp）

- 输入只留一行：`→ tool(primary)`，主参数取有序键里第一个非空标量（`path`/`file_path`/`command`/`cmd`/`pattern`/`query`/`url`/`action`/`target`/`name`），单行压平后截到 120 字符（`session-history-format.ts:86`、`session-history-format.ts:166-230`）；普通工具的 args 正文不进 prompt。
- 输出 8 KiB / 80 行：头尾各半、中段省略，标记 `[…Nln elided…]`，单行巨块用 `[…NB elided…]`（`session-history-format.ts:88-89`、`session-history-format.ts:277-298`；标记格式 `tui/src/tools/streaming-output.ts:551-556`）。
- diff（`patch` 的结果里出现 `@@` 行）给 300 行，字节上限不变（`session-history-format.ts:90-91`）。
- `clarify` 按 omp 的 `ask` 处理：每个字符串字段 2 KiB / 20 行，整段 JSON 8 KiB（`session-history-format.ts:92-93`、`session-history-format.ts:261-275`）。
- `error` 与 result 同一上限，不给全文豁免（omp 的错误预览也过 8 KiB 的 result 段）。
- 删 `REVIEW_EVENT_CHARS` 骨架与 `_render_tool_skeleton`/`_head_tail`：字段有界之后那条路径不可达。
- 逃生口保留：`tool_call_id` 仍在工具行上，`minir_hermes_session_search` 按 id 取全文。omp 靠重跑 `read`/`grep` 拿全文，我们没有等价的重跑面，这一处是替代而非偏离——只增不减信息。
- 块形状不变（`<tool seq=… name=… status=…>`），只换字段的截断规则：信封是本插件自己的，不照 omp 的 markdown 重画。

### 6.4 与 omp 的关系

omp 的 `expandToolIO: true` 是"展开而不是摘要"，不是无上限：工具**输出**在渲染时截到 8 KiB / 80 行（diff 300 行、同一字节上限），中段省略；工具**输入**根本不展开，只有 `→ tool(primary)` 一行 120 字符（`session-history-format.ts:86-93`、`session-history-format.ts:166-230`、`session-history-format.ts:254-298`）。§6.3 已改成同一组参数。

**三处设计与 omp 一致（2026-10-29 裁定，逐条改判）**：

1. 不设总预算层：删 `MAX_PROMPT_CHARS`、"超限丢最旧整条事件"的路径（`adviser_mode.py:2293-2307`）与 `_trim_history` 的"移动起点"。omp 没有这一层——工具事件由 §6.3 的字段级截断兜住，历史由每次 review 前的驱逐兜住（第 3 条）。`max_prompt_chars` 配置项一并删除，不留别名。
2. 推理随事件走：删 `<recent_reasoning>` 会话累计块，改为每条事件块内的 `_thinking:_` 行，只带本次 delta。omp 的 `includeThinking` 在渲染 assistant 块时把 thinking 拼进去（`session-history-format.ts:573-575`），没有独立的"最近推理"区。
3. 驱逐每次 review 前跑，四要素照 `advisor/tool-result-eviction.ts`：候选＝`read`/`grep`/`glob` 的 toolResult 且 token ≥ `MIN_PRUNE_TOKENS`（`:21-26`）；保护从最新一条非合成 user 起的尾部（`:33-39`）；按 f(i)=saved(i)−rewrite(i) 取 margin 最大的切点，平坦时取较浅（`:76-95`）；原地抹成 `[Stale result elided - N tokens]`、置 `prunedAt`、失效缓存（`:97-107`）。

后果（旧口径作废）：原写"2 的累计推理先把预算吃满，1 再丢整条事件"；总预算层删除后这条因果链不存在，`recent_reasoning` 的 132,039 峰值只作为 T7.2 的现状证据（§11.3）。

## 7. 落地顺序与验收

可执行条目与状态在 `docs/todo/adviser-parity-todo.md`：T1–T6 对应本节六步，T7/T8 来自 §3 与 §5.3，T9 来自 §5.4（已定、未排期；均不在本节步骤内）；映射表在该文件顶部，本节只管顺序与验收口径。

| 步 | 动作 | 归属 | 验收（账本口径） |
| --- | --- | --- | --- |
| 1 | P-0021 + `advise` 工具 + 删修补链 | 补丁+插件 | 除工具收发外无额外调用（实测下限 2：发出 + 收尾）；`no_object` 归零；ack 原文可查 |
| 2 | P-0023 + 裁决状态对齐 | 补丁+插件 | 注入率、误打断率；分级与 omp 对照一致 |
| 3 | P-0024 缓存面 + 驱逐定价（5.2） | 补丁+插件 | 同一 adviser 同 scope；`usage.cache_read_tokens` 非零；**按 `prompt_tokens` 中位降 ≥40%**（`prompt_chars` 只含增量 prompt，量错了对象）；驱逐按 omp 语义——每次 review 前跑、token 计价、保护最近一次 review；p50 <3 s |
| 4 | 工具输入/输出截断同 omp（6.3） | 插件 | 单条结果 ≤8 KiB / 80 行（diff 300 行）、args 只一行 120 字符；529k 事件不再整段进 prompt；`tool_call_id` 仍在工具行 |
| 5 | ~~P-0022 硬取消~~ 撤消：同 omp，保留 0005 合作式中断 | 补丁回退 | 轮询工具看到 `is_interrupted`；不轮询的命令跑完、结果被放弃，进程不杀 |
| 6 | cadence 3 → 1 | 配置 | 注入率 >50% |

执行状态：1、2、3、6 已落；4 的骨架版已落、按本决定重做为 omp 形状；5 是回退。原建议顺序 4 → 1 → 2 → 3 → 5 → 6 保留作记录。
每步独立回滚：补丁接在 0010 之后编号，按编号停用；插件按 `event_cap`/`eviction` 开关停用。

---

## 8. 不能同构的两处（原写三处，第 3 条本轮证伪）

1. **缓存键的形状与继承**：omp 的 advisor 用 `host.agent.promptCacheKey ?? advisor 自己的 provider session UUID`（`session-advisors.ts:1368`）——父会话 pin 了 key 时**故意共用同一分片**；我们按 §10.6 传 `f"{session_id}--{adviser}"`、永不继承父 scope，最终 key 由宿主按 `(instructions, tools, scope)` 算成内容哈希（`auxiliary_client.py:1535-1547`）。分片独立与跨轮稳定都能做到，但 key 的取值与继承策略不等价。
2. **活对象 vs 状态中继**：omp 的 advisor 与 harness 共享内存，路由读的是活对象；我们即使有 P-0023 也多一层间接。
3. **（本轮证伪，不算差异）工具执行器是全局的**：omp 也没有独立执行器——advisor 拿的是按名单另建的同一批工具实例（`session-advisors.ts:1330-1333`），`WATCHDOG.yml` 可以给它 `write`/`edit`（`prompts/advisor/system.md:25`），"只读"同样靠工具集约定；这一条不是我们弱于 omp。

---

## 9. 风险

1. **超大事件的漏判**：只看到被省略的中段时最容易说"你没验证"。缓解：头尾都在、省略标记写明行数（`[…Nln elided…]`）、`tool_call_id` 可换全文；逃生口使用率纳入验收。
2. **逃生口被反复调用**：同一条 529k 结果可能被连读几次。缓解：同一 review 内同一 `tool_call_id` 只允许读一次，使用率进验收。
3. ~~**硬取消误伤**~~：随 P-0022 撤消失效（不改进程，也就没有"带走正在写盘的工具"这条风险）。
4. **补丁跟版**：P-0021/P-0023/P-0024 改的是宿主内部结构，宿主升级要跟着改。缓解：与 0001/0005 同样的编号与测试方式，`scripts/install.py --check` 里加对应能力探测。
5. **观测偏差**：`scheduled` 占 64%，按触发分组时必须与 `tool` 合并。
6. **roster instructions 的权重上升（D4）**：`WATCHDOG.yml` 的 instructions 从 user 消息的数据块移到 system 消息末尾，获得 system 级权重；项目文件不是操作员独占时，这等于把项目文本提到指令层。缓解：system 消息里按来源标名，并留一个关掉 `watchdog_roster` 的开关。
7. **无总预算层后的越窗面**：非工具事件（用户/助手正文）不截断，omp 同样不截；它的兜底是 advisor 自己上下文的 `maintainContext`（重置 + 重渲染有界批，`advisor/runtime.ts:1086-1105`），而本插件每次 review 是一个新请求，没有可重置的上下文。残余风险：极端长的正文事件直接触发 provider 400。要求是这个错误可见，不是被静默丢事件掩盖。

---

## 10. 已定（2026-10-29 评审）

| # | 决定 | 落到哪 |
| --- | --- | --- |
| 1 | P-0021 的 `extra_tools` 只对 nested run 开放，主 run 一律不变 | §4 P-0021 隔离要求 |
| 2 | `advise` 一次只带一条 note（与 omp 一致） | §5.1 handler 签名 |
| 3 | 单事件阈值 24,000，先跑一周看逃生口使用率再调。**2026-10-29 改判：同 omp**——工具输入只留 120 字符主参数一行，结果 8 KiB / 80 行（diff 300 行、同字节上限），`clarify` 字段 2 KiB / 20 行，`error` 不豁免；`REVIEW_EVENT_CHARS` 骨架删除 | §6.3、§7 步 4 |
| 4 | 硬取消要做——真同构，P-0022 列入范围。**2026-10-29 改判：不硬取消，同 omp 语义**——omp 的 steer 不杀在飞工具（§11.2.1），0005 的合作式中断才是同形的那个；0022 撤消，Hermes 没有 `interruptible` 声明面，窄硬中止不移植 | §4 P-0022、§7 步 5 |
| 5 | 驱逐阈值与 `cache_epoch` 记在账本（`adviser_reviews`），不记 transcript | §5.2、store DDL |
| 6 | P-0024 的 scope 用 `f"{session_id}--{adviser}"` 派生，不继承父会话 | §4 P-0024 |
| 7 | 第 2 段贴正文（受 `max_project_chars` 约束）；T7.1 的 project_context 路径化回退 | §5.4、§11.3 |
| 8 | 第 3 段用记忆后端 instructions 整块，与 `system_prompt_block()` 同源，补 stores 布局 | §5.4 |
| 9 | 第 4 段同 omp 的窄条件（cwd 不在 repo 内 + 唯一直接子仓） | §5.4 |
| 10 | roster instructions 移到 system 消息末尾，按 system 级指令生效 | §5.4、§9 |
| 11 | 第 2–4 段 review 与 consult 共用 | §5.4 |
| 12 | 三处设计与 omp 一致：删总预算层（`MAX_PROMPT_CHARS`、丢事件路径、移动起点）、推理随 delta、驱逐四要素 | §6.4、T3.5–T3.8、T4.7、T7.2 |

后果：§7 的 1–4、6 已落；第 5 步改为撤消 P-0022（第 4 项已定"不硬取消，同 omp 语义"），撤消动作列入下一批。T9（§5.4 的五段落点）已定设计、未排期，条目在 todo。

---

## 11. 修订记录

### 11.1 第一轮（2026-10-29，初读 `advisor/` 与 41 份 transcript）

依据：`hermes-omp-ui/vendor/oh-my-pi/packages/coding-agent/src/advisor/`（`settings.ts`、`advise-tool.ts`、`config.ts`、`emission-guard.ts`、`tool-result-eviction.ts`、`transcript-recorder.ts`）。

更正：

1. §1 C3：omp 驱逐与草稿同公式，但草稿漏了约束——只裁 `read`/`grep`/`glob`、保护最近一次 review、按 token 计、每次 review 前跑，且取 margin 最大切点。本插件的同名函数五处都不同（选点、单位、候选集、边界、触发）。
2. §1 C5：omp 的 `reviewInterval` 数 eligible primary update（`session-advisors.ts:673-676`），`cadence` 数 fresh 事件条数，两者单位不同。
3. §3 思考可见：第一轮写"omp 无 `includeReasoning`、输入不含主 agent 推理"是**错的**，见 §11.2.2；`recent_reasoning` 与 omp 同源，差在作用域。
4. §6.4：原写"唯一有意偏离"的那一处并不唯一，改为四处（单事件阈值、`soul_constraints` 常驻块、推理注入作用域、驱逐触发）。
5. §7 步 1 验收：工具交付下 `api_calls` 的下限是 2（发出 + 收尾），"恒 1"达不到。
6. §1 C2：状态输入已接（`live_state`），"缺状态输入"过期；代码里只消费了 `aborting`。

### 11.2 第二轮（2026-10-29，精读 `agent-loop.ts`、`session-history-format.ts`、`session-advisors.ts`、`runtime.ts`）

1. **C4 证伪**：omp 的 steer 不杀在飞工具。批次内轮询发现排队消息后，只硬中止声明 `interruptible` 的纯等待工具（`wait`/`vibe`），其余工具收合作式 `steeringSignal`（可自行转后台），消息在下一个边界注入；注释明写 side-effecting 前台工具永不被 steer 杀（`agent-loop.ts:3292-3312`、`agent-loop.ts:3423-3437`）。`#abortInProgress` 是用户 Esc 的旗标（`agent-session.ts:1170`、`agent-session.ts:9088`）。0005 的"停止等待、被放弃不是杀死"与 omp 同形；P-0022 的 kill 进程树是更硬的自选，§10 #4 已定撤消（§11.4）。
2. **omp 也注入主 agent 推理**：`#includeThinking = true`（`runtime.ts:314`），渲染为 `_thinking:_ …`（`session-history-format.ts:573`），系统提示写"including thoughts"（`prompts/advisor/system.md:24`）；只在分类器拒绝时剥一次并重试，换模型身份时重开（`runtime.ts:1436-1455`、`runtime.ts:753-758`）。差异是作用域：每次 delta vs 按会话累计。
3. **omp 有单事件上限**：`expandToolIO` 是"展开"不是"无上限"，每项工具 IO 截到 8 KiB / 80 行（diff 300 行、同字节上限），中段省略（`session-history-format.ts:86-93`、`session-history-format.ts:254-298`）。24,000 字符骨架与它同层同机制，只差阈值与单位。
4. **omp 没有独立 ToolSession**：advisor 用按名单另建的同一批工具实例（`session-advisors.ts:1330-1333`），`WATCHDOG.yml` 可授予 `write`/`edit`（`prompts/advisor/system.md:25`），"只读"同样靠工具集约定。
5. **omp 的缓存键父优先**：`advisor.agent.promptCacheKey = host.agent.promptCacheKey ?? advisorProviderSessionId`（`session-advisors.ts:1368`）——父 pin 时共用同一分片；我们的 `session--adviser` 永不继承，属于自选而非仿 omp。
6. **C6 细化**：advisor 有自己的 loop guard，复用主会话 `model.toolCallLoopGuard.*`，一次纠正后仍重复就 abort 本条 update（`loop-guard.ts:27-113`）；嵌套 run 至今没有对等物（todo「先查再动」已确认 `agent_cfg` 没透传），列为 C6 的补齐候选。

### 11.3 实测

实测（10-29，29 次 review）：prompt 中位 38,915 字符，其中 `soul_constraints` 24,227（恒定）、`recent_reasoning` 中位 4,271 / 峰值 132,039、`transcript_delta` 中位 34。同一口径 21:30 复测：10-29 共 28 行，`prompt_chars` 中位 39,855（快照随流量漂移；块级拆分来自 transcript，本轮未复算）。

配套：`docs/todo/adviser-parity-todo.md` 的 T3.3 / T3.5–T3.8 / T4.7、T6.3、T7 与"待清理"清单与本轮同步。

**2026-10-29 改判（D1）**：`project_context` 的路径化回退为正文（`soul_constraints` / `rulers` 仍是句柄），下面这组 2,466 的实测成为 T9 之前的基线。

已落：`soul_constraints`、`rulers`、`project_context` 三块改为**路径与行句柄**，正文由 reviewer 用 `read_file` / `minir_state` 自取。实测 prompt 从 27,076 字符降到 2,466，`<transcript_delta>` 重新拿到完整事件。代价是 review 多一次按需读取，收益是被审回合不再被固定块挤掉。

实现状态：补丁 0021 / 0023 / 0024 与插件改造（`advise` 工具、删修补链、驱逐、`cache_scope`）已随 `2b2dd36` 落地；单事件截断仍是骨架版，待换成 §6.3 的字段级；0022 待回退。账本类验收（p50、`prompt_tokens` 中位、注入率、误打断率）需要真实流量，见 todo 状态行。

### 11.4 决策记录（2026-10-29）

- **D1–D5 落点（同批答复）**：第 2 段贴正文 / 第 3 段用记忆 instructions 整块且与 `system_prompt_block()` 同源 / 第 4 段窄条件同 omp / roster instructions 进 system 末尾 / 第 2–4 段 review 与 consult 共用。逐条落点见 §5.4，决定表 §10 第 7–11 项。
- **adviser system prompt 五段**（操作员 2026-10-29 定）：`system.md` + `context-files.md` + `memory-context` + `active-repo-watchdog.md` + advice 的规则和判定，五段全部进 system 消息，不进 review 的 user 消息；omp 的装配点是 `session-advisors.ts:1314-1319`。本插件现状、实测长度与 D1–D5 见 §5.4。
- **#4 硬取消**：改为"不硬取消，同 omp 语义"。依据：omp 的 steer 只硬中止声明 `interruptible` 的纯等待工具（`agent-loop.ts:3292-3312`），其余走合作信号 + 边界注入；Hermes 没有 `interruptible` 声明面，窄硬中止无从落地。落地动作＝撤消 0022（清单见 §4 P-0022 段），0005 不动。
- **#3 单事件上限**：改为同 omp。依据 `session-history-format.ts:86-93`（8 KiB / 80 行、diff 300、ask 2 KiB / 20 行）、`session-history-format.ts:166-230`（输入只一行 120 字符）、`tui/src/tools/streaming-output.ts:551-556`（中段标记）。原骨架的三条理由（1.83% 超 24k、529k 峰值、逃生口）仍成立，但截断形状换成 omp 的字段级；逃生口保留。
- **预算层**：删。omp 没有总预算层，也就没有"事件优先于固定块"的配额；`MAX_PROMPT_CHARS`（超限丢最旧整条事件）与 `_trim_history` 的移动起点一并删除，`max_prompt_chars` 配置项不留别名（T4.7）。事件大小由 §6.3 的字段级截断兜住，历史由每次 review 前的驱逐兜住（T3.5–T3.8）。

### 11.5 口径修正（2026-10-29）

- **「有意偏离」作废，三处按 omp 设计改判**：第一版把「还没收口的差距」和「保留的一层」压成同一个状态，与「同构」的目的相反；第二版只改了措辞，把总预算标成"保留"。现在按操作员 2026-10-29 的裁定重写 §6.4：三处都是设计差距，设计照 omp——无总预算层（删 `MAX_PROMPT_CHARS`、丢事件路径、移动起点）、推理随事件且只带本次 delta、驱逐每次 review 前跑并取 margin 最大切点。`soul_constraints` 常驻块已按 §5.4 移出。
