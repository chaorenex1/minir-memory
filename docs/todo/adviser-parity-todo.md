# adviser 与 omp 同构：实施 TODO

来源：`docs/draft/adviser-parity-draft.md`（论证、决定与实测都在那里；本文件只是它的可执行映射）。
每个目标两级：**目标**（带草稿出处）→ **实现步骤 / 检查列表 / 关联文件**。没有检查通过不算完成。
基线：`main` `50831ae`；核对在 `63c4b25`。参照：`docs/omp-adviser-hermes-integration.md`、`docs/draft/adviser-runtime-persistent-draft.md`。
约定：`[ ]` 未开始、`[~]` 进行中、`[x]` 完成、`[x] ~~…~~` 取消或作废；补丁不应用时，插件侧行为必须退回今天。

源码位置写法（只写文件名与相对路径，不写家目录、用户名或绝对路径；行号按当前检出，代码优先于行号）：

- 本仓库：`adviser_mode.py`、`adviser_guard.py` 在 `plugin/minir/adviser/`；提示词在 `plugin/minir/prompts/adviser/`；补丁在 `plugin/minir/host-patches/`；安装器在 `scripts/install_host_patches.py`；测试在 `tests/`。
- omp：`advise-tool.ts`/`runtime.ts` 等在 `hermes-omp-ui/vendor/oh-my-pi/packages/coding-agent/src/advisor/`；`session-*.ts` 在 `…/src/session/`；`agent-loop.ts` 在 `packages/agent/src/`。
- 宿主：已安装的 hermes-agent 检出（`agent/…`、`run_agent.py`、`config.yaml`）；`host-patches.json` 是宿主的补丁安装状态。

状态（2026-10-29，第二批已落）：T1–T4、T7.2、T8、T9 的代码与测试已落；T5.4 已在 live host 撤消 0022（T5.5 的 live 回归未跑）；T6.3 口径已定。T7.3、T5.5 与全部账本类验收（p50、`prompt_tokens`、注入率、误打断率、逃生口使用率）要真实流量。补丁 0021 / 0023 / 0024 已装入 live host（`plugin_runtime_v3`），系列 23 个补丁 `--check` 全 `already applied`。2026-10-30 修 T3.3：`prompt_tokens` / `cache_read_tokens` 是 stamp 之后才追加进 `_ADDED_COLUMNS` 的，`_SCHEMA_VERSION` 没跟着从 9 抬，旧库 `version >= _SCHEMA_VERSION` 提前 return，补列从未执行，`finish_adviser_review` 每次 `no such column` 被 `_finish_safe` 吞掉，23:53 起的 review 全卡在 `running`；版本已提到 10，补了 v9 旧库补列回归测试，live 库已迁到 10，迁后 4 条 review 正常收尾。同一批加了 `observe_children`（默认 false）：子 agent（含 b3/b4 层内 pass）不再进观察范围，之前 30 次 review / 678,597 prompt tokens 全打在 pass run 上、零建议；prompt 位于 `plugins/minir/agents` 的 pass core 即使 `observe_children: true` 也不进观察范围。
基线（草稿 §11.3）：2,596 条 review，p50 6,652 ms、p90 38,783 ms；1 次调用 82.6%；回合内注入 15.6%；静默 76.2%；失败 3.9%（non-JSON 58）。账本类验收（p50、`prompt_tokens` 中位、注入率、误打断率）要真实流量，列为上线后复核，不代表未实现。
不动（已对齐，别再重开）：增量游标、常驻 `AdvisorRuntime`、单飞合批、发射闸门、五通道投递、`immune_turns=3`、roster/成员覆写、审计、thinking 可见。
下一步批次：T5.4–T5.5（回退 0022）→ T4.3–T4.7（字段级截断 + 删总预算层）→ T1.6（ack 整句）→ T8（回退与 loop guard）→ T9（system prompt 五段）。说「落地」即按此顺序开。

---

## T1 `advise` 工具替换 JSON 契约

草稿：§4 P-0021、§5.1（C1 输出通道）
状态：已落（T1.1–T1.5），T1.6 待改。

实现步骤

