# JEV fork 层 · 设计草稿（插件内中央调度）

状态：草稿，落在本插件 `docs/` 下。没有接线、没有建表、没有建文件、没有改 config；§5.1 依赖的 0019 已由宿主原生提供（2026-10-30 随 `eb3c31cc72` 落地，见 `hermes_cli/plugins_dispatch.py` 的 `_CALLBACK_TIMEOUT_ATTR` / `_callback_budget`），本稿不再需要写补丁。代码块只说明形状，未抽出去跑。

依据：本仓库 HEAD 工作树（`git status` 8 个已改文件，本文不碰它们）；宿主源码 `C:\Users\zarag\AppData\Local\hermes\hermes-agent`；另见 §1 的行号。

## 0. 一句话

JEV 是插件里唯一回答「这个分叉能不能不叫醒主模型」的地方：确定性规则能判的就地判（sharp），判不了的交给 adviser 裁决一次，裁决结果成为下一次的规则。中央调度 = 一个 dispatcher + 一份可读的规则文件 + 一本台账；adviser 仍是唯一的语义裁决者，但它不执行、不新增模型路由。

### 0.1 操作者决定（2026-10-27）

1. **merge gate 不退化成显式 consult。** 操作者原话：「第三条 fail-closed 通过 host patch 将时间延长。」按此执行：用 host patch 把 `pre_tool_call` 的 30 s 默认上限按 callback 延长到 120 s，让闸门在 `pre_tool` 里同步问 adviser；fail-closed 语义不变，超时仍然阻断（§5.1）。
2. **target 词表只加 `delegation`。** plan / merge / ship 三次发言分别落在 `current_step`、`current_step` + gate、`current_step` / `final_answer`，不新增三个阶段词（§4.4）。
3. **阶段触发按截图做。** plan gate 在计划工件上强制复盘；merge gate 在 `pre_tool` 同步裁决；ship gate 在 `pre_verify` 上触发（§4.5）。

## 1. 事实基线

### 1.1 宿主给的可动手判定点

| hook | 能做什么 | 不能做什么 | 证据 |
| --- | --- | --- | --- |
| `pre_tool_call` | `block` / `approve` / `modify`（浅合并参数） | 给结果、换工具名 | `hooks.md:548-592`；`plugins_dispatch.py:59-60,67` |
| `transform_tool_result` | 替换结果字符串（第一个 `str` 生效） | 在执行前给结果 | `hooks.md:1671-1685`；`plugins_dispatch.py:42-52` |
| `llm_request` | 注入一段建议 | 改工具调用 | `__init__.py:2409-2415` |
| `post_tool_call` / `pre_llm_call` / `post_llm_call` | 观察、记账 | 改变当前调用 | `__init__.py:2257-2335` |

两条超时语义决定了 hot path 的形状：

- `pre_tool_call` 超时或异常是 **fail-closed**，工具直接被拒（`plugins_dispatch.py:59-60`）。所以 `resolve_now()` 里不能有模型调用，也不能有慢 I/O。gate 路径是唯一例外：它按 callback 延长预算，仍然 fail-closed（§5.1）。
- `transform_tool_result` 超时会被放弃，结果保持原样（`plugins_dispatch.py:42-52`）。所以它可以做一点计算，但不能被当成可靠的闸门。

### 1.2 插件已有的决策面

| 现有物 | 位置 | 与 JEV 的关系 |
| --- | --- | --- |
| `claims.PREDICATES` + `matches` | `claims.py:22-23,222-240` | operator 闸门仍在用；fork 规则不复用这套谓词，键归一化是另一套（§8.1） |
| operator claim 闸门 | `adviser_mode.py:2510-2572` | 仍是唯一的 operator 阻断路径；JEV 不改它的优先级 |
| live blocker 拒绝下一个变更工具 | `adviser_mode.py:2516-2545` | 与 retry 守卫并列，block 优先 |
| adviser 事件环 + 复盘队列 | `adviser_mode.py:1221-1260,1295-1333` | JEV 的 fork 候选从这里取，不新增事件源 |
| 复盘流水线 | `adviser_mode.py:1820-1919` | 插入点：`_select_claims` 旁边加 `_select_forks` |
| 同步咨询 | `adviser_mode.py:2098-2105` | 慢判定的执行体；只在 worker 线程里调 |
| 单一 Scheduler | `__init__.py:393-397`；`scheduler.py:111,115,185` | JEV 不新建时钟 |
| `minir_agents` 后台执行 | `minir_agents.py:15-22` | 工作分发已经存在；v1 的 `route` 只服务编排器 |

### 1.3 图里 JEV 的原义

截图口径：一次调用 < 0.5 s，判 1,683 个 fork；sharp 就地进代码，split 上交主脑；主脑看不到 sharp 那部分。

其中可搬的是「分叉判定 + 只上交少数」，不可搬的是「主脑之外的第二次生成」——宿主没有那个 seam，插件只能在主模型发出调用之后动手。§2.3 把四条物理上限写死，设计不假装能绕过。

## 2. JEV 是什么 / 不是什么

### 2.1 定义

fork 是一次有 ≥2 个可行动作、且其中一个可以不经主模型决定的分叉。只有一个可行动作的地方不是 fork，dispatcher 直接放行。

JEV 只回答一个问题：这个 fork 有没有一个可判定的就地动作，是什么？

- 有：返回 `Verdict`，由 hook 或调度器执行。
- 没有：`split`，什么都不做，主模型照常决定。
- 判不准：`split`，并把候选写进台账，等 adviser 下一次复盘。

### 2.2 哪些 fork 能 sharp

| kind | 何时可见 | sharp 动作 | 机制 | 只能 split 的情形 |
| --- | --- | --- | --- | --- |
| `arg` | `pre_tool_call` | 改写参数：路径归一、根目录、默认 flag | `modify` | 要换工具、要改意图 |
| `retry` | 同一键第二次失败后的 `pre_tool_call` | 拒绝重复，回带上次错误摘要 | `block` | 要换方案 |
| `scope` | `transform_tool_result` | 把大结果换成带行号的摘要 | 替换结果 | 摘要会丢判据 |
| `route` | 编排器 tick / 插件自己的 worker | inline handler 或固定 agent | `inline` / `dispatch` | 目标含糊、无 handler |

`route` 只服务插件自己拥有的执行（编排器节点、以后的 agent 派工），不参与主会话的工具选择。

### 2.3 物理上限（四条）

