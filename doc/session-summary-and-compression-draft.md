# session_summary 自带模板 + 删除 handoff 草稿

状态：已实施（2026-10-23）。代码、测试与文档按默认口径落地：终稿自带模板且不校验标题，压缩摘要由 B3 快速校准最新一次窗口总结。

v4 按评审意见第六次修正两处：终稿不再做 md 标题与顺序校验（模板只是提示，不是门）；压缩摘要不是会话起点到压缩时刻，而是用 B3 快速校准最新一次窗口总结。

范围：`session_summary` 的输出形态；压缩链路里摘要由谁产生；agent 输出自检工具。

非目标：不改 B3 的窗口摘要模板与上限（`templates/window_summary.md`）；不改 `knowledge_render` 的页面契约；不改 recall 打分与结论种类集合。

读法：`handoff` 指 `handoff.py` 这个 pass，它用一套自定义提示把 base job 的窗口摘要再校准一遍。删的是这套提示与标题校验，压缩期那次模型调用改由 B3 承担。

## 0. 三句话

- 终稿拥有自己的模板 `plugin/minir/templates/session_summary.md`，节按"输入能给什么"定：Request / Outcome / Decisions / Constraints / Verified Facts / Lessons / Open Threads / References。模板只进提示，不做标题与顺序校验：模型少写一节照存。
- 压缩用 B3 快速校准最新一次窗口总结：输入是上一次 base 刷新的窗口总结加之后的 turns，输出折叠进那条 assistant 消息。不读 chunk，不拼链，coverage 不是会话起点。
- 新增 `minir_content_valid`：`string` 模式量字符数，`token` 模式量 token 总量，让 agent 在返回前用本地确定性计数验收自己的输出。

## 1. 术语

| 词 | 含义 | 所有者 |
| --- | --- | --- |
| 窗口摘要 window summary | B3 写的滚动任务状态，八节 markdown | `templates/window_summary.md`、`window_summary.py` |
| chunk | base job 里 B3 为单个窗口写的摘要正文（去掉首行 `## Window Summary`），供 base 产物与召回 | `conclusions` 里 `kind=window_summary`，键 `chunk:<sid>:<from_ts>` |
| 压缩摘要 | 压缩时 B3 校准出的最新一次窗口总结，折叠进窗口的那条 assistant 消息 | 新增的 `compress_pass.py` |
| 会话终稿 session record | 会话闲置 24h 后写的那条最终记录 | `conclusions` 里 `kind=session_summary`，键 `session_summary:<sid>:final` |
| handoff | 压缩期用自定义提示二次校准的 pass；本次删除 | `handoff.py`（删除） |

## 2. 现状→目标

| # | 关注点 | 现状 | 目标 | 锚点 |
| --- | --- | --- | --- | --- |
| 1 | 终稿形态来源 | `HEADINGS = window_summary.FIELD_HEADINGS` | 自己读 `templates/session_summary.md`，只作提示 | `session_summary.py:31` |
| 2 | 形态是否贴合输入 | 八节照抄窗口摘要，含 Doing 与 Todo | 按输入字段定节（§3.2 映射表） | `agents/session_summary.md:22` |
| 3 | 模板是否进 prompt | 只有 core 加 `_OUTPUT_RULE`，模板从不出现 | `TEMPLATE:` 段把模板原文注入 system prompt | `session_summary.py:41` |
| 4 | 校验 | 引用窗口八标题，校验存在性与顺序 | 去掉标题与顺序校验，只做清洗与截断 | `session_summary.py:45` |
| 5 | 压缩的第一个模型调用 | base job 的 B3 为当前窗口写窗口摘要 | 不变；它服务 base 产物 | `__init__.py:1067` |
| 6 | 压缩的第二个模型调用 | handoff 用自定义提示校准 chunk | B3 快速校准最新一次窗口总结 | `context_engine.py:258` |
| 7 | 折叠内容 | 最新一块 chunk（经 handoff 改写） | 压缩期 B3 的校准结果 | `context_engine.py:363` |
| 8 | 收尾第一输入 | `_session_handoff` 先跑 handoff | 最新压缩记录；缺则最新 base 窗口摘要 | `__init__.py:1295` |
| 9 | 抽取输入 | `extract_pages(summary, handoff)` 两块 | 只收 summary 一块 | `extraction.py:162` |
| 10 | 输出自检 | 无，靠模型自估长度 | `minir_content_valid` 量字符或 token | `tools.py:51` |
| 11 | 配置面 | `handoff_enabled` / `MINIR_HANDOFF_ENABLED` | 删除；压缩由 B3 承担 | `__init__.py:509` |

