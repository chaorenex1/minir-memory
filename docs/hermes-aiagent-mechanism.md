# Hermes AIAgent 机制笔记

> 依据：本机 `C:\Users\zarag\AppData\Local\hermes\hermes-agent` 工作副本，HEAD `d923a5eb487a17c0a68801bba9ef0f56c81040a8`，工作树有 60 个本地修改（含 `agent/conversation_loop.py`、`agent/agent_init.py`）。文中行号取自该工作副本，与上游同 commit 会有偏移。
>
> 官方文档页 `developer-guide/agent-loop` 给的是正确骨架，但有三处落后于代码：文件规模、中断语义、`step_callback` 时机。第 7 节逐条列出。本文其余部分都对着源码核对过。
>
> 配图（单轮执行与回调出口）：`docs/hermes-aiagent-mechanism-diagram.html`（与本文同目录；生成工作区在 `.archify/workflow-agent-loop-20261022-133238/`，含 candidate JSON 与各道校验回执）。图只画主链路和四条回调出口，省略了压缩、审批、回退的具体分支；阶段顺序和回调落点与本文一致。

## 1. 结构：一个门面，一个循环，一组相位

`run_agent.py` 只有 1555 行，`AIAgent.__init__` 直接转发给 `agent/agent_init.py` 的 `init_agent`（`run_agent.py:282-288`）。真正的循环在别的文件里。

| 层 | 文件 | 职责 |
|---|---|---|
| 门面 | `run_agent.py` | `AIAgent` 类声明、`__init__` 转发、少量转发式方法 |
| 入口 | `agent/turn_facade.py` | `run_conversation` / `chat`：turn 准入、跨进程租约、relay 作用域，再调循环 |
| 循环 | `agent/conversation_loop.py` | `while` 主循环，串起各相位，收尾调 `finalize_turn` |
| 相位 | `agent/turn_*.py` | 每个相位一个函数，返回 `continue` / `break` / `return` |
| 执行面 | `agent/tool_executor.py` | 工具解析、审批、并发、结果回填 |
| 装配 | `agent/agent_init.py` | 把构造参数落到实例属性上 |

两个入口的实际签名（`agent/turn_facade.py:22-33`）：

```python
agent.run_conversation(user_message, system_message=None, conversation_history=None,
                       task_id=None, stream_callback=None, ...)  # 返回 dict
agent.chat(message, stream_callback=None)  # return run_conversation(...)["final_response"]
```

`task_id` 缺省时由 `turn_facade` 用 `uuid4` 生成（`turn_facade.py:59`），然后作为 `effective_task_id` 一路传进循环和工具层。

## 2. 一次 turn 的相位序列

主循环（`agent/conversation_loop.py:1648-1706`）的条件是双闸门：

```python
while (s.api_call_count < agent.max_iterations and agent.iteration_budget.remaining > 0) \
        or agent._budget_grace_call:
```

构造器里 `max_iterations` 默认是 `sys.maxsize`（`run_agent.py:241`）；实际生效的 500 来自 CLI/gateway 读配置 `agent.max_turns`（默认值见 `hermes_cli/cli_config_load.py:201`，命令行 `--max-turns` 可覆盖）。`iteration_budget` 是另一个计数，子代理不继承父额度，而是新建一份（`tools/delegate_tool.py:286` 注释 `fresh budget per subagent`）。

| 顺序 | 相位 | 源码 | 做什么 |
|---|---|---|---|
| 1 | `begin_iteration` | `conversation_loop.py:1651` | 开一轮，重置 turn 内状态 |
| 2 | `prepare_iteration` | `:1653` | 迭代准备；`step_callback` 在这里发 |
| 3 | `assemble_api_request` | `:1654` | 组请求体、临时 prompt 层、缓存标记 |
| 4 | `run_preflight_gate` | `:1655-1661` | 预检压缩闸门，可 `continue`（换轮）或 `return`（超时） |
| 5 | `announce_api_call` | `:1662` | 发出「开始请求」状态 |
| 6 | `_run_api_retry_loop` | `:1664-1670` | 重试、限流守卫、凭据轮换、回退模型 |
| 7 | `apply_retry_restarts` | `:1672-1676` | 重试后的重建，可 `continue` |
| 8 | `normalize_model_response` | `:1679-1683` | 统一三家 API 格式、抽 reasoning、处理 tool_calls |
| 9 | `run_tool_round` 或 `finish_text_response` | `:1684-1686` | 有 tool_calls 走工具轮，否则收尾 |
| 10 | `finalize_turn` | `:1697-1701` | 循环外收尾：持久化、usage、结果 dict |

