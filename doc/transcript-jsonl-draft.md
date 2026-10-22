# transcript JSONL 契约草案 v2

状态：已实现（v2 契约）。实现范围对应 §10，验收对应 §11；测试见
`tests/test_transcript.py`、`tests/test_transcript_process.py`、`tests/test_transcript_prompt.py`、
`tests/test_agent_history.py`、`tests/test_session_search.py`。

v2 相对 v1：
- 缓存明确为内存事件表；`last_cache` 改为 JSONL 内的标记行，取消 `.cache.json` sidecar。
- `minir_aiagent_search` 的数据源改为 TranscriptBuffer，读接口并入 buffer。
- 补三条已核事实（摄取截断、每流 4000 条上限、记录数无界），对应新增约束。

取代 `doc/minir-todo-draft.md` §6.1 中「超过两个 part 删除最旧整份」一节（与永久保留冲突）。

## 1. 需求重述

| # | 契约 |
| --- | --- |
| R1 | 一条事件一行 JSON，追加写。不重写、不压缩、不自动删除。 |
| R2 | host 会话收五类：user / system / assistant / tool / reasoning。 |
| R3 | 插件内每个 aiagent（层）收同样的五类。 |
| R4 | 隔离键：`platform / layer / session_id`。platform 在插件初始化时从 host 取得并记录。 |
| R5 | 读取用参数组合表达：全量、增量、滚动、时间、步骤、轮次。不建六套文件。 |
| R6 | 落盘：周期异步追加 + 会话结束有界落盘；最后一个 host 进程关闭时收尾。 |
| R7 | 缓存活在内存；磁盘是记录。全退出后的首个进程按 `last_cache` 标记重建内存。 |
| R8 | 工具：`minir_aiagent_search` 只读 TranscriptBuffer；`minir_hermes_session_search` 读 `state.db`。 |
| R9 | `adviser` 工具集删除：`minir_hermes_session_search` 归入 `minir`，`minir_aiagent_search` 新增，`adviser_consult` 删除。 |
| R10 | 旧 `.json` 不读、不迁、不删。 |
| R11 | 各层提示词按自身逻辑从 TranscriptBuffer 取 transcript 要点；取哪些行、怎么压缩由该层决定，渲染接口统一。 |
| R12 | `adviser_consult` 工具删除（注册、schema、handler、提示词段、toolset 列表）。`worker._adviser_consult` 是内部调用路径，不是工具，保留。 |

非目标：不改 store 表结构；不改层输出契约；不做去重/压缩；不迁移历史。
提示词只增加输入块（R11），不重写各层的判断逻辑。

## 2. 事实基础（已核）

| 事实 | 位置 |
| --- | --- |
| 现持久化为整文件 JSON + temp/os.replace 重写，2000 条分片，30 天 prune | `transcript.py:641-673`、`65-72`、`398-430` |
| 现摄取路径会 `_flatten` 并把单条截到 4000 字符，audit 镜像的是截断后的正文 | `transcript.py:205-209` |
| 内存每流最多留 4000 条（`_messages_per_file*2`） | `transcript.py:610-614` |
| 层可见窗口：每会话 400 条 / 20 万字符 / 20 个会话（`_max_sessions` 可配置） | `transcript.py:59-64`、`222-236` |
| `_max_sessions` 只约束窗口字典，**不约束** `_records`；记录数随会话数无界增长 | `transcript.py:150`、`222`、`600-608` |
| `adviser_consult` 是动态注册的模型可见工具，提示词里有同名段 | `__init__.py:289,534,1519-1524`、`adviser.py:41`、`adviser_prompts.py:34,86,161` |
| `worker._adviser_consult` 是 base/dialectic 的内部 consult 路径，与工具无关 | `worker.py:651-654,693` |
| host 流的 layer 名已是 `host` | `transcript.py:72` |
| 现路径 `<persist_dir>/<layer>/<session>…`；`record_aux` 是整体替换快照 | `transcript.py:635-639`、`246-266` |
| host 每轮把**整段会话** `messages` 交给 `sync_turn`，已脱敏、在后台串行 worker | `memory_manager.py:599-629`、`run_agent.py:913` |
| 插件收了 `messages` 但一行不读 | `__init__.py:673-703` |
| `initialize` 收到 `platform` 但没保存 | `agent_init.py:1267-1272`、`__init__.py:307-390` |
| 插件已有宿主脱敏器的导入与占位降级 | `adviser_mode.py:149-150` |
| 插件已有 contextvar 传运行期身份的范式 | `file_guard.py:35-37`、`minir_agents.py:43` |
| host 自己已把 reasoning / tool_calls / tool_name 存进 `state.db.messages` | 本机 `state.db` 列清单 |
| `agent_toolsets()` 现在把 `adviser` 授予**每个**插件 agent | `tools.py:129-145` |
| 层 agent 的 loop 不取 `reasoning_content` | `agent_loop.py` 全文无该字段 |

