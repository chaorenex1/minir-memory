# minir_agents 后台执行（复用后台进程完成通知管道）

状态：定稿 v2（2026-10-27）。只描述设计，未改代码。机制事实逐条标了 `文件:行`，取自本机 Hermes 源码与插件仓库；未实测的列在 §9。评审已定 F4、F5 与配额排队口径，§4 全部是决定。

## 1. 一句话

`minir_agents` 不再在工具调用里等答案：它把自定义 agent 交给一个后台线程跑，立刻返回句柄；跑完把事件塞进 Hermes 后台进程完成通知用的同一条队列（`process_registry.completion_queue`），主会话在下一个 turn 里以 `[ASYNC DELEGATION COMPLETE]` 收到结果。队列、消费者、空闲唤醒都是后台 shell 进程那一套，事件类型不同（§4 F1）。

## 2. 范围

| 项 | 内容 |
| --- | --- |
| 要解决的场景 | 一个长任务（搜索、审阅、写草稿）不该把主会话的一轮卡住几分钟；答案该像后台进程结束一样主动回到对话里 |
| 非目标 | 不做跨进程持久化（进程退出即丢）；不新增 job 查询工具；不改 `agent_loop.run` 本身；不替代 `delegate_task`（那是宿主的子代理，有自己的 ledger） |
| 做成的信号 | 调用后立刻返回 `status="running"`（或并发满时的 `queued`）；后台线程结束后，主会话在空闲 tick 上自动起一个新 turn，文本以 `[ASYNC DELEGATION COMPLETE` 开头，正文是 agent 的 answer |
| 不适用 | `-q` 一次性运行（无 idle tick，见 §3.4）；需要答案才能继续下一步的调用方（用 `background: false`） |

## 3. 行为

### 3.1 调用形态

参数只加一个 `background`（boolean，默认 `true`）。返回值变成句柄：

```json
{"agent_name": "researcher", "job_id": "minir-researcher-1793077200123",
 "session_id": "sess-…", "status": "running", "queue_position": 0, "background": true,
 "transcript_path": "<HERMES_HOME>/minir-memory/sessions/default/researcher/<session_id>.jsonl",
 "notified": true}
```

- 校验（`agent_name`、`user_prompts`、`model`、`tool_sets`、`context_files`、`MAX_DEPTH`）全部留在调用线程，`minir_agents.py:249-269`：参数错必须当场报错，不能变成一条“后台任务失败了”的通知。
- `system_prompt` 的扫描、context 文件的读取、历史读取也在调用线程完成（`minir_agents.py:265-279`）——这三件事都依赖 `provider` 实例和 `file_guard` 的 aux scope，后台线程里没有 provider。
- `background: false` 保留今天的同步语义与 payload（`minir_agents.py:330-346`），供“必须先拿到答案”的调用方使用。
- `status` 只有两个值：`running`（线程已起）与 `queued`（在 pending 队列里，`queue_position` 是 1-based 的全局位置）。两种情况的完成通知是同一条。

### 3.2 后台执行

- 执行体：`agent_loop.run(...)`（`agent_loop.py:261-271`）。它自己构造 `AIAgent`、自己解析模型路由（`agent_loop.py:297-329`），不持有调用方 provider，所以线程里跑是安全的。
- 线程：`spawn_context_thread`（宿主 `agent/memory_provider.py:29-33`），插件的 worker 已经用它起线程（`__init__.py:1573`）。ContextVar 跟着线程走，`_DEPTH`、`minir_agent_layer`、`file_guard` 的 scope 都不用手工搬。
- `_DEPTH` 在调用线程 +1 后由 ctx_bound 线程继承，`MAX_DEPTH` 语义不变（`minir_agents.py:262-264`）。
- transcript 写在后台线程里，**先 flush 再投递**（今天顺序的镜像，`minir_agents.py:318-328`）：通知里说“全文见 transcript”时文件必须已经存在。

### 3.3 完成事件

投递就是把一个 dict 放进宿主单例队列：`tools.process_registry.process_registry.completion_queue`（`tools/process_registry.py:695`、`:2568`）。字段：