关键点：相位之间靠返回值通信，不是靠异常。`_run_phase` 把 `action` 读出来再决定继续、跳出还是直接返回，所以任何一个相位都能安全终止整个 turn。

`normalize_model_response` 之后一定是二选一：有 `tool_calls` 就执行工具再回到第 1 步（不是回到第 3 步，请求要重新组），没有就结束。这就是「工具调用不结束 turn、只延长 turn」的实现位置。

## 3. 中断的真实语义

非流式请求跑在 worker 线程上，每次请求单独一个 client，另有 stale-call 检测器负责掐断连接并抛错（`agent/chat_completion_helpers.py:1261-1274`）：

```python
def interruptible_api_call(agent, api_kwargs):
    """Run the API call on a worker thread so the caller can detect interrupts
    without waiting for the full HTTP round-trip. ... a stale-call detector
    kills the connection and raises so the main retry loop can back off / rotate
    credentials / fall back."""
```

一个容易忽略的分支：cron 和委托子代理这类嵌套线程池上下文里，`should_use_direct_api_call` 为真，请求直接在调用线程内跑，不开 worker 线程（`chat_completion_helpers.py:1267-1270`，注释引 #62151）。原因是嵌套池会互相等死。

中断信号最终以 `InterruptedError` 的形式落到 `handle_api_interrupt`（`agent/turn_api_call.py:170-210`），行为分三种：

1. 有 pending redirect：保留修正内容，清掉取消位，重建消息后继续（`:183-185`）。
2. 有已流式输出的部分文本：**写入会话**，作为 `assistant` 行并带 `display_metadata: {"interrupted": True}`（`:202-206`），最终回复就是这段部分文本。
3. 没有部分文本：写入等待提示，最终回复是「等待模型 N 秒」这类句子（`:207-208`）。

第二种是官方文档写反的地方。文档说中断「不会把部分响应注入对话历史」，代码是把部分文本落盘、只对失控重复的文本做隐藏占位（`:196-201`，注释引 #112764）。做续写、重放、审计的工具如果按文档假设「中断后历史干净」，会读到实际不存在的干净历史。

## 4. 工具执行

工具轮的入口在 `agent/turn_tool_round.py`，顺序固定（`:155-169`）：

1. interim 助手文本等到 DB 落库之后才发射，避免 UI 先看到只在内存里的行（`:157-158`）。
2. 给 `stream_delta_callback` 送一个 `None`，冲刷已经打开的流式块（`:160-164`）。TTS 用的 `_stream_callback` 不收 `None`。
3. `_execute_tool_calls` 执行本轮全部工具。
4. `finish_tool_round` 记账。

并发规则（`run_agent.py:1299-1322`）：先由 `_plan_tool_batch_segments` 把整批调用切成有序的 `("parallel"|"sequential", calls)` 段，再逐段执行。

| 情况 | 行为 | 源码 |
|---|---|---|
| 整批 ≤ 1 个调用 | 顺序执行 | `run_agent.py:1310-1311` |
| 只有一段且是 parallel | 并发执行器 | `run_agent.py:1317-1319` |
| 只有一段且是 sequential | 顺序执行器 | 同上 |
| 多段 | `execute_tool_calls_segmented` 按发射顺序逐段跑 | `run_agent.py:1320-1322` |
| 命中 `_NEVER_PARALLEL_TOOLS` | 该调用直接成为顺序屏障 | `tool_dispatch_helpers.py:169-170` |
| 参数不是合法 JSON / 不是 dict | 同样降级为顺序屏障 | `tool_dispatch_helpers.py:171-182` |
| 结果回填 | 顺序与发射顺序严格一致 | `tool_dispatch_helpers.py:195-204` |

