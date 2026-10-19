# omp adviser 机制与 Hermes 接入设计

- 状态：design draft（只做分析与设计，不含实现）
- omp 基线：本机 `omp/18.12.2`（`C:\Users\zarag\AppData\Local\Tern\omp\bin\omp.exe`）；行号与文件引用取自上游 `can1357/oh-my-pi` 的 `packages/coding-agent/src/`，关键常量已在本机二进制里逐条核对
- Hermes 基线：本机安装 `C:\Users\zarag\AppData\Local\hermes\hermes-agent`
- 本文回答一件事：omp 的 adviser 如何工作，哪些部分能落到 Hermes 的 hook / middleware / memory-provider 三个缝上，落不上的用什么替代

## 0. 结论

1. omp 的 adviser 不是"多一段 prompt"，而是一个独立的 side agent：自己的模型、自己的上下文、自己的工具会话、自己的 transcript。核心循环是**增量 delta → 结构化 `advise` → 发射护栏 → 三档投递**。
2. 三档投递只有两档能直译到 Hermes：`llm_request` middleware（每个 provider attempt 一次，是唯一在工具循环内的注入缝）和 `pre_llm_call`（每回合一次，回合边界）。omp 的 `steer`（中断正在执行的工具并立刻插入消息）在 Hermes 没有对应 API。
3. "blocker 立刻改变行为"只能用 `pre_tool_call` block 近似：执行前拦截，block 文案作为该次工具结果返回给模型。代价是每条 blocker 表现为一次被拒绝的工具调用，且只对可拦截的工具有效。
4. "终局回答后自动续跑"只有半个通道：`pre_verify` 的 continue 只对"本回合改过文件且正在收尾"的回合生效，受 `max_verify_nudges` 限制（`agent/turn_stop_gates.py:52-76`）。其余场景只能进 ledger，等下一次注入。
5. adviser 应继续挂在 minir（memory provider）里，而不是做成纯 hook 插件：hook 无法注册工具、无法声明配置、无法进入 `hermes memory setup`；minir 已经拥有 ledger、后台 worker、`llm_request` 投递、`auxiliary.adviser` 路由和 `adviser_only` 策略。

## 1. omp 的 adviser 是什么

### 1.1 组成

| 组件 | 职责 | 出处 |
|---|---|---|
| `SessionAdvisors` | 每 session 一个；构建/重建 runtime、在 primary turn end 聚合、决定投递通道 | `session/session-advisors.ts` |
| `AdvisorRuntime` | 每 adviser 一个；cursor、delta 渲染、catch-up、上下文维护、重置 | `advisor/runtime.ts` |
| `advise` 工具 | 唯一的建议出口：`note` + `severity?`，支持 deferred（回合结束再发） | `advisor/advise-tool.ts:3-5` |
| `AdvisorEmissionGuard` | 归一化去重、noise 抑制、每次 update 的预算、已发缓存 | `advisor/emission-guard.ts` |
| transcript recorder | 每 session 落盘 `__advisor.jsonl`，供 `/advisor dump` | `~/.omp/agent/sessions/<cwd>/<session>/__advisor*.jsonl`（本机有 29 个） |
| `WATCHDOG.md` / `WATCHDOG.yml` | 项目级审查重点与多 adviser roster | `docs/advisor-watchdog.md` |

### 1.2 一次 review 的完整路径

