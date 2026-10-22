# omp adviser 机制与 Hermes 接入设计

- 状态：design + implementation（§1–§8 为设计与插件侧实现；§9 记录补齐 steer/续跑的宿主补丁系列与安装器）
- omp 基线：本机 `omp/18.12.2`（`C:\Users\zarag\AppData\Local\Tern\omp\bin\omp.exe`）；行号与文件引用取自上游 `can1357/oh-my-pi` 的 `packages/coding-agent/src/`，关键常量已在本机二进制里逐条核对
- Hermes 基线：本机安装 `C:\Users\zarag\AppData\Local\hermes\hermes-agent`（v0.22.2）
- 本文回答一件事：omp 的 adviser 如何工作，哪些部分能落到 Hermes 的 hook / middleware / memory-provider 三个缝上，落不上的用什么替代，以及为"完整 adviser"新增的最小宿主面

## 0. 结论

1. omp 的 adviser 不是"多一段 prompt"，而是一个独立的 side agent：自己的模型、自己的上下文、自己的工具会话、自己的 transcript。核心循环是**增量 delta → 结构化 `advise` → 发射护栏 → 三档投递**。
2. 三档投递在插件侧只有两档能直译：`llm_request` middleware（每个 provider attempt 一次，是唯一在工具循环内的注入缝）和 `pre_llm_call`（每回合一次，回合边界）。宿主内部有 `/steer` 与 `redirect`，但没有对插件暴露；补丁 0001 只暴露 steer，并且用 `display_kind="adviser"` 交付——不借用 `redirect` 的"用户更正"行，建议不冒充用户命令。
3. "blocker 立刻改变行为"保留 `pre_tool_call` block（执行前拦截，block 文案作为该次工具结果返回给模型），并新增 steer：模型在下一次尝试前就能看到理由，不必先撞一次被拒绝的工具。steer 不中止已经执行的工具，只在下一个安全边界生效。
4. "终局回答后自动续跑"原来只有半个通道（`pre_verify` 仅对改过文件的收尾生效）。补丁 0002 新增独立的 plugin stop gate：任意文本收尾都能续跑，预算 `agent.max_plugin_continuations`（默认 2），不占用编码验证的 `max_verify_nudges`。
5. adviser 应继续挂在 minir（memory provider）里，而不是做成纯 hook 插件：hook 无法注册工具、无法声明配置、无法进入 `hermes memory setup`；minir 已经拥有 ledger、后台 worker、`llm_request` 投递、`auxiliary.adviser` 路由和 `adviser_only` 策略。
6. 宿主补丁以 `git format-patch` 系列随插件发布，`scripts/install.py` 在安装时自动应用、验证、记录状态并支持回滚；宿主升级后重跑安装器即可。细节与验证见 §9。

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

补丁系列（§9）解决了投递侧；reviewer 侧的五个缺口在 §9.9 关闭。下表注明"现在"的状态。

| omp 能力 | 为什么不行 | 替代 / 现在 |
|---|---|---|
| 中断 steer（中止正在跑的工具） | 宿主有 `redirect`（中止在途模型请求，但会写 user-correction 行）和 `steer`（工具批次后插入），都没有对插件暴露；已运行的单个工具不会被杀 | 补丁 0001 暴露 `steer`，用 `display_kind="adviser"` 行；不借用 redirect，所以 blocker 也要等下一个安全边界，工具内部不中断 |
| 终局回答后自动开新回合 | 插件不能发起新 turn（surface 才能） | 补丁 0002：stop gate 续跑，最多 `max_plugin_continuations` 次；不是新 turn，但模型会再答一轮 |
| plan mode 只展示不 steer | 本机 plan mode 是外部插件（`plugin-catalog/plan-mode.yaml`），宿主没有可读状态 | 不做；`turn_state` 只返回宿主真有的字段，不猜 |
| 可见的建议卡片、`/advisor status` UI | 插件没有 UI 位 | continuation/steer 落一条 `display_kind="adviser"` 的可见 user row；ledger + `minir_search` + `minir_stats` 仍可查 |
| 每 adviser 独立 Agent/ToolSession | 需要宿主侧支持 | 已实现：每个成员一份 `AdvisorRuntime` + 独立 host session id；成本仍自记在 `minir_stats` |
| reasoning/token 级流式感知 | 插件流式 hook 只交付规范化文本 | 已实现 opt-in：prompt 取全量（`max_reasoning_chars` 可设上限），全量落盘 `sessions/reasoning/`；仍不消费 token 级工具中间态 |

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
| preserve 卡片 | 回合末尾追加 `display_kind="adviser"` 行（0010），flush 进 session DB 并触发 `message.adviser`；无补丁宿主回退到 gateway 消息 + ledger | 插件在回合结束后拿不到 agent，所以 writer 在 `post_llm_call` 捕获 |
| `watchdog_files` 开关 | 并入 `project_context`（AGENTS.md + WATCHDOG.md） | 一次发现两种文件，少一个配置 |
| roster 无上限 | 无上限；`paused: true` 单独停一个成员 | 用户要求不再静默截断 |
| 逐 token reasoning | 不订阅 | Hermes 插件流式 hook 只给规范化文本 |
| dialectic 产出 `advices` | dialectic 只修正 base，发布 `## Base Revision` note；advice 只有 adviser_mode 一个生产者 | 用户决定：两套 reviewer 合并为一条产出链 |