## 3. 设计 A：终稿自己的模板

### 3.1 形态

资产 `plugin/minir/templates/session_summary.md`，只是提示里的形状建议：

```markdown
## Session Summary

### Request

### Outcome

### Decisions

### Constraints

### Verified Facts

### Lessons

### Open Threads

### References
```

### 3.2 每节对着一个输入

| 节 | 内容 | 输入来源 |
| --- | --- | --- |
| Request | 这次会话要做什么，一条一行 | 会话开头与 markers 里的 title/summary |
| Outcome | 每个请求的落地结果，done/partial/dropped 各带一行结果 | SESSION RECORD 的 Current State 与检索到的执行记录 |
| Decisions | 做过的选择与理由 | SESSION RECORD 的 Decisions、markers 里的 notes |
| Constraints | 会话确立的硬约束 | SESSION RECORD 的 Constraints、notes |
| Verified Facts | 有证据的结论，每条带证据指针 | SESSION RECORD 的 Verified Facts、工具结果 |
| Lessons | 可复用的做法与坑 | markers 里 experiences 与 tools 两类 |
| Open Threads | 未完成的事与下一步 | SESSION RECORD 的 Open Issues 与 Next Actions |
| References | 路径、命令、错误、id、url | 工具结果与 notes |

usage 不进节：turns 与 tokens 已由 `record_session_usage` 单独存一份（`__init__.py:1192`），终稿里再写一遍只占预算。

为什么否掉窗口八节：Current State 的 Done/Doing/Todo 是活窗口的方向感，会话结束后 Doing 不可能有值，Todo 与 Next Actions 重复；终稿服务的是抽取与后续召回（成页维度是 entities/experiences/tools/projects/trajectories），要覆盖"要做什么、做成了什么、决定了什么、学到什么、还剩什么"。

### 3.3 提示、预算与清洗

- `session_summary.TEMPLATE_PATH`、`load_template()`、`SessionTemplateError`。缺资产或不可读＝装配失败（坏的资产是装配问题，不是模型格式问题）。
- `system_prompt()`：`layers.load_core('session_summary')` 加 `TEMPLATE:` 段（模板原文）加 `OUTPUT` 规则。`_OUTPUT_RULE` 只说"按 TEMPLATE 的节与顺序写，无前言、无收尾、无围栏"。
- 不解析标题、不校验顺序、不校验节数：模板是提示，不是门。模型少写一节、换序、加一节，记录照存。
- `normalize()` 只做清洗：剥 think 与代码围栏、去首尾空白、按 `MAX_CHARS` 截断；除截断外不丢内容，形状不符也不清空。
- 预算：总量 6000 字符（与 `MAX_CHARS` 同一个数）；Request 与 Outcome 是清单，不设单独上限；其余六节各不超过 800 字符。总量由 agent 调 `minir_content_valid(mode="string", limit=6000)` 自检（§7），单节上限写在模板里。

## 4. 设计 B：删除 handoff

`handoff.py` 整体删除：`load_template`、`_RULES`、`_SUMMARY_AGENT_RULES`、`normalize`、`summarize_agent`。压缩期那次模型调用保留，改由 B3 承担（§5）。