1. primary turn 结束触发 `onPrimaryTurnEnd`。`reviewMode=turn` 每回合都触发，`agent-end` 只在最终 yield 触发；`reviewInterval=N` 每 N 个 eligible update 才真正 dispatch，被跳过的 update 仍然被捕获，随下一次 review 一起发（`session-advisors.ts:123-182`）。
2. `runtime.onTurnEnd` 从自己的 cursor 取新增消息，渲染成 `### Session update`（实测 omp 18.12.2 的 advisor transcript 就是这个格式）：包含 user 文本、assistant 文本、reasoning、工具调用与结果；被截断的结果带 `elided` 标记；advisor 自己注入过的 `<advisory>` 在下一轮 delta 里被过滤，避免自我审阅。
3. adviser 是一个完整 `Agent` 实例，`ToolSession` id 带 `-advisor` 后缀，与 primary 不共享文件快照/seen-lines/冲突状态（`docs/advisor-watchdog.md`，"Tools and isolation"）。
4. system prompt 由五段拼成：`advisor/system.md`（含 `max_notes_per_update`）+ `context-files.md`（AGENTS.md 等）+ `memory-context.md`（记忆后端）+ `active-repo-watchdog.md`（父目录只有一个 git 子仓时）+ roster/共享 instructions（`session-advisors.ts:536-546`）。
5. 默认只读工具 `read`/`grep`/`glob`，memory 后端存在时加 `recall`；`tools: []` 表示没有调查工具，`advise` 始终存在；roster 可以扩到 `write`/`edit`/`bash`/`eval`，被明确标注为安全边界（`session-advisors.ts:547-548`、`docs/advisor-watchdog.md`）。
6. 建议走 `advise`，先过 emission guard：NFKC + 小写 + 去标点做 key；固定 noise 列表（"looks good"、"no issues" 等 40 余条）直接丢；同一 key 且 severity 不升级判 duplicate；每次 update 非 blocker 上限 `maxNotesPerUpdate`（默认 4，范围 1-32），blocker 豁免；已发 note 进 4096 条 FIFO 缓存（`emission-guard.ts:1-46`、`:97-134`）。
7. 投递决策 `Tp()` 依据 severity、是否 streaming、是否 aborting、是否终局且无排队工作、plan mode、deferred、interrupt-immune 窗口，返回 `steer` / `aside` / `preserve`（`advise-tool.ts:29-43`）。
8. 回合结束时把本回合攒下的 notes 排序、补 `turns_ago`，每 50 个 turn 额外附一次 `boundary-guidance.md`，再统一投递（`session-advisors.ts:44-45`、`:830-847`）。

### 1.3 severity 与通道

| severity | 语义 | 运行中 | 终局回答之后 |
|---|---|---|---|
| `nit` | 清理、简化、低风险边角 | 非中断 aside，下一安全边界进入 transcript | 保留为可见卡片 |
| `concern` | 实质风险、漏约束、方向可能错 | 条件允许时中断 steer | 按 `continueSeverity`（默认 concern）请求续跑 |
| `blocker` | 继续做大概率白干或产出坏结果 | 可中断 steer | 可以触发新回合 |

交付形态是 XML 属性，不额外写系统提示：`<advisory advisor="Architecture" severity="concern" turns_ago="2" guidance="weigh, don't blindly obey">…</advisory>`（`advise-tool.ts:12`、`:6`）。主 agent 的系统提示里没有"如何处理建议"的说明，这条 `guidance` 属性是唯一线索。

### 1.4 配置面

| 键 | 默认 | 作用 |
|---|---|---|
| `advisor.enabled` | false | 总开关 |
| `modelRoles.advisor` | 慢模型链 | 默认 adviser 的模型与 thinking 级别 |
| `WATCHDOG.yml: advisors[]` | 无 | roster，每项 `name/enabled/model/tools/instructions` |
| `advisor.reviewMode` | `turn` | `turn` 每回合，`agent-end` 只在最终 yield |
| `advisor.reviewInterval` | 1 | 每 N 个 eligible update 审一次 |
| `advisor.maxNotesPerUpdate` | 4（1-32） | 每次 update 的非 blocker 建议上限 |
| `advisor.syncBacklog` | `off` | `off`/`strict`/数字；数字表示落后几轮时暂停主 agent 最多 30s |
| `advisor.immuneTurns` | 3 | 一次中断后，接下来 N 个 turn 内的 concern/blocker 降级为非中断 |
| `advisor.continueSeverity` | `concern` | 终局回答后达到该级别才请求续跑 |
| `tier.advisor` | `none` | adviser 请求的 service tier |
| `retry.fallbackChains.advisor` | 无 | adviser 模型失败时的备份链 |

设置改动在下一个 primary 边界生效，不需要重建 runtime（`docs/advisor-watchdog.md`）。roster 项自带 cadence，省略即 `turn`/`1`。

### 1.5 失败与成本

