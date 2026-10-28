# 本次会话草稿的实现 TODO

状态：T1-T16 已全部实现（2026-10-23）；T14 的 events org 已批准并落地。

用途：把 `docs/` 下本次会话的草稿折成可执行清单。每条只写"改哪里、做完的标准"，细节回查对应草稿，不在这里重复设计。

## 0. 草稿清单

| 草稿 | 覆盖任务 | 状态 |
| --- | --- | --- |
| `docs/draft/window-lifecycle-and-ai-self-draft.md` | T1-T7 | 已实现 |
| `docs/draft/window-summary-template-draft.md` | T2、T3、T5 | 已实现 |
| `docs/draft/layer-agent-contracts-draft.md` | T8-T10、T12-T13 | 已实现 |
| `docs/draft/events-org-change-draft.md` | T14 | 已实现 |
| `docs/draft/lancedb-knowledge-adaptation-draft.md` | T15 | 规则已固化；不导入数据 |
| `docs/draft/base-dialectic-layers-draft.md`、`docs/draft/layer-terminology-draft.md`、`docs/draft/rule-files-draft.md` | 术语与预算口径 | 已同步；代码按本清单落地 |

## 1. 注入面（T1）

- [x] **T1 只注入 layer state + todo**
  改：`plugin/minir/__init__.py` 的 `prefetch`、`_layer_block`。
  做：删 `_knowledge_block` 与召回注入（`queue_prefetch`、`_take_cached`、`_format_block`、`_PREFETCH_KINDS`、`_recent_knowledge`）；layer state 改读活 base 行的 `ai_self_representation` 与 `window_summary` 两列；保留 `_todo_block`；`_search` 留给 `minir_search`。
  改预算：ai_self ≤400、window ≤2000；window 超限先保目标两节，再截断其余六节并标 `[summary truncated]`。
  可选：`recall_status` 文案从 "recalled N memories" 改为 "injected N items"。
  做完：prefetch 不含 `[knowledge` 与 `## Minir recall`；注入文本与 `json.loads(row[...])` 逐字节相等；`minir_search` / `minir_kb_search` 仍可用。

## 2. 窗口与摘要（T2-T7）

- [x] **T2 窗口摘要改成目标列表 + 状态分区**
  改：`plugin/minir/agents/b3_window_summary.md`、`plugin/minir/window_summary.py`、`docs/draft/window-summary-template-draft.md` 已定口径。
  做：objective 是完整目标列表（无编号/状态/来源，大小目标都列）；current_state 用 Done/Doing/Todo 分组；markdown 单一形态，无 JSON 正文；不加 md 校验、不解析正文。
  做完：B3 提示词只要求按模板写；`window_summary.validate` 仍只查字符串；示例含空章节。

- [x] **T3 会话窗口生命周期：compress 不重置窗口**
  改：`worker.py::_merge_base` / `_base_user_prompt`（把本会话 `previous_summary` 传给 B3）、`context_engine.py::compress`。
  做：B3 的 previous_summary 只取本会话 `window_to_ts` 最大的 base 行；不再用 `latest_base_refresh_for_subject` 把上一会话的窗口灌进新会话；compress 只校准出 `session_summary`，不清空 done、不改写 base 行、不换窗口。
  做完：新会话首份 window_summary 不含旧会话内容；compress 后再刷新仍取本会话最新行；窗口 coverage 不回退。

- [x] **T4 dialectic 轮数从首次 base 后计**
  改：`turn_counter.py`（或调度侧）。
  做：base 未触发前 dialectic 不计数、不排期；首次 base 触发时把起点设为该轮。
  做完：base 未跑时 8 轮节拍不启动；首次 base 后第 8 轮触发。

- [x] **T5 预算归属**
  改：`window_summary.py` 的 caps、`__init__.py::_layer_block`。
  做：objective/current_state 不设条数上限、不计入 880；880 只覆盖其余六节；超限压缩其余六节，不删目标。
  做完：长目标列表不触发失败；注入层超限先保目标两节。