推论一：`minir_hermes_session_search` 应继续读 `state.db`，不另造一套宿主会话检索；
五类角色在 state.db 已齐，JSONL 的 host 流只为缓冲区/层推理服务。

推论二：`messages` 是全对话，直接逐条追加会在每轮重复整段历史。摄取必须做去重（§5）。

推论三：全量落盘不能走 `add()`，否则先被 `_flatten` 和 4000 字符截断。新写入路径
`append_event()` 必须绕开这两处。

## 3. 路径与文件

```
<HERMES_HOME>/minir-memory/sessions/<platform>/<layer>/<session_id>.jsonl
<HERMES_HOME>/minir-memory/sessions/<platform>/<layer>/<session_id>.part2.jsonl
<HERMES_HOME>/minir-memory/run/<pid>-<rand>.lock
```

- `platform`：`initialize(**kwargs)["platform"]`，缺省 `cli`。
- `layer`：`host` 或插件 agent 名（`base` / `dialectic` / `adviser` / `card_review` /
  `extraction` / `minir_agents` 的 `agent_name`）。
- 轮转：2000 行一个 part，part 只增不删。读接口按 `seq` 跨 part 续读。
- 没有 sidecar 文件：缓存区间与去重指纹都写在 JSONL 里（§4 `marker` 行）。
- `.lock`：每进程一个，生命周期内持有独占锁。

## 4. 事件行 schema（v1）

公共字段，每行都有：

```
v        1
seq      流内单调递增整数（platform+layer+session 作用域）
ts       epoch 秒（float）
iso      UTC ISO8601
platform / layer / session_id
turn     宿主回合序号（层 agent 为自己的 run 内轮次）
step     回合内助手工具轮序号（0 起）
role     user | system | assistant | tool | reasoning
kind     msg | aux | run_start | run_end | gap | marker
run_id   插件 agent 的一次运行 id（host 流为空）
redacted 是否过脱敏器（false 表示宿主脱敏器不可用，按原样记录）
```

分角色字段：

| role / kind | 字段 |
| --- | --- |
| user | `text`、`author`、`fp` |
| system | `text`、`chars`、`hash`、`fp` |
| assistant | `text`、`model`、`usage`、`tool_calls[{id,name,args}]`、`fp` |
| tool | `tool_call_id`、`name`、`ok`、`duration_ms`、`result`（全量）、`error`、`fp` |
| reasoning | `text`（按助手消息聚合，不按 delta 逐行）、`model`、`fp` |
| aux | `status`、`started_at`、`ended_at`、`model`、`usage`、`prompt_hash` |
| gap | `dropped`（本流被跳过的行数）、`reason`（`compaction` / `resume` / `error`） |
| run_start/run_end | `run_id`、`input_hash`、`status` |
| marker | `marker`（`last_cache`）、`from_seq`、`to_seq` |

写入规则：

1. 不截断、不 `_flatten`。`_flatten` 只留在 prompt 构造侧（`transcript.py:89`）。
2. 写前过 `_redact_for_egress`；不可用时 `redacted:false` 并照常写。
3. 一行 JSON 后必须有换行；追加前若文件不以 `\n` 结尾先补。
4. 单行可能很大（工具结果全量）。读接口必须逐行流式，禁止 `readlines()`。
5. `fp` 是摄取去重指纹，也是重启后的 `last_seen`，不另存状态文件。

## 5. 摄取规则

`sync_turn` 的 `messages` 是全对话，规则：

1. 每流 `last_seen` = 该流最后一条 `msg` 行的 `fp`。
2. 每次 sync 从尾部向前找 `last_seen`，只追加它之后的切片。
3. 找不到（被压缩/重写）→ 先写一行 `gap{reason:"compaction",dropped:0}`，再追加整段切片，
   并更新 `last_seen`。