- 模型失败：重试 → 凭证轮换 → fallback 链；`usage-limit` 长时间阻塞则暂停该 adviser，其他 roster 成员不受影响。
- 审查重启：primary transcript 被重写（compact、resume、fork、re-prime）时 reset cursor，下一轮重放当前 bounded transcript，不延续旧上下文。
- 成本独立计费；`/advisor status` 按 adviser 显示模型、上下文占用、token、花费；headless 模式在退出前为未完成的 review 留预算（正常 10 分钟，错误退出 30 秒，`strict` 无上限）。
- 密钥在 provider 之前做混淆（obfuscation），但文档明确这不是"可以随便授权"的理由。

## 2. Hermes 侧可用的缝

### 2.1 hook 面

`hermes_cli/plugins.py:109-204` 是权威清单。与本设计相关的：

| hook | 触发 | 设计中的用途 | 限制 |
|---|---|---|---|
| `on_session_start` / `on_session_reset` / `on_session_finalize` | 会话生命周期 | 绑定 cursor、初始化/停止 reviewer | 不得调用模型（30s 超时） |
| `post_tool_call` | 每次工具结果后 | 进事件环：工具名、参数摘要、状态、错误 | 参数/结果可能含 secret |
| `on_interim_message` | 回合中途的可见文本 | 记录 assistant 意图 | 只有可见文本，无 reasoning |
| `post_llm_call` | 回合 finalize | 回合级收尾、补全 delta | 观察者，返回值忽略 |
| `pre_llm_call` | 每回合一次，回合序幕 | 只投递 aside 级别、每 id 一次 | context 拼进当轮 user message，整回合固定重放 |
| `pre_tool_call` | 工具执行前 | `adviser_only` 策略；可选 blocker 拦截 | block 文案作为该次工具结果返回（`agent/agent_runtime_helpers.py:2531-2544`） |
| `pre_verify` | 改过文件、准备收尾 | blocker 续跑（opt-in） | 受 `max_verify_nudges`，非代码回合不触发 |
| `post_api_request` / `api_request_error` | 每次 provider attempt 结果 | 确认投递 / 回滚 staged | 观察者 |
| `post_auxiliary_call` | 每次辅助模型调用 | 统计 adviser 自身 token/成本 | payload 与 `post_api_request` 同形，带 `aux_task` |

超时：`plugins.hook_callback_timeout` 默认 30 秒（`hermes_cli/config_defaults.py:1719`）。慢回调会被放弃并在 60 秒内静默，因此所有 hook 都必须是内存操作，模型调用一律放后台线程。

### 2.2 middleware 面

`llm_request` 在每个 provider attempt 前执行（`agent/turn_api_request.py:143-153`），回调返回 `{"request": {...}}` 整体替换 provider kwargs（`hermes_cli/middleware.py:88-99`）。它同步执行、没有超时包装，所以在里面等待就等于阻塞模型请求。

minir 现状（已实现）：读 `staged_advisories(request_id)`，否则读 pending；渲染成块后用 `_inject` 追加到请求副本的最后一条 user/tool 消息（遇到未配对的 `tool_calls` 就放弃，留给下一次 attempt）；`post_api_request` 标记 delivered，`api_request_error` 把 staged 退回 pending（`plugin/minir/adviser_mode.py:798-863`、`:879-905`）。

### 2.3 注入语义（决定设计边界）

- `pre_llm_call` 的返回值在回合序幕收集，拼进当轮 user message（`agent/turn_context.py:798-852`、`:1177`），随后以 `api_content` 固定重放（`:1289-1302`）。所以它**不能**做逐次迭代投递。
- system prompt 每 session 只构建一次并原样重放（`agent/turn_context.py:1251-1254`），插件不能中途改系统提示。
- 唯一能改变"这一次请求"的缝是 `llm_request`。

### 2.4 memory provider 面

- 同一时间只有一个外部 provider 活跃；工具处理器必须返回 JSON 字符串；`prefetch` 在关键路径上必须快。
- `initialize(session_id, **kwargs)` 拿到 `hermes_home`（按 profile 隔离），`sync_turn` 走后台线程。
- 配置走 `get_config_schema()` / `save_config()`，非敏感配置写 `$HERMES_HOME/minir*.json`，敏感项走 `.env`。

## 3. 能力映射