并行安全不是按工具名白名单一刀切：路径类工具按实际目标路径做预约，reader 之间重叠仍可并行，任何涉及 writer 的重叠就关闭当前段，让冲突调用落到冲突者之后（`tool_dispatch_helpers.py:187-204`）。不足两个调用的段降级为顺序，因为顺序执行器带更完整的内联分发。

`_NEVER_PARALLEL_TOOLS` 的实际内容（`tool_dispatch_helpers.py:30`）：

```python
_NEVER_PARALLEL_TOOLS = frozenset({"clarify", "manage_connections", "manage_catalog", "setup_beat", "set_theme"})
```

这五个都是要占住用户界面或安装态的东西：并行跑会让两个卡片抢同一个输入通道。

整批工具执行被 `scoped_connection_surface(agent_connection_surface(self))` 包住（`run_agent.py:1309`），所以连接面板的 surface 在整段时间里由 ContextVar 固定；side_agent 的降级判断因此在工具线程里也成立，不依赖调用线程恰好读到同一份属性。

## 5. 四个构造参数

四个参数都在 `AIAgent.__init__` 的签名里（`run_agent.py:236-281`），由 `agent/agent_init.py` 按三组规则落到实例上（`agent/agent_init_params.py:4-33`）：直接同名 passthrough、identity 存成 `agent._<name>`、callback 原样存成 `agent.<name>`。

### 5.1 `side_agent`

| 项 | 内容 |
|---|---|
| 类型/默认 | `bool = False`（`run_agent.py:279`） |
| 存储 | passthrough，`agent.side_agent`（`agent_init_params.py:15`） |
| 读取点 | `tools/connectors/turn.py:29-39`、`toolsets.py:467-471` |
| 语义 | 标记「这是依附在别人会话上的旁路代理」，用来收窄能力面，不改变循环本身 |

它只做两件具体的事：

1. 丢弃连接管理工具：`SIDE_AGENT_TOOL_DROPS = {"manage_connections"}`（`turn.py:35-39`），经 `agent_tool_drops` 在加载、MCP 刷新、前缀恢复三个时点统一生效（`toolsets.py:467-471`）。
2. 连接面板降级：`agent_connection_surface` 的优先级是 card > side > link（`turn.py:29-32`）。旁路代理没有 UI 卡片可弹，只能给链接。

旁路代理的循环和主代理完全一样，没有第二套执行器。TUI/Desktop 的后台提问即是这样构造的（`tui_gateway/agent_callbacks.py:427-457`）：

```python
return {..., "quiet_mode": True, "verbose_logging": False,
        "platform": "tui", "session_db": ..., "fallback_model": fallback,
        "side_agent": True}
```

注意它复用父代理的 `session_db`，只是自己拿一个 `session_id`（`bg_` 前缀），所以后台任务的行写进父会话的库，但不会污染父会话的 turn。

委托子代理也是同一路线，并且关掉交互回调和 memory（`tools/delegate_tool.py:273-286`）：`quiet_mode=True`、`log_prefix=f"[subagent-{i}]"`、`side_agent=True`、`skip_memory=True`、`clarify_callback=None`。

### 5.2 `quiet_mode`

| 项 | 内容 |
|---|---|
| 类型/默认 | `bool = False`（`run_agent.py:244`） |
| 读取点 | 初始化横幅、工具进度、压缩器、spinner |
| 语义 | 「别在 stdout 上做富展示」，不是「静音」 |

实际效果分四块：