1. **抢先工具选择做不到。** 主模型已经发出调用，插件只能改参数、拒绝、压缩结果。图里「sharp 直接进代码，主脑看不到」在这里只能兑现成「主脑看到的是蒸馏后的结果」。
2. **首次出现的 fork 不可能 < 0.5 s。** 判定要模型，模型调用是秒级。小于 0.5 s 只对重复出现的 fork 成立，靠规则命中，不靠推理。
3. **1,683 个思考分叉看不到。** 插件能观察的是工具级分叉，一个回合几十个量级。设计按可观察的分叉算，不按图上的数字算。
4. **30 s 的 fail-closed 上限不是物理上限。** 它是宿主的默认预算；本稿按操作者决定用 host patch 按 callback 延长到 120 s（§5.1）。延长只改变「闸门能等一次裁决」，不改变第一条：主模型已经作出的工具选择仍然抢不到。

## 3. 中央调度：一个判定入口，三档速度

### 3.1 三条路径

```
        pre_tool_call / transform_tool_result / scheduler tick
                              │
                    operator 闸门（既有路径，block 优先）
                              │
              ┌───────────────┴────────────────┐
      integration 调用?                     其他 fork
              │                                │
   resolve_gate()（同步，≤120 s）      resolve_now()（内存查表）
              │ go / no-go                     │ sharp / split
              ▼                                ▼
        放行或阻断                    rewrite / compress / inline
                                               │ 无规则
                                     resolve_slow()（worker 线程）
                                               │
                                      adviser.consult(timeout)
                                               │ verdict
                                      learned.json（low，shadow）
                                               │ 再命中 2 次
                                      升 medium，开始生效
```

- operator 闸门在 hook 里先跑，`block` 永远赢；dispatcher 不重复判定 operator，也不覆盖既有拒绝。
- `resolve_now()`：只做内存查表，不调模型，不写库。operator 闸门与 spec route 由调用方先跑，dispatcher 只看规则表。普通 fork 只走这条。
- `resolve_gate()`：只对声明的 integration 调用（`git merge` / `rebase` / `cherry-pick` / `apply` / `patch` / `gh pr merge`）同步咨询 adviser；`go` 返回 `None`，`no-go` 或超时返回 `block`。它不用于普通工具调用，也不写 `learned.json`（§5.1）。
- `resolve_slow()`：只从 adviser 复盘线程或 `Scheduler` 的时间任务里调；超时返回 `split`，绝不从 hook 里调。

### 3.2 Verdict 与权威阶梯

```
Verdict(fork_id, key, decision, source, strength, payload, reason)
decision ∈ {rewrite, compress, inline, dispatch}   # 规则表只有这四种
source   ∈ {rule, spec, adviser, guard, gate}
```

`block` 不来自规则表。dispatcher 的阻断只有两处：内置的 retry 守卫与 operator 授权的 integration gate；operator 阻断仍由现有的 claim 闸门与 `block_reason` 负责（`adviser_mode.py:2490-2545`）。学出来的规则永远没有能力让一次调用停下。

| strength | 来源 | 改写参数 | 压缩结果 | 选执行体 |
| --- | --- | --- | --- | --- |
| `exact` | SOUL / `source: operator_edit` 的卡 | 是 | 是 | 是 |
| `high` | 确定性谓词、图文件里的 spec route | 是 | 是 | 是 |
| `medium` | adviser 裁决 + 2 次命中 + 0 次推翻 | 是 | 是 | 是 |
| `low` | adviser 裁决一次 | 只 shadow | 只 shadow | 只 shadow |
| `none` | 无 | — | — | — |

不来自规则表的阻断有两处：operator claim（既有路径）与 retry 守卫（固定谓词，不可学习）。gate 是第三条，只在操作者显式开启时存在。这条与 `claims.apply_ceiling`（`claims.py:272-293`）同一条阶梯，不另立一套。

`gate` 不是学出来的规则，是操作者把闸门交给 adviser。`gates: on` 就是这份授权，所以 gate 的 `no-go` 和超时都能阻断。它只作用于 integration 调用，不写入 `learned.json`，也不改变「学出来的规则不能阻断」。


### 3.3 升级与回退

- 命中 `hits += 1`，写 `fork_stats`。
- 主模型重复了被拒绝的调用，或最终答案反转了被压缩的结论：记 `overturn`。
- `hits >= 2` 且 `overturns == 0`：复盘线程把 `learned.json` 里的 `strength` 从 `low` 改到 `medium`。
- 一次推翻：降回 `low`；两次推翻或超过 TTL：从 `learned.json` 删除。
- 所有升降级都只改那一份 JSON，SQLite 只记计数和台账；规则本身始终可读、可 diff、可手改。

### 3.4 「中央」的边界

| 集中 | 不集中 | 理由 |
| --- | --- | --- |
| 分叉判定、规则表、台账 | `auxiliary.*` 的模型路由配置 | 判定需要单一权威；路由是预算决策，各有自己的配置面 |
| adviser 的 fork 裁决入口 | 抽取、卡审、知识页的内部策略 | 那些是各自领域的判断，塞进 dispatcher 只会得到一个 god object |
| 编排器的 `route` 决策 | 子代理的 prompt、工具集与生命周期 | dispatcher 只选谁去执行，不规定怎么执行 |
| 一个时钟上的 fork 任务 | 既有时间任务的周期 | `Scheduler` 已经是唯一时钟（`__init__.py:393-397`），另起一个只增加竞态 |

### 3.5 route 的判据

`inline` 只在三个条件全中时成立：节点 stage 在 `INLINE_STAGES`（`fork` / `merge`）、有一个注册的 handler、验收是机械的（计数、相等、文件存在、退出码）。三者缺一就 `dispatch`。

没有 handler 的 fork 节点不是错误：按 spec route 派给 agent，JEV 只把这次判定记进台账。handler 注册表由插件自己维护，不调模型，所以「inline 会不会接错活」是一个可测的集合问题，不是概率问题。


## 4. adviser 的分工

### 4.1 它裁决什么

复盘增加一块输入 `active_forks`（0-3 条），每条给：

```
id、kind、key、tool、args 摘要、最近一次结果或错误、为什么现在问（谓词或重复次数）
```

候选按三条打分取前 3：本轮有错误或重试、同一键在本轮出现 ≥2 次、与最近一条用户目标有词重叠。已经被生效规则覆盖的 fork 不再问——问一次就够，重复问只会烧预算。

复盘输出增加一个字段：