| omp 机制 | omp 实现 | Hermes 等价 | 判定 |
|---|---|---|---|
| 增量感知 | cursor + delta 渲染，含 reasoning 与工具意图 | `post_tool_call` + `on_interim_message` + `post_llm_call` 组 delta；无 reasoning | 降级 |
| 结构化建议 | `advise(note, severity)` | reviewer 返回 JSON `advices[]`（已有 `severity`/`target`） | 直译 |
| 发射护栏 | normalize + noise + dedupe + 预算 + 已发缓存 | 需补：目前只有 `max_advices`、severity floor、TTL | 部分 |
| 中断 steer | `deliverAs: "steer"` + `triggerTurn` | 无 API；最近似的是 `pre_tool_call` block | 不可直译 |
| 非中断 aside | yield queue，下一安全边界 | `llm_request` 在下一次 attempt 注入 | 直译 |
| 保留卡片 | `preserveAdvisorCard` 可见卡片 | 无卡片位；只能留 ledger + `minir_search` | 降级 |
| 终局续跑 | concern/blocker 触发新回合 | `pre_verify` continue（仅代码收尾） | 部分 |
| interrupt immune | 按 turn 计数冷却 | 用 host turn 号自记 | 直译 |
| plan mode 保留 | 检测 plan mode 后只显示不 steer | 插件拿不到 plan mode 状态 | 不可用 |
| syncBacklog | 落后 N 轮时暂停主 agent（≤30s） | `llm_request` 内有界等待 | 降级（strict 危险） |
| 多 adviser roster | WATCHDOG.yml，各自模型/工具/指令 | provider 内多实例，自建隔离与计费 | 可行但昂贵 |
| WATCHDOG.md | 用户级 + 祖先目录 + 当前目录，支持 `@import` | 自建发现（当前目录到 git root） | 直译 |
| project-context | AGENTS.md 等注入 system prompt | 读 AGENTS.md 进 adviser prompt | 直译 |
| memory-context | 记忆后端 instructions + recall | minir recall 结果作为 `<memory-context>` | 直译 |
| 自身建议过滤 | delta 渲染前过滤 advisor 消息 | 按 `api_request_id` + block hash 比对 | 直译 |
| transcript 重写 reset | compact/resume/fork 后 reset cursor | `on_pre_compress` + `on_session_reset` | 直译 |
| 密钥混淆 | 会话 secret obfuscator | `agent/redact.py`、`agent/monitoring/redaction.py:redact_bounded` | 直译 |
| 成本统计 | 每 adviser token/cost | `post_auxiliary_call` 的 usage 汇总 | 可行 |
| transcript dump | `__advisor.jsonl` + `/advisor dump` | 自建 jsonl（可选） | 可行 |

## 4. 接入设计

### 4.1 放置

继续放在 `plugin/minir/`，作为 `adviser_mode` 的演进，而不是新插件：

- 需要注册工具（`adviser_consult`、`adviser_session_search`）和中间件，hook 插件做不到；
- 需要配置向导与 profile 隔离，memory provider 已经有；
- 复用 `advisories` ledger、`auxiliary.adviser` 路由、`adviser_only` 策略和 `_MUTATING_TOOLS` 清单；
- 单 provider 规则天然保证不会有两个 adviser 同时抢投递。

### 4.2 数据流

见 `.archify/architecture-omp-adviser-20261019-1255/omp-adviser-hermes.html`（架构图）。文字版：

```
primary 回合
  post_tool_call / on_interim_message / post_llm_call
        └─> 事件环（内存，48 条 × 1200 字符）
                └─> reviewer 后台线程（cadence / 工具错误 / 变更后触发）
                        ├─ delta 渲染（cursor → 新事件）
                        ├─ adviser prompt（system + project + memory + roster）
                        ├─ 输出 JSON advices
                        └─> EmissionGuard（normalize/dedupe/noise/budget/cache）
                                └─> ledger: pending
llm_request（每 attempt）
        └─> 取 pending/staged ≥ severity_floor → 注入 → staged
                └─> post_api_request 确认 delivered ｜ api_request_error 退回 pending
pre_tool_call（opt-in）
        └─> blocker + 变更类工具 → block + note
pre_verify（opt-in）
        └─> blocker + 本回合改过文件 → continue
```

### 4.3 感知层