- `run_with_timeout` 移到 `agent_loop.run_with_timeout`：剩余两个调用方（`session_summary.py:100`、`extraction.py:216`）本来就 import `agent_loop`；ContextVar 复制语义与两例测试一起搬，不新开模块。
- `context_engine.py`：删 `handoff` import（`context_engine.py:26`）、`_summary_enabled`（`context_engine.py:147`）、`on_session_start` 里的重算（`context_engine.py:162`）、`compress()` 的 handoff 分支与 `expire_pending_handoffs` 调用（`context_engine.py:258`、`context_engine.py:267`）；`cadence` 只剩 `store_path` 与压缩闸门 `configured_reasoning`。
- `cadence.py`：删 `configured_handoff`（`cadence.py:20`）；保留并简化 `configured_reasoning`，它现在是压缩期 B3 的闸门。`reasoning_enabled: false` 的既有契约是"无任何模型调用"（`__init__.py:316-318` 与 schema 描述），删掉闸门就会让该配置在压缩边界照样产生模型调用，因此这条不按删处理。
- `__init__.py`：删 `handoff` import（`__init__.py:66`）、schema 的 `handoff_enabled`（`__init__.py:509`）、`_session_handoff`（`__init__.py:1295`）。
- `__init__._run_session_extraction`：`session_record = 最新压缩记录（kind=session_summary）或 latest_base_refresh 的 window_summary`；`summary_text = session_summary.summarize_agent(session_id, session_record=..., markers=..., usage=..., window_end=...)`；`summary_text` 为空时用 `session_record` 兜底；`extract_pages` 不再收 handoff。
- `extraction.py`：`build_prompt` 与 `extract_pages` 去掉 handoff 参数（`extraction.py:162`、`extraction.py:193`）与 `session_handoff` 块（`extraction.py:169`）；模块 docstring 改成"终稿是第一个输入"。
- `prompting.py`：`RESERVED_LABELS` 删 `session_handoff`（`prompting.py:41`），加 `session_record`；`session_summary` 保留。
- `store.py`：删 `expire_pending_handoffs`（`store.py:1078`）；旧库里的 pending `handoff` 行改在 `_repair_rows`（`store.py:561`）里按 `kind='handoff' AND status='pending'` 置 `expired`，随每次 open 幂等执行。
- `session_summary.TASK_KEY`（`session_summary.py:29`）无读者，一并删除。

## 5. 设计 C：压缩用 B3 快速校准最新一次窗口总结

压缩路径上本来就有一次 B3：`on_pre_compress` 记 BASE_LAYER job 后 `drain_session`（`__init__.py:1067`），B3 为这个窗口写窗口摘要，服务 base 产物。压缩摘要不读那份 chunk，另调 B3 一次：

- coverage 是**最新一次窗口**：上一次 base 刷新的 `window_to_ts` 到压缩时刻。不是会话起点。
- `<previous_summary>` 是上一次 base 刷新的窗口总结（`base_refreshes.window_summary`），它就是 B3 的 MERGE 基线，所以校准结果的覆盖范围仍递推到会话前面；窗口内新增 turns 若为空（drain 刚跑过），这次就是把上一份总结按模板与 caps 重写一遍。
- `<window_summary_transcript>`：该窗口的 turns，`history.read_messages(state_db, session, from_ts, to_ts)`（`history.py:113`）经 `transcript_prompt.format_row`（`transcript_prompt.py:108`）渲染；没有新 turns 时留空块。
- `<user_card>` / `<ai_card>`：最新 `base_refreshes` 行的卡字段（`store.py:150`），与 base 流程同一份卡。
- `<window_template>`：`templates/window_summary.md`。
- 调用：system 由 `layers.load_core("b3_window_summary")`（`layers.py:86`）加模板加 B3 输出契约组成；执行走 `agent_loop.run`（`agent_loop.py:259`），`layer="window_summary"`（该函数没有 `agent` 参数，`layer` 才是运行与 transcript 标签），不给工具，超时 90 秒。
- 输出只做清洗：剥 think 与围栏、去首尾空白、按 `_MAX_SUMMARY_CHARS` 截断（`context_engine.py:264`）。不校验标题与顺序，不跑 `validate_summary_pass`：少一节的总结照折叠。
- 折叠：`[minir summary #<run_id>]` 加清洗后的正文；消息格式、notice、压缩 gate、head/tail 过滤都不动。
- 闸门：`reasoning_enabled` 为 false 时不调 B3，直接折叠 `previous`，没有则空模板加 notice。
- 失败：用 `<previous_summary>`，没有则空模板，并保留 notice。
- 为什么不读 chunk：chunk 是 base 层的落库中间态（`kind=window_summary`，键带窗口起点，`store.py:994`），供 base 产物与召回；压缩摘要属于上下文层，要的是此刻由 B3 校准出的最新窗口总结，不经过这行存储。
- 记录：压缩摘要写一行 `session_summary:<n>`（`store.next_session_summary_index`，`store.py:930`），见 §10 F2；终稿的第一输入直接取它。