- T1.1 [x] 补丁 P-0021：`extra_tools` 经 `run_agent.py` 进入 `agent/agent_init.py` 的构造，**只对 nested run 开放**；工具表在 `agent/tool_executor.py`、`agent/inline_tool_executors.py` 合并，同名覆盖。
- T1.2 [x] `advise(note, severity?)` handler：guard → ledger → `_deliver_host`；嵌套 run 走不到主请求的投递路径。
- T1.3 [x] `prompts/adviser/review.md`：删 OUTPUT CONTRACT，改为"用 advise 工具表达，不要输出 JSON"。
- T1.4 [x] 删 `structured_review` 与 JSON 修补分支；解析失败记 `error="no_object"`。
- T1.5 [x] 账本记 ack 结果（delivered / queued / dropped + 原因）。
- T1.6 [x] ack 文案对齐 omp 六条整句（`advise-tool.ts:192-200`）：`Delivered.` / `Queued for the end of the turn. Do not re-raise.` / `Dropped: empty note.` / `Dropped: nothing actionable.` / `Dropped: already raised.` / `Dropped: this update's advice budget is spent.`

检查列表

- [x] 主 run 的 `enabled_toolsets` 与补丁前逐字节一致。
- [x] 除工具收发外无额外调用（下限 2：发出 + 收尾）。
- [x] ack 在账本能对到 omp 原文（六句逐字）：`ADVISE_ACKS` 与 `advise-tool.ts:192-200` 逐字一致，`test_the_advise_acks_are_omp_sentences_verbatim`。
- [ ] `no_object` 占比上线后复核（目标：归零）。

关联文件

- `plugin/minir/adviser/adviser_mode.py`
- `plugin/minir/prompts/adviser/review.md`
- `plugin/minir/host-patches/0021-Let-a-nested-run-own-a-private-tool.patch`
- `tests/test_adviser_mode.py`

---

## T2 裁决状态对齐（`live_state`）

草稿：§4 P-0023、§1 C2（裁决权）
状态：已落（T2.1–T2.4）。

实现步骤

- T2.1 [x] 补丁 P-0023：`plugin_runtime.live_state(session_id) -> {streaming, aborting, tool_batch, terminal_idle}`。
- T2.2 [x] 消费面：`_deliverable_rows`（`adviser_mode.py:2936`）只用 `aborting` 做闸门；`_deliver_host`（`adviser_mode.py:3028`）用 `live`/`aborting`；`streaming`/`tool_batch`/`terminal_idle` 只进日志，不参与通道选择。
- T2.3 [x] 对照 `advise-tool.ts:118-137` 的八项入参逐条决定用不用：`severity`、`continueSeverity`、`streaming`、`aborting`、`autoResumeSuppressed`、`terminalAnswerNoQueuedWork`、`interruptImmuneTurnActive`、`preserveOnly`。
- T2.4 [x] 数据面：`advisories(severity, status)` + `adviser_reviews.acks` 可按 severity 分组。

检查列表

- [x] 分级结果与 omp 逐条对照一致：同 rank（nit/concern/blocker）、`concern`/`blocker` 中断、`continue_severity` 同义。
- [ ] 误打断率、漏投率可从账本读出。
- [x] 八项入参逐项有"用 / 不用 + 理由"的记录：`_deliver_host` 的选择表（用 severity / continueSeverity / streaming / aborting / terminalAnswerNoQueuedWork / interruptImmuneTurnActive；不用 autoResumeSuppressed / preserveOnly，各有理由）。

关联文件

- `plugin/minir/adviser/adviser_mode.py`
- `plugin/minir/host-patches/0023-Expose-live-turn-state-to-plugins.patch`
- `plugin/minir/store/store.py`
- `tests/test_adviser_channels.py`

---

## T3 缓存面与驱逐

草稿：§4 P-0024、§5.2、§6.4 第 3 条（C3 上下文策略）
状态：已落（T3.1–T3.3、T3.5–T3.8；T3.4 作废）。

实现步骤