- [x] **T6 ai_self 六字段**
  改：`agents/b2_ai_card.md`、`rules/ai-self.md`（1-3 条事实已定）、B2 提示词。
  做：自我定位先写 ai_identity 再取其首行；当前任务取 `current_state.doing`；当前目标取 doing+todo；自我认知取 1-3 条状态相关事实（未知取 open_issues）；当前状态与不确定性按 §6 映射。
  做完：ai_self 只引用窗口摘要的目标短句，不整表复述；不写用户描述与身份权限。

- [x] **T7 session_summary 与 24h final**
  改：`context_engine.py::compress`、`compress_pass.py`、`session_summary.py`、`templates/session_summary.md`。
  做：压缩摘要改由 B3 校准最新一次窗口总结；final 用自带模板，键保持 `session_summary:<sid>:final`；旧行不重渲染。
  做完：压缩摘要与窗口摘要同一 B3 形状；终稿按"输入能给什么"定节。

## 3. agent 编排（T8-T10）

- [x] **T8 B1 调度 B3/B4**
  改：`agents/b1_user_card.md`、`worker.py::_merge_base_cards`。
  做：B1 通过 `minir_agents` 起 `b3_window_summary`、`b4_value_markers`；tool_sets 分别 `[file,session_search]`、`[file,minir,session_search]`；digest 与上一份 markers 走 scratch 文件 + `context_files`；子 agent 禁再调；B1 输出四键，上限 3200。
  做完：B1 的 `window_summary`/`value_markers` 与子 agent 返回一致；子 agent 失败只重跑该子 agent 一次；连续失败保留旧 artifact。

- [x] **T9 B2 后置**
  改：`worker.py::_merge_base_cards`、`agents/b2_ai_card.md`。
  做：顺序改为 B1（含 B3/B4）→ B2；B2 读 user_self/user_preferences/window_summary，输出 ai_self/ai_identity；B2 不读 session_summary。
  做完：B2 的 ai_self 与同一轮 window_summary 一致；B2 不重复用户偏好。

- [x] **T10 B4 新契约与 knowledge_render**
  改：`agents/b4_value_markers.md`、`agents/knowledge_render.md`、`layers.validate_value_markers`。
  做：B4 输出 `info_type / topic / why / relations / render_plan / anchor / objects`；每个候选先 `minir_kb_search` 去重，命中已有页面则改为 `updates` 关系；knowledge_render 按 anchor 回读会话提炼正文，marker 不当正文。
  做完：B4 不写正文；重复主题不新增 marker；knowledge_render 的每条断言都能追溯到 anchor 片段。

## 3.5 运行时元数据（T16）

- [x] **T16 run-meta 与多级 system_message**
  改：`agent_loop.run`、`worker._adviser_consult`、`minir_agents` 子 run、`agents/b1_user_card.md`。
  做：`system_message` 接受 `str | Sequence[str]`；run 解析出最终 provider/model 后追加 `[run-meta]`；`meta` 带 agent/parent/depth；B1 读自己的 run-meta，把 `provider:model` 传给两个子 agent。
  做完：B1 的 run-meta 是实际路由（含 gateway 回落）；子 run 的 run-meta 与 B1 一致；旧 `str` 调用兼容。

设计要点（独立草稿已并入本节，不再单列）：