- 初始化横幅不打（`agent_init.py:758`、`791`、`809`、`817`、`1001`、`1022` 等）。
- 工具进度行不再走富展示，改用统一 spinner（`agent/tool_executor.py:1220`、`1722-1727`）。
- 压缩器的内部过程信息静音（`agent/context_compressor.py` 里几十处 `if not self.quiet_mode`）。
- 部分场景下工具摘要行直接打印，条件是 quiet 且没有 callback 接手且平台是 cli（`agent/status_output.py:52-63`）。

一个反直觉的事实写在源码注释里（`agent/chat_completion_helpers.py:2282-2283`）：

```python
# Strict machine-readable mode (-Q, oneshot): keep diagnostics off stdout. quiet_mode is
# NOT the gate — the interactive CLI runs quiet_mode=True by default and must see this.
```

也就是说交互式 CLI 默认就跑在 `quiet_mode=True`。判断「是否允许写 stdout」的正确开关是 `suppress_status_output`（默认 False，`agent/agent_init.py:2420`；后台 review fork 置 True，`agent/background_review.py:994`）。把 `quiet_mode` 当成机器可读模式的闸门，会做出一个在交互式 CLI 里什么都不显示的 bug 修复。

### 5.3 `log_prefix` 与 `log_prefix_chars`

| 项 | 内容 |
|---|---|
| 类型/默认 | `log_prefix: str = ""`，`log_prefix_chars: int = 100`（`run_agent.py:246`） |
| 用途 | 行首标记 / 预览截断长度 |
| 例子 | 子代理 `[subagent-1]`（`tools/delegate_tool.py:277`） |

`log_prefix` 靠字符串拼接进状态行，例如 `self._vprint(f"{self.log_prefix}📋 Restored ...")`（`run_agent.py:1041`）和 `f"{self.log_prefix}{message}"`（`agent/status_output.py:79`、`252`）。没有任何转义或宽度对齐，前缀里放 ANSI 颜色会直接破坏行宽计算。

`log_prefix_chars` 只影响预览截断，见参数说明「Number of characters to show in log previews for tool calls/responses」（`run_agent.py:1500`）。两个名字相近但职责无关，一个管前缀，一个管截断。

### 5.4 `thread_id`

| 项 | 内容 |
|---|---|
| 类型/默认 | `Optional[str] = None`（`run_agent.py:268`） |
| 分组 | gateway identity，存成 `agent._thread_id`（`agent_init_params.py:17-22`） |
| 持久化 | 写进 session 行的 `thread_id` 列（`run_agent.py:333-344`） |
| 消费 | 出站投递路由（`gateway/delivery.py:300-310`）、hook 上下文（`gateway/hooks.py:5-9`） |

它是「这条消息属于哪个话题/子线程」的路由键，不是会话 id。投递时 caller 显式给的 thread 优先于目标自带的 thread（`delivery.py:300`），Telegram 私有聊天的命名话题要经过 `createForumTopic` 换一次 id（`:305-310`）。

为什么和会话行绑在一起，源码注释给了理由（`run_agent.py:333-334`）：gateway 的 SessionStore 退化成 JSONL 时，这次惰性建行可能是唯一一次持久化写入，一条没有路由身份的行之后无法恢复。

## 6. 回调

### 6.1 装配链

1. 构造参数进 `AIAgent.__init__`（`run_agent.py:251-264`）。
2. `init_agent` 用 `_CALLBACK_PARAMS` 循环 `setattr`，名字原样（`agent/agent_init.py:2417-2419`）。默认全是 `None`。
3. 调用点用 `getattr` 读，`None` 就跳过。两处专门吞异常的包装：`_call_callback`（`agent/status_output.py:65-72`）和 `_call_quietly`（`agent/stream_delivery.py:22-31`）。

`_CALLBACK_PARAMS` 有 24 项（`agent_init_params.py:23-33`），下表多出一行的 `plugin_message_callback` 由「表面层」在初始化之后自己装上（`agent/agent_init.py:552-553`）。审批不在这套机制里：它是工具层注册的 `set_approval_callback`（`cli.py:408`、`972`），工具在执行时才用，agent 构造时并不知情。