- T3.1 [x] 补丁 P-0024：`run(cache_scope=...)` 透传；scope 传 `f"{session_id}--{adviser}"`，不继承父会话。
- T3.2 [x] `AgentRun` 带 `usage{prompt_tokens, cache_read_tokens, cache_write_tokens, completion_tokens}`。
- T3.3 [x] 账本列 `prompt_chars`、`evicted_chars`、`cache_epoch`（store 迁移 + 旧库打开测试）。
- T3.4 [x] ~~`_trim_history` 的"移动起点"降级为兜底~~ 作废（2026-10-29）：随总预算层一起删（T4.7）。
- T3.5 [x] 驱逐函数：公式 `saved−rewrite` 已落、只动已完成工具结果；切点改取 margin 最大（omp `tool-result-eviction.ts:76-95`），不再取最浅。
- T3.6 [x] 候选集：只裁 `read`/`grep`/`glob` 的 toolResult（omp `:21-26`）；写文件、终端的输出不裁。
- T3.7 [x] 触发与边界：每次 review 前跑（等价 `evictStaleResults=true`）；保护从最新一条非合成 user 起的尾部（omp `:33-39`），不再只按 `EVICTION_TAIL_ROWS=6` 行数。
- T3.8 [x] 计价单位：按 token、`MIN_PRUNE_TOKENS=50`（omp `pruning.ts:292`），现在是字符；改写用 `[Stale result elided - N tokens]` + `prunedAt` + 失效缓存（omp `:97-107`）。

检查列表

- [ ] `prompt_tokens` 中位降 ≥40%（`prompt_chars` 只含增量 prompt，量错了对象）。
- [ ] `cache_read_tokens / prompt_tokens` 不降。
- [ ] p50 <3 s。
- [ ] `evicted_chars>0` 的行从 1/188 升到多数 review。
- [ ] 漏报率不升。

关联文件

- `plugin/minir/adviser/adviser_mode.py`（`_evict_tool_results`、`_trim_history`）
- `plugin/minir/host-patches/0024-Let-a-caller-pin-the-prompt-cache-scope.patch`
- `plugin/minir/store/store.py`
- `tests/test_adviser_mode.py`、`tests/test_adviser_history.py`

---

## T4 工具输入/输出字段级截断 + 删总预算层

草稿：§6.3、§6.4 第 1 条（C3）
状态：已落（T4.1、T4.3–T4.7；T4.2 作废）。

实现步骤

- T4.1 [x] 事件落 `tool_call_id`；`minir_hermes_session_search` 按 id 取全文（逃生口，保留）。
- T4.2 [x] ~~骨架版（`REVIEW_EVENT_CHARS=24_000`、首 400 + 尾 200）~~ 作废，见 T4.3。
- T4.3 [x] `_render_event`：args 只留 120 字符主参数一行（有序键 `path`/`file_path`/`command`/`cmd`/`pattern`/`query`/`url`/`action`/`target`/`name`）；result 8 KiB / 80 行、中段省略（`[…Nln elided…]`，单行巨块 `[…NB elided…]`）；diff 300 行。
- T4.4 [x] 删 `REVIEW_EVENT_CHARS`、`_render_tool_skeleton`、`_head_tail`；`adviser_mode.py:2295` 注释改为"事件已按字段有界"。
- T4.5 [x] 测试：529,008 字符结果 → 正文 ≤8 KiB、头尾都在、有省略标记；100 行结果 → 40+40+标记；`write_file` 的 content 不出现在 prompt；`error` 40k → 8 KiB；`tool_call_id` 仍在工具行。
- T4.6 [x] `prompts/adviser/review.md`：改为"截断处有 `[…Nln elided…]`，要全文用 tool_call_id 读"。
- T4.7 [x] 删总预算层（omp 没有这一层）：`MAX_PROMPT_CHARS`、丢最旧整条事件的路径（`adviser_mode.py:2293-2307`）、`_trim_history` 的移动起点、`max_prompt_chars` 配置项（不留别名）。

检查列表