4. 进程重启时 `last_seen` 从该流文件尾部的最后一条 `msg` 行恢复，避免 resume 时整段重放。
5. 中断回合不经过 `sync_turn`（`run_agent.py:905`），JSONL 的 host 流接受这一缺口；
   宿主会话的完整真相在 `state.db`。

五类角色的来源：

| role | host 流 | 插件层流 |
| --- | --- | --- |
| user / assistant / tool | `sync_turn(messages)` | `AgentRun.messages` |
| system | `messages` 首条（实现时断言一次是否含 system；不含则取 `pre_llm_call` 的 system，并按 `hash` 去重） | `system_message` 参数 |
| reasoning | `on_stream_delta` 的 delta 按助手消息聚合 | 需在 `agent_loop` 解析 `reasoning_content`（新增） |

## 6. 内存缓存与跨进程

磁盘是唯一共享真相，内存是缓存。每个进程持自己的缓存，互不共享内存。

### 6.1 缓存内容

- 结构：每流一个事件表（与 JSONL 同构的行），不是整段快照。
- 上限：`buffer_events_per_stream`（默认 4000，沿用 `transcript.py:610-614`）；
  流数量 `buffer_streams`（默认 20，即 `_MAX_SESSIONS` 从 8 提到 20，可配置，
  `transcript.py:62`、`138`）。当前会话与调用者所在层永不驱逐。
- 字节上限：`buffer_bytes`（默认 256 MB，可配置）。只按条数封顶挡不住全量工具结果：
  20 流 × 4000 条在长工具输出下会到 GB 级。超预算时按流从最旧开始淘汰，同样记 `evicted_before`。
- 驱逐：丢弃内存里最早的事件时记 `evicted_before = seq`，不写盘。
  读接口用它算 `coverage`，让调用者知道自己看到的是哪一段。
- 现状修正：`_records` 今天不驱逐（`_max_sessions` 只管窗口字典）。
  缓存成为读取源以后，这个无界增长会变成常驻内存成本，必须先补驱逐，再让工具读它。

### 6.2 关闭与重建

- **启动**：创建并锁定 `run/<pid>-<rand>.lock`，再尝试获取其它所有 lock。
  全部可获取 → 本进程是全局关停后的首个进程，进入重建。
- **重建**：每个流从文件尾部反向找最后一个 `marker/last_cache`，把 `[from_seq,to_seq]`
  的事件装入内存。当前会话必载；其余按 `updated_at` 取最近的流，上限 `buffer_streams`。
  marker 缺失或区间有洞时不回放该段，按需从磁盘读。
- **关闭**：flush 自己缓冲的事件 → 为本进程写过的每个流追加一行
  `marker/last_cache{from_seq,to_seq}` → `fsync` → 释放自己的 lock → 探测其它 lock。
  无他人持有即本进程是最后一个，对所有已写流再做一次全量 `fsync`。
- **崩溃**：不保证收尾执行，所以 R6 的「全部落入硬盘」只能作为收尾优化，不是持久性保证。
  行在产生时已追加；丢失上限是一批缓冲。崩后没有 `last_cache` 的流不回放缓存。
- **竞态**：两个进程同时判定「我是首个/最后」都可能发生；重建与收尾都幂等，不加分布式锁。
- **seq 分配**：在该流的文件锁内读最后一行取 `max(seq)` 再自增。

## 7. 读取接口（六词 = 参数）

唯一入口是 TranscriptBuffer 的读方法：memory-first，覆盖不足时从该流自己的 JSONL 补读。
调用方不直接碰文件。

```python
buffer.read_events(platform, layer, session_id, *,
                   cursor=None,             # 增量：seq > cursor
                   last_n=None,             # 滚动：最后 n 条 + dropped_before
                   since=None, until=None,  # 分时间：[since, until)
                   turn=None, step=None,    # 轮次 / 步骤：int 或 (a, b) 半开区间
                   roles=None, kinds=None,
                   limit=None, page=None,   # 全量：分页遍历
                   ) -> {"rows": [...], "next_cursor": int, "dropped_before": int,
                         "coverage": (int, int), "source": "buffer" | "disk" | "mixed",
                         "truncated": bool}
```