```json
{"forks": [
  {"id": "f1", "decision": "rewrite",
   "action": {"args": {"path": "C:/.../doc/x.md"}},
   "reason": "两份同名草稿，活的是这份"}
]}
```

解析规则：

- `id` 不在本轮候选里 → 丢弃。
- `decision` 不在 `{rewrite, compress, inline, dispatch}` → 丢弃。复盘裁决不接受 `block`：阻断只走 operator 闸门、retry 守卫与 gate（§5.1）。
- `action` 必须是 ≤2 KB 的 JSON；路径只允许绝对路径或项目相对路径，越界即丢弃。
- `rewrite` 只接受 `args` 字典，且只能改原调用里已经存在的键；新增键一律丢弃。改参数不等于给工具换语义。
- 没给 `reason` 的裁决不写规则，只记台账。

### 4.2 它复核什么

下一次复盘拿到本轮生效过的 sharp 决策（从台账读），逐条给 `accepted` / `overturned`。推翻必须带证据（用户改回来了、测试失败、diff 显示改错）。

### 4.3 它不做什么

- 不执行 sharp 动作，不写文件；复盘裁决不阻断调用，只有 gate 例外（§5.1）。
- 不新增 `auxiliary.minir_fork` 路由：v1 复用 `auxiliary.adviser`（`adviser.py:1-8,30-44`），避免第二个大脑和第二个预算。
- 不在普通 hook 里被调用。唯一的例外是 `resolve_gate()`：操作者显式开启 `gates: merge`，host patch 把该 callback 的预算延长到 120 s，超时仍 fail-closed（§5.1）。
- adviser 关闭时 JEV 只跑静态规则；`forks: on` + adviser 关闭要在启动时打一条警告，不静默假装会学习。

### 4.4 target 覆盖：截图的三次发言

现有 14 个 target（5 action + 2 artifact + 7 consistency）够表达，不够复现。逐条落点：

| 截图发言 | 落点 target | 处理语 | 断点 |
| --- | --- | --- | --- |
| plan「正路吗？先跑迁移」 | `current_step`（计划就是下一步的序列）；理解错目标才是 `user_intent` | blocker: stop; re-plan | 计划不是工件，第一次工具调用前看不到 |
| merge「go/no-go」 | `current_step`（合并就是下一个动作）；命令本身写错才 `tool_choice` | blocker / concern | 后台 review 是异步的，建议可能晚于 merge；由 §5.1 的 gate 补 |
| ship「漏了什么？加 1 个测试」 | `current_step`（去跑测试）；只是完成声明不实才 `final_answer` | blocker | 选错 target，agent 会改措辞而不是跑检查 |
| 其余时间沉默 | 无 target，靠 EmissionGuard | — | `review_interval=1` 时每个 eligible update 都复盘，与沉默相反 |

词表只加一个 `delegation`：

- `advice_target.md:100-102` 明确把 SOUL 的 `# Delegation & Kanban` 排除在 soul_* 判断面之外。
- 截图有 3 个子代理，「交出去什么上下文、验收是什么、子代理的结论要不要复验」没有 target 承载。`tool_choice` 只管 `delegate_task` 的参数，`current_step` 只管「去派活」，都不叫 handoff。
- 处理语：blocker「不要派出 / 不要接受子代理的结论，先用一个具名检查复验」；concern「收窄上下文，写明验收」。
- 落点五处：`prompting.py` 的 `ADVICE_TARGETS` 与 `ACTION_TARGETS`、`advice_target.md`、复盘 prompt 的 TARGETS、解析测试、统计。
- `prompting.py` 里 `ADVICE_TARGETS`（带处理语）与 `ACTION_TARGETS`（只有名字）是两份手写清单，必须同时加；再补一条 `set(ADVICE_TARGETS names) == set(ACTION_TARGETS)` 的测试，防止下一次漂移。
- 不加 `plan` / `merge` / `ship`：`current_step` 与 `final_answer` 的处理语已经覆盖，再拆只把统计拆碎，行为不变。

```python
# file: plugin/minir/prompting.py
ADVICE_TARGETS = (
    ...,
    ("delegation", "the handoff to a subagent and the acceptance of its result",
     "blocker -> do not hand off or accept the child's claim; verify with a named check. "
     "concern -> narrow the context and name the acceptance. nit -> optional."),
)
ACTION_TARGETS = ("tool_choice", "current_step", "final_answer", "user_intent",
                  "general", "delegation")
```

### 4.5 三个阶段门

| gate | 触发 | 形态 | 不通过时 |
| --- | --- | --- | --- |
| plan | 计划工件写入（`todo_list` 或 interim 文本）；没有工件就退到第一次变更调用 | 强制复盘 `force=True`，异步送达 | 建议，不阻断；主代理下一轮自己改 |
| merge | `pre_tool_call` 认出 integration 命令 | 同步 `resolve_gate()` | 阻断（含超时），§5.1 |
| ship | `pre_verify` / `transform_llm_output` | 一次 `verify_continue` + `current_step` 建议 | 不通过就再验一次；建议必须落在 `current_step` |

输入：三次 gate 都吃本回合的完整 digest（用户消息、计划、工具调用与结果、interim 意图、待执行的命令），从 transcript 拼，截到 ~8k 字符；不是复盘用的 delta。

gate 只对主回合生效：adviser 触发的回合（host patch 0012 打了标签）不再 gate，否则咨询会递归成自己问自己。

预算：每回合最多 3 次 gate + blocker 触发的复盘；`review_interval` 从 1 提到 8（或 `review_mode=agent-end`），把步中闲聊关掉。截图里的 6 次调用是预算结果，不是 target 的结果。

### 4.6 `minir_advise`：拉式入口

`minir_advise` 是主代理主动叫顾问的工具；merge gate 是推式闸门，两者不互相替代。

```
minir_advise(phase: "plan" | "merge" | "ship", question?: str)
  -> {verdict: "go" | "no-go" | "concern", target: str, advice: str}
```

- 放在 `tools.py` 的 `TOOL_NAMES` 与 schema 表里，和 `minir_todo` 同级。
- 内部调 `consult()`（`adviser_mode.py:2098`），`context=` 传本回合 digest；不新开模型路由。
- 只读：不写文件、不改 config、不记结论；返回一段带 target 的建议。
- 防自咨询：adviser 的 toolset 不含 `minir_advise`；handler 再检查当前 turn 是不是 adviser 触发的 turn，是就拒绝。
- plan / ship 用拉式；merge 用推式，因为「不通过就不许合」不能靠主代理自觉。

## 5. 接线点