- [x] 单事件上限由字段决定（8 KiB / 80 行、diff 300 行、args 一行）。
- [x] 529k 的 1,511 条（1.83%，最大 529,008 字符）不再能把 review 打出窗口：`test_a_529k_tool_result_is_bounded_per_field`。
- [x] 除被省略的中段外，头尾都在（测试断言 head/tail 与标记）；逃生口使用率仍待流量。
- [x] 塞 30 条大事件的历史，review 不再因超限丢事件：没有丢事件路径，`test_four_large_tool_events_all_survive_in_one_prompt`。
- [x] `rg MAX_PROMPT_CHARS plugin/` 无结果（`REVIEW_EVENT_CHARS`、`EVICTION_TAIL_ROWS`、`history_chars` 一并删除）。
- [x] 单次请求超出模型窗口时看到 provider 错误：非工具正文不再截断（代码路径已核）；真实越窗未跑，标注为未验证。

关联文件

- `plugin/minir/adviser/adviser_mode.py`
- `plugin/minir/prompts/adviser/review.md`
- `tests/test_adviser_mode.py`

---

## T5 撤回硬取消

草稿：§4 P-0022（已撤消）、§1 C4（打断语义）
状态：T5.1–T5.3 作废；T5.4 已落（live host 已撤消 0022）；T5.5 待 live 回归。

实现步骤

- T5.1–T5.3 [x] ~~曾按 P-0022 落地（进程树杀、两态区分）~~ 作废（2026-10-29）：omp 的 steer 不杀在飞工具（`agent-loop.ts:3292-3312`），0005 的合作式中断才是同形语义。
- T5.4 [x] 撤消 0022：宿主 `git apply --reverse --check` → reverse（两个 hunk）；删补丁文件与 `manifest.json` 条目；`host-patches.json` 摘掉 0022——先给安装器加 `--drop NAME`（否则 `--revert` 因 state 有、series 无而报错，`scripts/install_host_patches.py:477-480`）。
- T5.5 [~] 回归（live）：打断后轮询工具看到 `is_interrupted()`；不轮询的前台命令跑完、结果被放弃（abandoned），进程不杀；turn 正常收尾。

检查列表

- [x] `request_interrupt` 返回值回到 `{"ok": true, "reason": ""}`（不再带 `killed`）；宿主文件已无 `_kill_live_tool_processes`，`import hermes_cli.plugin_runtime` 通过。
- [ ] 一次真实打断里 `ps` 仍能看到那条命令（与 omp 同形）。
- [ ] turn 照常收尾。

关联文件

- `plugin/minir/host-patches/0022-Kill-in-flight-tool-processes-on-a-plugin-interrupt.patch`
- `plugin/minir/host-patches/manifest.json`
- `scripts/install_host_patches.py`
- `tests/test_install_host_patches.py`

---

## T6 节拍

草稿：§1 C5
状态：T6.1–T6.2 已落；T6.3 口径已定（不改代码，定义见下方"本批落地记录"）。

实现步骤

- T6.1 [x] `cadence` 3 → 1（默认值 `adviser_mode.py:112`；门限 `adviser_mode.py:2381-2385`）。
- T6.2 [x] 配置与文档同步（integration §4.8）。
- T6.3 [x] 计数单位对齐（只定口径）：omp `reviewInterval` 数 eligible primary update（`session-advisors.ts:673-676`，跳过的不算、被审回合的续跑不算），本插件 `cadence` 数 fresh 事件条数；先定口径再谈同构。

检查列表

- [ ] 回合内注入占比从 15.6% 提到 >50%。
- [ ] `api_calls` 不涨。
- [x] 一轮多事件与 adviser 触发续跑两种边界的行为可解释：见"本批落地记录"的额外交付口径。

关联文件

- `plugin/minir/adviser/adviser_mode.py`
- `docs/omp-adviser-hermes-integration.md`
- `config.yaml`（`adviser_mode.cadence`）

---

## T7 推理注入的作用域

草稿：§3、§6.4 第 2 条、§11.2.2
状态：T7.1 已落（project_context 那半已由 T9.1 回退）；T7.2 已落；T7.3 待观察；T7.4 取消。

实现步骤

- T7.1 [x] 三个文件块改为路径引用（`soul_constraints` / `rulers` / `project_context`）：内容由 reviewer 自己 `read_file`、`minir_state` 拉取。实测 prompt 27,076 → 2,466 字符，soul 块 25,240 → 383，project 1,546 → 47。
  - 注（2026-10-29）：`project_context` 的路径化被 T9.1 回退（D1 定正文），其余两块保持句柄；2,466 是 T9 之前的基线。