## 7. 风险与开放问题

- 注入会改动当轮 user message，破坏 provider 前缀缓存；建议中途注入只给 blocker/concern，nit 走回合边界。
- 宿主 continuation 追加的是一条持久化 user row（`display_kind="adviser"`），resume 会重放；这是有意让用户看到建议，也意味着它会计入上下文。预算 `agent.max_plugin_continuations`（默认 2）防止循环。
- `pre_tool_call` block 可能被模型当作工具故障反复重试；必须限次并标记为已投递。
- `llm_request` 内等待会直接拖慢请求，`sync_backlog: wait` 只应在交互式场景开放。
- 两套 reviewer 已合并：dialectic 只修正 base（`## Base Revision`），advice 只有 adviser_mode 一个生产者；重复建议不再跨源撞 dedupe。
- 已决策：允许 adviser 阻断工具（`block_tools: true`）；愿意为 roster 付多份模型成本；gateway 场景 preserve 主动发消息（`notify_gateway: true`）；宿主 bridge 的 `host_steer`/`host_continue` 默认开启。仍未定：`verify_continue` 是否开启（默认关，和宿主 continuation 是两条独立通道）、roster 具体成员的模型分配、宿主升级后补丁的维护节奏。

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
| 宿主已有 steer/redirect（未暴露给插件） | `agent/interrupt_control.py:267-341`；`agent/turn_iteration_prep.py:175-181` |
| 插件控制桥与 stop gate | `hermes_cli/plugin_runtime.py`；`agent/turn_stop_gates.py`（patch 0001/0002） |
| 安装器与状态 | `scripts/install_host_patches.py`；`$HERMES_HOME/minir-memory/host-patches.json` |

## 9. Host 补丁：完整 adviser（已落地）

§1–§8 描述的是"只用插件缝"能到达的上限。完整 adviser 缺的三件事都在宿主里：没有 agent 句柄就 steer 不了正在跑的回合，插件发不起新回合的续跑，插件的续跑请求也没有落点。这一节记录补齐它们的最小宿主改动、打包方式、安装/回滚路径和验证证据。

### 9.1 新增的宿主面

`hermes_cli/plugin_runtime.py`（patch 0001）是一个按 session 绑定的控制桥：

| API | 语义 | 复用的宿主机制 |
|---|---|---|
| `bind_turn(agent)` / `unbind_turn(agent)` | `run_conversation` 进入/退出时登记活回合 | 现有 turn 生命周期；弱引用，不延长 agent 寿命 |
| `steer(session, text)` | 下一安全边界插入一条 `display_kind="adviser"` 的 standalone user row | `agent.steer(kind="adviser")`；**不走** `redirect`，建议不借用用户权威 |
| `interrupt(session, text)` | 先排队建议，再中止当前工具批次 | `agent.steer` + `agent.interrupt(tool_reason=...)`；轮询 `is_interrupted()` 的工具会停，不轮询的工具被放弃而非杀死 |
| `queue_post_turn(session, text)` | 终局后交给 surface 一个新回合 | 合并进结果已有的 `pending_steer` 契约；CLI/gateway/TUI 本来就会自动提交 |
| `set_flag(session, key, value)` | 发布协作式会话状态（如 `plan_mode`） | 只作元数据，不是授权；经 `turn_state()["flags"]` 读取 |
| `request_continuation(session, text, token=)` | 给 stop gate 留一条消息；每 session 同时只允许一条 | 新面；由 stop gate 消费 |
| `discard_continuation(session)` | 预算耗尽时丢弃，并把 token 标 `expired` | stop gate 用；插件据此退回 pending |
| `continuation_status(session, token)` | `pending` / `taken` / `expired` / `unknown` | 插件据此把没送到的建议退回 pending |
| `turn_state(session)` | `live/turn_id/platform/coding/pending_continuation` | 只报告宿主真有的值 |

