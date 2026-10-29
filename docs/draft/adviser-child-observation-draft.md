# adviser 观察后台 minir agent

状态：已落地（2026-10-28）。§6 的改动点都已实现，代码与片段不完全相同，差异见 §11。
2026-10-30 修订：子 agent 观察改为默认关闭，新增 `adviser_mode.observe_children`（默认 false）；§0.12、D4、§10、§11 已按此改写。
依据：2026-10-27 的对话与四轮决定（U1 到 U4），加上对 `plugin/minir` 与本机 Hermes 源码（`C:\Users\zarag\AppData\Local\hermes\hermes-agent`）的追踪。机制事实都带 `file:line`，行号取自当天代码。
代码验证状态见 §7：实现按 §6.4 的清单跑过测试（四个测试文件，加 conftest 一处 fixture；不跑全量、e2e、smoke）；运行期未验证项照旧列在 §7。

## 0 当前结论

已定：
- 观察入口是一个独立的 watch 注册表，不放宽 `_primary`。
- 每个后台 run 在派发时拿到确定性的 host session id，并交给 `agent_loop.run`。
- 开关两个：`adviser_mode.enabled` 是总开关；`adviser_mode.observe_children`（默认 false）决定子 agent 的 run 是否进观察范围。关掉时它的 reasoning 日志、review 与 adviser 副本都不写，策略路径仍只有主会话。
- 预算复用现有的 `max_reviews_per_turn` 与花名册 `cadence`，不另设冷却或每 run 上限。
- 落盘分三股流：host 流、子 run 实时流、子 run 最终 transcript。子 run 的写入从不落 host 层，每一行都带 `run_id`。
- 评审上下文（digest / rulers / stats / history）取父会话，事件与 reasoning 取子会话：子会话的评审质量与主会话一致。
- 进度每批工具一行，走父会话的投递路径；父会话不 live 时只留在 watch 的 steps 里。
- 实时轨迹可读：`minir_aiagent_search` 按 child session 在缓存或落盘文件里查。
- 子 run 的门禁是配置，语义与主 `adviser_only` 相同。
- 评审按 `adviser_target` 收缩：子 run 没载入 SOUL，就跳过 soul 相关 check 并标 n/a。
- 每个子评审有独立限额：父块 6k、事件 12k、总 18k，都是常量。
- 进度行的合并窗口是 2 秒：窗口内的多批并成一行，内容不丢。
- 收缩判据是一张固定映射：目标 → 需要的 capability；缺就不产出，评审结果标 n/a。
- 子 run 被 `adviser_only` 拦下时，拒绝文本就是反馈，同时记一条 blocked 工具事件进 ring。

未决：无。四轮决定（U1 到 U12）都落成了 §4 的 D1 到 D14；剩下的是运行期验证，见 §7。

## 1 原文

1. "怎么让这些后台minir-agent实时得到adviser的评价"
2. "按你的分析adviser能全面观察子run,不止评价"
3. "u1落盘区分是host还是具体的minir-agent"
4. "u2 先复用,开启adviser的成本很高，要开一起开"
5. "u3 进度出口需要"
6. "u4 子会话的评审质量和主会话一致"

## 2 一句话

给每个后台 run 一个可寻址的 host session id 并登记成 watched child；adviser 的 recorder hook 放行这些 session，于是同一套观察模型（user / intent / reasoning / tool / final）覆盖子 run。评价、中止、进度都从这套观察里长出来，共用一个开关。

## 3 机制核对