### 6.2 全表

签名从调用点抄，不是从文档抄。

| 回调 | 签名 | 触发点 | 消费方 |
|---|---|---|---|
| `tool_progress_callback` | `(event_type, name=None, preview=None, args=None, **kw)` | 工具开始/结束，事件名 `tool.started` / `tool.completed`，完成事件另带 `duration`、`is_error`、`result`（`agent/codex_runtime.py:394-411`） | CLI spinner、gateway 进度、preview restart |
| `tool_start_callback` | `(call_id, name, args)` | 工具调用真正开始，带真实 call id | TUI/Desktop 工具卡、Slack 原生卡（`gateway/run_turn_runner.py:794-807`） |
| `tool_complete_callback` | `(call_id, name, args, result)` | 工具返回 | 同上 |
| `tool_result_metadata_callback` | `(call_id, name, args, result) -> dict` | 结果 append 之前准备展示元数据，原始内容不改（`tool_executor.py:1148-1154`） | gateway 结果渲染 |
| `tool_gen_callback` | `(name)` | 从流里解析出工具调用、参数尚在生成 | spinner 里的工具预览（`stream_delivery.py:364-366`） |
| `thinking_callback` | `(text)` | 模型开始/停止思考，或等待提示 | 「thinking...」指示器 |
| `reasoning_callback` | `(text)` | 推理增量，或非流式下的整段推理（`chat_completion_helpers.py:1565-1573`） | 推理面板 |
| `status_callback` | `(kind, message)` | 统一状态出口，`_emit_status` / `_emit_warning` 都走它（`status_output.py:74-82`） | CLI 行、gateway 状态、ACP |
| `notice_callback` | `(AgentNotice)` | 粘性通知，如额度恢复 | `notification.show` |
| `notice_clear_callback` | `(key)` | 撤销通知 | `notification.clear` |
| `clarify_callback` | `(questions)` | `clarify` 工具 | CLI 提问、gateway 交互消息 |
| `read_terminal_callback` | `(start=None, count=None)` | 工具要读终端屏 | Desktop 终端面板 |
| `read_preview_callback` | `(start=None, count=None)` | 读 preview 页文本 | Desktop 预览 |
| `drive_preview_callback` | `(payload)` | 操作 preview 页 | Desktop 预览 |
| `read_window_below_callback` | `()` | 枚举原生窗口 | Desktop |
| `connection_callback` | `(payload)` | 连接授权卡片 | `connection.request`；同时决定 surface 是 card |
| `tour_callback` | `(payload)` | 引导流程 | Desktop 渲染器 |
| `theme_callback` | `(payload)` | 换主题 | `theme.apply` 或 `theme.pick` |
| `setup_beat_callback` | `(payload)` | 首次配置进度 | Desktop 引导 |
| `step_callback` | `(api_call_count, previous_tool_round)` | 每次迭代准备阶段（`agent/turn_iteration_prep.py:158-163`） | gateway 的 `agent:step`、ACP（`acp_adapter/events.py:255-259`） |
| `stream_delta_callback` | `(text)`，`text=None` 表示冲刷 | 每个文本增量（`stream_delivery.py:298-334`） | CLI/gateway 流式显示 |
| `interim_assistant_callback` | `(text, *, already_streamed=False)` | 带工具调用的回合里，中间叙述文本 | 中间气泡，需要去重（`stream_delivery.py:106-122`） |
| `event_callback` | `(event_type, payload)` | 生命周期事件，目前确认的有 `session:compress`（`conversation_compression.py:3458-3467`、`codex_runtime.py:247-252`） | plugin hook |
| `reaction_callback` | `(kind)` | 情绪反应（ily、<3、good bot） | TUI/Desktop 表情 |
| `plugin_message_callback` | `(row)` | 插件注入的行（adviser steer/continuation） | 表面层即时渲染（`tui_gateway/agent_callbacks.py:186-189`） |

### 6.3 三条纪律