设计约束：

- 全部内存操作、线程安全、不调用模型；后台 reviewer 可以直接调用。
- 回合结束即 `unbind`：`live_turn=False`，未消费的 continuation 标 `expired`。插件必须把 `ok=False` 当成"没送到"，不能假设送达。
- `steer` 复用 `/steer` 的既有不变量（独立 user row、不涂改已持久化的 tool row），但行类型是 `display_kind="adviser"`，不套 `[OUT-OF-BAND USER MESSAGE]` 的"用户直接发言"marker；文本本身仍以 `[adviser — advisory only]` 开头。宿主 UI 对未知 kind 目前按 user row 渲染，后续可按 kind 渲染成卡片（见 9.5）。
- 绑定点在 `agent/conversation_loop.py::run_conversation` 的 try/finally 内，异常路径也会解绑。

### 9.2 stop gate 的 continuation 通道

`agent/turn_stop_gates.py`（patch 0002）在 verify-on-stop / `pre_verify` / kanban 之前先查一次 `plugin_runtime.take_continuation(session)`：

1. 有请求且本回合用量 < `agent.max_plugin_continuations`（默认 2）→ 把候选答案作为 interim 落盘、追加一条 `display_kind="adviser"` 的 user row、`final_response=None`，继续循环。
2. 超预算 → 丢弃该请求（token 标 `expired`，插件据此把建议退回 pending），走原有 gate。丢弃而不是留到下一回合，避免一条旧建议在用户的新请求里突然出现。
3. 该行不是 synthetic flag，因此会持久化并在 resume 时重放——这是"用户能看到建议"的替代品；它也不占用 `max_verify_nudges` 的编码验证预算。
4. 每回合 `_plugin_continue_nudges` 在 `agent/turn_context.py` 的 `_PER_TURN_RESET_STATE` 重置。

为什么放在 verify-on-stop 之前：blocker 的时效性高于"改完代码要验证"，而两条预算彼此独立；如果先跑 verify，一次普通收尾会把 blocker 挤到下一回合。

### 9.3 投递顺序（插件侧）

`adviser_mode._deliver_host` 在 `store.add_advisories` 之后、`_maybe_notify` 之前运行：

| 顺序 | 条件 | 动作 | 失败回落 |
|---|---|---|---|
| 1 | 活回合 + severity ≥ `continue_severity` | `request_continuation` | 进 2 |
| 2 | 活回合 + severity ≥ `severity_floor`（immune 窗口内只放 blocker） | `steer`；blocker 用 `interrupt=True` | 进 3 |
| 3 | 其余 | ledger → `llm_request` / aside / gateway notify | 保持 pending |

竞态处理：host 调用前先 `stage_advisories` 预占，防止 `llm_request` 同时把同一批建议注入；host 接受后标 `injected`，拒绝则 `reset_staged` 放回。若该回合已经出了终局回答（`_terminal` 有值），`_deliver_host` 直接返回，不做 continuation：stop gate 已经跑过，强塞只会和"回合结束"竞态；这些行留给 gateway notify 或下一回合的 aside。continuation 的 token 记在 `_host_tokens`，下一回合 `on_pre_llm_call` 调 `_reconcile_host_delivery`：`taken` 结束，其余状态把行退回 `pending`（新 `store.release_advisories`），避免"标了 injected 但没人看过"。

### 9.4 打包、安装、回滚

宿主补丁不能靠插件加载时 monkeypatch（代码已 import、进程不会重载）。它们以 `git format-patch` 系列存放：