| 说法 | 实际机制 | 后果 |
| --- | --- | --- |
| 后台 run 是独立会话 | `_execute` 不传 `session_id`（minir_agents.py:377-397），`init_agent` 生成新 id（agent_init.py:1193） | 插件从不知道这个 id，无法寻址 |
| hook 能看到子 run | `post_tool_call` payload 带执行 agent 的 `session_id`（tool_executor.py:1726 → model_tools.py:692-693） | 事件到了，但被 `_primary` 丢掉 |
| `_primary` 只认本会话 | adviser_mode.py:958-968 | 子 session 一律返回空串 |
| 开关是纯配置 | `enabled()` 只读 `adviser_mode.enabled`（adviser_mode.py:970-972） | 一个键就能"一起开、一起关" |
| 子 run 的 turn 是绑定的 | conversation_loop.py:1824 `bind_turn`；plugin_runtime 按 session id 找 agent（plugin_runtime.py:76-84, 325-357） | `steer`/`interrupt` 可用，前提是 id 可知 |
| steer 在批次边界插入 | interrupt_control.py:317-338；agent_runtime_helpers.py:3708-3745 | 实时 = 下一个工具批次，不是请求中途 |
| 完成通知回父会话 | 事件 `session_key` 是父会话（minir_agents.py:349-352）；CLI 在 idle tick drain（cli_process_notifications.py:51-74） | 父会话只看到 ≤2000 字的 summary |
| 策略门也走 `_primary` | `block_reason` :2500-2502、`late_blocker` :2598、`verify_continue` :2617 | 放宽 `_primary` 会把 `adviser_only` 施加到子 agent 的写操作 |
| ring 是窗口 | MAX_RING=96、MAX_EVENT_CHARS=12000、MAX_PROMPT_CHARS=24000（adviser_mode.py:66-68） | 长 run 会丢最早的事件；完整记录在 transcript |
| 评审上下文全取 `session` | `_prompt` 的 digest / rulers / versions / stats / history 都按 `session` 取（adviser_mode.py:1656-1700） | 子会话没有 store turn 行，digest 为空，评审会变泛 |
| 目标本来就能收缩 | `soul_targets_enabled` 加"无 soul 块时 soul_* 标 n/a"（adviser_mode.py:106、1521、1920-1923） | 子 run 缺 SOUL 时沿用同一条路，不造新机制 |
| transcript 两套写入口 | `append_event` 与 `record_aux` 都收 `run_id`（transcript.py:407-410、599-602） | 一股流能按 run 区分，也能把实时流单独成流 |
| transcript 在结束后才写 | `_execute` 跑完才 `record_aux`/flush（minir_agents.py:406-413） | 跑到一半只有实时流，没有完整记录 |
| 子 agent 不能派回自己 | `minir_agents` 在 `READ_ONLY_DROPPED_TOOLS`（agent_loop.py:100-103） | 进入子 run 的只有 steer/interrupt |

观察到的字段与主会话同档：子 run 的 prompt（`on_pre_llm_call`）、中间 assistant 文本（`on_interim_message`）、reasoning（`on_stream_delta`）、每次工具调用的 args/result/error（`on_post_tool_call`）、final（`on_post_llm_call`）。

## 4 决策槽位

| 槽 | 问题 | 已定 | 放弃 |
| --- | --- | --- | --- |
| D1 观察入口 | 放宽 `_primary` 还是另开注册表 | 另开 `agent_watch`；recorder hook 走 `_observe`，策略路径仍走 `_primary` | 零改动接入 |
| D2 子 run 的 id | 随机 id 还是确定性 id | 派发时生成 `parent:minir:agent:job`，交给 `agent_loop.run(session_id=...)`，并写进句柄 | 不动 minir_agents |
| D3 观察范围 | 只看评价还是全观察 | 全观察进 ring；评价、中止、进度三个消费者 | 一上来就做 pre-tool 门禁 |
| D4 开关 | 每个消费者一个键还是一个总键 | 两个键：`adviser_mode.enabled`（总开关）与 `adviser_mode.observe_children`（默认 false，子 agent 是否进观察范围，2026-10-30 修订） | 分消费者开关 |
| D5 预算 | 每 run 评几次 | 复用 `max_reviews_per_turn` 加花名册 `cadence`；depth 常量 1 | 新增冷却与每 run 上限 |
| D6 落盘 | 实时写哪里，怎么区分 host | 三股流：host / 子 run 实时（child session + run_id）/ 子 run 最终（parent session + run_id） | 混写一股流再靠 fp 去重 |
| D7 评审上下文 | 子会话的空 digest 怎么补 | digest / rulers / stats / history 取父会话；事件与 reasoning 取子会话 | 给子会话补 store turn 行 |
| D8 进度出口 | 进度给谁、走哪、多密 | 父会话的投递路径；每批工具一行 | 新 UI 或新工具 |
| D9 策略 | 子 run 是否继承 `adviser_only` | 继承，语义与主会话相同；开关是配置 | 给子 run 单独一套策略 |
| D10 子评审限额 | 用多少字 | 常量：父块 6k、事件 12k、总 18k | 和主会话共用 24k |
| D11 读入口 | 子 run 的轨迹怎么查 | `minir_aiagent_search` 按 child session 先读缓存再读落盘 | 只写不读 |
| D12 进度合并 | 每批一行怎么不刷屏 | 2 秒窗口内并成一行，窗口外立刻发 | 逐条直发 |
| D13 收缩映射 | 哪些目标跟着哪个能力走 | `TARGET_NEEDS`：目标 → capability，缺则不产出并标 n/a | 按 flags 临时判断 |
| D14 被拦反馈 | 子 run 被策略拦下时怎么知道 | 拒绝文本即反馈；同时记一条 `status="blocked"` 的工具事件进 ring | 额外发 advisory 行 |

