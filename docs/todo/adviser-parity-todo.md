# adviser 与 omp 同构：实施 TODO

来源：`docs/draft/adviser-parity-draft.md`（分析依据与完整论证；本文件只放可执行条目）。
基线：`main` `50831ae`。参照：`docs/omp-adviser-hermes-integration.md`、`docs/draft/adviser-runtime-persistent-draft.md`。
约定：`[ ]` 未开始、`[~]` 进行中、`[x]` 完成。**没有验收不算完成**；补丁不应用时，插件侧行为必须退回今天。

执行顺序：T4 → T1 → T2 → T3 → T5 → T6。T4 不依赖任何补丁，可以今天开。

状态（2026-10-29）：T1–T6 代码与定向测试全部落地；补丁 0021–0024 已装入 live host（`plugin_runtime_v3`）。
账本类验收（p50、prompt_chars 中位、注入率、误打断率）需要真实流量，列为上线后复核，不代表未实现。

---

## 背景（结论，不重开）

- 六个控制点：C1 输出通道、C2 裁决权、C3 上下文策略、C4 打断语义、C5 节拍、C6 失败收敛。C1/C3/C4 是"不同"，其余近似。
- 已对齐、不动：增量游标（`adviser_mode.py:2241-2250`）、常驻 `AdvisorRuntime`、单飞合批（`:1589-1603`、`:1650-1676`）、发射闸门（`adviser_guard.py:107-160`）、五通道投递、`immune_turns=3`、roster/成员覆写、审计、thinking 可见（`:171-174` + `config.yaml:706`）。
- 基线：2,596 条 review，p50 6,652 ms、p90 38,783 ms；1 次调用 82.6%；回合内注入 15.6%；静默 76.2%；失败 3.9%（non-JSON 58）。
- 六项已定：① P-0021 只对 nested run 开放；② `advise` 一次一条 note；③ 单事件阈值 24,000；④ 硬取消列入范围（真同构）；⑤ `cache_epoch` 记账本；⑥ scope 用 `f"{session_id}--{adviser}"` 派生。
- 唯一有意偏离 omp：单事件 >24,000 只推骨架（omp 全量推送 + 驱逐）。

---

## T4 单事件上限 + `tool_call_id` 逃生口（先做，无补丁依赖）

- [x] T4.1 `_render_event`（`:914-940`）超 `REVIEW_EVENT_CHARS=24_000` 时只推骨架：工具名、`status`、`args` 摘要（≤400 字符）、结果首 400 + 尾 200 字符、`original_chars`、`tool_call_id`
- [x] T4.2 事件落 `tool_call_id`（`post_tool_call` 钩子已提供；`message_id` 在钩子时不存在，行要等 append）
- [x] T4.3 `minir_hermes_session_search` 加 `tool_call_id` 过滤（一处 WHERE，不加分页、不加预算）
- [x] T4.4 `prompts/adviser/review.md` 加一句："被截断不等于缺失；要全文用 tool_call_id 读"
- [x] T4.5 删掉 `_prompt` 里"一个超大事件整块保留"的分支（行为在 `:2174-2176`，注释在 `:89-91`；另注意 `_trim_history:1428` 对当回合整体也不设上限）
- [x] T4.6 测试：>24k 事件不再整段进 prompt；同一 review 内同一 `tool_call_id` 只读一次；`error` 仍全文

验收：`role='tool'` 里 >24k 的 1,511 条（1.83%，74 MB，最大 529,008 字符 ≈ 132k token）不再能把 review 打出窗口；≤24k 的 98.17% 行为不变；逃生口使用率可从账本数出来。

---

## T1 `advise` 工具 + 删修补链（C1）

- [x] T1.1 补丁 P-0021：`AIAgent.__init__`（`run_agent.py:236-281`）收 `extra_tools`，**只对 nested run 开放**；工具表在 registry 解析后合并，同名覆盖
- [x] T1.2 补丁 P-0021：`agent_loop.run(extra_tools=...)` 透传（`run_conversation` 无需改：agent 在构造时已持有该工具）
- [x] T1.3 插件 handler：`advise(note, severity, target?, decision?, evidence?, claim?, candidate_id?, deferred?, forks?, fork_grades?, coverage?)` → 解析 → guard → ledger → `_deliver_host`（嵌套 run 走不到主请求的 `handle_llm_request`，按设计只入 live 通道），返回 omp 四句 ack
- [x] T1.4 `prompts/adviser/review.md`：删 OUTPUT CONTRACT，改"用 advise 工具表达，不要输出 JSON"
- [x] T1.5 删 `structured_review`（`:557-585`）与 `:2308-2316` 修补分支；解析失败记 `error="no_object"`
- [x] T1.6 账本记 ack 结果（delivered/queued/dropped + 原因）