- 全量：`limit/page` 分页，跨 part。
- 增量：`cursor` 取上次结果的 `next_cursor`。
- 滚动：`last_n` 反向读，`dropped_before` 说明被排除的最小 seq。
- 时间/步骤/轮次：过滤参数，不改变文件布局。
- `coverage` 与 `source` 必须回显，否则调用者无法区分「没有」和「不在缓存」。

## 8. 工具

### 8.1 `minir_aiagent_search`

- 数据源唯一为 TranscriptBuffer，不直接读文件、不读 `state.db`。
- 只能查调用者自己的层：`layer` 不接受参数，从 contextvar `minir_agent_layer` 取；
  未绑定 → 报错，不猜「全部层」。绑定点在各层 runner 进入 `AgentRun` 时（范式同
  `file_guard.AUX_SCOPE`）。
- 作用域：本会话 / 本层全部会话；不跨 platform。跨会话仍走 `read_events`，
  缓存没命中的部分由 buffer 从磁盘补。
- 形状：`search` / `messages` / `around` / `stats`，参数即 §7 的组合。
- 返回必带 `coverage` / `source` / `dropped_before`。

### 8.2 `minir_hermes_session_search`（改名）

- `adviser_session_search` → `minir_hermes_session_search`，`TOOLSET` 改 `minir`。
- 继续读 `state.db`；新增默认过滤：`sessions.source == 当前 platform`。
- 覆盖：宿主调用者默认 `sessions.source == 当前 platform`，`platform="*"` 显式取消过滤；
  agent 调用者无条件只回当前 host 会话，`platform` 参数被忽略（不是拒绝，避免模型改参数重试）。
- 调用者身份用 `minir_agent_layer` 是否绑定判断：绑定了就是 agent 运行中，没绑定就是宿主。
- 返回信封里回显实际生效的过滤条件；过滤后为空但库里有行时给出提示，
  避免「平台名与 source 不一致」被误读为「没有会话」。

### 8.3 删除 `adviser` 工具集与 `adviser_consult`

`agent_toolsets()` 现在把 `adviser` 授予每个插件 agent（`tools.py:142`）。`adviser_consult`
整体删除后，合并进 `minir` 的唯一新增能力是 `minir_hermes_session_search`，
而它对 agent 调用者已被 §8.2 限死当前 host 会话，权限放大消失。

`adviser_consult` 的删除面（缺一项就留下半死的工具）：

| 位置 | 动作 |
| --- | --- |
| `adviser.py:41` 起的 schema 与 handler | 删 |
| `__init__.py:1519-1524` 的按需注册 | 删；`_adviser_consult_registered` 字段与 `534` 的 gating 一并删 |
| `adviser_prompts.py:34,86,161` | 删「Never call adviser_consult」与「## adviser_consult」段 |
| `agent_loop.py:99` 的工具名列表 | 删 |
| `tools.py:38` | 删 `ADVISER_TOOLSET` |
| 单测与 README 的工具表 | 删 |

不在删除面：`worker.py:651-654` 的 `_adviser_consult`。那是 base/dialectic 在未注入
`complete` 时走内部 consult 的路径，与模型可见工具同名但无关；删掉会让两层失去默认模型路由。

## 9. 提示词接入 transcript 要点

各层取什么由该层决定（R11），但渲染与约束统一。渲染器不懂提示词，提示词不懂文件。

### 9.1 渲染接口

```python
buffer.digest(platform, layer, session_id, *,
              roles=("user", "assistant"), include_tool_results=False,
              tool_result_chars=400, include_reasoning=False,
              max_chars=4000, max_events=200,
              turn=None, step=None, since=None) -> str
```

输出是纯文本行，块内不出现 JSON：

```
[user t3] 把 deployment 的镜像标签改成 sha
[assistant t3 s1] 先看当前 manifest
[tool t3 s1 exec_command ok 1.2s chars=18432] head…tail (elided)
[reasoning t3] … (elided, 3120 chars)
[coverage] seq 1204..1892 in buffer; 3 earlier events of this session read from disk
```

规则：

1. 任何省略都带 `elided` 和原始 `chars`，让模型知道缺了什么、缺多少。
2. 块尾必须有 coverage 行。提示词不得声称看到「完整会话」。
3. `max_chars` 是硬预算，超了从最旧的事件整行丢弃，不截半行。
4. 块名由各 pass 自己定，但必须在 INPUTS 段声明、由 builder 发出。