| 位置 | 改什么 | 注意 |
| --- | --- | --- |
| `__init__.py:392` 后 | 构造 `self._fork = ForkDispatcher(self)` | 在 `_adviser_mode` 之后，两者互相可见 |
| `__init__.py:2115` 附近 | 新增 `transform_tool_result` 注册 | 用同一个 `_hook(...)` 包 provider |
| `_on_pre_tool_call`（`2322-2335`） | 先问 dispatcher；`block` 结果优先于 `modify` | file_guard 的拒绝仍排第一 |
| `_on_post_tool_call`（`2305-2319`） | 在 `_adviser_mode.on_post_tool_call` 之前喂 `observe_tool` | adviser 模式关闭时也要计数（`2850-2856` 会提前 return） |
| `adviser_mode._review_one`（`1855`） | `_select_forks(fresh, session)`；传进 `_prompt`（`1869`） | 与 claims 并列，不合并成一块 |
| `parse_review`（`542`） | 接受 `forks` 字段 | 未知 id、非法 decision 丢弃并计数 |
| `_apply_fork_verdicts` | 复盘成功后原子替换 `learned.json` | 单一写者：复盘 worker |
| `orchestrator_router.Router.decide` | 规则那一步改成问 dispatcher | 只在草稿里，模块尚未存在 |
| `Scheduler.add_time("fork_judge", …)` | 仅 `fork_judge=consult` 时注册 | 与记忆抽取共用一个时钟，不抢同一轮 |
| `tools.py` 的 `TOOL_NAMES` / schema 表 | 新增 `minir_advise` | 只读工具；不进 adviser 自己的 toolset |
| `fork.py` | 新增 `resolve_gate()` | 只由 `pre_tool_call` 的 integration 分支调 |
| `register()`（`2075`） | 给 pre_tool 包装器加 `__hook_timeout__ = 120.0` | 配合 host patch 0019，§5.1 |
| `host-patches/0019-*.patch` + `manifest.json` | 按 callback 读预算 | 不改全局 `hook_callback_timeout` |
| `scripts/verify_against_host.py` | 断言 0019 的读取路径 | 与现有 host 断言同一种写法 |

自调用防护：`minir_*`、`adviser_*` 工具一律不产生 fork，也不做结果替换，避免 dispatcher 自己的读写再触发自己。

### 5.1 host patch 0019：按 callback 延长 pre_tool 预算

决定（操作者，2026-10-27）：不改全局 `plugins.hook_callback_timeout`。那会把 `transform_tool_result`、`pre_llm_call` 等所有 bounded hook 一起拖到 120 s，一次插件挂死就让整轮都慢。改成 callback 自己声明预算，只延长 minir 的 `pre_tool_call`。

宿主现状：

- `hook_callback_timeout` 默认 30 s，最大 600 s（`plugins.py:1083-1108`；`plugins_dispatch.py:164-165`）。
- `pre_tool_call` 超时或异常 fail-closed，其他 bounded hook 超时跳过（`plugins_dispatch.py:256-294`）。
- 预算在 `invoke_hook` 里解析一次，然后传给 `_run_hook_callback_bounded`（`plugins_dispatch.py:275,281`）。

补丁 0019 只做三件事：

1. 新增 `_CALLBACK_TIMEOUT_ATTR = "__hook_timeout__"` 和一个 `_callback_budget(cb, default)`，读 callback 自己的声明，夹在 `(0, 600]`。
2. `invoke_hook` 调用 `_run_hook_callback_bounded` 前用 `_callback_budget(cb, timeout)`，不再直接传全局值。
3. 不碰 async 路径（`invoke_hook_async`）和 shell hook；`pre_tool_call` 走的就是 sync 路径。

diff 形状：

```diff
# file: hermes_cli/plugins_dispatch.py
+_CALLBACK_TIMEOUT_ATTR = "__hook_timeout__"
+
+def _callback_budget(cb, default):
+    declared = getattr(cb, _CALLBACK_TIMEOUT_ATTR, None)
+    if declared is None:
+        return default
+    try:
+        value = float(declared)
+    except (TypeError, ValueError):
+        return default
+    if value <= 0:
+        return default
+    return min(value, _MAX_HOOK_CALLBACK_TIMEOUT_SECS)
...
-                    ret = self._run_hook_callback_bounded(hook_name, cb, kwargs, timeout)
+                    ret = self._run_hook_callback_bounded(
+                        hook_name, cb, kwargs, _callback_budget(cb, timeout))
```

插件侧：

```python
# file: plugin/minir/__init__.py  (register)
pre_tool = _hook(_on_pre_tool_call, self)
pre_tool.__hook_timeout__ = 120.0
ctx.register_hook("pre_tool_call", pre_tool)
```

预算与语义：

- gate consult 预算 90 s（`gate_timeout_s`），callback 预算 120 s，留 30 s 给 digest 和解析。
- consult 超时 -> `resolve_gate()` 返回 block，消息写「adviser gate timed out or answered nothing after 90s」。fail-closed 不变。
- `no-go` -> block，消息带 adviser 的理由和 target；`go` -> `None`，工具照常执行。
- 只对 integration 命令 consult；普通 `read_file` / `terminal` 仍然是毫秒级 `resolve_now()`，不会付这 90 s。
- 全局 `hook_callback_timeout: 0` 仍然关闭 threaded path（`plugins_dispatch.py:276`），callback 声明不会把它重新打开；声明只在 threaded path 已开启时覆盖预算。

验证：

- 0019 已是宿主代码，没有安装步骤；`_CALLBACK_TIMEOUT_ATTR` 与 `_callback_budget` 直接读宿主文件核对。
- `scripts/verify_against_host.py` 加一条断言：`_CALLBACK_TIMEOUT_ATTR` 存在，且 `invoke_hook` 的 bounded 调用走 `_callback_budget`。
- host 侧探针：全局 `hook_callback_timeout=0.2`，一个声明 `__hook_timeout__=3` 的 pre_tool callback sleep 1 s 必须放行；同一个 callback 去掉声明必须被 fail-closed 阻断。这条能证明声明覆盖了全局值，而不是被默认值掩盖。

爆炸半径与回退：

- 声明是 host 级能力，任何插件都能用；没声明的 callback 仍按 30 s。写进 patch 的 commit message，避免变成 minir 私有 hack。
- 回退：从 manifest 移除 0019 重装，插件侧的 attr 变成死属性，预算回到 30 s；`gates: off` 是插件级回退。

## 6. 存储