```
plugin/minir/host-patches/
  manifest.json     series/capability/base_version/base_commit + 每个 patch 的 sha256
  0001-...patch     plugin_runtime + run_conversation 绑定 + 单测
  0002-...patch     stop gate + 每回合预算 + 单测
  0003-...patch     Desktop 的 adviser transcript card + hydration/等价性 + 单测
  0004-...patch     history/compaction 把 adviser row 当"user-shaped but not human"处理
  0005-...patch     plugin interrupt、post-turn trigger、协作式 session flags
  0006-...patch     message.adviser 实时事件 + gateway/Desktop/TUI 消费
  0007-...patch     TUI adviser 卡（severity、hydrate）
  0008-...patch     classic CLI 的 adviser 块
  0009-...patch     web session inspector 的 adviser 标签
  0010-...patch     preserve writer：回合末尾追加卡片并 flush 进 session DB
  0011-...patch     web inspector 按 severity 着色 + turns-ago
  0012-...patch     adviser trigger 回合标 display_kind；adviser steer 不计用户介入
  0013-...patch     web 原生聊天 transcript（gateway WS + adviser 卡 + 工具/审批）
scripts/install.py              复制插件 → 应用系列 → 验证
scripts/install_host_patches.py 应用/检查/回滚（可单独运行）
```

安装器行为：

- 先 `git apply --check`，再 `git apply`；只查/写 patch 涉及的文件，宿主上无关的本地改动不受影响。
- 冲突、本地改动覆盖到同一文件、manifest 校验和不符 → 退出码 2，不写任何文件。
- 应用前把涉及文件复制到 `$HERMES_HOME/minir-memory/host-patches-backup/<timestamp>/`；应用后 `compileall` + 用宿主解释器 import `hermes_cli.plugin_runtime` 与 `agent.turn_stop_gates`，失败自动逆序回滚。
- 状态写 `$HERMES_HOME/minir-memory/host-patches.json`；重复运行按"已应用"跳过（幂等）。
- 系列按前缀判定"已应用"：后一个 patch 可以修改前一个新建的文件，所以不能拿每个 patch 的反向检查独立判断；`--check` 只对第一个未应用 patch 做独立检查，其余标 "after predecessors"。
- `--check` 只报告；`--revert` 逆序撤销并验证模块消失；宿主升级后 `git apply --check` 失败时，安装器报告系列期望的基线版本，不做 `reset`/`stash`。

自动应用的边界：Hermes 的 `hermes plugins install` 没有 post-install hook，所以"安装时自动应用"落在仓库的 `scripts/install.py`（一条命令完成复制+打补丁）。宿主升级会覆盖补丁，重跑安装器即可；状态文件保留，已补的 patch 会被识别。

### 9.4.1 UI 补丁（0003）

Desktop 的 transcript 水合把 `display_kind="adviser"` 当作 machine notice 处理：

- 水合成 `role="system"`，不再显示成用户 prompt；把渲染块解析成 `severity`/`label`/`body`/`items`。
- `chat-runtime` 把解析结果放进 `metadata.custom.adviser`；`SystemMessage` 渲染成带 severity 着色（blocker 红、concern 橙、其余 tertiary）的卡片，正文走 Markdown。
- `chatMessagesEquivalent` 比较 `adviser`，否则 resume 时内容等价会阻止卡片重绘。
- 解析不了的行仍然显示（`severity="advisory"`），不丢内容；普通 user row 不受影响。
- 覆盖范围是 transcript 水合、resume 和 REST prefetch。宿主在注入行时也会发 `message.adviser`
  事件；Desktop 收到后立即 append 一张 card（同一 `plugin_message_id` 使后续 hydrate 变成 no-op），
  所以卡片在回合中就能出现，不必等重载。

### 9.4.2 Adviser 历史（TranscriptBuffer，无 DB）

`_record_adviser_transcript` 把每次 review 的 `result.messages` 追加到同一个 adviser 会话：

- `record_aux("adviser", "<session>--<adviser>", messages, usage=...)` 写
  `<HERMES_HOME>/minir-memory/sessions/adviser/<session>--<adviser>.json`；这是 adviser 的完整会话，
  不是单次 review 的快照。
- 文件里带 `model/provider/toolsets/system_message/usage`；token 与 cost 来自 `agent_loop` 返回的
  turn 计数，不写任何 SQLite 行。