- T7.2 [x] `recent_reasoning` 改回合级：删累计块，改为每条事件块内的 `_thinking:_` 行，只带本次 delta（omp `session-history-format.ts:573-575`）。
- T7.3 [ ] 观察一次真实 review 是否按需读 SOUL.md（读权限与提示都已具备，行为未观察到）。
- T7.4 [x] ~~预算优先级 / `events_omitted` 判据~~ 取消（草稿 §11.4）：omp 没有总预算层。

检查列表

- [x] `<recent_reasoning>` 峰值 ≤ 8,000 字符：累计块已删，单条事件的 `_thinking:_` 上限 8,000（`max_reasoning_chars` 可调），`test_reasoning_rides_the_event_it_led_to`。
- [ ] 单次 review 的按需读取 ≤2 次。
- [ ] prompt 中位 ≤ 20,000。
- [ ] p50 不升过 3 s。
- [ ] 一次 note 以 SOUL 原文为据，并出现对应的 `read_file`（T7.3 的行为证据）。

关联文件

- `plugin/minir/adviser/adviser_mode.py`（`_reasoning`、`_reasoning_since`、`<recent_reasoning>` 块）
- `plugin/minir/prompts/adviser/review.md`
- `tests/test_adviser_history.py`

---

## T8 失败收敛

草稿：§5.3、§1 C6、§11.2.6
状态：已落（T8.1 route 回退；T8.2 loop guard）。

实现步骤

- T8.1 [x] 模型回退：`auxiliary.adviser` 指到带 fallback 的 provider（零代码），或插件内换 route 重试一次。omp 的链：分类器拒绝 → 剥主推理重试 → 换模型 → 放弃本条 update（`runtime.ts:1436-1505`）。
- T8.2 [x] 嵌套 run 的 loop guard：显式传 `tool_loop_guardrails`（`plugin/minir/agentrun/agent_loop.py:335` 现在不传 `agent_cfg`），或照 omp 做一次纠正、再犯中止（`loop-guard.ts:27-113`）。

检查列表

- [ ] `process ended` 以外的失败有一半被回退吃掉。
- [x] 重复工具调用被纠正一次、再犯中止：`test_the_loop_guard_corrects_once_then_halts`（host 控制器上 allow → warn → halt）；`terminal` 被宿主列为 failure-tolerant，永不 halt，换 `read_file` 验证。
- [ ] `api_calls` 不涨超过 1。

关联文件

- `plugin/minir/agentrun/agent_loop.py`
- `config.yaml`（`auxiliary.adviser`）
- `tests/test_adviser_runtime_persistent.py`

---

## T9 system prompt 五段

草稿：§5.4（D1–D5 已定）、§10 第 7–11 项
状态：已落（T9.1–T9.5）。

实现步骤

- T9.1 [x] 第 2 段贴正文：`_project_context` 恢复标注 + 全文，停止条件改为 cwd 或祖先命中 `.git`；`<project-context>` 从 user 消息移到 system 消息。
- T9.2 [x] 第 3 段 `memory-context`：把 `_memory_prompt_block()` 抽成与 `system_prompt_block()` 共用的渲染（一处改两侧同步），并补一段 stores 数据目录布局（来源 `minir:data-layout` 技能，不复制全文）；用户卡仍走 `<rulers>`。
- T9.3 [x] 第 4 段 `active-repo-watchdog`：按 omp 的窄条件解析（cwd 不在任何 repo 内 + 恰好一个直接子仓 + HEAD 可解析）并注入 `<attention>`。
- T9.4 [x] 第 5 段与 roster instructions：advice 规则留在 system 消息；roster instructions 从 `<adviser_instructions>` 数据块移到 system 消息末尾，按来源（config / `WATCHDOG.yml`）标名。
- T9.5 [x] consult 共用：第 2–4 段进 consult 的 system 消息（`core.md` + `consult.md` 之后）。

检查列表

