# adviser 成本与时机：在既有实现之上的三个缺口（草稿 v1，已被 `adviser-parity-draft.md` 取代）

状态：待评审。未改一行代码。
基线：`main` `50831ae`。工作树另有 `plugin/minir/knowledge/knowledge.py`、`kb_write.py` 与两个测试文件的未提交改动（取消知识页行列上限），与本草稿无关。
本稿位置：**不是**从零设计，是对已有实现与已有决策的一次增量提案。凡是标注"已定案"的条目，评审不需要重新讨论，除非显式进入 §6 的修订请求。

---

## 0. 沿革与依赖（先读这一节，否则会重复劳动）

本仓关于 adviser 已经有六份文档和两轮落地，本稿只在它们的空隙里提改动：

| 文档 | 状态 | 它已经定下的事（本稿不重开） |
| --- | --- | --- |
| `docs/omp-adviser-hermes-integration.md` | §1–§9 已落地 | 能力映射（§3）、五条投递通道 A–E（§4.6）、发射护栏（§4.5）、背压策略 `off/wait`（§4.7）、照搬不了的部分（§5）、实施切片 P1–P5（§6）、风险清单（§7）、宿主补丁 0001/0002（§9） |
| `docs/draft/adviser-runtime-persistent-draft.md` | 已实现（2026-10-22） | 每 `<session>--<adviser>` 一份常驻 `AdvisorRuntime`；history 传回 `agent_loop.run`；独立 host session id；事件字段**不截断**；超 24k 只丢最旧整事件；`turns_ago`/`deferred`/boundary guidance；turn-end 聚合；roster 无上限；暂停策略 |
| `docs/draft/adviser-brain-to-hand-draft.md` | P0–P4 已实现（2026-10-25） | claim 认领、重复 claim 抑制、blame 计数、claim 回灌 card_review |
| `docs/draft/adviser-child-observation-draft.md` | 已落地（2026-10-28） | adviser 观察后台 minir agent 的口径 |
| `docs/draft/adviser-knowledge-link-draft.md` | 契约部分落地 | 建议附带记忆引用的后缀格式、检索映射、路径校验 |
| `docs/draft/adviser-prompt-draft.md` / `adviser-session-modes-draft.md` | 草稿/已落地 | 统一主 adviser、`adviser_consult` 与 review 两入口、research/implement 两种会话模式 |

因此本稿**不重开**：五通道投递、EmissionGuard、常驻 runtime、claims/forks、turns_ago/deferred、roster、暂停、宿主补丁边界。它只处理三件事：**一次 review 的调用形态、输出契约的体量、以及到达时机**。三者都不在既有文档的"已定案"列表里，属于落地后暴露的新缺口（见 §4 的实测）。

---

## 1. 问题定义：什么叫"与 omp 相同"

不定义为"功能一样"，定义为六个可观察属性，每条都能从账本读出：

| # | 属性 | omp 的表现 | 本插件实测 |
| --- | --- | --- | --- |
| P1 | 一次 review 的模型调用数 | 通常 1 次 | 1 次占 82.6%，2 次以上 404 次 |
| P2 | 单次 review 的墙钟 | 秒级（1 次 prefill + ≤80 token 输出） | p50 6.6 s、p90 38.8 s |
| P3 | 到达时机 | aside 在下个 step 边界、concern 打断 | 回合内注入仅 15.6% |
| P4 | 沉默是常态 | 大多数 review 不发声 | 76.2% silent（已达标） |
| P5 | 不重复、不空话 | 发射闸门硬拦 | `adviser_guard.py` 已逐行移植（已达标） |
| P6 | 可回滚 | 每个机制单点配置 | 部分（`sync_backlog`、`cadence`、`history_chars` 可调；调用形态不可） |

结论：P4、P5 已相同（对应 integration §4.5 与 §6 的 P2 切片）；P1、P2、P3 是本稿的战场；P6 是落地方式的要求。

---

## 2. 实测基线（新证据，既有文档里没有）

口径：`minir-memory/memory.db` 全时段；延迟取 `latency_ms > 0` 的 2,596 行。