## 5 设计记录

### 设计 A：评价优先

原文

```
怎么让这些后台minir-agent实时得到adviser的评价
```

一句话：把每个后台 run 登记成 watched child，评审它的工具事件，把结论 steer 进去。

覆盖：D1、D2、D4、D5。

换来：
- 子 run 在跑的时候就能收到纠正，不用等完成通知。
- 复用现有的评审、ledger、severity、投递；`_deliver_host` 只多一个 `child` 判断。
- 不动宿主，`plugin_runtime_v2` 已经够用。

放弃：
- 只观察工具边界，看不到工具执行中间。
- 每次评审是一次模型调用；开关一开就按花名册的 cadence 计费。

失效面：
1. 子 run 在评审返回前结束：`steer` 返回 ok 但行没人取，建议丢失。只能保证"结束前插入的行"进最终 messages。
2. 单个长工具调用期间没有边界，建议迟到。
3. 冻结的 `_primary` 语义被绕开：如果以后有人把 `_observe` 用到策略路径，子 agent 会突然被 `adviser_only` 拦住。
4. 主会话的评审仍然只看到句柄和完成通知，看不到子 run 的中间步骤（要看得进设计 B）。

### 设计 B：全观察

一句话：观察层与消费者层分开。观察层负责"这个子 session 的事件进 ring、进实时流"；评价、中止、进度都是消费者，全部跟随同一个开关。

覆盖：D1 到 D14。

换来：
- 同一个 run 可以同时被评价、被中止、被查看，不用各写一条链。
- `interrupt` 立刻可用：`host_bridge.interrupt(child)` 与 steer 同一张 `_LIVE` 表。
- 子会话的评审拿到父会话的 digest，质量与主会话一致，不需要给子会话补记忆行。
- 进度不需要新出口：父会话怎么收 adviser 的话，就怎么收子 run 的进度，而且每批工具都有。
- 轨迹可查：`minir_aiagent_search` 按 child session 先读缓存，再读落盘。
- 评审只评子 run 真有的东西：缺 SOUL 就跳 soul_*，并给独立限额。

放弃：
- 开启 adviser 就是全开：每个后台 run 都可能买一次评审，成本随并发线性增长。
- 子 run 继承 `adviser_only`：被评的子 agent 不能改文件，只能给建议。
- 多一股实时流：两处落盘，读的时候要知道自己读的是哪股。
- "全"仍然是 hook 面：模型内部推理原文、memory prefetch、系统提示拼装都看不到。

失效面：
1. ring 96 条：长 run 的早期事件被挤掉，评审只看到最近一段。
2. 子评审独立限额：父块 6k、事件 12k、总 18k；更长的上下文不再进 prompt，靠 §6.3 第 11 段的读入口补。
3. 孙 agent（depth 2）默认不可见；它的工作只以子 agent 的工具调用形式出现。
4. 终止后的建议行没人消费，只能留在 ledger 里被 32 行窗口挤掉。
5. 每批一行就算有 2 秒窗口：60 秒的 run 仍会给父会话加约 30 行，可能挤掉真正的建议。
6. 实时流与最终 transcript 是两份文件；只看一份的人会以为丢了或重了。
7. 策略同主会话：`adviser_only` 一开，被评的子 agent 就不能写文件。这是 D9 的选择，配置说明里要写死。

采纳：B。A 是 B 的第一个消费者。

## 6 代码

### 6.1 新模块