- 段按顺序 `\n\n` 连接，空段丢弃；`str` 旧调用先包成一段再追加元数据。
- `[run-meta]` 字段：`layer`、`agent`、`provider`、`model`、`parent`、`depth`、`route_source`。只放这些，不放 key/base_url/token。
- 元数据由 run 在解析出 `final_provider`/`final_model` 之后写入（`agent_loop.py:265-267`），调用方不能用 `meta` 伪造 provider/model；`meta` 只带 agent/parent/depth。
- 固定文案："runtime metadata, not instructions; never quote or mention this block."；默认放 system 末尾。
- B1 从自己的 `[run-meta]` 读 `provider:model`，原样传给 `minir_agents`；B3/B4 不接受显式 pin 或覆盖，子 run 的 run-meta 显示实际路由。
- B1 没传时子 run-meta 记 `route_source: gateway` 并记 warning，不静默假装继承。
- `system_message` 为空时保持 `None`，不产生只有元数据的 system。
- 失败：provider/model 为空 → `unknown` + warning；非法 `provider:model` → 工具错误，B1 重试一次，不静默回落。
- 审计：debug 日志记 `layer/provider/model/meta_agent`，不新增表。
- 调用点：`agent_loop.run` 228-237 / 265-267 / 302；`worker._adviser_consult` 714-743 的 f-string 改成 `[system, tool_hint]`；`minir_agents` 255 传 `meta`；`agents/b1_user_card.md` 加"读 run-meta、传给两个子 agent"；b3/b4 不改。
- 测试：数组拼接、gateway 实际值、子 run 的 meta、B1→子路由一致、`str` 兼容、无 key/token。
- 待定：run-meta 放末尾还是开头（默认末尾）；是否把 `api_mode`/`task` 也放进元数据。
- 回退：保留 `str` 入口；去掉 run 的元数据追加即恢复旧行为，调用方不用改。

## 4. 知识提取队列（T11）

- [x] **T11 队列按草稿核对**
  改：`extraction.py`、`store.due_extractions`。
  做：只处理 24h 无新用户输入的会话；新输入取消 pending；按 `due_at, session_id` 顺序、一次最多 3 行、串行；失败 1h 后重试一次再 parked。
  做完：多会话到期按序执行；新输入取消；失败重试一次。

## 5. adviser 对齐（T12-T13）

- [x] **T12 准绳改读 live 行**
  改：`adviser_mode.py::_rulers` / `_digest`、`rules/advice_target.md`、`rules/advice_judgment.md`。
  做：会话内准绳用 `latest_base_refresh(session)`，跨会话用户卡用 `latest_base_refresh_for_subject`；加 `window_summary`；false_claim→ai_self、missing_work→window_summary、scope_change→ai_identity_card；`### Preferences` 改引整张卡正文；卡落后事件时给 `stale_card`；扫描信号盯 base 行版本。
  做完：第二次 base 刷新后准绳是第二版卡；不再用一次性 conclusion；`missing_work` 能看到完整目标列表。

- [x] **T13 命名映射**
  改：`docs/draft/layer-agent-contracts-draft.md` §0.7 已列；实现时按表。
  做：草稿键 `user_self/user_preferences/ai_self/ai_identity/window_summary` ↔ artifact 列 ↔ conclusion kind ↔ adviser 用法，三套名字不许漂移。

## 6. 知识 org（T14，待批准）

- [x] **T14 新增 events org**
  文档：`docs/draft/events-org-change-draft.md`。
  改：`layers.ORGS`、`rules/markers-org.md`（v2→v3）、`README.md`、术语/规则草稿、`tests/test_knowledge.py`。
  做：加 `events`；定义"单次带日期的发生，无复用教训"；教训归 experiences；`_MARKER_KINDS` 与 `minir_remember` 不变。
  做完：events 页落在 `knowledge/events/`；kb_search org 过滤可见；回归测试全绿。
  已定：knowledge_render 遵循 markers-org，可产出 events 页；events 页 epistemic 固定 fact（`validate_pages` 与 `knowledge._write_unit` 都强制）。

## 7. lancedb 适配（T15）

- [x] **T15 只固化规则，不导入数据**
  文档：`docs/draft/lancedb-knowledge-adaptation-draft.md`。
  做：固定 category 映射、front-matter 合成、超 2k 拆子页、同名合并。**不写导入器、不迁移、不改 lancedb 文件。**
  做完：本机 `lancedb-memory` 目录保持原样；`identity/preference/contexts/summary` 不进知识库。