| 指标 | 值 |
| --- | --- |
| p50 / p90 / mean | 6,652 ms / 38,783 ms / 20,240 ms |
| 最大 | 1,397,448 ms（23 分钟） |
| 调用次数 | 0 次 49、1 次 2,143（82.6%）、2 次 265、≥3 次 139 |
| 1 次调用 | 均值 11.7 s |
| 2 次调用 | 均值 55.3 s |
| 3 次调用 | 均值 89.0 s |
| 5 次调用 | 均值 100.2 s |
| 按触发 | tool 30.2 s、scheduled 12.8 s、final 85.1 s |
| 结果 | silent 2,006（76.2%）、advised 514、failed 103（3.9%）、dropped 9 |
| 失败原因 | non-JSON 58、process ended 36 |
| 投递 | delivered 376（62%）、injected 94（15.6%）、expired 94、dropped 22、pending 18 |
| 严重度 | blocker 120、concern 478、nit 6 |

两条口径说明：

- `trigger='scheduled'`（1,691 行）不是第三种触发，是 worker 从 `_next_trigger` 取值取不到时的默认串（`adviser_mode.py:1662`）；分触发看时应与 `tool` 合并。
- `nit` 只有 6 条是 `severity_floor="concern"`（`:104`）的过滤结果，不是 reviewer 不提 nit。

```sql
select api_calls, count(*) n, round(avg(latency_ms)) avg_ms
  from adviser_reviews where latency_ms > 0 group by api_calls;
select latency_ms from adviser_reviews where latency_ms > 0 order by latency_ms;
select sum(injected_at is not null) inj, sum(delivered_at is not null) del, count(*) n
  from advisories;
```

---

## 3. 已经做对、不要动的地方（对照 integration §3/§6 与 persistent draft）

| 机制 | 现状 | 依据 |
| --- | --- | --- |
| 增量感知 | 每 adviser 独立 cursor，delta 渲染，含 progress 标记 | integration §4.3；`adviser_mode.py:2241-2250` |
| 常驻状态 | 每 `<session>--<adviser>` 一份 `AdvisorRuntime`，history 与 cursor 跨 review 保留 | persistent draft §1 |
| 模型会话 | 每次 review 新建嵌套 `agent_loop.run`，独立 host session id | persistent draft §1；`:2296-2306` |
| 合批 | `_reviewing`/`_dirty`：在飞只置脏，结束后重排一次 | `:1589-1603`、`:1650-1676` |
| 发射护栏 | 归一化/noise/去重/预算/4096 FIFO | `adviser_guard.py`（integration §4.5 已验收） |
| 投递 | A `llm_request` 注入、B aside、C `pre_tool_call` 拦工具、D `pre_verify` 续跑、E preserve；immune/deferred/turns_ago | integration §4.6、§6 P3/P4 |
| 失败与暂停 | usage/429 暂停 `pause_seconds`；连续 3 次失败暂停；进程退出 join worker | persistent draft §4；integration §4.9 |
| roster | 无上限，成员可覆盖 model/toolsets/cadence，可 `paused` | persistent draft §4 |
| 观测 | `adviser_reviews` 逐次记录 status/api_calls/latency/model；`advisories` 全生命周期 | store |

所以"与 omp 相同"这件事，**结构上已经完成**，剩下的是量纲问题。

---

## 4. 三个缺口（诊断）

### 4.1 调用形态：一次调用就贵，第二次更贵（P1、P2）

- 82.6% 的 review 只调一次模型，均值仍 11.7 s。输入是"历史 + 全量 delta"，不是 omp 那种"增量几 KB"：`history_messages=60` / `history_chars=120_000`（`:179-180`）经 `history=` 传入 `agent_loop.run`（`:2302`）；`MAX_PROMPT_CHARS=24_000`（`:72`）；事件环 `MAX_RING=96`（`:70`）。
- 1→2 次调用，均值 11.7 s→55.3 s。触发器是 58 次 non-JSON 走 `structured_review`（`:2308-2316`），而它自己最多再发 **2** 次请求（`json_schema` 失败再退 `json_object`，`:580-585`）。"修补一次"最坏是 3 次计费调用，正是 ≥3 次那 139 行的来源。
- 这不是 omp 的形状：omp 的 advise 是一个工具调用，结构化由工具 schema 保证，不存在"先散文再重述"。

### 4.2 输出契约：比设计时定的重（P1、P2）

- `docs/omp-adviser-hermes-integration.md` §4.4 设计的输出是 `advices[]`，字段 `note/severity/target/evidence/confidence`。
- 落地后的 `prompts/adviser/review.md` OUTPUT CONTRACT 已是：`decision` 散文 + `evidence[]` + `claim`（claims 落地后必填）+ `candidate_id` + 可选 `forks[].action.args`（单个 ≤2 KB）+ `fork_grades[].reason`。每条 advice 的输出体积比设计时大一个量级，而输出 token 直接进墙钟。
- claims 与 forks 是 brain-to-hand 的已定案产出，不能删；问题是它们**每次都要**，而不是"有 ACTIVE CLAIMS / ACTIVE FORKS 时才要"。契约里"claims 列表为空则 claim 可省"的规则存在，但 `decision`/`evidence` 恒为必填。