- [x] system 消息里出现文件正文；user 消息不再有 `<project-context>`。
- [x] 同一 config 下两次 review 的 system 消息逐字相同；`max_project_chars` 仍生效。
- [x] adviser 的 memory 段与主 agent 的逐字相同。
- [x] 唯一直接子仓的父目录命中 `<attention>`；repo 内不出现；两个子仓不出现。
- [x] user 消息不再有 `<adviser_instructions>`；system 消息末尾出现同名指令。
- [x] consult 的 system 消息含第 2–4 段；review 的第 5 段不出现在 consult。

关联文件

- `plugin/minir/adviser/adviser_mode.py`（`_review_system`、`_project_context`、roster instructions）
- `plugin/minir/adviser/adviser_prompts.py`
- `plugin/minir/__init__.py`（`_memory_prompt_block`、`system_prompt_block`）
- `plugin/minir/prompts/adviser/`
- `tests/test_adviser_mode.py`

---

## 总验收（全部通过才算这批完成）

通道与裁决

- [x] 主 run 的 `enabled_toolsets` 与补丁前逐字节一致。
- [x] `advise` 的 ack 六句与 omp 原文逐字一致（账本字段待流量对账）。
- [ ] 除工具收发外无额外调用；`api_calls` 下限 2 可从账本读出。
- [x] 分级与裁决入参与 omp 逐条对照一致（误打断率、漏投率待流量）。

上下文与成本

- [x] 推理随事件、只带本次 delta；`<recent_reasoning>` 峰值 ≤8,000（块已删，单条上限 8,000）。
- [x] 单事件 ≤8 KiB / 80 行、diff 300 行、args 一行；529k 事件不再整段进 prompt。
- [x] 无总预算层：超长历史不丢事件（越窗时的 provider 错误由代码路径保证，未 live 验证）。
- [ ] 驱逐每次 review 前跑、取 margin 最大切点；`evicted_chars>0` 从 1/188 到多数。
- [ ] `prompt_tokens` 中位降 ≥40%；`cache_read_tokens / prompt_tokens` 不降。
- [ ] p50 <3 s；回合内注入占比 >50%；`api_calls` 不涨。

打断与失败

- [ ] 打断后轮询工具看到 `is_interrupted()`；`ps` 仍见命令；turn 收尾。
- [x] `request_interrupt` 不再返回 `killed`（宿主源码与 import 已核）。
- [ ] `process ended` 之外的失败有一半被回退吃掉；loop guard 纠正一次、再犯中止。

system prompt

- [x] 五段齐备：合同 / `<project-context>` / `<memory-context>` / `<attention>` / advice 规则。
- [x] review 的 user 消息只剩本回合素材；consult 共用第 2–4 段。

可回滚

- [x] 每个补丁可单独 `--drop` → `--revert`（`--drop` 已加并测试）；应用失败时插件行为退回今天（既有 fallback 设计，未逐补丁演练）。
- [x] 每批改动自带测试，并在落地时跑过（本批 167 条 adviser/agent_loop/installer 测试通过）。

---

## 本批落地记录（2026-10-29）

**代码**（本批改动范围，未提交：`adviser_mode.py`、`agent_loop.py`、`prompting/memory_block.py`、`__init__.py`、`store.py`、`review.md`、`manifest.json`、安装器、测试）：