## 6. 设计 D：收尾链

- `_session_record(session)` = 最新非 `:final` 的 `session_summary` 行，缺则 `latest_base_refresh(session)` 的 `window_summary`（`__init__.py:1300` 现在取的就是它）。
- `session_summary.build_prompt`：`SESSION RECORD (the latest compression summary; derived data)` 加 `data_block("session_record", ...)`；`SESSION`、`WINDOW END`、markers、usage、检索指令都保留。
- `agents/session_summary.md`：ROLE 与 EVIDENCE 去掉 handoff 来源，SHAPE 改成"照 `templates/session_summary.md` 输出"，预算改成 §3.3 的口径，并加上返回前自检的指令（§7.4）。
- `agents/knowledge_render.md`：输入列表去掉 `<session_handoff>`，页面预算改成调 `minir_content_valid` 自检（§7.4）。

## 7. 设计 E：minir_content_valid

### 7.1 用途

agent 拿不到自己的 tokenizer，也不该靠"感觉长度"裁内容。这个工具把两个计数做成一次本地调用，让 agent 在返回前验收：量出来超了就删，删完再量一次。

### 7.2 契约

| 项 | 内容 |
| --- | --- |
| 名字 | `minir_content_valid` |
| `content` | string，必填；待验收的正文 |
| `limit` | integer，必填，至少 1 |
| `mode` | `string`（默认）或 `token` |
| 返回 | JSON：`ok`、`mode`、`unit`、`measured`、`limit`、`over`、`remaining`；token 模式多一个 `estimator` |

- `string`：`measured = len(content)`，按 Unicode 码点计；`unit = "chars"`。
- `token`：`measured = rules.estimate_tokens(content)`（`rules.py:29`：CJK 一字一 token，其余四字符一 token，乘 1.1 向上取整），`unit = "tokens"`，`estimator = "rules.estimate_tokens"`。这不是厂商 tokenizer，量级用于预算把关，和页面预算用的是同一个估算器（`knowledge.py:562`）。
- `ok = measured <= limit`；`over = max(0, measured - limit)`；`remaining = max(0, limit - measured)`。
- 只报数：不截断、不改写、不落库、不调模型。空 `content` 合法（`measured = 0`）。
- 错误用 `tool_error`：`mode` 非法、`limit` 缺失或小于 1、`content` 缺失。

### 7.3 两条投放路径，一份实现

实现放 `plugin/minir/content_valid.py`：`measure(content, mode) -> int` 与 `handle(args) -> str`。它不依赖 store，所以两个入口都要在 store 检查之前接上：

- `plugin/minir/tools.py`：加 `_TOOL_CONTENT_VALID` schema，进 `TOOL_NAMES`（`tools.py:51`）、`SCHEMAS`、`_EMOJI`；`register()` 里用直连 handler，不走 `_dispatch`（`tools.py:263` 的 `_dispatch` 要求 `provider._store` 非空）。
- `plugin/minir/__init__.py:handle_tool_call`（`__init__.py:1520`）：在 `self._store is None` 的检查之前分派到 `content_valid.handle`。
- `plugin/minir/plugin.yaml`：`provides_tools`（`plugin.yaml:31`）加名字，否则 `hermes plugins validate` 报未声明工具。
- `README.md` 工具表加一行。

### 7.4 谁调它

- `agents/session_summary.md`：返回前 `minir_content_valid(mode="string", limit=6000, content=<draft>)`；`ok` 为 false 时按 `over` 删最弱的行，再量一次。
- `agents/knowledge_render.md`：每页 `mode="string", limit=2000`；需要按 token 预算收口时用 `mode="token"` 加对应的 token 限额。
- 主 agent 同样可用：`get_tool_schemas` 返回的就是 `SCHEMAS`（`__init__.py:1516`），memory 工具面自动带上。