- 进程内保留同一条 thread（4000 条 / 400k 字符上限），用于下一条 prompt 的
  `<recent_adviser_turns>` 和 `minir_stats` 的进程级 usage；重启后文件仍在，连续性从新线程开始。
- `TranscriptBuffer` 仍是唯一的写者：原子写、重试、4000 条上限、30 天 prune 都不变。

### 9.4.3 Adviser row 的宿主语义（0004）

- `_merge_consecutive_users`：如果不排除 adviser，下一条真实用户消息会被原地合并进已持久化的建议行，改写用户从未说过的话。
- `_pending_tool_round`：建议行紧跟在最新 tool result 之后；如果不跳过尾部的 adviser 行，"未回答的工具轮次"会被判空，压缩时 tool result 会错误配对。

两处都改成与 `/steer` 行相同的白名单。建议行保持 byte-identical，压缩仍把 tool result 和模型回复配对。

### 9.5 现在的能力边界

| 能力 | 之前 | 现在 |
|---|---|---|
| 中途 steer | 只能改下一次请求副本 | `agent.steer(kind="adviser")`，下一安全边界生效；blocker 可用 `agent.interrupt` 立即停止等待当前工具批次（协作式：不轮询的工具被放弃） |
| blocker 阻断工具 | `pre_tool_call` block | 保留；blocker 先 interrupt，再以 steer 行给出理由 |
| 终局后续跑 | 仅 `pre_verify`（改过文件） | 任意文本收尾可 continuation；terminal advice ≥ `trigger_severity`（默认 concern）可 `queue_post_turn` 让 surface 自动开新回合（每 session 1 次） |
| preserve 卡片 | 只有 ledger / gateway 消息 | 四个宿主 surface 都按 `display_kind="adviser"` 渲染 adviser 块：Desktop 水合成 advisory card（severity 着色、label、Markdown 正文）；TUI 画 `◆ adviser` 卡（live `message.adviser` 事件 + 水合行，按 `plugin_message_id` 去重），Web chat 走 TUI 的 PTY 自动继承；classic CLI 打印带 label 的块（流式回复框结束时释放，resume recap 单列）；Web session inspector 用 adviser 样式显示 label 与正文 |
| plan mode | 拿不到 | 宿主 `set_flag` 可发布 `plan_mode`；另外 adviser 从 `plan_mode` 工具的 `on/off/submit/status` 结果镜像状态（插件装了就生效）。为真时只展示，不 steer/interrupt/trigger |
| reasoning 流 | 拿不到 | 插件侧已可订阅 `on_stream_delta(kind="reasoning")`，但需宿主 `plugins.stream_reasoning_deltas: true` 且插件 `include_reasoning: true`；prompt 取全量（`max_reasoning_chars` 可设上限），全量落盘 `sessions/reasoning/<session>.log`（无字符上限） |
| reviewer 会话 | 每次 review 新起 AIAgent，只回灌最近 3 批建议 | `AdvisorRuntime` 常驻：cursor、history、turn 计数、暂停状态；`agent_loop.run` 把 history 作为 `conversation_history` 传入 |
| delta 保真 | args 500 / result 700 / error 300，单事件 1200 | transcript delta：args/result/error 不设单字段上限，事件级 reasoning 取 12k 预览；ring 96，prompt 24k 只丢最旧整事件，单个超大事件完整保留 |
| turn 语义 | 无 turns_ago / 聚合 / guidance / deferred | note 带 `turn` 和 `deferred`；turn-end 聚合 pending（单块 ≤ `max_advices`，其余留到下块）；每 50 turn 附 `[boundary-guidance]`；deferred 只在 turn-end 出现 |
| roster | 最多 4 个 | 无上限；每个成员独立 runtime/host session；`paused: true` 或 usage-limit 只停该成员 |
| 每 adviser 成本面板 | 自记 | 不变 |

### 9.6 配置增量

`memory.minir.adviser_mode`（`_DEFAULTS`）：