- T4：`_render_event` 按字段有界（args 一行 120 字符、`clarify` 2 KiB / 20 行、result/error 8 KiB / 80 行、diff 300 行），`_head_tail` / `_render_tool_skeleton` / `REVIEW_EVENT_CHARS` / `MAX_PROMPT_CHARS` / `MAX_HISTORY_*` / `EVICTION_TAIL_ROWS` 删除；非工具正文不再截断（越窗时由 provider 报错）。
- T3：驱逐改 token 计价（`estimate_tokens`、`MIN_PRUNE_TOKENS=50`）、候选集 `read_file`/`search_files`、保护从最新非合成 user 起、margin 最大切点、每次 review 前跑；`[Stale result elided - N tokens]` 兼作幂等键；账本 `evicted_chars` 记字符、`cache_epoch` 记切点次数。
- T7.2：删 `<recent_reasoning>` 与 `_reasoning` 累计；reasoning 以 `_thinking:_` 行随事件，只带本次 delta（默认上限 8,000，`max_reasoning_chars` 可调）。
- T1.6：ack 改 `ADVISE_ACKS` 六句，与 `advise-tool.ts:192-200` 逐字一致。
- T2.2 / T2.3：`_deliver_host` 用 `live`/`streaming`/`tool_batch`/`aborting` 推出 `terminal = answered and not busy`（omp 的 `terminalAnswerNoQueuedWork`）；八项入参的选择表写在函数 docstring 里。
- T8：`agent_loop.run(tool_guardrails=...)` 用宿主控制器显式替换（`platform="subagent"` → hard stop）；`_review_one` 在被 pin 的 route 失败（非 quota/limit）时换 gateway route 重试一次。
- T9：system 消息五段（合同 / `<project-context>` 正文 / `<memory-context>` / `<attention>` / advice 规则），roster instructions 移到末尾并按来源标名；记忆块抽到 `prompting/memory_block.py`，`_memory_prompt_block()` 与 adviser 共用；consult 共用第 2–4 段。
- T5.4：安装器加 `--drop NAME`（只摘记录、不动 checkout，测试覆盖）；0022 的两个 hunk 在 live host 上撤消（见下），补丁文件与 `manifest.json` 条目删除，`manifest.json` 记 `withdrawn`。

**0022 的撤消方式**：`git apply --reverse --check` 失败——0023 的 hunk 插在 0022 尾部上下文里，反向时上下文不匹配。改为按补丁的 pre-image 精确回退两处（docstring 与 `return`）、删除 `_kill_live_tool_processes()`，再 `--drop` 记录。核对：宿主 `hermes_cli/plugin_runtime.py` 无 `"killed"` 键、无该函数，`import hermes_cli.plugin_runtime` 通过（`plugin_runtime_v3`），`request_interrupt` 的返回行是 `{"ok": True, "reason": ""}`、docstring 已回到"abandoned, not killed"。0023 里的版本注释仍提 P-0022（补丁字节不能改，否则破坏其可逆性），撤消事实记在 `manifest.json.withdrawn`。

**T6.3 的额外交付口径**（不改代码）：omp 的 `reviewInterval` 数 **eligible primary update**——一个工具批次或一次回合结束算一次；被 cadence 跳过的 update 不计数（留到下次 review），被审回合的续跑不计数（同一个 turn 已计过）。本插件 `cadence` 数 **fresh 事件条数**（游标之后的 ring 事件）。两者在“一轮多事件”时发散：3 个事件 + cadence 3 会触发一次，omp 记 3 次 eligible update 也触发一次——同结果；但 1 个工具批次里 6 个事件、cadence 3 本插件触发一次而 omp 记 1 次、不触发。边界口径：adviser 触发续跑开的新回合是同一个 `turn_id` 的延续，不重复计数（`reviewed_turn` 相同则 `review_count` 递增而不是重置）；被审核回合的续跑若换了 `turn_id` 则算新回合。改单位是另一条独立任务。

**system 消息实测**（本仓库 cwd、默认 config，字符）：

| 段 | 字符 |
| --- | --- |
| 1 合同（core + review + budget） | 7,417 |
| 2 `<project-context>`（本仓库根无 AGENTS.md/WATCHDOG.md） | 0 |
| 3 `<memory-context>` | 4,029 |
| 4 `<attention>`（cwd 在 repo 内） | 0 |
| 5 advice 规则 | 17,478 |
| roster instructions | 61 |
| 合计 | 28,991 |

草稿估的是 33,300–39,300；差额来自第 2 段为 0（本仓库没有这两个文件）和第 3 段 4,029（草稿的 8,315 含用户卡，D2 明确把卡留在 `<rulers>`）。

**测试**：`tests/test_adviser_mode.py`、`test_adviser_history.py`、`test_adviser_channels.py`、`test_adviser_runtime_persistent.py`、`test_agent_loop.py`、`test_install_host_patches.py` 共 169 条通过（本批改动后）；全量 `pytest tests` 942 条通过。