## 8. 改动清单

| 文件 | 动作 | 说明 |
| --- | --- | --- |
| `plugin/minir/handoff.py` | 删除 | 自定义提示与标题校验退休 |
| `plugin/minir/compress_pass.py` | 新增 | 压缩期 B3 的装配、调用与清洗 |
| `plugin/minir/agent_loop.py` | 修改 | 收下 `run_with_timeout` |
| `plugin/minir/session_summary.py` | 修改 | 自带模板，删标题校验与 handoff 输入 |
| `plugin/minir/templates/session_summary.md` | 新增 | 终稿形态提示 |
| `plugin/minir/content_valid.py` | 新增 | 两个计数模式，一份实现 |
| `plugin/minir/tools.py` | 修改 | 新 schema、注册、直连 handler |
| `plugin/minir/agents/session_summary.md` | 修改 | 形态引用模板，去 handoff 与校验措辞，加自检指令 |
| `plugin/minir/agents/knowledge_render.md` | 修改 | 去掉 `<session_handoff>`，加自检指令 |
| `plugin/minir/context_engine.py` | 修改 | 折叠改调压缩期 B3，删 handoff 分支 |
| `plugin/minir/cadence.py` | 修改 | 删 `configured_handoff`；保留 `configured_reasoning` 作压缩闸门 |
| `plugin/minir/__init__.py` | 修改 | 删 import、schema 键、`_session_handoff`；收尾改 session_record；工具分派提前 |
| `plugin/minir/extraction.py` | 修改 | 去掉 handoff 参数与数据块 |
| `plugin/minir/prompting.py` | 修改 | 保留标签换名 |
| `plugin/minir/store.py` | 修改 | 删 `expire_pending_handoffs`，legacy 清理进 `_repair_rows` |
| `plugin/minir/plugin.yaml` | 修改 | 版本 0.16.0 → 0.17.0，`provides_tools` 加名字 |
| `README.md` | 修改 | 模块表、压缩段、配置段、环境变量列表、recall 段、工具表 |
| `pyproject.toml` | 修改 | 注释里的 `handoff.py` |
| `scripts/verify_against_host.py` | 修改 | 去掉 `handoff_enabled` 与 `MINIR_HANDOFF_ENABLED` |
| `doc/layer-terminology-draft.md` | 修改 | 退休 Task Handoff 三条（kind 列表、术语行、对照行） |
| `doc/window-lifecycle-and-ai-self-draft.md` | 修改 | 第 184、255 行改述压缩期 B3 |
| `doc/layer-agent-contracts-draft.md` | 修改 | 第 814 行"8 字段"改为引用终稿模板 |
| `doc/session-todo-draft.md` | 修改 | 第 55、56 行同步 |
| `doc/conclusions-only-draft.md` | 修改 | 第 300 行 legacy 清理的新位置 |

## 9. 测试