- cursor 按 session 保存；`on_session_start` 把 cursor 置为当前 transcript 长度（对应 omp 的"中途启用不重放历史"）。
- delta 渲染补三件事：工具意图与结果分块、错误显式标记、进度标记 `[in progress - more steps follow]`（omp 用它要求 reviewer 不对半成品下判断）。
- 注入过的建议按 `api_request_id` 记录，渲染前从 delta 里剔除，避免 adviser 审自己的建议。
- 进入 adviser prompt 之前统一过 `redact_bounded` 截断与脱敏；单事件 1200 字符、整包 12000 字符的上限保留。
- Hermes 不把 reasoning 交给插件，设计不依赖它：`on_stream_delta` 默认不订阅。

### 4.4 推理层

- prompt 采用 omp `system.md` 的语义，压成 minir 现有的 `CORE + MODE_REVIEW` 结构：角色（user/code-quality/robustness 的影子审查者）、只报具体技术风险、不复述 agent 已经知道的错误、不评判用户意图、不在缺证据时断言、blocker 必须先自查。
- 新增两段上下文：`<project-context>`（AGENTS.md + 从当前目录到 git root 的 `WATCHDOG.md`，带字符上限）和 `<memory-context>`（minir recall 的 top-N，标注"可能过期，当前对话优先"）。
- 工具：`adviser_session_search`（只读）替代 omp 的 read/grep/glob；不给写工具。每次 review 的工具预算 2-3 次，对应 omp 的 "Per advise: 2-3 tool calls"。
- 输出：保持 JSON，字段 `advices[]`（`note`、`severity`、`target`、`evidence`、`confidence`）。`max_advices` 仍是单次投递上限 3，reviewer 每轮产出上限改为 `max_notes_per_update`（默认 4，blocker 不限）。

### 4.5 发射层（补 EmissionGuard）

| 规则 | 实现 | 依据 |
|---|---|---|
| 归一化 | NFKC + lower + 非字母数字折成空格 | `emission-guard.ts:2-4` |
| noise | 固定短语表直接丢（"looks good"/"no issues"/"continue"…） | `emission-guard.ts:5-43` |
| 重复 | 同 key 且 severity 不升级 → duplicate；升级 → 放行 | `:104-106` |
| 预算 | 每轮非 blocker ≤ `max_notes_per_update`，先淘汰最小 rank 的 pending | `:109-131` |
| 已发缓存 | 4096 条 FIFO，命中即 duplicate | `:44`、`:79-90` |
| 丢弃原因 | 记 `empty/noise/duplicate/rate-limit` 指标，否则无法调参 | `:60-65` |

ledger 状态机沿用现有：`pending → staged(api_request_id) → delivered`；任何失败路径回 `pending`；TTL 3600s。

### 4.6 投递层

| 通道 | Hermes 实现 | 条件 |
|---|---|---|
| A 中途注入 | `llm_request` 注入 `<advisory severity=… advisor=… turns_ago=… guidance="weigh, don't blindly obey">` | severity ≥ `severity_floor`（默认 concern） |
| B 边界 aside | `pre_llm_call` 返回一次性 context（按 advice id 去重） | nit，或 immune 窗口内的 concern |
| C 工具拦截 | `pre_tool_call` 对 `_MUTATING_TOOLS` 返回 block + note | `block_tools: true` 且 blocker；每回合最多 2 次 |
| D 续跑 | `pre_verify` 返回 continue | `verify_continue: true` 且 blocker 且本回合改过文件 |
| E 保留 | 只写 ledger，可通过 `minir_search` 查到 | 其余情况；对应 omp 的 preserve 卡片 |

immune turns：记录最近一次 C/D 触发的 turn 号，`immune_turns` 内 concern 降级为 B，blocker 仍可 C/D。

### 4.7 背压与预算

- reviewer 永远在后台线程；`llm_request` 只读内存/ledger，绝不在请求路径调用模型。
- `sync_backlog`：`off`（默认，立刻注入当前 advice）/ `N`（落后 N 个 update 时等待 reviewer，最多 `wait_ms`=1500）/ `strict`（无上限，仅交互式）。等待超时就按现状注入，不能卡死请求。
- 每次投递的字符上限保持 900；`max_advices` 保持 3。
- 每 session 设 token 预算上限（adviser 自身用量），超限只保留 C/D，不再做定期 review。

### 4.8 配置契约

沿用 `memory.minir.adviser_mode`，新增或调整：