### 6.1 规则文件（正文权威）

```
<HERMES_HOME>/minir-memory/forks/
  static.json    操作者手写，插件只读
  learned.json   复盘 worker 写，原子替换（tmp + os.replace）
```

条目：

```json
{"id": "r-arg-7f3a2c", "scope": "minir-memory", "kind": "arg",
 "key": "arg:read_file:{\"path\": \"C:/Users/zarag/projects/*\"}",
 "decision": "rewrite", "strength": "medium", "authority": "inference",
 "action": {"args": {"path": "C:/Users/zarag/projects/minir-memory/doc/x.md"}},
 "ttl_days": 14, "created_at": 1793000000}
```

放文件而不是表，理由与卡片、知识页、编排图一致：规则是内容，要能读、能 diff、能手改。SQLite 只存计数与台账，不存规则正文。

键是精确匹配，v1 不支持通配或正则：匹配语言会带来歧义与测试面，而 `path_family` 已经把同族路径折叠成同一个键。需要更宽的匹配时，先看键碎片率，再考虑加 `key_prefix` 字段，不先加正则。

### 6.2 表（生命周期与审计）

```sql
CREATE TABLE IF NOT EXISTS fork_stats (
  rule_id    TEXT PRIMARY KEY,
  scope      TEXT NOT NULL,
  hits       INTEGER NOT NULL DEFAULT 0,
  overturns  INTEGER NOT NULL DEFAULT 0,
  last_hit   REAL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS fork_ledger (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  turn_id    TEXT NOT NULL DEFAULT '',
  kind       TEXT NOT NULL,
  key        TEXT NOT NULL,
  decision   TEXT NOT NULL,
  source     TEXT NOT NULL,
  strength   TEXT NOT NULL,
  applied    INTEGER NOT NULL DEFAULT 0,
  outcome    TEXT NOT NULL DEFAULT '',
  note       TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS fork_ledger_session ON fork_ledger(session_id, created_at);
```

迁移跟 `_DIALECTIC_REFRESHES_SCHEMA` 同一个模式（`store.py:386-389,807`），`_SCHEMA_VERSION` 从 5 到 6（`store.py:73`）。

台账只记「有动作或有裁决」的 fork；纯放行只进 `metrics` 计数。否则每回合几十条，七天就把表撑成流水账。台账的 `decision` 额外接受 `split`，那是 P0 影子期的记录；规则表仍然只有四种。

### 6.3 作用域

- `scope` 默认是项目根目录名（`minir-memory`），不是会话 id：会话换了规则还该在。
- 同一 key 在两个 scope 下各有一份，跨项目不串。
- `static.json` 里的条目 `scope: "*"` 表示全 profile，仅操作者能写。

## 7. 配置

```yaml
adviser_mode:
  forks: off              # off | shadow | on
  fork_rules_max: 500     # learned.json 上限，满了先淘汰最久未命中
  fork_compress_chars: 4000
  fork_retry_limit: 2     # 第 N+1 次相同失败被拒
  fork_judge: review      # review | consult
  fork_ttl_days: 14
  gates: off              # off | shadow | merge；shadow 只记裁决，不阻断
  gate_timeout_s: 90      # consult 预算；callback 预算 120 s
  advise_tool: on         # minir_advise
  review_interval: 8      # 阶段触发之外，减少步中复盘
```

默认 `off`，行为与今天完全一致。新增键要同步 manifest、README 与 `verify_against_host.py` 的配置断言，按会话模式那一版的做法（`docs/draft/adviser-session-modes-draft.md:90-99`）。host 侧的 120 s 不是配置项，是 callback 上的 `__hook_timeout__`（§5.1）。

## 8. 代码骨架

### 8.1 键与阶梯

```python
# file: plugin/minir/fork_rules.py
"""Fork keys, matching and the strength ladder. No model, no I/O."""

from __future__ import annotations

import json
import re
from typing import Any

KINDS = ("arg", "retry", "scope", "route")
DECISIONS = ("rewrite", "compress", "inline", "dispatch")
STRENGTH = {"none": 0, "low": 1, "medium": 2, "high": 3, "exact": 4}
_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s\"']+")
_LONG_NUMBER = re.compile(r"\b\d{2,}\b")


def path_family(value: str) -> str:
    """A path with its leaf replaced by *: sibling reads share one family."""
    cleaned = value.replace("\\", "/").rstrip("/")
    head, _, _leaf = cleaned.rpartition("/")
    return f"{head}/*" if head else "*"


def signature(args: Any) -> str:
    """Stable shape of tool args: paths become families, long numbers placeholders."""
    try:
        raw = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(args)
    raw = _PATH.sub(lambda match: path_family(match.group(0)), raw)
    raw = _LONG_NUMBER.sub("<n>", raw)
    return raw[:400]


def fork_key(kind: str, tool: str = "", *, args: Any = None, goal: str = "") -> str:
    if kind == "arg":
        return f"arg:{tool}:{signature(args)}"
    if kind == "retry":
        # The error text is not part of the key: pre_tool has no error yet, post_tool does.
        return f"retry:{tool}:{signature(args)}"
    if kind == "scope":
        return f"scope:{tool}:{signature(args)}"
    if kind == "route":
        return f"route:{signature(goal)}"
    raise ValueError(f"unknown fork kind {kind!r}")


def may_apply(rule: dict[str, Any], *, hook: str) -> bool:
    """Each hook accepts exactly one shape; low rules stay shadow."""
    decision = str(rule.get("decision") or "")
    strength = str(rule.get("strength") or "none")
    if decision not in DECISIONS or strength not in {"exact", "high", "medium"}:
        return False
    if hook == "transform":
        return decision == "compress"
    if hook == "route":
        return decision in {"inline", "dispatch"}
    return decision == "rewrite"


INTEGRATION_HEADS = (
    "git merge", "git rebase", "git cherry-pick", "git apply", "patch -p", "gh pr merge",
)


def is_integration(tool: str, args: Any) -> bool:
    """Only a terminal command that lands history is a merge gate; reads and status are not."""
    if tool != "terminal" or not isinstance(args, dict):
        return False
    command = str(args.get("command") or "").strip().lower()
    return command.startswith(INTEGRATION_HEADS)
```

### 8.2 dispatcher