### 9.2 各层默认投影（可改，理由是用途不同）

| pass | 取 | 不取 | 理由 |
| --- | --- | --- | --- |
| base B1/B2 | user/assistant 文本 + 工具名 | 工具结果 | 卡片写画像，不写代码 |
| base B3 window_summary | 全部角色，工具结果头尾截断 | reasoning | 摘要要覆盖发生了什么 |
| base B4 markers | user/assistant 文本 | 工具结果 | 标记来自陈述，不来自命令输出 |
| dialectic | 工具调用与结果 | reasoning（用户未开启时） | 它审主张与证据 |
| card_review | 与卡片相关的行 + 现有卡片 | 其余 | 校准要原文 |
| extraction | user 文本 | 其余 | 抽的是用户陈述 |
| adviser | 已有 `_event_block`，来源从事件 ring 换成 buffer | — | 重启后仍能看全轮次 |
| minir_agents | 子 agent 自己的流 | 其他层 | 自身历史 |

### 9.3 接入约束

- INPUTS 段声明该块；builder 发同名块；无数据时省略整块，不发空块。prompt 里写「你会收到 X」
  就一定要收到，否则模型按不存在的输入推理。
- 预算只写一个数：块内写字符数，与 `max_chars` 用同一个常量。
- 每层的块名唯一；测试按块名断言，不按正文关键词（正文会随层演进）。
- 现有注入点：`adviser_mode.py:1283`（`long_term_context`）、`:1300`（`transcript_delta`）、
  `:1593`，事件块在 `:568`。`context_engine.py:7` 明说原始文本不进 context engine，
  本设计不改它：digest 由各 pass 的 builder 自己发。其余 pass 的 builder 位置在实现时
  按 §10 逐项落位，不在本稿猜。

## 10. 改动清单

| # | 文件 | 改动 |
| --- | --- | --- |
| 1 | `transcript.py` | 新增 `append_event()`（绕过 `add()` 的 flatten/截断）与 `read_events()`；事件表替换快照；`last_cache` 标记与重建；驱逐补流上限与 `evicted_before`；`read_aux` 走 buffer；删 `prune`、`_trim_locked`、`_write_record`、`_write_atomic`；路径加 `platform/layer` |
| 2 | `transcript_process.py`（新） | lock 生命周期、首/末进程判定、`last_cache` 重建入口 |
| 3 | `__init__.py` | `initialize` 记录 `platform`；`sync_turn` 消费 `messages`（§5）并改掉「Nothing is written to disk」docstring（`673-685`）；`on_session_finalize` 有界 flush；删 prune 调用 |
| 4 | `agent_loop.py` | 解析 `reasoning_content` 进 `AgentRun` |
| 5 | `agent_history.py`（新） | `minir_aiagent_search` 的 schema 与 handler，layer 来自 contextvar |
| 6 | `transcript_prompt.py`（新） | §9.1 的 `digest()` 渲染器与每层预设（纯文本、省略标注、coverage 行） |
| 7 | 各层 prompt 资产与 builder | 每个 pass 在 INPUTS 声明块、builder 发块（§9.2 表；B1-B4 在 `worker.py`/`layers.py`，card_review、extraction、dialectic 在各自模块） |
| 8 | `session_search.py` | `TOOL_NAME`、`TOOLSET`、platform 默认过滤、agent 调用者只回当前会话 |
| 9 | `adviser.py:41`、`__init__.py:289,534,1519-1524`、`adviser_prompts.py:34,86,161`、`agent_loop.py:99` | 删除 `adviser_consult` 工具的全部注册面（§8.3 表） |
| 10 | `adviser_mode.py:55,86,787-789,1404`、`tools.py:38,142`、`handoff.py:149-150`、`adviser.py:42` | 工具集归并，删除 `adviser` 常量 |
| 11 | `plugin.yaml` | `provides_tools`：改名 + 新增 `minir_aiagent_search` |
| 12 | `README.md` | 工具表、toolset 说明、transcript 格式与保留策略 |
| 13 | `doc/minir-todo-draft.md` | §6.1 标注被本稿取代 |
| 14 | `tests/` | `test_transcript_buffer.py`（新）、`test_transcript_process.py`（新）、`test_transcript_prompt.py`（新）、`test_agent_history.py`（新）、`test_adviser.py:265-305`、`test_session_search.py`、`test_adviser_history.py:66,88` 全改 `.jsonl`；删 `adviser_consult` 相关断言 |