```python
# file: plugin/minir/agent_watch.py
"""Background agent runs the adviser may observe.

``minir_agents`` hands every background job a session id it can address (the run's own
``AIAgent.session_id``) and registers it here. Three readers use this table:

* the adviser recorder hooks, which admit a watched session where they would otherwise drop
  everything that is not the provider's own session;
* the live transcript mirror, which writes each event to the run's own stream;
* ``minir_agents``' progress line, which reads the last steps of a run.

Nothing here budgets reviews or reads config: one switch (``adviser_mode.enabled``) turns the
whole observation on, and the adviser's existing per-turn cap and roster cadence bound the cost.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

#: A grandchild is not watched: review cost grows with depth, and the parent review already
#: sees the grandchild's work as the child's own tool calls.
DEFAULT_MAX_DEPTH = 1
#: Kept for the progress line; the ring itself is the adviser's.
PROGRESS_STEPS = 8
#: Lines inside this window merge into one push, so a fast tool loop cannot flood the parent.
PROGRESS_MIN_GAP = 2.0

CHILD_BRIEF = (
    "[background agent review]\n"
    "You are reviewing a live background agent run ({agent}, job {job}). It is not the "
    "operator's session and it cannot answer you. Your text is inserted after its next tool "
    "batch as an adviser row: write one to three sentences naming the exact tool or step to "
    "change, and nothing else.\n"
    "[end background agent review]\n"
)


@dataclass
class Watch:
    """One background run the adviser is allowed to observe."""

    session: str
    parent: str
    agent: str
    job_id: str
    depth: int = 1
    steps: deque = field(default_factory=lambda: deque(maxlen=PROGRESS_STEPS))
    advice: list = field(default_factory=list)
    capabilities: dict = field(default_factory=dict)
    pending_progress: str = ""
    last_push_at: float = 0.0


def child_session_id(parent_session: str, agent: str, job_id: str) -> str:
    """The host session id of one background run: stable, addressable, one per job."""
    parent = str(parent_session or "").strip() or "session"
    name = str(agent or "").strip() or "agent"
    job = str(job_id or "").strip() or "job"
    return f"{parent}:minir:{name}:{job}"


class WatchRegistry:
    """Process-wide table of watched runs, keyed by the run's session id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, Watch] = {}

    def register(self, *, parent: str = "", agent: str = "", job_id: str = "",
                 depth: int = 1, max_depth: int = DEFAULT_MAX_DEPTH,
                 capabilities: Optional[dict] = None) -> Optional[Watch]:
        """Watch one run; ``None`` when it has no job id or is too deep to watch."""
        if not str(job_id or "").strip():
            return None
        try:
            level = int(depth)
        except (TypeError, ValueError):
            level = DEFAULT_MAX_DEPTH
        try:
            ceiling = int(max_depth)
        except (TypeError, ValueError):
            ceiling = DEFAULT_MAX_DEPTH
        if level > max(0, ceiling):
            return None
        watch = Watch(session=child_session_id(parent, agent, job_id),
                      parent=str(parent or ""), agent=str(agent or ""),
                      job_id=str(job_id).strip(), depth=level,
                      capabilities=dict(capabilities or {}))
        with self._lock:
            self._items[watch.session] = watch
        return watch

    def unregister(self, session: str) -> None:
        with self._lock:
            self._items.pop(str(session or ""), None)

    def get(self, session: str) -> Optional[Watch]:
        with self._lock:
            return self._items.get(str(session or ""))

    def is_child(self, session: str) -> bool:
        return self.get(session) is not None

    def note_step(self, session: str, line: str) -> None:
        """One raw progress line per tool batch; bounded, newest last."""
        watch = self.get(session)
        text = str(line or "").strip()
        if watch is not None and text:
            watch.steps.append(text)

    def push_line(self, session: str, line: str, *, gap: float = PROGRESS_MIN_GAP,
                  now: Optional[float] = None) -> str:
        """Merge lines inside the gap window; return the text to push now, or '' to wait."""
        watch = self.get(session)
        text = str(line or "").strip()
        if watch is None or not text:
            return ""
        merged = f"{watch.pending_progress}; {text}" if watch.pending_progress else text
        clock = time.time() if now is None else float(now)
        if clock - watch.last_push_at < max(0.0, float(gap)):
            watch.pending_progress = merged
            return ""
        watch.pending_progress = ""
        watch.last_push_at = clock
        return merged

    def note_advice(self, session: str, text: str) -> None:
        """Keep what was delivered to the run, in order; the audit trail reads it."""
        watch = self.get(session)
        if watch is not None and text:
            watch.advice.append(str(text))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


REGISTRY = WatchRegistry()


def register(**kwargs: object) -> Optional[Watch]:
    return REGISTRY.register(**kwargs)


def unregister(session: str) -> None:
    REGISTRY.unregister(session)


def get(session: str) -> Optional[Watch]:
    return REGISTRY.get(session)


def is_child(session: str) -> bool:
    return REGISTRY.is_child(session)


def note_step(session: str, line: str) -> None:
    REGISTRY.note_step(session, line)


def push_line(session: str, line: str, **kwargs: object) -> str:
    return REGISTRY.push_line(session, line, **kwargs)


def note_advice(session: str, text: str) -> None:
    REGISTRY.note_advice(session, text)


def clear() -> None:
    REGISTRY.clear()


def child_brief(session: str) -> str:
    """The extra system paragraph for a child review; empty for the primary session."""
    watch = get(session)
    if watch is None:
        return ""
    return CHILD_BRIEF.format(agent=watch.agent or "agent", job=watch.job_id or "job")
```