```python
# file: plugin/minir/fork.py
"""One entry point for every fork. Deterministic on the hot path, adviser off it."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from . import fork_rules


@dataclass(frozen=True)
class Verdict:
    key: str
    decision: str
    source: str
    strength: str
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


class ForkDispatcher:
    def __init__(self, provider: Any) -> None:
        self._provider = provider
        self._lock = threading.Lock()
        self._rules: dict[str, list[dict[str, Any]]] = {}
        self._failures: dict[str, tuple[int, str]] = {}

    # -- hot path ---------------------------------------------------------

    def resolve_now(self, kind: str, tool: str = "", *, args: Any = None,
                    hook: str = "pre_tool", session: str = "") -> Optional[Verdict]:
        """No model, no disk write. Returns None = split (leave the call alone)."""
        key = fork_rules.fork_key(kind, tool, args=args)
        for rule in self._active(kind):
            if rule.get("key") == key and fork_rules.may_apply(rule, hook=hook):
                return Verdict(key=key, decision=rule["decision"], source="rule",
                               strength=rule["strength"], payload=rule.get("action") or {})
        return None

    def observe_tool(self, session: str, tool: str, args: Any, *,
                     error: str = "") -> None:
        """Feed the retry guard; runs on every post_tool, including adviser-off."""
        key = fork_rules.fork_key("retry", tool, args=args)
        with self._lock:
            if len(self._failures) > 512:  # one session's working set, not a leak
                self._failures.clear()
            count, last = self._failures.get(key, (0, ""))
            self._failures[key] = (count + 1, error or last) if error else (0, "")

    def retry_guard(self, tool: str, args: Any) -> Optional[Verdict]:
        key = fork_rules.fork_key("retry", tool, args=args)
        with self._lock:
            count, last = self._failures.get(key, (0, ""))
        limit = int(self._config("fork_retry_limit", 2))
        if count <= limit:
            return None
        return Verdict(key=key, decision="block", source="guard", strength="high",
                       reason=f"same failure {count} times: {last[:200]}")

    # -- slow path --------------------------------------------------------

    def judge(self, kind: str, tool: str = "", *, args: Any = None,
              timeout: float = 30.0) -> Optional[Verdict]:
        """Adviser consult. Caller must be a worker thread, never a hook."""
        from . import adviser, agent_loop

        question = json.dumps({"kind": kind, "tool": tool, "args": args},
                              ensure_ascii=False)[:2000]
        result = agent_loop.run_with_timeout(
            lambda: self._provider._adviser_mode.consult(question), timeout)
        if result is None:
            return None
        verdict = self._parse_verdict(result, kind, tool, args)
        if verdict is not None:
            self.record(verdict, applied=False)
        return verdict

    # -- gate path --------------------------------------------------------

    def is_integration(self, tool: str, args: Any) -> bool:
        return fork_rules.is_integration(tool, args)

    def resolve_gate(self, session: str, tool: str, args: Any, *,
                     timeout: float = 90.0) -> Optional[Verdict]:
        """Synchronous no-go gate for integration calls; only pre_tool calls this.

        Timeout, an exception, or an empty answer blocks: this path is fail-closed by design.
        """
        from . import agent_loop

        question = (f"Gate: {tool} {json.dumps(args, ensure_ascii=False)[:1200]}\n"
                    "Answer GO or NO-GO: <one reason naming a target>.")
        try:
            answer = agent_loop.run_with_timeout(
                lambda: self._provider._adviser_mode.consult(
                    question, context=self._turn_digest(session)),
                timeout)
        except Exception:
            answer = None
        text = str((answer or {}).get("answer") or "").strip()
        if not text:
            return Verdict(key=fork_rules.fork_key("gate", tool, args=args),
                           decision="block", source="gate", strength="exact",
                           reason=f"adviser gate timed out or answered nothing after {timeout:.0f}s")
        if text.upper().startswith("GO"):
            return None
        return Verdict(key=fork_rules.fork_key("gate", tool, args=args),
                       decision="block", source="gate", strength="exact",
                       reason=text[:400])

    def _turn_digest(self, session: str) -> str:
        """Full turn: user message, plan, tool calls/results, interim intent, pending call.

        Built from the transcript, capped at ~8k chars. The review delta is not enough here.
        """
        ...

    def _parse_verdict(self, result: dict[str, Any], kind: str, tool: str,
                       args: Any) -> Optional[Verdict]:
        """One adviser answer -> one Verdict. Malformed input means split, never a guess."""
        try:
            payload = json.loads(str(result.get("answer") or ""))
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        decision = str(payload.get("decision") or "")
        if decision not in fork_rules.DECISIONS:
            return None
        return Verdict(key=fork_rules.fork_key(kind, tool, args=args),
                       decision=decision, source="adviser", strength="low",
                       payload=payload.get("action") or {},
                       reason=str(payload.get("reason") or "")[:400])

    # -- bookkeeping ------------------------------------------------------

    def record(self, verdict: Verdict, *, applied: bool, outcome: str = "") -> None:
        store = getattr(self._provider, "_store", None)
        if store is not None:
            store.record_fork(verdict, applied=applied, outcome=outcome)

    def _active(self, kind: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._rules.get(kind) or ())

    def load(self, scope: str) -> None:
        """static.json + learned.json, once per session start and per review commit.

        Rules are content and live in profile-local files; this method only caches them.
        """
        ...

    def _config(self, key: str, default: Any) -> Any:
        cfg = getattr(self._provider, "_config", None) or {}
        block = cfg.get("adviser_mode") if isinstance(cfg, dict) else None
        return (block or {}).get(key, default)
```

`_failures` 是进程内计数，重启归零。这是有意的：重试守卫的收益是当轮止损，不值得为它写库。

### 8.3 hook 合并

```python
# file: plugin/minir/__init__.py  (inside _on_pre_tool_call)
reason = resolved._adviser_mode.block_reason(session_id, tool_name,
                                             args=kwargs.get("args"))
if reason:
    return {"action": "block", "message": reason}
guard = resolved._fork.retry_guard(tool_name, kwargs.get("args"))
if guard is not None:
    return {"action": "block", "message": f"jev: {guard.reason}"}
gate = None
if resolved._fork.is_integration(tool_name, kwargs.get("args")):
    gate = resolved._fork.resolve_gate(session_id, tool_name, kwargs.get("args"),
                                       timeout=float(resolved._fork._config("gate_timeout_s", 90)))
if gate is not None:
    return {"action": "block", "message": f"jev gate: {gate.reason}"}
fork = resolved._fork.resolve_now("arg", tool_name, args=kwargs.get("args"),
                                  hook="pre_tool", session=session_id)
if fork is not None and fork.decision == "rewrite":
    return {"action": "modify", "args": dict(fork.payload.get("args") or {})}
return None
```