## 11. 验收

| 场景 | 断言 |
| --- | --- |
| 一次 host 回合 | 五类角色各至少一行；`seq` 单调；行尾有换行；4000 字符以上的正文完整 |
| 连续两轮 sync | 第二轮不重复第一轮的行（§5 指纹去重） |
| resume 后首轮 | 不重放历史，只追加新尾部 |
| 压缩后 sync | 出现一行 `gap`，随后追加整段，不静默丢 |
| 末行半行 | 读接口跳过残行并计数，不抛异常 |
| 增量读 | `cursor` 之后无重复、无遗漏 |
| 滚动读 | 返回最后 n 条且 `dropped_before` 正确 |
| 跨 part | 读接口能在 part1→part2 连续取行 |
| 全退出后启动 | 只重建 `last_cache` 区间；当前会话必载；流数受上限约束 |
| 缓存驱逐后读 | 早于 `evicted_before` 的请求由磁盘补读，`source` 为 `disk`/`mixed` |
| 两进程 | 后启动者不重建缓存；各进程各写自己的 `last_cache`；重建取每流最新一条 |
| 崩溃（kill -9） | 已追加行可读；无 `last_cache` 的流不回放；下次启动不因残行失败 |
| 层隔离 | `minir_aiagent_search` 无法查别的层；`minir_hermes_session_search` 对 agent 只回当前会话 |
| 平台过滤 | 默认只回当前 platform 的会话；`platform="*"` 才跨平台 |
| 流上限 | 第 21 个流进入时按 LRU 驱逐，当前会话与调用者层仍在 |
| 字节上限 | 总字节超 `buffer_bytes` 时从最旧流开始驱逐，`evicted_before` 随之更新 |
| digest 预算 | 超 `max_chars` 时整行丢弃最旧事件，不截半行；每个省略项带 `elided` 与 `chars` |
| digest coverage | 块尾有 coverage 行；提示词不含「完整会话」类断言 |
| prompt 块 | 每个声明的块都由 builder 发出；该层无数据时整块省略，不发空块 |
| 旧文件 | `.json` 不被读取、不被删除、不被改写 |

## 12. 风险

1. **永久增长**：无 prune，磁盘只涨。`minir_stats` 需增加按 platform/layer 的行数与字节数，
   让用户自己看得见再决定删。
2. **内存增长**：缓存成为读取源后，流数与每流事件数都必须有硬上限。今天的 `_records`
   无上限（`_max_sessions` 不管它），不补驱逐就是常驻内存泄漏。
3. **重复写入**：`messages` 是全对话，`fp` 是唯一防线；指纹算法变更会使旧 `last_seen`
   失效并触发一次整段重放（表现为重复行）。`fp` 带版本号。
4. **大行**：工具结果全量落盘，单行可达数十 MB。流式读，禁止整体加载。
5. **脱敏缺口**：只有 `messages` 通道是宿主脱敏过的；`pre_llm_call` 的 system 与
   `on_stream_delta` 的 reasoning 是脱敏前数据，必须自己过 `_redact_for_egress`，
   否则会把密钥写进永久文件。
6. **平台名漂移**：`initialize(platform=...)` 的取值与 `state.db.sessions.source` 不是同一枚举
   （本机已见 `plugin-agent` / `subagent` / `oneshot` / `local` / `feishu` / `api_server` / `kanban`），
   过滤默认值要以映射表落地，并在返回信封里回显。
7. **删除不彻底**：`adviser_consult` 有 6 个注册面（§8.3）。漏一处就会出现「工具还在名单里但 handler 已删」，
   模型一调就报错；或者提示词继续教模型调用一个不存在的工具。完成条件是
   `grep -rn adviser_consult plugin tests` 只剩 `worker.py` 的内部方法。
8. **提示词预算**：每层多一个块，按每块数百到近千字符计，五个 pass 就是每轮数千字符。
   预算必须写成常量并由 `digest` 硬裁；块内不逐条列工具结果。
9. **两份渲染**：JSONL 是全量，prompt 里是裁剪版。提示词不能把「磁盘上有」当已知；
   省略项必须带 `chars`，否则模型会把省略读成事实缺失。