### 6.2 minir_agents.py

三处改动：登记、`session_id`、`run_id`。`record_aux` 写的是最终流，属于这个 run 的行带 `run_id`。

```python
# file: plugin/minir/minir_agents.py
# 1) import
from . import agent_history, agent_loop, agent_watch, file_guard

# 2) _execute(): 登记在第一个请求之前；结束后注销。capabilities 决定评审能查什么
    watch = agent_watch.register(
        parent=job.session_key, agent=job.agent, job_id=job.job_id, depth=job.depth,
        capabilities={
            "soul": bool(job.flags.get("load_soul_identity", True)),
            "context_files": not job.flags.get("skip_context_files", False),
            "memory": not job.flags.get("skip_memory", False),
            "file": "file" in (job.toolsets or ()),
            "toolsets": list(job.toolsets or ()),
        })
    try:
        result = agent_loop.run(
            ...
            session_id=watch.session if watch is not None else "",
            ...
        )
    finally:
        if watch is not None:
            agent_watch.unregister(watch.session)

# 3) _execute(): 最终流带 run_id，句柄带 child_session_id
    transcript.record_aux(job.agent, job.session_key, result.messages,
                          model=result.model, provider=result.provider,
                          toolsets=result.toolsets, system_message=job.system_prompt,
                          run_id=job.job_id)
    ...
        "transcript_written": written,
        "child_session_id": watch.session if watch is not None else "",
    }
```

三股流，读的时候先看 `<layer>/<session>.jsonl`：

| 流 | layer | session_id | run_id | 写入者 |
| --- | --- | --- | --- | --- |
| host 主会话 | `host` | 父会话 | 空 | 宿主 ingest |
| 子 run 实时 | `<agent>` | `child_session_id` | `job_id` | adviser 的 recorder 镜像 |
| 子 run 最终 | `<agent>` | 父会话 | `job_id` | `record_aux` |

### 6.3 adviser_mode.py