`args` 在宿主里通常是 dict；如果拿到 JSON 串，先 `json.loads` 再交给 `is_integration`，否则 gate 会静默跳过。

`block` 永远优先于 `modify`。dispatcher 里会返回 `block` 的只有 retry 守卫和 integration gate；规则表没有 `block`，所以学出来的规则不会静默拦工具。

### 8.4 结果压缩

```python
# file: plugin/minir/__init__.py
def _on_transform_tool_result(tool_name: str = "", args: Any = None, result: Any = None,
                              session_id: str = "", status: str = "",
                              provider: Optional[MinirMemoryProvider] = None,
                              **kwargs: Any) -> Optional[str]:
    resolved = _resolve_provider(provider)
    if resolved is None:
        return None
    return resolved._fork.transform(session_id, tool_name, args, str(result or ""),
                                    status=status)
```

```python
# file: plugin/minir/fork.py
def transform(self, session: str, tool: str, args: Any, result: str, *,
              status: str = "") -> Optional[str]:
    """Marked, bounded. None leaves the result untouched."""
    if str(status).lower() in {"error", "failed", "timeout"}:
        return None
    cap = int(self._config("fork_compress_chars", 4000))
    if len(result) <= cap:
        return None
    verdict = self.resolve_now("scope", tool, args=args, hook="transform")
    if verdict is None:
        return None
    lines = result.splitlines()
    if len(lines) < 4:  # one long JSON line: no line structure to keep
        head, tail, omitted = result[: cap // 2], result[-cap // 2:], ""
    else:
        keep = max(2, cap // 4)
        head_lines, tail_lines = lines[:keep], lines[-keep:]
        omitted = f", {len(lines) - len(head_lines) - len(tail_lines)} lines omitted"
        head, tail = "\n".join(head_lines), "\n".join(tail_lines)
    return (f"[jev:rule={verdict.key} original={len(result)} chars{omitted}]\n"
            f"{head}\n…\n{tail}")
```

替换必须带标记。主模型有权知道这是蒸馏结果，测试也要断言这个标记在。

## 9. 测试与验收

### 9.1 单元测试（真实 `MinirStore` + 临时 SQLite，不调模型）

```
test_signature_collapses_sibling_paths
test_signature_keeps_different_tools_apart
test_a_low_rule_is_shadow_only
test_a_learned_rule_never_blocks
test_retry_guard_blocks_after_the_limit
test_retry_guard_resets_after_a_success
test_compress_marks_the_result_and_keeps_line_numbers
test_transform_returns_none_for_error_results
test_transform_returns_none_when_no_rule_matches
test_overturn_demotes_then_expires
test_adviser_fork_verdict_rejects_an_unknown_id
test_adviser_fork_verdict_never_blocks
test_route_falls_back_to_default_when_the_judge_times_out
test_inline_node_must_have_a_registered_handler
test_ledger_records_only_non_allow_forks
test_gate_go_returns_none
test_gate_no_go_blocks
test_gate_timeout_blocks_with_a_named_reason
test_gate_is_not_consulted_for_an_ordinary_tool
test_minir_advise_rejects_a_self_consult
test_delegation_target_is_delivered
test_per_callback_timeout_overrides_the_global   # host 侧，不在插件测试里
```

### 9.2 分阶段与停止条件

| 阶段 | 做什么 | 验收 | 停止条件 | 回退 |
| --- | --- | --- | --- | --- |
| P0 shadow | 只算键、记 `split` 台账，不产生任何动作 | 24 h；键碎片率 = 唯一 fork / 工具调用 ≤ 0.5；hook p95 ≤ 1 ms（不含台账写） | 碎片率 > 0.8 或 p95 > 5 ms，先修归一化 | `forks: off` |
| P0.5 gate shadow | 对 integration 调用 consult，只记裁决，不阻断 | 20 次 gate 抽样与人工判断一致 ≥ 80%；consult p95 ≤ 60 s | 一致率 < 60% 或 p95 > 90 s | `gates: shadow` → `off` |
| P1 静态规则 | `static.json` 的重试守卫 + 压缩 | 20 条抽样无假阳；错误结果从不被替换 | 假阳 ≥ 1 条即停 | 删规则条目 |
| P2 学习规则 | adviser 裁决 → `low` shadow → `medium` 生效 | sharp 占比 ≥ 20%；推翻率 ≤ 5%；无 `inference` 规则阻断 | 推翻率 > 10% 或出现一次非法阻断 | 清空 `learned.json` |
| P3 路由 | 编排器 `route` fork，慢判定在 tick 线程 | inline 节点 ≤ 20%；超时全部落默认 agent | 任一 inline 节点验收失败即降级 | 关 `fork_judge` |

数字是目标，不是实测。P0 的意义就是先把这些数字量出来，再决定 P1 做不做。

## 10. 未验证 / 风险 / 回退

未验证：

- 本文没有实现任何东西：没有表、没有 JSON、没有 hook、没有测试运行。
- `pre_tool_call` 的 30 s 超时与 fail-closed 是读码（`plugins_dispatch.py:59-67`），没有实跑超时分支。
- `transform_tool_result` 的替换顺序与超时放弃是读码（`plugins_dispatch.py:42-52`），没有实跑。
- 两个插件同时返回 `modify` 的累积行为只读了文档（`hooks.md:582`），没有用两个插件实测。
- hook 延迟、键碎片率、sharp 占比、推翻率全部是目标值，没有基线。
- 编排器 `orchestrator_router` 还不存在；`route` 的接线是对草稿（`orchestrator-draft.md:727`）写的。
- adviser 复盘提示词只给了字段契约，没有写具体措辞，也没有测过解析率。
- host patch 0019 没有写、没有应用；`__hook_timeout__` 目前只是一个 spec，`scripts/verify_against_host.py` 也还没有这条断言。
- gate 的 90 s 预算、go/no-go 一致率、`minir_advise` 的返回延迟都没有实测。
- `minir_advise` 的自咨询防护没有实现，也没有测过 adviser turn 的识别。
- `delegation` target 没有加，解析、投递、统计都没有测过。
- 本回合 digest 的 8k 上限没有算过 token，也不知道 transcript 在长回合里够不够拼。

风险与对策：