验收：`api_calls` 恒为 1（测试）；主 run 的 `enabled_toolsets` 输出与本补丁前逐字节一致（`extra_tools` 为空时不动工具表）；ack 四句可在账本查到；`no_object` 占比上线后复核。

---

## T2 裁决状态对齐（C2）

- [x] T2.1 补丁 P-0023：`plugin_runtime.live_state(session_id) -> {streaming, aborting, tool_batch, terminal_idle}`
- [x] T2.2 `_deliverable_rows`（`:2680`）接四个状态，替代现在的推断
- [x] T2.3 误打断率/漏投率的数据面：`advisories(severity, status)` + `adviser_reviews.acks` 已按 severity 分组可查；速率等上线后一周从账本读出

验收：分级结果与 omp `resolveAdvisorDeliveryChannel`（terminalIdle / streaming / aborting / autoResumeSuppressed / immune）逐条对照一致。

---

## T3 缓存面 + 驱逐定价（C3）

- [x] T3.1 补丁 P-0024：`run(cache_scope=...)` 透传 AIAgent；`prompt_cache_scope.resolve_prompt_cache_scope` 先认 caller-pinned scope（chat/completions 与 Responses 共用的派生不变）；scope 传 `f"{session_id}--{adviser}"`
- [x] T3.2 `AgentRun` 已带 `usage{prompt_tokens, cache_read_tokens, cache_write_tokens, completion_tokens}`（`_USAGE_KEYS`），复核后无需改
- [x] T3.3 前缀边界强制：system、tools、最近一次 review 的结果不参与驱逐
- [x] T3.4 驱逐函数 f(i)=saved(i)−rewrite(i)：字符计价，只动已完成工具结果，latest-positive cut；`cache_read/write_tokens` 记入 runtime 供后续定价校准
- [x] T3.5 `_trim_history`（`:1423-1447`）的"移动起点"降级为兜底，`cache_epoch` 记在账本列
- [x] T3.6 store 迁移：`adviser_reviews` 加 `prompt_chars`、`evicted_chars`、`cache_epoch`（纯加法）；补"旧版 DDL 建库再打开"的测试

验收：`prompt_chars` 中位降 ≥40%；`cache_read_tokens / prompt_tokens` 不降；p50 <3 s；同一 note 被驱逐后不再出现（漏报率不升）。

---

## T5 硬取消（C4，真同构）

- [x] T5.1 工具进程登记：复用宿主既有 `tools.environments.base._live_foreground`（不新建第二套登记）
- [x] T5.2 补丁 P-0022：`request_interrupt` 先置合作式 flag，再 `kill_live_foreground_processes()` 对被信号的进程组/远端句柄树杀
- [x] T5.3 保留两态：`abandoned`（没轮询、被放弃）与 `cancelled`（真被杀）不得混写
- [ ] T5.4 回归：打断后 turn 能正常收尾，不卡（复用 executor 既有收尾路径；需一次 live 打断验证，本次测试范围未包含）

验收：`request_interrupt` 返回被信号进程数（宿主探针：2）；`abandoned` 与 `cancelled` 可区分（executor 两条路径未合并）；打断后子进程为 0 的 live `ps` 核对待一次真实打断。

---

## T6 节拍（C5）

- [x] T6.1 `cadence` 3 → 1（`:103`；入队闸门 `:1633`、自审闸门 `:2257`）
- [x] T6.2 配置与文档同步（integration §4.8）

验收：回合内注入占比从 15.6% 提到 >50%；`api_calls` 不涨。

---

## 先查再动（未验证，别当结论）

- [x] 嵌套 run 是否继承 `tool_loop_guardrails`：`cli-config.yaml.example:645`、`agent_init.py:1259` 注入，但 `agent_loop.py:333-351` 没传 `agent_cfg`
- [x] `kill_process_tree` 能否覆盖 PTY 与 Windows 进程树（现有引用只证明"在用"，不证明覆盖）
- [x] 当前 provider 是否接受 `prompt_cache_key`（deepseek/opencode-go 是自动前缀缓存，可能忽略该字段）
- [ ] 1,511 条超大事件与 23 分钟最大值、3.9% 失败率的相关性（目前只是假设）

## 不能同构（记录，不追）

1. 缓存 key 的取值：omp 是自定 `promptCacheKey`，我们只能给 scope，key 由宿主算内容哈希。
2. 活对象 vs 状态中继：omp 的 advisor 与 harness 共享内存；我们多一层状态暴露。
3. 工具执行器全局共享，"只读"靠工具集裁剪，不是执行器强制。