| 字段 | 值 | 为什么 |
| --- | --- | --- |
| `type` | `"async_delegation"` | 唯一两侧消费者都收的类型（§7）。不是 `completion`：见 §4 的 F1 |
| `session_key` | `provider._session_id` | CLI 靠它证明归属（`process_registry.py:1792-1805`、`cli_process_notifications.py:5-20`）；gateway 靠它查会话来源（`run_notifications.py:644-663`） |
| `session_id` | `job_id` | 只做展示与日志；`completion` 的 consumed/poll 跳过逻辑不看它（`process_registry.py:1771-1780`） |
| `delegation_id` | `minir-<agent>-<毫秒>-<序号>` | 缺行时 `claim_completion_delivery` 返回 True（“legacy event created before durable dispatch”，`async_delegation.py:481-489`），两侧都放行；带前缀避免与真 delegation id 撞号 |
| `status` | `"completed"` / `"error"` | `_DONE = ("completed","success")`，其余按失败渲染（`process_registry_notifications.py:10`、`:303-312`） |
| `summary` | answer，截到 `MAX_NOTIFICATION_CHARS` | 宿主不截断 summary（`:306`）；太长会灌满主会话上下文 |
| `goal` / `goals` | `user_prompts` 首行（截 120） | `goals[0]` 是 CLI 行标题（`process_registry_notifications.py:319-341`），`goal` 进模型文本（`:166-167`） |
| `api_calls`、`duration_seconds`、`model`、`role`、`toolsets` | `AgentRun` 上的值 | 渲染器的 preamble 读这几个字段（`:159-173`） |
| `dispatched_at`、`completed_at` | epoch 秒 | `_preamble` 打印“多久之前派发”（`:162-165`） |
| `restored` | 不设 | 设了等于宣称来自磁盘回放，`_owns_event` 会 fail-closed（`process_registry.py:1803-1805`） |

结果文本由宿主渲染，插件不拼（`process_registry_notifications.py:280-316`）：

```
[ASYNC DELEGATION COMPLETE — minir-researcher-1793077200123]
A background subagent you dispatched earlier has finished. …
Original goal: <goal>
Role: plugin   Model: <model>
Status: completed   API calls: 3   Duration: 41.2s
--- RESULT ---
<summary>
```

标题里的 id 就是 `job_id`（`process_registry_notifications.py:284` 取 `delegation_id`）。ledger 里没有这一行是设计的一部分：`claim_completion_delivery` 对缺行返回 True（`async_delegation.py:481-489`），claim 之后的 `complete/settle` 对同一行是 UPDATE 0 行的 no-op；本设计不写 ledger，也不出现在 `restore_undelivered_completions` 的回放里（那是按行读，`:334-364`）。

### 3.4 谁在什么时刻把它变成新 turn

| Surface | 消费者 | 触发点 |
| --- | --- | --- |
| CLI（本机当前形态） | `_tui_idle_tick` → `_drain_process_notifications("cli-idle")`（`cli_tui_runtime_mixin.py:72-83`） | agent 没在跑时的空闲 tick，事件进 `_pending_input`（`cli_process_notifications.py:74`），下一轮 prompt 就是它 |
| CLI（turn 刚结束） | `_drain_process_notifications("cli-post-turn")`（`cli_tui_runtime_mixin.py:233`） | 结果落在相邻的下一轮 |
| CLI viewer 连着 gateway | 只画不判：`queue_admitted_turn(self, store)`（`cli_process_notifications.py:37-43`） | 由 gateway 准入并起 turn |
| gateway / Desktop | `_async_delegation_watcher`（`run_notifications.py:1578-1606`） | 每 2s 一拍，只挑 `type == "async_delegation"`（`:1604`），空闲也会起 turn |
| `-q` / 一次性运行 | 无 | 没有 idle tick；事件随进程消失。返回值里的 `transcript_path` 是唯一出口 |