1. 键碎片化会让规则永不命中。对策：P0 先量，路径按 family、数字按占位；键里不放自由文本。
2. 压缩藏掉判据，主模型基于残缺信息做决定。对策：带标记、保行号、设上限、原文可从源文件重取。
3. 普通 fork 的 hook 里任何慢操作都会 fail-closed 拒绝工具。对策：`resolve_now` 只查内存；模型只在 worker 线程与 gate 路径。
4. 学出来的规则跨项目串味。对策：`scope` 绑项目根；`learned.json` 可按 scope 删。
5. 两个大脑打架。对策：一条权威阶梯，operator 永远第一；adviser 规则不能阻断，gate 只在操作者开启时阻断。
6. `learned.json` 并发写坏。对策：唯一写者是复盘 worker，写临时文件再 `os.replace`。
7. 台账膨胀。对策：只记有动作或有裁决的 fork，纯放行进 `metrics`；七天清理。
8. 规则文件被手改坏。对策：加载时逐条校验，坏条目跳过并在日志里点名，不整份拒绝。
9. gate 把一次 merge 卡住最多 120 s，adviser 挂了就变成了「不能合」。对策：P0.5 先 shadow 量一致率；超时消息写明原因与重试方式；`gates: off` 立即回退。`run_with_timeout` 只停止等待，不杀死 consult 线程；被放弃的咨询可能在后台继续跑到模型返回，预算按 token 也要算这一笔。
10. 0019 是一个 host 级能力，任何插件都能声明长预算。对策：commit message 写清楚；默认不声明仍是 30 s；manifest 与 base commit 一起锁。

回退：`forks: off` / `gates: off` 之后 hook 返回 `None`，文件与表留着不影响旧代码；host 侧从 manifest 移除 0019 重装，预算回到 30 s；彻底回退是删两个模块、两个表与 `forks/` 目录。

## 11. 与现有草稿的关系

| 草稿 | 关系 |
| --- | --- |
| `orchestrator-draft.md` | 补上 §6 表里「JEV fork 层：结构就位，无分类器」的空缺；`Router.decide` 的规则那一步改问 dispatcher |
| `adviser-brain-to-hand-draft.md` | 复用权威阶梯与既有 operator 闸门；JEV 不新增 claim 类型，也不改 `apply_ceiling`。fork 的键归一化是另一套，`PREDICATES` 不参与 |
| `adviser-prompt-draft.md` | 复盘输出加 `forks`；target 词表加 `delegation`；身份与路由不变 |
| `adviser-session-modes-draft.md` | 不耦合；research 模式照样可以用 `arg` / `scope` 规则，是否启用由 `forks` 配置决定 |
| `conclusions-only-draft.md` / 文件优先合同 | 规则正文进文件，SQLite 只记计数与台账，符合该合同 |

刻意不做的：

- 不新建调度器。已有的单一 `Scheduler` 是唯一时钟（`__init__.py:393-397`），fork 判定只加一个时间任务；adviser 复盘仍走它自己的队列线程。合并或另起时钟都没有判定收益。
- 不新增 `auxiliary.minir_fork`。第二个模型路由会带来第二套预算与第二套权威；以后有本地快模型，直接实现 `judge()` 即可，契约不变。
- 不让 adviser 当 dispatcher。每个 fork 都付一次模型调用，正是 JEV 要消掉的成本。merge gate 是操作者授权的 no-go 判断，不是路由；它不决定谁执行，只决定这一次能不能落地。
- 不做行为克隆。主模型上次怎么选，只作为 adviser 的证据，不自动变成规则。

## 12. 更改记录

- 2026-10-27：初稿。定位为插件内 fork 判定层，配合 adviser 复盘形成规则闭环；未落地。
- 2026-10-27 第二稿：加入 adviser target 覆盖（§4.4）、`delegation` 词表、三阶段 gate（§4.5）、`minir_advise`（§4.6）与完整 turn digest；按操作者决定加入 host patch（当时编号 0017，现为 0019），把 `pre_tool_call` 的 callback 预算按 callback 延到 120 s，fail-closed 语义不变（§5.1）。仍未落地、未建表、未应用补丁。
- 2026-10-27 第三稿：host patch 编号 0017 改为 0019。0017 已被 reasoning 显示系列占用（`0017-Resend-the-reasoning-display-flag-when-the-session-rotates.patch`），0018 也已存在（`0018-Commit-the-reasoning-level-to-a-rotated-session.patch`），本稿的补丁排在 0018 之后。只改编号，设计内容未变。

- 2026-10-28 落地：按本稿实现，`plugin/minir/fork_rules.py` 与 `fork.py` 为判定层，
  `store.py` 到 v6（`fork_stats` / `fork_ledger`），`__init__.py` 接线（`pre_tool_call`
  合并、`transform_tool_result`、`__hook_timeout__ = 120`、`initialize` 里载入规则），
  复盘增加 `forks` / `fork_grades`（`adviser_mode.py` + `review.md` + 响应 schema），
  `delegation` 目标进三处词表，`minir_advise` 上 provider 面，host patch 0019 写完并通过
  `git apply --check` 与本机 host 测试（未安装到现有 checkout）。与稿子的差异，逐条：
  - `minir_advise` 不进 `minir` toolset（adviser 每个 run 都拿这个 toolset），所以防自咨询
    是集合事实而不是运行期检查；handler 另外拒绝 watched child session。
  - 慢判定 `judge()` 只接受 `inline` / `dispatch`：consult 的合同是 prose（`consult.md`
    「Do not return JSON」），`rewrite` 需要参数负载，那个合同属于复盘，不属于 consult。
  - 不加 `fork_judge` 配置键，也不注册 `Scheduler.add_time("fork_judge")`：本树没有
    `orchestrator_router`，`route` fork 没有生产者，注册一个没有工作的时钟任务是空转。
  - `review_interval` 默认仍是 1。§7 要求「默认 off，行为与今天完全一致」，把复盘默认降到 8
    会改掉每个用户今天的顾问行为；README 里写成建议值。
  - `gates: merge` 但 adviser 关闭时不阻断（否则每一次 merge 都被静默卡住），启动时告警。
  - `fork_ledger` 多一列 `rule_id`：§4.2 的 grade 要落到具体规则，没有这一列无法降级。
  - `verify_against_host.py` 对 0019 的断言按宿主能力存在与否分支：本机安装的是 0001-0015，
    0019 未装，硬断言会让探针因安装状态而不是插件问题变红；缺失时打印 NOTE 并写进结果 JSON。
  - 未验证：gate/go-no-go 一致率、键碎片率、sharp 占比、真实 hook p95，都仍是目标值；
    `gates: merge` 的 120 s 预算只有在 0019 装到宿主后才会真正生效。