**一、回调坏了不能弄坏循环。** `_call_callback` 和 `_call_quietly` 都是 `except Exception` 加 debug 级日志。`step_callback` 有自己的 try/except（`turn_iteration_prep.py:160-163`）。写回调时不要指望异常能传回 agent。

**二、`None` 是流冲刷哨兵。** 工具轮开始前，`stream_delta_callback(None)` 让显示层把已开的气泡收尾（`turn_tool_round.py:160-164`）。回调实现必须处理 `None`，否则工具轮会把半截文本永远留在屏幕上。TTS 的 `_stream_callback` 明确不接收 `None`，两个通道语义不同。

**三、一个流只有一个写者。** 被重试取代的流不能再往 UI 里吐 token，`_fire_stream_delta` 和 `_fire_reasoning_delta` 都会先查 `_stream_writer_superseded()` 再发（`stream_delivery.py:298-304`、`343-347`，注释引 #65991）。中间叙述还要去重：`_delivered_interim_texts` 记录已送达文本，前缀匹配用于判断「这段是不是已经流过了」（`:112-122`）。

### 6.4 最小集与全集

全量装配在 `tui_gateway/agent_callbacks.py:142-196`：21 个常驻回调加上按配置启用的 `interim_assistant_callback`，全部映射到 sid 级事件。旁路代理只装需要的几个，`preview.restart` 就是最小集的范例（`agent_callbacks.py:528-555`）：

```python
return {"tool_start_callback": tool_start, "tool_complete_callback": tool_complete,
        "tool_progress_callback": tool_progress,
        "tool_gen_callback": lambda name: progress(f"Preparing {name}"),
        "status_callback": _restart_status}
```

没有 `stream_delta_callback`，没有 `clarify_callback`，因为重启面板只需要进度行。这个取舍本身就是设计说明：回调是可选的观察面，不是 agent 的必需品。

## 7. 与官方文档的三处偏差

| 官方文档说法 | 代码实际 | 证据 |
|---|---|---|
| `run_agent.py` 是 15k+ 行的巨型文件，循环在里面 | 1555 行门面，循环在 `agent/conversation_loop.py`，相位在 `agent/turn_*.py` | `wc -l run_agent.py`；`turn_facade.py:1-7` 模块注释 |
| 中断后不会把部分响应注入历史 | 部分文本会作为 `assistant` 行落库并标记 `interrupted`；只有失控重复用隐藏占位 | `agent/turn_api_call.py:196-206` |
| `step_callback` 在「每次完整 agent 轮次结束后」 | 在每次迭代的准备阶段，参数是 `(api_call_count, previous_tool_round)` | `agent/turn_iteration_prep.py:158-163`；`acp_adapter/events.py:255-259` |

文档的中文页比英文页更旧：中文页仍写 `run_agent.py` 是 15k 行，英文源文件已经改成门面 + 相位的描述（`website/docs/developer-guide/agent-loop.md:9`）。对照时以英文源文件和代码为准。

## 8. 源码索引

| 想改什么 | 从这里进 |
|---|---|
| turn 生命周期、相位顺序 | `agent/conversation_loop.py:1648-1706` |
| 准入、租约、relay 作用域 | `agent/turn_facade.py:22-222` |
| 构造参数落到属性 | `agent/agent_init.py`、`agent/agent_init_params.py` |
| 状态输出与 quiet 语义 | `agent/status_output.py` |
| 流式、interim、单写者 | `agent/stream_delivery.py` |
| 工具解析、审批、并发 | `agent/tool_executor.py` |
| 工具轮的轮次前后 | `agent/turn_tool_round.py` |
| 回调装配（TUI/Desktop） | `tui_gateway/agent_callbacks.py:142-196` |
| 旁路代理构造 | `tui_gateway/agent_callbacks.py:427-457` |
| 子代理构造 | `tools/delegate_tool.py:273-286` |
| 能力面收窄 | `tools/connectors/turn.py`、`toolsets.py:467-471` |
| 出站 thread 路由 | `gateway/delivery.py:300-310` |