**实测发现（驱逐阈值，2026-10-29）**：把 `sessions/default/adviser/` 下 41 份历史 transcript 重建成宿主 message 形状，跑新的 `_evict_tool_results`：候选（`read_file`/`search_files`、≥50 token）合计 61,369 token，受保护尾部（最新一条 user 消息起）合计 562,184 token，**41 份里 0 份触发**。原因不是实现错误：omp 的公式是单请求口径，只要尾部比“更早的陈旧结果”大，任何切点都不划算；本插件的一次 review prompt 是 5–20k token（尾部），而 adviser 自己读的 read/search 结果通常 0–2k token。这直接意味着 T3 的“`evicted_chars>0` 占多数 review”在长会话里也要累积很多轮才可能成立。按已定语义保留 omp 公式**未改**；是否改成多请求视界（`saved × 预期 review 次数`）或把尾部换成“上一次 review 的 delta 而非整条 prompt”，是操作员的决定，记录在此。

**仍未验证**（要真实流量或 live 回合）：全部账本类验收（`prompt_tokens` 中位、p50、注入率、误打断率、逃生口使用率）、T5.5 的打断 live 回归、T7.3 的按需读 SOUL 行为、越窗 provider 错误的现场观察。

---

## 附 A 已核实的前提（别当假设）

- [x] 嵌套 run 的 `tool_loop_guardrails`：宿主 `agent_init.py` 的 `AIAgent(...)` 没有 `agent_cfg` 参数，它从 `load_config_readonly()` 建控制器，所以嵌套 run 只继承文件里的默认（交互档：只警告、不硬停）→ T8.2 改为在 `agent_loop.run(tool_guardrails=...)` 里显式替换控制器。
- [x] 宿主没有 `interruptible` 工具声明面（`tools/interrupt.py` 只有合作式 `is_interrupted()`）→ 窄硬中止不可移植，T5 的依据。
- [x] `kill_live_foreground_processes()` 在宿主 `tools/environments/base.py:95`；撤消 0022 只断开它与 `request_interrupt` 的连接。
- [x] ~~`kill_process_tree` 能否覆盖 PTY / Windows 进程树~~ 随 T5 作废，不再需要。
- [x] 安装器 `revert_series` 对"state 有、series 无"的条目直接报错（`scripts/install_host_patches.py:477-480`）→ 回退必须先 `--drop`。
- [ ] 1,511 条超大事件与 23 分钟最大值、3.9% 失败率的相关性（目前只是假设；T4 落地后重测）。

## 附 B 两处不能同构（草稿 §8，不设任务）

- 缓存 key 的取值与继承：omp 在父会话 pin 了 key 时直接共用分片；我们永不继承父 scope，key 由宿主算内容哈希。
- 活对象 vs 状态中继：omp 的 advisor 与 harness 共享内存；我们多一层状态暴露。

## 附 C 待清理（先标记后清理）

| 位置 | 是什么 | 为什么清 | 前置 |
| --- | --- | --- | --- |
| `adviser_mode.py:472-560` `parse_review` + `_json_object` | JSON 契约解析与修补链的残留 | `advise` 工具上线后生产不再调用，只有 `tests/test_adviser_mode.py:646-647` 还在测它 | 无 |
| `adviser_mode.py:2507` `legacy = _json_object(...)` | 退役契约探测 | 只用于把"模型还在吐 JSON"记成 `no_object` | `no_object` 归零一周后 |
| [x] ~~`_evict_tool_results` + `_trim_history` 字符预算~~ | 已按 T3.5–T3.8 重写：token 计价、候选集、margin 最大切点、每次 review 前跑 | — | 已清 |
| [x] ~~`_render_event` 骨架分支 + `_render_tool_skeleton`/`_head_tail`~~ | 已删；字段级截断接手 | — | 已清 |
| [x] ~~`_reasoning` / `<recent_reasoning>`~~ | 已删；`_reasoning_since` 保留为事件级 delta（上限 8,000） | — | 已清 |
| `plugins/minir-adviser/__init__.py:279` 的 `/adviser` 命令 | 仍在注册的命令插件 | `config.yaml:396` 与 `README.md:38` 都写"命令已删除、配置是唯一开关"，与实现矛盾 | 决定删插件或改文档 |