```yaml
  host_steer: true        # 活回合时优先用宿主 steer
  host_interrupt: true    # blocker 停止当前工具批次（协作式）
  interrupt_severity: blocker
  host_continue: true     # severity ≥ continue_severity 时请求续跑
  trigger_turn: true      # terminal advice ≥ trigger_severity 可让 surface 开新回合
  trigger_severity: concern
  max_post_turn_triggers: 1   # 每 session，防自激
  preserve: true            # 终局无动作的建议落成回合末尾卡片（0010）
  shutdown_wait_ms: 5000    # 退出前给 in-flight review 的预算；strict 取 strict_max_ms
  watchdog_roster: true   # 合并最近 WATCHDOG.yml 的 advisors[]
  roster: []              # {name, model?, instructions?, toolsets?, cadence?, review_mode?}
  include_reasoning: true   # 需同时开宿主 plugins.stream_reasoning_deltas
  max_reasoning_chars: 0    # 0 = prompt 用全量；>0 只保留末尾 N 字符
  reasoning_log: true       # include_reasoning 开启时，全量 reasoning 写 sessions/reasoning/
  pause_seconds: 300        # usage/rate-limit 失败后该 adviser 的暂停时长
  failure_pause_seconds: 60 # 连续失败达到上限后的短暂停
  max_consecutive_failures: 3
  boundary_every: 50        # turn-end block 每 N turn 附 boundary guidance
  history_messages: 60      # 模型侧 history 最多条数
  history_chars: 120000     # 模型侧 history 最多字符数
  sync_backlog: off       # off | wait(<=60s) | strict(<=strict_max_ms)
  strict_max_ms: 30000
```

宿主 `agent.max_plugin_continuations`（默认 2）是安全阀，和插件的 `immune_turns`、`continue_severity` 正交。
roster 的 `cadence` 只约束定时 tool review；final/error/mutation/显式 review 不受它限制。
`review_interval` 按 eligible update 计数（tool 批次和回合结束各算一次），被跳过的 update 留到下一次 review。
终局 `nit`/未达 `trigger_severity` 的 note 走 `preserve`：`post_llm_call` 捕获的 writer 在回合末尾追加
`display_kind="adviser"` 行（`display_metadata.preserved=true`），flush 进 session DB 并发 `message.adviser`，
不另起回合；无 0010 的宿主自动回退到 ledger/gateway 通道。若 review 完成时下一条用户消息
已经开启了新回合，捕获的 writer 按 turn 号作废，建议回退到 ledger/steer，避免旧卡片排在新消息之后。
reviewer 固定只读：`agent_loop` 在运行前移除 `write_file`/`patch` 以及 memory 写入/派发工具；历史只写
`sessions/adviser/`，不进 `state.db`。

### 9.7 验证记录

| 检查 | 命令 | 结果 |
|---|---|---|
| 宿主补丁单测（worktree，v0.22.2） | `python -m pytest tests/agent/test_plugin_runtime.py tests/agent/test_plugin_continuation_gate.py -q` | 16 passed |
| 宿主回归（stop gate 邻近） | `python -m pytest tests/agent/test_verification_continuation_budget.py tests/agent/test_verification_stop.py tests/agent/test_verify_hooks.py tests/agent/test_turn_finalizer_cleanup_guard.py -q` | 30 passed, 1 skipped |
| 真实安装 | `python scripts/install.py --check` → `python scripts/install.py` | apply 6, verified `plugin_runtime_v1` |
| 幂等 | 再跑 `python scripts/install.py` | 6 个 "already applied" |
| 安装后宿主单测 | 在 `hermes-agent` 下跑同样两个测试文件 | 16 passed |
| Desktop 类型检查 | `apps/desktop && ../../node_modules/.bin/tsc -p . --noEmit` | exit 0 |
| Desktop 相关回归 | `vitest run use-message-stream steering-recovery chat-messages system-message` | 504 passed |
| Adviser row 语义 | `pytest tests/agent/test_steer.py -k adviser`（live host） | 2 passed |
| 宿主相关集合 | `pytest test_steer test_protected_tail_pressure test_plugin_runtime test_plugin_continuation_gate` | 48 passed, 3 failed（本机 `home_io_guard` 误报真实 HERMES_HOME，与补丁无关） |
| 插件契约 | `hermes plugins validate plugin/minir` | Validation passed（仅既有 private-core-import 警告，新增的 `hermes_cli.plugin_runtime` 是补丁提供的面） |
| 插件套件 | `python -m pytest` | 539 passed, exit 0 |
| Adviser 历史/roster | `pytest tests/test_adviser_history.py tests/test_agent_loop.py tests/test_transcript.py` | 全绿（历史落盘、usage、WATCHDOG 合并、per-entry cadence、reasoning、strict 配置） |