`display.background_process_notifications: off` 会挡掉 `completion` 类事件，但放行 `async_delegation`（`cli_process_notifications.py:59-61`）——本设计因此不受该开关影响。

### 3.5 失败路径

| 情况 | 结果 |
| --- | --- |
| `agent_loop.run` 抛异常 | `status="error"`、`error=str(exc)`，事件照投；主会话看到 “did not complete successfully (status=error)” 加错误文本（`process_registry_notifications.py:307-313`） |
| transcript 写入失败 | 事件照投，`summary` 前面加一行 `transcript_error: …`；`payload` 里同样带（沿用 `minir_agents.py:326-328` 的容错） |
| 完成线程里抛任何未捕获异常 | `try/finally` 包住：必须投一条 `status="error"`，否则主会话永远等不到 |
| 并发已满 | 进 pending 队列，返回 `status="queued"`；只有 pending 也满（会话 16 / 全局 64）才 `tool_error`（§5） |
| 进程在跑完前退出 | 无通知，无 durable 记录；transcript 里没有 `kind="result"` 行——§9 记录为已知边界 |

## 4. 已定口径

每条都是决定；依据列是证据，不是选项。

| 编号 | 分叉 | 取值 | 依据 |
| --- | --- | --- | --- |
| F1 | 事件类型 | `async_delegation` | `completion` 在 gateway 侧不被投递：`_drain_gateway_watch_events` 只留 watch/heartbeat，async 另路 requeue，其余类型（含 `completion`）直接丢掉（`gateway/run.py:3063-3072`），watcher 也只挑 async（`run_notifications.py:1604`）。用 `completion` 等于只有 CLI 能用 |
| F2 | 是否写 durable ledger 换一个可回放的 delegation 行 | 不写；`delegation_id` 用 `minir-` 前缀的事件自造 id | ledger 只有私有写入口（`async_delegation.py:166-193` 的 `_persist_dispatch`），插件去写等于冒充 `delegate_task`；缺行时 claim 本身就放行（`:481-489`），所以自造 id 只影响标题，不影响投递 |
| F3 | 后台线程池 | 每任务一个 `spawn_context_thread` | 插件已有先例（`__init__.py:1573`），配额由 §5 兜住；`Scheduler`（`scheduler.py:94-152`）是 turn/time 台账，不适合一次性任务 |
| F4 | 默认后台还是默认同步 | 后台（`background: true`）；`background: false` 保留同步（§3.1） | 需求原文“变成后台执行”；同步开关是给“下一步要答案”的调用方留的路 |
| F5 | 答案超长 | `MAX_NOTIFICATION_CHARS = 2000`，超出保留头 2000 字，并在 `summary` 末尾附 transcript 路径与总长度 | 宿主不截断（`process_registry_notifications.py:306`）；2000 由评审定 |
| F6 | pending 队列长度上限 | 每会话 16、全局 64，再满才 `tool_error` | “超了排队”必须有界，否则 agent 循环能堆出无界队列（§5） |

## 5. 并发与配额

| 项 | 值 | 理由 |
| --- | --- | --- |
| 每会话同时运行数 | 3 | 每次完成都花主会话一轮 turn；超过 3 条并发通知会把一轮对话灌满 |
| 全进程同时运行数 | 8 | 防止一个会话挂 3 个、十个会话把进程打爆 |
| 超限行为 | 进 pending 队列（按提交顺序，派发规则见下一行），返回 `status="queued"` | 评审定：排队而非报错；调用方立刻拿到句柄，不被阻塞 |
| pending 上限 | 每会话 16、全局 64 | 有界排队；再满才 `tool_error`（F6） |
| 派发规则 | 从队首扫描，取第一条“其会话计数 < 3”的 job；全局已满 8 时整轮不派发 | 跳过已满会话，避免一个会话的排队把别的会话饿死 |
| 派发时机 | 每次有 job 结束时，在锁内减计数并连续派发直到某个上限重新卡住 | 不起定时器，队列只在“有人完成”时前进 |
| `MAX_DEPTH` | 不变（3） | 后台线程继承 `_DEPTH`，递归限制照旧 |