```python
# file: plugin/minir/adviser_mode.py
# 1) import
from . import agent_watch

# 2) after _primary(): 观察入口与策略入口分开
    def _observe(self, session_id: str = "") -> str:
        """The session a recorder hook may write to: a watched child, else the primary.

        Policy paths (``blocks_tool``, ``late_blocker``, ``verify_continue``) keep calling
        ``_primary``: a child must not inherit ``adviser_only`` from the session that spawned it.
        Depth was capped when the run was registered; no second config key gates it here.
        """
        child = agent_watch.get(str(session_id or ""))
        return child.session if child is not None else self._primary(session_id)

# 3) six recorder sites: only these change
#    on_pre_llm_call / on_interim_message / on_stream_delta /
#    on_post_api_request / on_post_tool_call / on_post_llm_call
-        session = self._primary(session_id)
+        session = self._observe(session_id)

# 4) _record(): 入 ring 之后镜像到子 run 的实时流（host 流永不触碰）
        watch = agent_watch.get(session)
        if watch is not None:
            transcript = getattr(self._provider, "_transcript", None)
            if transcript is not None:
                transcript.append_event(
                    watch.agent, watch.session, str(event.get("role") or ""),
                    kind=str(event.get("kind") or "observe"),
                    text=str(event.get("text") or ""),
                    run_id=watch.job_id,
                    **{k: v for k, v in event.items()
                       if k in ("tool_calls", "tool_call_id", "result")})
            # 每批工具一行；父会话 live 才推，否则只进 steps
            if str(event.get("kind") or "") == "tool":
                self._child_progress(watch, str(event.get("text") or ""))

    def _child_progress(self, watch: Any, text: str) -> None:
        """One line per tool batch; the 2s window merges a burst before it reaches the parent."""
        line = f"[child {watch.agent} {watch.job_id}] {text[:160]}"
        agent_watch.note_step(watch.session, line)
        push = agent_watch.push_line(watch.session, line)
        if push and host_bridge.live(watch.parent):
            host_bridge.steer(watch.parent, push)

# 5) maybe_schedule(): 子 run 只在工具时评审，完成时交给父会话
        if agent_watch.is_child(session) and trigger != "tool":
            return

# 6) _review_one(): 子会话的 brief
                system_message=(_review_brief(cfg, hermes_home=self._hermes_home())
                                + agent_watch.child_brief(session)),

# 7) _prompt(): 上下文取父会话，事件与 reasoning 取子会话；按能力收缩，按额度截断
#    常量接在 MAX_PROMPT_CHARS 后面（adviser_mode.py:66-69）
CHILD_CONTEXT_CHARS = 6_000
CHILD_EVENT_CHARS = 12_000
CHILD_PROMPT_CHARS = 18_000

    def _prompt(self, session: str, ...):
        watch = agent_watch.get(session)
        context = (watch.parent if watch is not None else "") or session
        caps = dict(watch.capabilities) if watch is not None else {}
        ...
        digest = self._digest(context)
        rulers = self._rulers(context)
        versions = self._ruler_versions(context)
        stats = self._stats_block(context)
        ...
        history = transcript_prompt.digest_block(
            "adviser", transcript, platform, HOST_LAYER, context, max_chars=3_000)
        if watch is not None:
            parent_block = parent_block[:CHILD_CONTEXT_CHARS]          # 6k
            event_budget = CHILD_EVENT_CHARS                           # 12k
            max_chars = CHILD_PROMPT_CHARS                             # 18k
        # 缺能力的目标不产出（映射见 7a）
        targets = [t for t in targets if caps.get(TARGET_NEEDS.get(t, ""), True)]

# 7a) plugin/minir/prompting.py：目标 → 需要的能力，一张表，缺省不拦
TARGET_NEEDS = {
    "soul_tool_policy_check": "soul",
    "soul_workflow_check": "soul",
    "soul_craft_check": "soul",
    "user_preferences_check": "memory",
    "user_identity_check": "memory",
    "knowledge_claim_check": "memory",
    "ai_self_representation_check": "memory",
    "code_economy": "file",
}

# 8) _deliver_host(): child 只有 steer/interrupt（进度在 _record 里发，不在这里）
        child = agent_watch.get(session) is not None
        if (not terminal and not child and cfg.get("host_continue")
                and top >= SEVERITY_ORDER[cfg["continue_severity"]]):
        if (terminal and not child and cfg.get("trigger_turn")
                and top >= SEVERITY_ORDER[cfg["trigger_severity"]]):
        if terminal and not child and cfg.get("preserve"):

# 9) block_reason(): 子 run 走同一套策略（mode 是配置，语义与主会话一致）
    def _policy(self, session_id: str = "") -> str:
        child = agent_watch.get(str(session_id or ""))
        return child.session if child is not None else self._primary(session_id)

    # blocks_tool / block_reason 里的 self._primary 换成 self._policy
    # 拦下时：拒绝文本就是给子 run 的反馈；同时记一条 blocked 事件，评审和 transcript 都看得到
    if reason:
        self._record(session, "tool", tool_name=tool_name, status="blocked",
                     extra={"reason": reason})
        return reason

# 10) _review_one(): 收缩掉的目标标 n/a，沿用 soul_targets_enabled 的写法（:1920-1923）
            watch = agent_watch.get(session)
            caps = dict(watch.capabilities) if watch is not None else {}
            for target, need in TARGET_NEEDS.items():
                if not caps.get(need, True):
                    coverage.setdefault(target, "n/a")

# 11) minir_aiagent_search(): 按 child session 查轨迹，先缓存再落盘
#     child id 自描述：<parent>:minir:<agent>:<job>，layer 从中间那段取
    def _child_stream(child_session: str) -> tuple[str, str, str]:
        parent, _, rest = str(child_session).partition(":minir:")
        agent, _, job = rest.partition(":")
        return agent, parent, job

    layer, parent, job = _child_stream(child_session)
    rows = buffer.rows(layer, child_session)          # 缓存优先
    if not rows:
        rows = read_jsonl(path_for_aux(layer, child_session, profile=profile))  # 落盘
    # run 已结束、实时流不在了：读最终流 <layer>/<parent>.jsonl，按 run_id == job 过滤
```

### 6.4 测试（已实现并执行）