```yaml
memory.minir.adviser_mode:
  enabled: false          # 现有
  mode: watch             # 现有：watch | adviser_only
  cadence: 3              # 现有：工具错误/变更后仍触发
  severity_floor: concern # 现有
  max_advices: 3          # 现有
  max_chars: 900          # 现有
  review_mode: turn       # 新增：turn | agent-end
  review_interval: 1      # 新增：≥1
  max_notes_per_update: 4 # 新增：1..32，blocker 不限
  continue_severity: concern
  immune_turns: 3
  sync_backlog: off       # 实现为 off | wait（等待在途 review，有界）
  wait_ms: 1500
  block_tools: true       # pre_tool_call 拦截（用户已确认开启）
  verify_continue: false  # pre_verify 续跑
  aside_at_turn_start: true
  notify_gateway: true
  project_context: true   # AGENTS.md + WATCHDOG.md
  max_project_chars: 1500
  redact: true            # 宿主 redact_for_export，导入失败时占位
  roster: []              # [{name, enabled, model?, instructions?, toolsets?}]
```

### 4.9 生命周期与失败路径

| 事件 | 动作 |
|---|---|
| `on_session_start` | 绑定 cursor（= 当前 transcript 长度）、启动 worker |
| `on_session_reset` / `on_session_finalize` | 停 worker、过期 advice、关闭 recorder |
| `on_pre_compress` | reset cursor（对应 omp 的 transcript 重写） |
| review 失败/解析失败 | 不写入任何 advice；review 行记 `failed`，下一轮从同一 cursor 重算 |
| 注入失败（未配对 tool_calls、wire 形状不支持） | 不 stage，等下一次 attempt |
| 进程退出 | `shutdown()` join worker，超时后放弃并记日志 |

### 4.10 观测

`minir_stats` 增加每个 adviser 的：review 次数、产出 note 数、按原因丢弃数、投递成功率、上下文占用、adviser 自身 token/成本（来自 `post_auxiliary_call` 的 usage，按 `aux_task` 过滤）。

## 5. 照搬不了的部分

| omp 能力 | 为什么不行 | 替代 |
|---|---|---|
| 中断 steer（abort 正在跑的工具） | Hermes 没有插件可见的中断/插话 API | `pre_tool_call` block（只在工具边界，opt-in） |
| 终局回答后自动开新回合 | 插件不能发起新 turn | `pre_verify` continue（仅代码收尾），或等下一次用户消息 |
| plan mode 只展示不 steer | 插件拿不到 plan mode 状态 | 不做，或由用户显式开关 |
| 可见的建议卡片、`/advisor status` UI | 插件没有 UI 位 | ledger + `minir_search` + `minir_stats` |
| 每 adviser 独立上下文/成本面板 | 需要宿主侧支持 | 自记，面板放 `minir_stats` |
| reasoning/token 级流式感知 | 插件流式 hook 只交付规范化文本 | 不依赖 reasoning 设计 prompt |

## 6. 实施切片（已落地）

实现：`plugin/minir/adviser_guard.py`（发射护栏）、`plugin/minir/adviser_mode.py`（roster/delta/通道/网关）、
`plugin/minir/store.py`（`adviser` 列、`pending_blocker`、`advisory_fingerprints`、统计）、
`plugin/minir/__init__.py`（`pre_verify` 注册、aside、block、stats）、`plugin.yaml`。

| 切片 | 内容 | 验收断言 | 测试 |
|---|---|---|---|
| P1 感知 | delta cursor（每个 adviser 独立）、项目/记忆上下文、自身建议过滤 | 3 次工具调用后 delta 含全部事件且不含已注入块 | `test_roster_runs_each_adviser_with_its_own_cursor`、`test_recorded_events_drop_injected_advisory_blocks`、`test_prompt_carries_project_instructions` |
| P2 发射 | normalize/noise/dedupe/预算/缓存/丢弃指标 | 同 note 同 severity 丢弃；升级放行；非 blocker 超预算丢弃；blocker 不受限 | `tests/test_adviser_guard.py`、`test_repeat_across_reviews_is_dropped`、`test_noise_only_review_is_silent_and_records_drop_reason` |
| P3 投递 | `llm_request` 注入 + staged 确认/回滚 + immune turns + turn-boundary aside | floor 以下不注入；blocker 下一次 attempt 可见；immune 窗口内 concern 不注入 | `test_immune_window_suppresses_concerns_but_not_blockers`、`test_aside_block_publishes_a_nit_once` |
| P4 干预 | `pre_tool_call` blocker 拦截、`pre_verify` 续跑 | 变更类工具被拦截且 note 可见；非变更类不受影响；每回合最多 2 次；续跑只一次 | `test_blocker_refuses_a_mutating_tool_once`、`test_one_turn_can_be_refused_at_most_twice`、`test_verify_continue_fires_once_and_is_opt_in` |
| P5 roster + 网关 | 多 adviser、按名归属、独立统计、网关 notify | 两条 advice 带不同 `adviser`；网关回合结束后主动发消息；CLI 不发 | `test_gateway_turn_notifies_preserved_advice`、`test_cli_turn_does_not_notify`、`test_turn_end_notifies_advice_published_before_the_terminal_marker` |