| 用例 | 断言 | 文件 |
| --- | --- | --- |
| 模板进 prompt | `system_message` 含 `## Session Summary` 与全部 `###` 标题 | `tests/test_session_finalisation.py` |
| 缺资产 | 抛 `SessionTemplateError`，不是返回空串 | `tests/test_session_finalisation.py` |
| 少节照存 | 回包缺 `### Lessons` 时 `normalize` 原样保留其余内容，不返回空串 | `tests/test_session_finalisation.py` |
| 换序照存 | 回包把 `### References` 提前，内容仍完整保留 | `tests/test_session_finalisation.py` |
| 清洗与截断 | 围栏与 think 被剥掉；超 6000 字符按 `MAX_CHARS` 截断 | `tests/test_session_finalisation.py` |
| 压缩调 B3 | 断言传给 `agent_loop.run` 的 system 含 B3 core 与窗口模板，`layer` 为 `window_summary` | `tests/test_context_engine.py` |
| 窗口起点 | digest 的 `from_ts` 是上一次 base 刷新的 `window_to_ts`，不是会话起点 | `tests/test_context_engine.py` |
| previous 是窗口总结 | `<previous_summary>` 等于最新 `base_refreshes.window_summary` | `tests/test_context_engine.py` |
| 不读 chunk | store 没有 chunk 行时压缩仍产出压缩摘要 | `tests/test_context_engine.py` |
| 不校验标题 | B3 回包少一节时仍折叠，消息带标记且非空 | `tests/test_context_engine.py` |
| 失败回退 | B3 抛错时用 `previous`，再没有则空模板加 notice | `tests/test_context_engine.py` |
| 推理关闭 | `reasoning_enabled=false` 时不调 B3，折叠 `previous` | `tests/test_context_engine.py` |
| 落库 | 默认写一行 `session_summary:<n>`，content 等于折叠文本 | `tests/test_context_engine.py` |
| 收尾输入 | `seen["session_record"]` 等于最新压缩记录；没有记录时回退 base 窗口摘要 | `tests/test_session_finalisation.py` |
| string 计数 | ASCII 10 字符为 10，CJK 5 字符为 5；`measured == limit` 时 ok 为 true | `tests/test_content_valid.py` |
| token 计数 | 100 个 ASCII 字符为 28、10 个 CJK 为 11；与 `estimate_tokens` 逐字相等 | `tests/test_content_valid.py` |
| 参数错误 | 非法 mode、limit 为 0、缺 content 各返回 tool_error；空 content 返回 0 且 ok | `tests/test_content_valid.py` |
| 无 store 可用 | provider 的 `_store` 为 None 时 `minir_content_valid` 仍返回结果 | `tests/test_content_valid.py` |
| 注册面 | `TOOL_NAMES`、`SCHEMAS`、`provides_tools` 三处都含该名字 | `tests/test_content_valid.py` |
| legacy 清理 | pending `handoff` 行 reopen 后变 `expired` | `tests/test_store.py` |
| 线程语义 | 两例搬到 `agent_loop.run_with_timeout` | `tests/test_agent_loop.py` |
| 删除 | `tests/test_handoff.py` 整个文件移除 | `tests/test_handoff.py` |
| 全量 | `python -m pytest -q` 通过 | 仓库根 |

## 10. 未决（每条给默认口径）

### F1 handoff 提示词留不留

- 默认：删。压缩摘要由 B3 承担，`handoff.py` 的自定义 `_RULES` 与 `normalize` 没有第二个使用者。
- 备选：保留 handoff 作为压缩摘要的专用提示（即现状），但它与 B3 是两套形状契约，不推荐。
- 我按默认写。

### F2 压缩摘要是否落库

- 默认：写一行 `session_summary:<n>`（`store.next_session_summary_index`）。它不是 chunk 的副本，而是压缩摘要；终稿与召回都要读它。
- 备选：不写，只留在折叠消息里；终稿改读最新 base 窗口摘要。选它则删 `next_session_summary_index` 与 §9 的落库用例。
- 我按默认写。

## 11. 风险

- drain 刚跑过时窗口内没有新 turns，这次 B3 等于把上一份窗口总结按模板与 caps 重写一遍；如果模型只做同义改写，压缩摘要与 base 摘要会几乎相同。这不是 bug，是"快速校准"的定义，但收益有限。
- 压缩期 B3 与 base job 的 B3 是两次独立调用（同一份 `agents/b3_window_summary.md`）；改提示要同时过两条路，测试各一条。
- 输出不做标题校验，模型可能漏节或换序；读取方（抽取、召回）按文本处理，不按标题解析，所以能接受。
- B3 失败时回退 `previous`，仍失败则空模板加 notice；原文仍在 host 的 state.db（`minir_hermes_session_search`）与 store 里可检索。
- `token` 模式是估算，不是厂商 tokenizer；用于预算把关，不用于和 API 用量对账。
- 终稿节集合换成新八节后，`_session_marker_snapshot` 的 `summary` 字段（`__init__.py:1232`）在新库上来自压缩记录，老库上可能为空；不影响终稿与抽取。
- 旧库的 `handoff` 行：pending 变 expired，active 行保持原样；没有读者，不做删除。