```python
# file: tests/test_agent_watch.py
# id 稳定性；注册后可按自己的 session 寻址；depth 上限与空 job 拒绝；
# unregister 后拒绝；steps 有界（8 条）；advice 保持顺序；capabilities 原样带回

# file: tests/test_adviser_mode.py（追加）
# test_a_watched_child_run_is_recorded_and_reviewed
# test_a_child_event_reaches_the_runs_own_stream
# test_the_child_review_uses_the_parent_digest
# test_child_advice_is_steered_and_never_queued
# test_every_tool_batch_emits_one_progress_line
# test_a_blocked_tool_records_a_blocked_event
# test_target_needs_drops_what_the_child_cannot_do
# test_the_child_policy_follows_the_configured_mode
# test_a_grandchild_is_not_recorded
# test_disabled_records_nothing
# test_a_watch_from_another_profile_is_not_observed
# test_a_child_prompt_is_bounded_more_tightly_than_the_primary

# file: tests/test_minir_agents.py（追加）
# test_a_run_registers_its_session_while_it_runs
# test_the_final_stream_carries_the_run_id_and_leaves_the_host_stream_alone

# file: tests/test_minir_agents.py（修改）
# test_background_false_keeps_the_blocking_payload 的键集加了 "child_session_id"

# file: tests/test_agent_history.py（追加）
# test_a_child_trace_reads_the_runs_own_stream
# test_a_finished_run_falls_back_to_the_final_stream_by_run_id
```

执行范围只有这四个文件（加 `tests/conftest.py` 的一个 autouse 清理 fixture）；不跑全量、不跑 e2e、不跑 smoke。三股流的断言分在写入方：host 流与最终流在 `test_minir_agents.py`，实时流在 `test_adviser_mode.py`（镜像的写入方是 adviser，不在 `minir_agents` 里）。

## 7 验收

- 机制核对：§3 的每一条都在插件与宿主源码里读过，行号是当天的。
- 实现：§6 的改动点都落在设计 B 上，差异见 §11。
- 已执行：`python -m pytest tests/test_agent_watch.py tests/test_adviser_mode.py tests/test_minir_agents.py tests/test_agent_history.py`（113 项通过）。相邻面 `tests/test_adviser_channels.py tests/test_provider.py tests/test_contract.py` 也通过（123 项）。不跑全量、e2e、smoke。
- 未验证（运行期，需要真实 run）：后台 run 在跑时收到 advisory 行；`agent.steer` 在 in-process 子 run 上被 drain；三股流在真实 run 里的落盘位置与轮转；进度合并在真实节奏下的时延。

## 8 边界

- 实时 = 下一个工具批次边界；一个长工具调用期间插不进去，建议和进度都一样。
- 迟到即丢：run 结束后 `_pending_steer` 没人取，只有 surface 会把 leftover 变成下一轮。
- 观察是窗口：96 条事件、文本 12k、评审 prompt 24k；父会话的上下文块又占一块，所以子评审单独限额。
- 孙 agent 默认不可见（depth ≤1）。
- 子会话读不到自己的 store turn 行；评审靠父会话的 digest，`minir_aiagent_search` 靠 child id 反解 layer。
- 三股流各写各的：实时流只在 adviser 开启时存在，最终流永远存在。
- 进度每批工具一行，2 秒窗口内的多批并成一行；父会话 live 才推，不 live 只进 steps。
- 策略同主会话：`adviser_only` 一开，被评的子 agent 就不能改文件。这是 D9 的设计选择，配置说明里写死。
- 收缩靠派发时的 flags：子 run 的 flags 和 toolsets 就是它的能力清单，flag 写错评审就会多查或少查。
- `agent.steer` 在子 run 的 in-process 路径是否每次都被 drain，只在代码里看到调用点（agent_runtime_helpers.py:3708-3745），没有真实 run 观察。
- 新增了 config 键 `observe_children`（默认 false）：后台 run 默认不进观察范围，`enabled` 一开也只观察主会话。

## 9 未决

无。U9 到 U12 已落成 D10 到 D14；剩下的只有 §7 列出的运行期验证，那不是待定项。

## 10 修订记录

- 2026-10-27 初稿：观察层 + 评价消费者。
- 2026-10-27 二稿：吸收 U1 到 U4。落盘按 host / 实时 / 最终三股流区分；开关收成一个，预算复用现有键；进度走父会话的投递路径；评审上下文改取父会话。
- 2026-10-27 三稿：吸收 U5 到 U8。进度改成每批工具一行；实时轨迹可按 child session 查（先缓存再落盘）；子 run 继承配置里的策略，评审按 `adviser_target` 收缩（缺 SOUL 跳 soul_*）；每次子评审单独限额。
- 2026-10-27 四稿：U9 到 U12 不再留作未决。限额落成常量（6k/12k/18k）；进度用 2 秒窗口合并；收缩落成 `TARGET_NEEDS` 映射；被拦时记 blocked 事件并返回拒绝文本。
- 2026-10-28 落地：按 §6 实现，差异记在 §11；测试按 §6.4 的清单补上并执行。
- 2026-10-30 修订：子 agent 观察默认关闭。落地后层内 pass（b3/b4）被当作可观察 run，review 与 reasoning 日志堆在 operator 会话旁（30 次 review、678,597 prompt tokens、0 条建议）；D4 改成两个键，`_observe` 加 `observe_children` 门槛，补测试 `test_a_child_run_stays_out_of_scope_by_default`。