与本文原始设计的偏差（实现时收紧）：

| 设计 | 实现 | 原因 |
|---|---|---|
| `sync_backlog: off \| strict \| <int>` | `off \| wait` + `wait_ms` | strict 会无限阻塞 provider 请求；`llm_request` 同步且无超时 |
| preserve 卡片 | gateway 主动发消息 + ledger 保留 | 插件没有 UI 位；用户已确认要主动发 |
| `watchdog_files` 开关 | 并入 `project_context`（AGENTS.md + WATCHDOG.md） | 一次发现两种文件，少一个配置 |
| roster 无上限 | 最多 4 个 | 每个都要真金白银跑一次模型 |
| 逐 token reasoning | 不订阅 | Hermes 插件流式 hook 只给规范化文本 |
| dialectic 产出 `advices` | dialectic 只修正 base，发布 `## Base Revision` note；advice 只有 adviser_mode 一个生产者 | 用户决定：两套 reviewer 合并为一条产出链 |

## 7. 风险与开放问题

- 注入会改动当轮 user message，破坏 provider 前缀缓存；建议中途注入只给 blocker/concern，nit 走回合边界。
- `pre_tool_call` block 可能被模型当作工具故障反复重试；必须限次并标记为已投递。
- `llm_request` 内等待会直接拖慢请求，`strict` 只应在交互式场景开放。
- 两套 reviewer 已合并：dialectic 只修正 base（`## Base Revision`），advice 只有 adviser_mode 一个生产者；重复建议不再跨源撞 dedupe。
- 已决策：允许 adviser 阻断工具（`block_tools: true`）；愿意为 roster 付多份模型成本；gateway 场景 preserve 主动发消息（`notify_gateway: true`）。仍未定：`verify_continue` 是否开启、roster 具体成员的模型分配。

## 8. 证据索引

| 结论 | 证据 |
|---|---|
| omp 版本与 prompt 资产 | `omp --version` = 18.12.2；上游 `packages/coding-agent/src/prompts/advisor/*.md` |
| adviser 默认只读工具、advise 始终可用 | `session-advisors.ts:547-548`；`docs/advisor-watchdog.md` |
| 三档投递与 severity | `advise-tool.ts:29-43`；`omp.sh/docs/advisor` |
| 发射护栏 | `emission-guard.ts:1-134` |
| delta 与 cursor | `runtime.ts:138-202`；`~/.omp/agent/sessions/**/__advisor*.jsonl` 实测格式 |
| Hermes hook 清单 | `hermes_cli/plugins.py:109-204` |
| hook 超时 | `hermes_cli/config_defaults.py:1719` |
| `pre_llm_call` 每回合一次 | `agent/turn_context.py:798-852`、`:1177` |
| 注入进当轮 user message | `agent/turn_context.py:1289-1302` |
| `llm_request` 每 attempt 一次 | `agent/turn_api_request.py:143-153`；`hermes_cli/middleware.py:88-99` |
| `pre_verify` 续跑 | `agent/turn_stop_gates.py:52-76` |
| `pre_tool_call` block 的可见形态 | `agent/agent_runtime_helpers.py:2531-2544` |
| adviser 成本统计入口 | `agent/auxiliary_hooks.py`（`post_auxiliary_call` + usage summary） |
| minir 现有投递 | `plugin/minir/adviser_mode.py:798-863`、`:879-905` |