运行中的 job 记在进程内 dict（`job_id → {agent, session_id, started_at, thread}`），排队中的 job 记在 `pending`（`collections.deque`，按提交顺序）；两者都只用于配额与日志，不落盘。

排队有一条容易被忽略的语义：**提交时快照**。prompt、system prompt、context 文件、历史都在调用线程读完（§3.1），排队期间不再回读；文件在等待期间改了什么，跑起来也看不到。这跟“现在调一次”一致，不引入时点漂移。

排队中的 job 没有线程，进程退出即丢（§3.5 最后一行）。提交时打一行日志 `[minir] %s queued at %d (%d/%d session, %d/%d global)`，便于在 CLI 里确认它没在跑是排队而非丢单。

## 6. 与现有代码的接口变化

| 文件 | 变化 |
| --- | --- |
| `plugin/minir/minir_agents.py` | `run()` 拆成“校验+解析（调用线程）”与“执行+投递（后台线程）”；新增 `_submit`、`_execute`、`_notify`、`_dispatch` 与 `_pending`（配额与排队） |
| `plugin/minir/tools.py:169-209` | schema 加 `background`（boolean，默认 true）；描述改成“立即返回，结果作为后台完成通知回到会话” |
| `tests/test_minir_agents.py` | 现有 15 个测试断言同步 payload（`:34-213`），全部走 `background: false`；新增后台/事件/失败/配额用例（§8） |
| `README.md` | 工具段落一句：后台执行 + `background: false` |
| host patch 系列 | **不动**。本设计只用宿主已有的队列与消费者 |

## 7. 机制依据

| 事实 | 出处 |
| --- | --- |
| 队列是宿主单例，模块级 `process_registry` | `tools/process_registry.py:695`、`:2568` |
| 弹出即渲染，带归属判定（`session_key` 正证） | `tools/process_registry.py:1829-1882`、`:1792-1805` |
| 普通 completion 的文本形状与 consumed 跳过 | `tools/process_registry_notifications.py:463-506`、`tools/process_registry.py:1771-1780` |
| async_delegation 的文本与行标题 | `tools/process_registry_notifications.py:280-316`、`:319-341` |
| 缺 ledger 行时 claim 仍放行（“legacy event”） | `tools/async_delegation.py:481-489` |
| CLI 空闲 tick 与 turn 后各 drain 一次 | `hermes_cli/cli_tui_runtime_mixin.py:72-83`、`:233` |
| CLI drain 把文本放进下一轮输入 | `hermes_cli/cli_process_notifications.py:36-74` |
| CLI 归属按 session_key（含压缩链解析） | `hermes_cli/cli_process_notifications.py:5-20` |
| gateway 只挑 async_delegation | `gateway/run.py:3053-3073`、`gateway/run_notifications.py:1578-1606` |
| gateway 由 session_key 还原投递来源 | `gateway/run_notifications.py:644-708` |
| `agent_loop.run` 自建 AIAgent，不依赖 provider 实例 | `plugin/minir/agent_loop.py:261-271`、`:297-329` |
| 插件起上下文线程的既有方式 | `agent/memory_provider.py:29-33` + `plugin/minir/__init__.py:1573` |

## 8. 测试

| 用例 | 断言 |
| --- | --- |
| 默认后台 | 立即返回 `status="running"` 与 `job_id`；join 后 `agent_loop.run` 被调用一次 |
| schema | `SCHEMAS` 里 `minir_agents` 的 properties 含 `background`，默认 `true`，其余参数不变 |
| 事件字段 | 假队列（monkeypatch `minir_agents._notification_queue`）：`type`、`session_key`、`status`、`summary`、`goal`、`delegation_id` 以 `minir-` 开头、无 `results`、无 `restored` |
| claim 形状 | `delegation_id` 以 `minir-` 开头、`session_key` 非空、无 `restored`（缺行放行是宿主行为，见 §7） |
| 顺序 | 事件投递时 transcript 文件已存在且含 `kind="result"` |
| 失败 | `agent_loop.run` 抛错 → `status="error"`、`error` 文本、事件仍投递 |
| 同步开关 | `background: false` → payload 与今天逐字段相同 |
| 并发满了排队 | 会话内第 4 个调用 → `status="queued"`、`queue_position` 正确、不起线程；放掉一个后它自动开始 |
| 队列上限 | pending 到 16（会话）/ 64（全局）→ `tool_error` |
| 跨会话不饿死 | 会话 A 排满 3 条时提交会话 B → B 立刻起线程 |
| 长答案 | answer > 2000 → `summary` 截断到 2000 且附 transcript 路径 |
| 无 transcript | `provider._transcript is None` → 仍投递，`summary` 前一行说明无审计文件 |