### 4.3 时机：cadence 与 p50 共同把它推到回合之外（P3）

- 回合内注入 15.6%，其余走 turn_end 聚合（`delivered` 376）。
- 两个乘数：`cadence=3`（`:103`，入队闸门 `:1633`，自审闸门 `:2257`）——工具事件攒到第 3 个才开审；以及 p50 6.6 s。二者相乘，note 通常在回合结束后才产生。

### 4.4 已定案的"不截断"与成本直接冲突（不擅自改，见 §6）

- persistent draft §2 明确："args / result / error 不设单字段或单事件上限；工具结果原样保留，不做截断"；它更写明"单个超大事件完整保留，所以 prompt 可能因这一个事件超出 24k"（`:57-58`）。
- 它的"未决/风险"也自认了代价：ring 持有大结果，最坏 96 × 10MB ≈ 960MB（`:123-125`），并给出替代方向"磁盘 transcript + 偏移读取，而不是重新引入字段上限"。
- 实测侧的证据是正面的成本：`_render_event`（`:914-919`）docstring 写"there is no per-field elision"，一次 100 KB 的 grep 会把同一批其它事件挤出 24k 窗口。
- 所以这一条**不能**作为"缺陷修复"直接改，只能作为修订请求（§6）。

---

## 5. 方案：三批，批次 1 先落

### 批次 1 · 把 review 分成两档，让常见情形一次调用完成

| 条目 | 改法 | 锚点 |
| --- | --- | --- |
| 1.1 quick 档 | `trigger ∈ {tool, scheduled}` 走新增 `_review_quick()`：`call_model("adviser", …)` + `response_format`，**不给工具、不传 history**，1 次调用 | 分派点 `:2307`；`call_model` 用法见 `:582`；通道已在 integration §4.4 的"reviewer 只读工具"预算内 |
| 1.2 deep 档 | 保持现状 `agent_loop.run`（工具、history、claims/forks），只用于 `trigger ∈ {final, mutation, error, explicit}` | `:2296-2306` |
| 1.3 删 non-JSON 修补链 | 结构化交给 `response_format`；非 JSON 记 `status='failed', error='no_object'` 并计数，不再重问。仅当 provider 明确拒绝 `response_format` 时允许 1 次 plain completion | `:2308-2316`、`:580-585` |
| 1.4 quick 契约瘦身 | quick 只返回 `{"action":"silent"}` 或 `{"action":"advise","advices":[{severity,target,text,claim?}]}`；`decision`/`evidence`/`forks`/`fork_grades` 只在 deep 要求。`parse_review` 需允许 `evidence`/`decision` 缺省 | `review.md` OUTPUT CONTRACT；`parse_review` `:588` |
| 1.5 quick 禁 blocker | quick 分支丢弃 blocker 并计入 `dropped["blocker_in_quick"]`。现有 `parse_review` 只有下限 `severity_floor`（`:612` 保留 ≥floor），没有上限 → 新增 ceiling，不是调高 floor | `:588`、`:612` |
| 1.6 quick 的提示 | `review.md` 追加一段只在 quick 注入的说明："本档没有工具；不得断言未渲染的事实；blocker 不由本档给" | `adviser_prompts.review_prompt` `:92` |
| 1.7 路由 | 配置即可（无代码）：`auxiliary.adviser.model` 指向非推理模型。可选 route 级 reasoning 覆盖要改公共代码（`agent_loop.py:223`），单独评审 | integration §4.8 |

为什么可行：`call_model("adviser", …, response_format=…)` 已在用（就是修补链本身），quick 档不引入新通道，只把"先散文再修补"换成"一次就结构化"。

代价：quick 看不到文件，只能凭 transcript 判断，所以禁止 blocker——这与 omp 的红线（只引用渲染过的证据）和 integration §4.4 的"每次 review 工具预算 2–3 次"是同一取舍的两侧。

### 批次 2 · 计量与历史（P2；先量后改）

