# 常驻 AdviserRuntime 合同

状态：已实现（2026-10-22；全文 542 tests passed）
范围：`plugin/minir/adviser_mode.py`、`agent_loop.py`、`store.py`、`adviser_prompts.py`、相关测试
非目标：恢复 omp 的无限 `strict` backlog；改变宿主投递顺序；把 reasoning 全量送进 provider prompt

## 术语

- **AdvisorRuntime**：每个 `<session>--<adviser>` 一份的常驻对象，持有 cursor、history、turn 计数、暂停状态。
- **transcript delta**：自该 adviser 的 cursor 之后的新事件，渲染成带 elision 标记的 transcript 片段。
- **deferred note**：reviewer 标了 `"deferred": true` 的建议，本回合不投递，回合结束聚合时再投。
- **turns_ago**：当前 turn 序号减 note 产生时的 turn 序号。

## 现状 → 目标

| 项 | 现状 | 目标 |
| --- | --- | --- |
| reviewer 会话 | 每次 review 新起 AIAgent，只回灌最近 3 批建议摘要 | 常驻 `AdvisorRuntime`，把 history 传给 `agent_loop.run`，同一 adviser 接着上一轮的工具结果和推理继续 |
| 感知 | 事件只存 args 500 / result 700 / error 300，reviewer 看 digest | 事件存完整 args/result/error（不做字段截断），reasoning 取最多 12000 字符的 delta 预览；prompt 整体 24k 有界，只丢最旧整事件并插 `[... N older events omitted ...]` |
| reasoning | 仅在内存里保留 2400 字符，不落盘 | 全量 append 到 `sessions/reasoning/<session>.log`，无字符上限；prompt 只取有界片段 |
| 回合语义 | 无 turns_ago；无 50-turn guidance；无 per-note deferred | note 带 turn/deferred；turn-end 聚合 pending（单块 ≤ `max_advices`）；每 50 turn 附 boundary guidance |
| roster | `MAX_ROSTER=4`，共享 cursor 表 | 无上限；每个 adviser 独立 runtime/messages/cursor；可单独 paused |

## 设计

### 1. AdvisorRuntime 与 history

```python
@dataclass
class AdvisorRuntime:
    session: str
    adviser: str
    cursor: int = 0
    messages: list[dict] = field(default_factory=list)
    reviewed_turn: str = ""
    review_count: int = 0
    failures: int = 0
    paused_until: float = 0.0
    pause_reason: str = ""
```

- `AdviserMode._runtimes: dict[str, AdvisorRuntime]`，key 为 `<session>--<adviser>`。
- `agent_loop.run(..., history=..., session_id=...)`：history 过滤掉 `system` 后作为
  `conversation_history` 传给宿主的 `AIAgent.run_conversation`；session_id 固定为
  `minir-adviser:<session>--<adviser>`，让宿主的会话/工具状态按 adviser 隔离。
- runtime 首次创建时，从 `sessions/adviser/<id>.json` 读取最近的 messages，跨进程重启后仍能接上
  自己的调查；ring 的 cursor 不跨进程持久化，重启后的第一个 review 从当前 ring 的 0 开始。
- history 传给模型前有界：最多 60 条、120k 字符，裁在 user 边界；完整 transcript 文件仍由
  `TranscriptBuffer` 维护（4000 条 / 400k 字符）。

### 2. Transcript delta 与 reasoning

- 事件字段：`kind/turn_id/text/tool/status/args/result/error/reasoning`。
- args / result / error 不设单字段或单事件上限；工具结果原样保留，不做截断。事件级
  reasoning 是 delta 预览，最多保留末尾 12000 字符；完整流在 reasoning 文件里。
- `MAX_RING = 96`，`MAX_PROMPT_CHARS = 24000`：prompt 只丢最旧的**整事件**，并插入
  `[... N older events omitted ...]`；单个超大事件完整保留，所以 prompt 可能因这一个事件
  超出 24k。
- prompt 用 `<transcript_delta>` 包住纯文本 transcript 块，不再用 JSONL digest。
- `on_stream_delta` 同时写两份：内存 6000 字符预览（prompt 用）和全量 reasoning 文件
  `sessions/reasoning/<safe-session>.log`（append，每回合 flush，无字符上限）。
- 事件级 reasoning 取“上次事件之后到本次 tool call 之前”的增量，挂在对应 tool 事件上。

### 3. Note 的 turn / deferred / 聚合 / guidance