## 11 实施记录

§6 是示意代码。落地时改了下面这些地方，因为按原样写不成立或会留下副作用：

1. **进度行不能取 `event["text"]`**：工具事件的正文在 `tool` / `args` 里，`text` 是空的，原样写只会推一行空文本（`push_line` 对空串直接返回）。改成 `_progress_line`：工具名 + 非 ok 的状态 + 参数前 120 字。
2. **实时流的行形状**：ring 的 `kind`（user/intent/tool/final）映射成 transcript 的 `role`（user/assistant/tool）与 `kind="msg"`，`run_id=job_id`；工具行的工具名进 `name`、结果进 `result`。这样 `read_aux` 和 `minir_aiagent_search` 的 `msg` 视图能直接把这股流当对话读。
3. **放行不止六个 hook**：`record_user` / `record_intent` / `record_tool` 内部各自又查了一次 `_primary`，只改 hook 里的六处，ring 里不会有 user 事件。`pre_llm_call` 的适配器（`__init__.py`）也提前按 provider session 过滤，同样改成放行 watched child。
4. **能力清单里的 `file`**：`tool_sets` 省略时 `job.toolsets` 是空的但 `all_tools=True`，子 run 实际持有 file 工具；`_capabilities` 用 `all_tools or "file" in toolsets`，否则默认子 run 会白白丢掉 `code_economy`。
5. **"缺则不产出"落在两处**：草稿里的 `targets = [...]` 没有可过滤的变量（目标词表在 `rules/advice_target.md` 的正文里）。实现改成 prompt 里给一张 `unavailable_targets` 清单，`_review_one` 解析后按 `TARGET_NEEDS` 丢掉这些目标、记 `unavailable_target` 并把 coverage 标 `n/a`。
6. **父块 6k**：草稿的 `parent_block` 不存在。实现把 digest / rulers / versions / stats / history 拼成一个 `parent_context` 块，截到 `CHILD_CONTEXT_CHARS`；事件 12k 是 `transcript_delta` 的截断点，带可见标记；总限额 18k 接在原有的整段截断循环上。
7. **评审计费记到父会话**：`accounting_scope(..., context)`。子会话 id 是造出来的，记到它名下会在宿主会话表里留一行幽灵会话（`accounting.py` 的注释就是这么警告的）。
8. **`preserve` 与网关通知**：`_deliver_host` 的 `preserve` 分支也加了 `not child`（草稿只写了 continue 与 trigger）；`_maybe_notify` 对 watched child 直接返回，免得子 run 的建议走聊天通道。
9. **未做**：`handle_llm_request`（`llm_request` 中间件）仍只认 `_primary`，所以子 run 的建议只有 steer / interrupt 一条路，与 D8 一致。
10. **`Watch` 多一个 `profile`**：一个进程可以服务多个 profile，而 ring 与 transcript 按 profile 分区。`register` 记下派发者的 profile（取自 transcript），`_observe` 只在同一 profile 内放行该 child；两边都为空时不拦，测试和旧调用方不受影响。

11. **子 agent 默认不观察**：`_observe` 增加 `adviser_mode.observe_children`（默认 false）。10-28 的实现把每个 `agent_watch` 登记的 run 直接放行，而 `minir_agents` 的每个 job（含 b3/b4 层内 pass）都会登记，于是 reasoning 日志与 adviser 副本按 `<parent>:minir:<agent>:<job>` 落盘。默认关闭后这些 run 的记录与评审全部不生效；要恢复旧行为把该键设为 true。策略门禁单独走 `_policy`，不复用 `_observe`：未观察的子 run 仍继承 `adviser_only`，不会被这个键顺带放开。`plugins/minir/agents` 下的 pass core（b3/b4）也不靠这个键：`minir_agents` 按 `prompt_source` 判定，注册时就带 `observe=False`，`_observe` 直接拒绝，所以 `observe_children: true` 不会把它们带回来。

`minir_agents` 的 `record_aux` / `append_event` 本来就有 `run_id`，`transcript.py` 没有改。