| 条目 | 改法 | 锚点 |
| --- | --- | --- |
| 2.1 计量先行 | `adviser_reviews` 加 `tier`、`prompt_chars`、`evicted_chars`、`cache_read`；跑两天再决定 2.2 | `store/store.py` `_SCHEMA_VERSION` |
| 2.2 trim 不移动起点 | 超限时先把最旧的工具结果换占位符（omp 的 saved−rewrite：只在"省下的 > 重写的代价"时切），仍超再移动起点并记 `cache_epoch` | `_trim_history` `:1423-1447`；persistent draft §1 的 60 条/120k |
| 2.3 提示字节稳定 | system 分段加进程内缓存（按 roster+budget 的 hash）。**低收益**，CPU/IO 卫生，不列为性能项 | `adviser_prompts.py:92` |
| 2.4 reviewer token 预算 | integration §4.7 设计过"每 session 设 adviser 自身 token 预算，超限只保留 C/D"，`_DEFAULTS` 46 个键里**没有**对应项，属未实现。补它 = 给成本一个硬上限 | `:100-186`（键清单） |

### 批次 3 · 时机（P3）

| 条目 | 改法 | 锚点 |
| --- | --- | --- |
| 3.1 起点提前 | `cadence` 默认 3 → 1。合批已存在，调用次数不涨，只是把第一次 review 提前 | `:103`、`:1633`、`:2257` |
| 3.2 到达率指标 | `injected/(injected+delivered)` 做成常态指标，目标 >50% | `advisories` |
| 3.3 通道复核 | 按 integration §4.6 复核 A–E 是否按语义走（nit→B、concern→A、blocker→A/C/D）；`immune_turns=3` 已在 | `:2680`、`:2751`、`:3207` |

---

## 6. 修订请求：字段截断（需要评审批准，不是缺陷修复）

**提议**：把"逐字段截断"限定在 **quick 档的 delta 渲染**，deep 档维持 persistent draft §2 的"不截断"。

- 被修订的原文：persistent draft §2（`plugin/minir/adviser_mode.py` 的 `_render_event` 不设字段上限）。
- 理由（两条相互独立）：
  1. 成本：一次 100 KB 的 grep 会把同批其它事件挤出 24k 窗口，reviewer 因此看不到真正要看的东西——不是"少看细节"，而是"看错内容"。
  2. quick 档本来就是受限证据面：它没有工具、禁止 blocker；在这个前提下给它"完整但挤爆窗口的 transcript"没有意义。
- 修订后的规则（仅 quick）：`result`/`args` 保留 head 70% + tail 30%，中间记 `[elided N chars — seq=<n>]`；`status`/`error` 全文；单事件上限 `REVIEW_TOOL_CHARS=6_000`。
- 若评审拒绝修订：quick 档退化为"整包 `_bounded(12k)`"，即沿用现有 `MAX_PROMPT_CHARS` 的丢整事件逻辑，只是窗口更小；`prompt_chars` 的收益会小一档，但不动已定案语义。
- deep 档一律维持现状（不截断），本稿不改。

---

## 7. 宿主边界（沿用 integration §5 与 §9，不重复论证）

1. **中断 steer**：宿主不向插件暴露"杀在飞工具"；补丁 0001 给了 `host_steer`（工具批次后插入）与 `host_continue`（stop gate 续跑），所以"step 边界"在插件侧的最近点仍是 `llm_request`（`:3207`）。
2. **发起新回合**：插件不能起 turn；`pre_verify` 的续跑是补丁 0002 的实现，最多 `max_plugin_continuations` 次。
3. **plan mode 状态**：不读，不猜（§5 第 3 行）。
4. **per-run 工具注册**：`AIAgent.__init__` 只有 `enabled_toolsets`（`run_agent.py:236-281`），`agent_loop.run` 无 `extra_tools`（`:282-293`）。omp 的"advise 作为终止工具"要再动宿主补丁，本草稿不做。
5. **provider 缓存键**：宿主没有 per-run cache key；插件能保证的只是"前缀字节稳定 + 会话 id 稳定"（`:2303`）。§7 已记录投递侧注入会破坏前缀缓存，这是已知代价。

---

## 8. 改动清单