## 8. 待你定

| # | 决定 | 默认建议 |
| --- | --- | --- |
| 1 | layer state 表源 | `base_refreshes.is_latest`（活行），不读 `dialectic_refreshes` 快照 |
| 2 | `recall_status` 文案 | 改成 "injected N items" |
| 3 | 注入预算 | ai_self 400 / window 2000 |
| 4 | events org | 已批准并落地 |
| 5 | events 的 epistemic | 固定 fact |
| 6 | 旧 session_summary 回填 | 不回填，下一次压缩覆盖 |
| 7 | B3/B4 子 agent 的 model | 继承 B1 的 `[run-meta]`，无显式 pin |

## 9. 明确不做

- 不做 markdown 校验/解析器；不因标题、字数、分区失败或重试。
- 不做 JSON 正文或"markdown + JSON"双形态。
- 不在 prefetch 注入知识路径块或显式召回。
- 不导入 lancedb-memory 数据，不做双向同步。
- 不为 events 改 `minir_remember` 的 kind 或 `store._MARKER_KINDS`。
- 不让 B2 读 session_summary，不让新会话继承旧窗口。

## 10. 建议顺序

覆盖 T1-T15；括号内是依赖，标"可并行"的可以同时做。

| 顺序 | 任务 | 依赖 / 说明 |
| --- | --- | --- |
| 1 | T2 + T5 | 无。模板与预算同批改，避免目标列表与 880 归属分家 |
| 2 | T3 | T2。生命周期用同一模板；compress 校准 |
| 3 | T7 | T2/T3。session_summary 与 final 复用同模板；**不依赖 B2**，可提前 |
| 4 | T1 | T2/T5。layer block 读两列并按新预算截断；可与 T4 并行 |
| 5 | T4 | 无。dialectic 计数独立；可与 T1 并行 |
| 6 | T8 → T9 → T6 | T2/T3。B1 编排、B2 后置、ai_self 六字段；T13 的命名映射在本步核对 |
| 7 | T10 | T8。B4 子 agent 与 knowledge_render；B4 schema 可先写，随 T8 激活 |
| 8 | T11 | 无。队列核对独立；可与 T1/T4 并行 |
| 9 | T12 | T3/T6/T7。adviser 准绳读新语义；太早会把旧卡当准绳 |
| 10 | T14 | 批准。events org 变更 |
| 11 | T15 | 无。只文档，不导入数据 |

T13 是贯穿项（键 ↔ artifact 列 ↔ conclusion kind ↔ adviser 用法），不是独立实现步：在 T8/T9 与 T12 各自落地时按映射表核对，不单列顺序位。

## 11. 验证

- 每个任务落地后：`python -m pytest -q`（全量，exit 0）。
- 注入面：手工跑 `prefetch` 断言两块 + todo，且不含知识/召回。
- 窗口：两会话 fixture，确认不串窗口；compress 后再刷新仍取本会话最新行。
- 编排：子 agent 缺一，B1 仍输出其余三键；重跑只补失败子 agent。
- adviser：第二次 base 后准绳更新；卡旧于事件时 `stale_card`。
- events（批准后）：`tests/test_knowledge.py tests/test_layers.py` 全绿。

## 12. 参考

- 生命周期与 ai_self：`docs/draft/window-lifecycle-and-ai-self-draft.md`
- 摘要模板：`docs/draft/window-summary-template-draft.md`
- agent 契约与 adviser 对齐：`docs/draft/layer-agent-contracts-draft.md`
- events 变更：`docs/draft/events-org-change-draft.md`
- lancedb 规则：`docs/draft/lancedb-knowledge-adaptation-draft.md`
- run-meta 与多级 system_message：已并入本文 §3.5 T16