## 9. 未验证

- gateway / Desktop 上的端到端投递：只读了代码，没有在 gateway 模式下实际跑一次（需要一条真实 session_key 与一个在线 adapter）。
- `agent/notification_presentation.diagnostic_process_event` 对我们这种事件的判定（CLI 用它决定是否把通知标成 diagnostic，`cli_process_notifications.py:70-73`）。
- 压缩链之后 `session_key` 的归属：`_owns_process_notification` 会解析 resume id（`:13-20`），但“agent 跨压缩仍在跑”的场景没有实测。
- `/new` 之后到达的完成事件：gateway 对 `completion` 的 permanent-gone 分支有明确处理（`run_notifications.py:1160-1167`），有 ledger 行的 `async_delegation` 走 ledger 结算；我们这种自造 id、无 ledger 行的事件在那种边界上的行为没有验证。
- transcript writer 在后台线程与主线程并发写同一 agent 流时的行为（同一 `agent_name` 两次并发运行）。

## 10. 实施顺序

| 步 | 改动 | 通过条件 |
| --- | --- | --- |
| 1 | `plugin/minir/minir_agents.py`：`_execute` / `_notify` / `_dispatch` / `_pending` 与 `background` 分支。先按 §8 写新用例，看着它们红再动实现 | `python -m pytest tests/test_minir_agents.py -q` 全绿（现 15 条改走同步路径 + 新增 11 条） |
| 2 | `plugin/minir/tools.py:169-209`：`background` 参数与模型可见描述 | `python -m pytest tests/test_adviser.py tests/test_minir_agents.py -q`（工具名单断言在 `test_adviser.py:184`） |
| 3 | `README.md`：工具表行、`minir_agents.py` 文件图、Custom agents 段落 | `python -m pytest -q`，基线 774 passed |
| 4 | 手工 smoke：真实 CLI 会话调 `minir_agents`（默认后台） | 立刻回 `status="running"`；结束后主会话出现以 `[ASYNC DELEGATION COMPLETE — minir-` 开头的新 turn；再调 `background: false` 确认同步答案 |

数据库：本设计不改 schema、不写 store，没有迁移，因此没有备份步骤。若以后把 pending job 落到 `store.jobs`，备份与迁移补在那一次改动里。

## 11. 更改记录

- v1（2026-10-27）：首版。定 F1 事件类型 = `async_delegation`、F2 不写 ledger、F3 每任务一线程；F4、F5、配额留给评审。
- v2（2026-10-27）：定稿。F4 保留 `background: false`；配额 3/8 且超限排队（新增 F6 pending 上限 16/64）；F5 截断 2000；补 §10 实施顺序。
- v3（2026-10-27）：按 §10 实施完成。`minir_agents.py` 拆出 `_Job` / `_execute` / `_notify` / `_dispatch` / `_pending`；`tools.py:169-205` 加 `background`；测试 15 条改走同步路径 + 新增 11 条（26 条全绿，全量 785 passed）。真实队列 smoke（独立 `HERMES_HOME`、真 `process_registry`、真渲染器）产出的文本确认：`[ASYNC DELEGATION COMPLETE — minir-smoke-…]`、`Role: plugin`、`Toolsets: *`、`--- RESULT ---` 后接答案，transcript 在投递前已落盘，drain 后队列无残留。