| 文件 | 位置 | 改动 | 批次 |
| --- | --- | --- | --- |
| `adviser/adviser_mode.py` | `_review_one` `:2228`，分派 `:2307` | quick/deep 分派 | 1 |
| 同上 | 新增 `_review_quick()` | `call_model` + `response_format`，约 40 行 | 1 |
| 同上 | `:2308-2316` | 删修补链，改 `no_object` 计数 | 1 |
| 同上 | `_render_event` `:914` | 仅 quick 档 head/tail 截断（§6 批准后） | 1 |
| 同上 | 常量 `:70-91` | 加 `REVIEW_TOOL_CHARS`、`QUICK_PROMPT_CHARS` | 1 |
| 同上 | `_prompt` `:1997` | quick 档省 forks/applied_forks；claims 块保留 | 1 |
| 同上 | `_DEFAULTS` `:100` | 加 `review_tier: both/quick/deep` | 1 |
| 同上 | `_trim_history` `:1423` | 驱逐优先、移动起点记 epoch | 2 |
| 同上 | `_DEFAULTS` + `_cfg` | 加 reviewer token 预算键（§4.7 未实现项） | 2 |
| `adviser/adviser_prompts.py` | `review_prompt` `:92` | 按 tier 注入；进程内缓存（可选） | 1/2 |
| `prompts/adviser/review.md` | OUTPUT CONTRACT | 拆出 quick 契约段 + "无工具"段 | 1 |
| `store/store.py` | `_SCHEMA_VERSION`、`adviser_reviews` DDL | 加 `tier`/`prompt_chars`/`evicted_chars`/`cache_read` | 2 |
| `tests/test_adviser_mode.py` | — | 分派、quick 不发工具、quick blocker 被丢、截断后可解析 | 1 |
| `tests/test_adviser.py` | — | `parse_review` 允许 evidence/decision 缺省；`response_format` 回归 | 1 |

不动：`adviser_guard.py`、`fork.py`、`claims.py`、`host_bridge`、五通道投递、persistent draft 的其余条款。

---

## 9. 验收

观察窗口：每批 ≥300 次 review（按当前节奏 1–2 天）。全部按 trigger 分组，避免 final 的 85 s 均值污染结论。

| 指标 | 基线 | 批次 1 | 批次 3 |
| --- | --- | --- | --- |
| `api_calls=1` 占比 | 82.6% | >95% | — |
| p50 延迟 | 6,652 ms | <3,000 ms | — |
| non-JSON 失败 | 58 次 | <0.5% | — |
| 回合内注入占比 | 15.6% | — | >50% |
| 静默率 | 76.2% | ≥60%（不能把真问题省掉） | ≥60% |
| 每次 review 输入字符 | 未测 | — | 下降 ≥40%（批次 2） |
| quick 档 blocker 计数 | — | 恒为 0（测试断言） | — |

回滚：`review_tier=deep` 回到今天的单一路径；`cadence=3` 恢复原起点。两者都是配置，不回退代码。

---

## 10. 风险

沿用 integration §7 已有的四条（注入破坏前缀缓存、continuation 计入上下文、block 可能被重试、`wait` 拖慢请求），本稿新增：

1. **quick 档空话**：看不到文件时最容易说"你没验证 X"。缓解：quick 禁 blocker + 提示里"只引用渲染过的证据、未渲染即 UNKNOWN" + 断言 quick 的 blocker 计数为 0。
2. **§6 的修订若被批准，deep/quick 的证据面不一致**：同一份 transcript 两种渲染，评审要为"哪一档说了什么"负责写进日志（`evicted_chars` 已可区分）。
3. **store 迁移**：加列是加法，但 `PRAGMA user_version` 变化后旧库必须能打开（`store/store.py:746-761`）；补"旧版 DDL 建库再打开"的测试。
4. **观测偏差**：`scheduled` 行占 64%，当独立触发看会看反。
5. **response_format 兼容**：provider 不支持时 `call_model` 会剥字段（`:580-585` 注释），剥完是否退化成散文决定 §1.3 的取舍。

---

## 11. 未决项（评审要拍的）

1. quick 档带不带 history？**建议不带**：guard 已去重，prior advice 在 prompt 里；带 history 会把输入拉回 100k 量级。
2. tool 触发是否一律 quick？**建议一律**，需要读代码核实的成员用 per-member `tier: deep` 覆盖（roster 已支持成员覆写）。
3. §6 的截断修订批不批？批 = quick 档按 6k 单事件上限；不批 = quick 用整包 12k 兜底。
4. quick 契约是否保留 `claim`？**建议保留**（claims 已定案，缺 claim 会被 `parse_review` 丢），但允许 `evidence` 为空数组。
5. 批次 1 是否连带把 `cadence` 从 3 改成 1（3.1）？一起改会让"变快是因为 tier 还是因为起点"混在一起，**建议分开验收**。