- `advisories` 表增加 `turn INTEGER NOT NULL DEFAULT 0`、`deferred INTEGER NOT NULL DEFAULT 0`。
- `parse_review` 接受 `deferred`；`add_advisories` 写入 `turn` / `deferred`。
- `render_block(..., current_turn=..., guidance=...)`：
  - 每条 note 在 label 后加 `N turns ago:`；`N=0` 省略。
  - 每 50 turn 在 block 内附 `[boundary-guidance] ...`。
- 回合结束聚合：`trigger in {"final","agent-end"}` 时，`_deliver_host` 读取该 session 的
  pending rows（含 deferred 和旧 turn 的 note），一次渲染、一次投递，单块最多
  `max_advices` 条（默认 3，配置上限 8），其余留在 pending 等下一次聚合；中途投递只取当前
  turn 且未 deferred 的行（blocker 例外）。
- `deferred=1` 的行不参与中途 steer/continue/llm_request/aside，只在 turn-end 聚合中出现。
- `turns_ago` 使用 `_current_turn(session)`；turn 计数器已存在，不新增状态。

### 4. Roster、独立 session、暂停

- 删除 `MAX_ROSTER` 截断；`WATCHDOG.yml` 与 config 的条目全部保留。
- 每个 adviser 的 runtime 独立持有 messages/cursor；review 运行在独立 AIAgent + 独立 host
  session_id 上。
- 暂停：
  - roster 条目可写 `paused: true`，该 adviser 跳过 review。
  - 运行期失败时，usage/rate-limit 文本（`usage limit`、`rate limit`、`quota`、`429`、
    `too many requests`）触发 `pause_seconds`（默认 300s）暂停；连续失败达到
    `max_consecutive_failures`（默认 3）也暂停 `failure_pause_seconds`（默认 60s）。
  - 其他 adviser 不受影响；暂停到期后自动恢复。

## 改动清单

1. `agent_loop.py`：`run`/`run_async` 增加 `history`、`session_id`；传给 `run_conversation`。
2. `store.py`：`advisories` 增加 `turn`、`deferred`；`_ADDED_COLUMNS` 迁移；`add_advisories` 写入。
3. `adviser_prompts.py`：JSON 合同加 `deferred`；新增 `BOUNDARY_GUIDANCE` 和
   `boundary_guidance(turn)`。
4. `adviser_mode.py`：AdvisorRuntime、事件字段、prompt 渲染、reasoning 文件、turn-end 聚合、
   turns_ago/deferred、roster 上限、暂停。
5. 测试：`test_adviser_history.py`、`test_adviser_mode.py`、`test_adviser_channels.py`、
   `test_agent_loop.py`、`test_store*.py` 增加对应断言。
6. 文档：`doc/omp-adviser-hermes-integration.md` §5/§9.5 更新为已实现与剩余边界；
   `plugin/minir/plugin.yaml` 版本号随发布递增。

## 测试

- history：第二次 review 的 `conversation_history` 含第一次 review 的 assistant/tool 消息。
- transcript：tool 事件保留 >700 字符 result；prompt 超 24k 时只丢最旧整事件，含
  `[... N older events omitted ...]`。
- reasoning：`include_reasoning` 开启时 reasoning 文件包含完整 delta，长度不受 2400 限制。
- deferred：中途 review 不投递 deferred note；final review 的 block 含它。
- turns_ago：跨 turn 的 note 在 block 中出现 `N turns ago`。
- guidance：第 50 turn 的 block 含 boundary guidance。
- roster：第 5 个 adviser 仍被 review；`paused: true` 的成员被跳过。
- pause：usage-limit 文本触发 runtime 暂停，另一成员继续。

## 未决 / 风险

- 宿主 `_transcript` 的 4000 条上限仍会裁掉最旧 adviser 消息；模型侧另有 60 条 / 120k 上限。
- reasoning 文件是明文落盘，包含模型原始推理；这是用户明确要求，默认仅在
  `include_reasoning` 开启时写入。
- `turn` 由插件的 `_turn_ids` 计数；宿主若在同一 turn 内改变 opaque turn id，旧 note 的
  `turns_ago` 会偏大，但不影响投递。
- 无上限 roster 意味着配置错误可以启动很多 reviewer；由 operator 负责，不再静默截断。
- 字段不截断后，ring 会按原样持有大工具结果：一个 10MB 的 result 占 10MB，96 个事件最坏
  约 960MB。这是当前明确的选择；若内存成为问题，再改为磁盘 transcript + 偏移读取，而不是
  重新引入字段上限。