### 9.8 reviewer 自身：常驻 runtime（已实现）

| omp 行为 | 本实现 |
|---|---|
| 常驻 `AdvisorRuntime`，保留同一个 adviser 会话和 cursor，增量喂 delta | `AdvisorRuntime` per `<session>--<adviser>`：cursor/messages/reviewed_turn/failures/paused_until；`agent_loop.run(history=...)` 把 history 作为 `conversation_history`；runtime 首次创建时从 `sessions/adviser/<id>.json` 恢复，重启后仍能接上 |
| bounded transcript delta，含完整工具结果和 reasoning | 事件存完整 args/result/error，reasoning 取事件级 12k 预览（全文在 reasoning 文件）；prompt 渲染 `<transcript_delta>`，总量 24k 只丢最旧整事件，单个超大事件完整保留，丢旧事件时插 `[... N older events omitted ...]` |
| reasoning 完整文件落盘、无字符限制 | `on_stream_delta` 同时写全量 prompt（`max_reasoning_chars` 可设上限）和 `sessions/reasoning/<safe-session>.log` 全量 append；每个 turn/工具边界 flush |
| note 带 turns_ago、回合结束聚合、50 turn boundary-guidance、单条 deferred | `advisories.turn/deferred` 列；`render_block(current_turn, guidance)` 加 `(N turns ago)` 和 `[boundary-guidance]`；`trigger=final` 读取 pending（单块 ≤ `max_advices`，其余留到下块）；中途通道跳过 deferred 和旧 turn 的非 blocker |
| roster 无上限，每 adviser 独立 agent/tool session，可单独暂停 | `_advisers` 不再截断；每个成员独立 runtime + `minir-adviser:<session>--<name>` host session；`paused` 条目或 usage-limit 失败只暂停该成员（默认 300s） |

测试：`tests/test_adviser_runtime_persistent.py`（history、full result、reasoning 文件、deferred、turns_ago、roster、pause、restart resume）、`tests/test_adviser_host_bridge.py`（turn-end 聚合）。全量 542 passed。

### 9.9 未决

- `host_continue: true` 只是默认值；第一次真实会话应观察 continuation 频率。如果 concern 触发太多，把 `continue_severity` 提到 `blocker`。
- interrupt 是协作式的：轮询 `is_interrupted()` 的工具会停，不轮询的只是被放弃，线程/副作用可能继续；显式 `agent.interrupt` 仍会把回合标成用户介入，adviser 的 steer 不再置该标记。
- `trigger_turn` 依赖 surface 自动提交 leftover `pending_steer`；CLI/gateway 会在 0012 之后把该行持久化成 `display_kind="adviser"`，所以 trigger 回合也显示成卡片。Web chat 默认走 0013 的原生 transcript，PTY 仍可一键切换。未打补丁的 surface（消息平台等）仍按原始 user 行处理。
- plan mode 有两条来源：宿主 `set_flag`（需插件发布）和 `plan_mode` 工具的 `on/off/submit/status` 结果镜像。plan-mode 插件未安装时 flag 保持 unknown，adviser 行为不变。
- 实时卡片已由宿主 `message.adviser` 事件驱动；如果客户端断线，下一次 hydrate 仍会补上（同一 `plugin_message_id` 去重）。
- reasoning 默认开启：宿主 `plugins.stream_reasoning_deltas` 与插件 `include_reasoning` 都开才生效；prompt 取全量（`max_reasoning_chars: 0`），全量写 `sessions/reasoning/`，不写进 adviser transcript。
- roster 已无上限；配置错误会启动很多 reviewer，由 operator 负责。`paused: true` 只跳过该成员。
- strict backlog 现在是"有界 strict"：`strict_max_ms` 默认 30s，`llm_request` 仍同步执行；无界等待会让 provider 请求和 gateway 头阻塞，不建议。
- 宿主升级后 patch 冲突需要人工重做 hunk；`base_version`/`base_commit` 是提示，不是自动 rebase。
- 已应用的宿主改动在 `git status` 里是未提交的工作区修改；这是有意的（升级不会因为本地提交而 fast-forward 失败），但安装器状态文件才是"哪些 patch 已应用"的权威。
