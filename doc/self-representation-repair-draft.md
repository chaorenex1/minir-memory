# 新增五个 advice target 及判定（草稿）

状态：草稿，供评审（v9）。代码基线：`plugin/minir/` 提交 `088b355`（工作区干净）；`adviser_prompts.py`、`prompts/adviser/`、`rules/advice_target.md`、`rules/advice_judgment.md` 已随该提交落地。

本稿在现有 advice target 词表上新增五个并列目标与对应判定，不改既有七个 target 的语义、阈值或投递通道：

| 建议目标 | 准绳 | 判定类型 |
| --- | --- | --- |
| `user_preferences_check` | `base.user_peer_card`（用户偏好列） | `violated` / `temporary_override` / `stale_card` |
| `ai_self_representation_check` | `base.ai_self_representation` | `false_claim` / `missing_work` / `scope_change` |
| `soul_tool_policy_check` | SOUL 的 `# Precedence`、`# Tool policy`、`# Boundaries` | `tool_policy_breach` |
| `soul_workflow_check` | SOUL 的 `# Workflow`、`# Verification & failure`、`# Delivery contract`、`# Critical` | `workflow_breach` |
| `soul_craft_check` | SOUL 的 `# Craft` | `craft_breach` |

五个目标互不包含、互不替代。`soul_tool_policy_check`、`soul_workflow_check`、`soul_craft_check` 是三个独立目标，不存在 `soul` 父目标；三个 soul 目标彼此并列，`user_preferences_check` 与 `ai_self_representation_check` 也各自独立。一条建议只写一个 target。

### 与 `rules/advice_*` 的关系

`rules/advice_target.md` 与 `rules/advice_judgment.md` 是判定口径的唯一来源；本稿只新增 token 与判定，其余规则原样沿用。

| 项 | 规则来源 | 本稿动作 |
| --- | --- | --- |
| target 词表 | `advice_target.md` 的 `ADVICE TARGETS` | 新增五个 token；既有七个不变 |
| 判定类型 | `advice_target.md` 各 target 的 `Kinds` | 新增九个 kind，不新增 `check` 字段 |
| 严重度上限 | `advice_judgment.md` 的 `SEVERITY CEILINGS BY TARGET` | 五个 target 各一行；本稿不另立第二份词表 |
| 证据门槛 | `advice_judgment.md` 的 `EVIDENCE GATE` | 沿用四项证据：`turn_uid`、准绳原句、实际观测、判定类型 |
| 发出与去重 | `EMIT GATE` / `DEDUPE` | 沿用；重复观测只计 `recur`，不再投递 |
| 知识引用 | 两个规则的 `KNOWLEDGE REFERENCES` | 沿用后缀格式与校验；本稿不新增格式 |
| 覆盖状态 | `advice_target.md` 的 `COVERAGE` | review 写入 `adviser_reviews.coverage` |
| 统计 | `advice_judgment.md` 的 `STATISTICS` | 统计块只改观察顺序，不改判定、阈值或预算 |
| 投递 | 既有 `ADVICE_HANDLING` 与 `severity_floor` | 五个 target 与既有 target 共用同一通道 |

实现层面的登记面、解析与统计是这些规则的代码落点，不是第二套规则。

范围：

- AI 自我表示的会话绑定与召回报错契约。
- 五个并列建议目标的登记、解析与处置。
- 建议统计面，以及统计反馈对顾问观察的影响（§3.7）。
- 五个目标进入既有建议管道：review 判定 → guard → `advisories` → 投递 → 统计（§4）。

非目标：`identity.md` / `preference.md` 的三天合并保持原样；建议不改自表示，不改历史 transcript；不按统计自动调整 severity、`severity_floor` 或建议预算；不引入新的顾问通道或审计表；不给 `minir_search` 增加内部 kind 的写能力；不做会话结束后的自动修复作业。

## 0. 术语

| 词 | 含义 | 位置 |
| --- | --- | --- |
| `ai_self_representation` | base 的 AI 自表示列，随会话行为漂移 | 列 `base_refreshes.ai_self_representation`；part 同名 |
| `ai_identity_card` | base 的 AI 身份列，稳定身份 | 列 `base_refreshes.ai_identity_card`；part 名 `ai_identity` |
| `base.ai_self_representation` | 已发布的 AI 自表示结论，当前 scope=agent、跨会话可召回 | `store._BASE_CONCLUSION_KINDS`（store.py:389-396） |
| `base.ai_identity_card` | 已发布的 AI 身份结论，长期 | 同上 |
| `base.user_peer_card` | 用户偏好结论，scope=user，长期 | 同上 |
| `base.user_representation` | 用户自表示结论，scope=user，长期 | 同上 |
| `render_ai_card` | 把 AI 两列渲染成 `## AI Card` 下的两个 `###` 节 | `layers.py:428-433` |
| `publish_base_conclusions` | 会话边界把六个校准列各写成一条结论 | `store.py:1480`；入口 `__init__.py:1198` |
| `_LONG_TERM_KINDS` | 跨会话召回白名单 | `store.py:343` |
| `_visibility_sql` / `search` | 唯一召回可见性谓词与读入口 | `store.py:1989` / `store.py:2006` |
| `user_preferences_check` | target：会话与用户卡-用户偏好冲突 | 本稿新增 |
| `ai_self_representation_check` | target：会话与自表示不一致 | 本稿新增 |
| `soul_tool_policy_check` | target：会话违反 SOUL 的工具与授权约束 | 本稿新增 |
| `soul_workflow_check` | target：会话违反 SOUL 的流程与完成声明约束 | 本稿新增 |
| `soul_craft_check` | target：会话违反 SOUL 的 Craft 约束 | 本稿新增 |
| HERMES_SOUL | `$HERMES_HOME/SOUL.md`，operator 手写的角色与约束 | 已有，16 节 13,238 字节（本机实测） |

## 1. 现状 → 目标

| Surface | 现状 | 目标 |
| --- | --- | --- |
| AI 自表示发布 | `publish_base_conclusions` 按 `_BASE_CONCLUSION_KINDS` 一列一结论；`ai_self_representation` 的 scope 是 `agent`，key 为 `agent:<subject>` | 该列 scope 改 `session`，key 变 `session:<sid>:<subject>`；其余五列不动 |
| artifact 切节 | `_ARTIFACT_SECTIONS` 没有 base 项（store.py:352-360），注释写明六列已按列发布 | 不恢复切节，本稿不碰 artifact 解析 |
| 跨会话召回 | `base.ai_self_representation` 在 `_LONG_TERM_KINDS` 内 | 移出；`base.user_representation`、`base.user_peer_card`、`base.ai_identity_card` 保留 |
| 召回缺 id | 空 id 时 `_visibility_sql` 只返回持久行，会话 kind 静默为空；`scope="all"` 是 `1=1` | 显式点名会话 kind 且无 id 时报错；`scope="all"` 静默排除会话 kind |
| 用户偏好核对 | 无。顾问 digest 只带两列各 320 字（adviser_mode.py:1235-1251），不逐条对账 | target `user_preferences_check`，准绳读完整 `base.user_peer_card` |
| 自我一致性判定 | 无。digest 不含 AI 两列，顾问只审当前动作 | target `ai_self_representation_check` |
| 角色约束核对 | 无。顾问的 run 不加载 SOUL（`agent_loop.run` 的 `load_soul_identity` 默认 False） | 三个独立 target，各读一组约束节 |
| 目标词表 | `rules/advice_target.md` 已写五个 token；`prompting.ADVICE_TARGETS` 仍只有 7 个 | 两处对齐（§3.1） |
| 建议的生成依据 | 只看当前 review 窗口内的即时事件，历史判定结果不参与 | 每次 review 注入统计块；统计反馈改变下一轮观察（§3.7） |
| 会话结束 | `on_session_end` → `_finish_session` → `adviser_mode.session_end` → `session_reset` → `expire_adviser_advices`（adviser_mode.py:2429），只做过期清理 | 保持只做过期清理；五个 target 不在会话结束后补做任何作业 |
| 建议管道 | 没有一致性 target；建议只有 review 内的即时判定 | 五个 target 共用一条管道：review 判定 → guard → `advisories` → 投递 → 统计；不新增 layer、job、写权限或第二套通道 |

## 2. 召回侧：会话绑定与报错

### 2.1 发布现状：改 scope，不拆节

base 的发布路径是 `MinirProvider.publish_base_conclusions`（`__init__.py:1198`）→ `store.publish_base_conclusions`（`store.py:1480`），每个校准列写一条结论：

```
_BASE_CONCLUSION_KINDS (store.py:389)
  user_representation    base.user_representation    user
  user_peer_card         base.user_peer_card         user
  ai_self_representation base.ai_self_representation agent   <- 改 session
  ai_identity_card       base.ai_identity_card       agent
  window_summary         base.window_summary         session
  value_markers          base.value_markers          session
```

`_ARTIFACT_SECTIONS` 没有 base 项，parser 也不再切 `## AI Card`。所以本稿不新增子节规则，也不改 `render_ai_card`（`layers.py:428`），只改一个 scope：

- `_BASE_CONCLUSION_KINDS` 的 `ai_self_representation` 行改为 `session`。
- `_scope_key("session", subject, session_id)` 生成 `session:<sid>:<subject>`（store.py:416-419；sid 为空时回退用 subject，这正是 §2.3 要改成报错的原因）；`publish_base_conclusions` 已经按 scope 决定是否写 `session_id`（store.py:1511），不需要新分支。
- `_LONG_TERM_KINDS` 去掉 `base.ai_self_representation`，保留另外三项。
- `_KIND_WEIGHT` 不动：它是打分权重，不是可见性。

旧行不迁移。`f0e9b25` 之前的 `base.user_card` / `base.ai_card` 已不在长期表内，本稿也不回收；已按 agent scope 发布的 `base.ai_self_representation` 行同理，留在库里作为惰性历史。`base_refreshes.conclusions_published_at` 一旦置位，该会话不再发布（store.py:1496），所以旧会话不会被回填，新会话按新 scope 写。这条路径只影响新发布，不阻塞启动，也不需要一次性重写。

### 2.2 召回规则

| 请求 | `base.ai_self_representation` | `base.ai_identity_card` |
| --- | --- | --- |
| `scope="session"` + 有效 id | 返回该会话的行 | 返回 |
| `scope="session"` + 空 id | 报错 | 正常返回（不依赖会话） |
| `scope="all"` | 不返回，显式点名时报错 | 返回 |

「显式点名」指 `store.search(..., kinds=...)` 只含会话绑定 kind。泛化查询（不限 kind）在 `scope="all"` 下静默排除，不报错，否则每个跨会话查询都会失败。

### 2.3 错误契约

错误在 `store.search` 抛出（store.py:2006），它在建谓词前就能看到 `kinds` 与 `session_id`；`_visibility_sql` 是纯谓词，不负责报错。没有 `store._search` 这个方法，工具入口在 `provider.dispatch` 的 `minir_search` 分支（`__init__.py:1619`），prefetch 走 `provider._search`（`__init__.py:1793`）。

```
错误码：session_scope_required
错误文案：AI self representation is session-scoped. Provide the main session id
         (minir_search session_id or the provider session) to read it.
附带字段：kind、requested_scope、resolved_session_id
```

- 不回退到别的会话，不降级为空数组，不静默换成 `base.ai_identity_card`。
- 错误必须以工具结果的形式回到主会话，而不是只写日志。工具入口现在是 `args.session_id or self._session_id`（`__init__.py:1634`）；会话绑定 kind 显式传空 id 时去掉这条回退，异常转 `tool_error`。
- 顾问读会话历史不需要 store 的会话 id：review prompt 已经带 `transcript_prompt.digest_block(...)` 的 `session_history`（adviser_mode.py:1285-1288）。store 的会话 id 只在读会话绑定结论时需要。

## 3. 五个并列建议目标

### 3.0 并列关系与分工

五个目标各自独立；下表一行一个建议目标。三个 `soul_*` 是三个目标，不是一个 `soul` 目标下的三个 check。

| target | 准绳 | 判定类型 |
| --- | --- | --- |
| `user_preferences_check` | `base.user_peer_card` | `violated` / `temporary_override` / `stale_card` |
| `ai_self_representation_check` | `base.ai_self_representation` | `false_claim` / `missing_work` / `scope_change` |
| `soul_tool_policy_check` | SOUL 的 `# Precedence`、`# Tool policy`、`# Boundaries` | `tool_policy_breach` |
| `soul_workflow_check` | SOUL 的 `# Workflow`、`# Verification & failure`、`# Delivery contract`、`# Critical` | `workflow_breach` |
| `soul_craft_check` | SOUL 的 `# Craft` | `craft_breach` |

并列规则：

- 五个 target 之间没有优先级，也没有替代关系。三个 `soul_*` 目标彼此并列，不共享父节点；停用其一不影响另外两个，也不影响前两个目标。
- 同一处观测只写一条建议。跨 target 的冲突各自成条；三个 `soul_*` 目标命中同一句话时，按 `soul_tool_policy_check` > `soul_workflow_check` > `soul_craft_check` 取第一个，不重复计数。
- target 就是 advice row 的 `target` 字段，没有独立的 check 字段。五个值直接进 `ADVICE_TARGETS`，解析、投递、统计都只认这一层。
- 五个 target 的处置一致：当轮发建议，动作方当场改；会话结束后不补做、不重跑、不改产物。`ai_self_representation_check` 与其他四个走同一条管道，没有专属作业或终态列。

共享规则：

| 项 | 规则 |
| --- | --- |
| 载体 | `advisories`（store.py:171），用已有的 `target` 列；不加列、不加表 |
| 投递 | 既有通道：中途块或回合边界，受 `severity_floor`（默认 concern）与投递门控约束 |
| 字段 | `target`，加上四项证据：`turn_uid`、准绳原句、实际观测（工具名与结果）、判定类型 |
| 缺证据 | 任一项缺失即不发。宁可不发，不做泛泛的「不一致」 |
| 顾问输入 | 会话历史（transcript digest）+ 准绳 + 统计块（§3.7） |
| 去重 | 沿用 advisories 的指纹逻辑，同一冲突不重复发；重复观测计入统计，不再投递 |

### 3.1 登记面

一处登记，多面同步。注意 reviewer 侧的指令已经不在 `adviser_prompts.py` 里：review prompt 由 `prompts/adviser/*.md` 加两段规则正文拼成（`review_prompt`，adviser_prompts.py:92），target 说明在 `rules/advice_target.md`。

| 面 | 位置 | 改动 |
| --- | --- | --- |
| 元组 | `ADVICE_TARGETS`（prompting.py:79） | 新增 `CONSISTENCY_TARGETS`（五个 token），并入；总数 7 → 12 |
| 系统块 | `ADVICE_TARGET_GROUPS`（prompting.py:108） | 增 `("Consistency targets:", CONSISTENCY_TARGETS)`；`_adviser_prompt_block` 按组渲染（`__init__.py:637`） |
| 投递词表 | `DELIVERED_TARGETS`（prompting.py:105） | 并入五 token |
| 处置矩阵 | `ADVICE_HANDLING`（prompting.py:124） | 五个 target 各补 blocker / concern 两格；`nit` 由 `DELIVERED_TARGETS` 生成。少一格会在 `advice_handling_section()`（prompting.py:191）拼系统块时 `KeyError`，不是静默 |
| 表格渲染 | `advice_handling_section` | 现有 7 列一张表不动，Consistency 五列另起一张，避免 12 列宽表 |
| 解析接受 | `adviser_mode.DELIVERED_TARGET_NAMES`（adviser_mode.py:55） | 改为直接取 `prompting.DELIVERED_TARGETS`，删掉本地 `ACTION_TARGETS + ARTIFACT_TARGETS` 重组。不并会让 `parse_review` 把五个一致性 target 当作未知 `_check` 拒收（记 `bad_target`） |
| 顾问指令 | `rules/advice_target.md` + `rules/advice_judgment.md` | 五个 token 的准绳、判定类型、证据要求、严重度上限、`coverage` 与 `bad_target` 均以规则正文为准；本稿不改规则语义，只补代码落点 |
| 解析 | `parse_review`（adviser_mode.py:439） | `_check` 结尾但不在 `CONSISTENCY_TARGETS` 的值拒收并记 `bad_target`；无 check 字段；`coverage` 状态图单独解析 |
| 投递渲染 | `render_block`（adviser_mode.py:498） | 标签显示 target 与 severity；长 token 由 `MAX_BLOCK_CHARS` 截断兜底 |
| 覆盖状态 | `adviser_reviews.coverage`（新列，JSON） | 五 target × `clean` / `found` / `n/a` |

五个 token 直接作为 target，不再拆 check：`soul_*` 三个是建议目标，不是 `soul` 的子检查。长 target 名会撑大投递标签，但不引入短名映射，避免同一目标出现两套词表；投递预算仍由 `render_block` 的截断处理。

### 3.2 `user_preferences_check`：会话 vs 用户卡-用户偏好

准绳：`base.user_peer_card` 的正文（用户卡 `### Preferences` 列的发布结果）；没有时退到全局文件 `$HERMES_HOME/minir-memory/user/preference.md`（`card_review.card_paths`，card_review.py:106-116）。

| 类型 | 含义 | 建议指向 |
| --- | --- | --- |
| `violated` | 行为或声明违背卡里写明的偏好 | 当场改，或停手确认 |
| `temporary_override` | 本次会话用户另一句话与卡冲突 | 问清是本次覆盖还是要改卡 |
| `stale_card` | 用户本会话表达了新偏好，卡里还没有 | 更新卡，不算违例 |

证据里的「准绳原句」= 卡里那一行原文。顾问现有摘要对两列各截 320 字（adviser_mode.py:1246），可能截掉偏好原句；而 `minir_search` 现在读不到这一列：

- 工具的 `type` 只收 `CONCLUSION_KINDS`（note、五类标记、todo），内部 kind 直接报错（`__init__.py:1629`）。
- 工具返回的正文另被 `_MAX_TOOL_EXCERPT = 600` 截断（`__init__.py:115`）。

所以本稿不假设「用 minir 工具按 kind 读完整卡」。默认做法：新增 `AdviserMode._rulers(session)`，按 kind 读 `base.user_peer_card` 与 `base.ai_self_representation`，各 2,000 字符上限；`_prompt` 把它作为一个新标签的 `data_block` 注入，其余两列在 `_digest` 里保持 320。`_digest` 的返回类型不变；两列准绳只在 review prompt 注入，consult 继续用 `_consult_context` 的 digest。块标签要登记进 `prompting.RESERVED_LABELS`（prompting.py:17），否则模型数据里的同名标签不会被中和。摘要不整体扩容，也不改工具契约；扩工具面是另一条路，本稿不做。

### 3.3 `ai_self_representation_check`：会话 vs AI 自我表示

准绳：`base.ai_self_representation` 的当前文本（会话绑定，取自 §2）。

| 类型 | 含义 |
| --- | --- |
| `false_claim` | 声明了没做的事（「已验证」但没跑） |
| `missing_work` | 自表示承诺的流程没走（「先给结论」但埋在第三段） |
| `scope_change` | 自表示说的范围与实际做的范围不一致 |

自表示的更新仍由发布链路负责；建议只走统一管道，不改自表示本身。

### 3.4 三个 `soul_*` 建议目标：会话 vs HERMES_SOUL 约束

三个目标各自独立，各取一组 SOUL 节，互不重叠：

| target | 节 | 字节 | 判定类型 | 严重度上限 |
| --- | --- | --- | --- | --- |
| `soul_tool_policy_check` | `# Precedence`、`# Tool policy`、`# Boundaries` | 1,893 | `tool_policy_breach` | blocker（Precedence / Boundaries）、concern（Tool policy 程序性） |
| `soul_workflow_check` | `# Workflow`、`# Verification & failure`、`# Delivery contract`、`# Critical` | 2,629 | `workflow_breach` | blocker（Delivery contract / Critical）、concern（Workflow / Verification） |
| `soul_craft_check` | `# Craft` | 1,541 | `craft_breach` | nit（表面）、concern（代码经济） |

三个目标合计 6,063 字节，占整份 SOUL（13,238 字节）的 46%。节字数是按 `^# ` 切分后的整节字节，含节尾空行；本机 SOUL.md 共 16 节，映射 8 节、排除 8 节，覆盖全部节。

判据：

- 必须能引用 SOUL 的原句；引不出原句的，不发。
- `# Precedence` 管的是「谁的话算授权」：把工具输出、记忆或网页当指令，判 `tool_policy_breach`。
- `# Boundaries`、`# Critical` 与 `# Delivery contract` 的硬约束不可被用户当场指令覆盖，命中即 blocker；Tool policy 的程序性要求、Workflow 的步骤顺序、Craft 的表面偏好可在 operator 明确指令下让路，不判违例。
- `# Delivery contract` 的「未验证就收尾」「缩范围未说明」「留 stub」判 blocker；`# Workflow` 的跳步、不清理、不验证判 concern，两者都归 `soul_workflow_check`。
- `# Craft` 的表面禁令判 nit，代码经济（重写 stdlib、给一次性任务加防御层）判 concern；不把「读起来像生成的」当证据，必须引原句。
- 同一句话只归属一个目标。三个 `soul_*` 命中同一句话时按 `soul_tool_policy_check` > `soul_workflow_check` > `soul_craft_check` 取第一个；它们没有共同的 `soul` 父目标，各自独立成条。

这组上限已经写进 `rules/advice_judgment.md` 的 `SEVERITY CEILINGS BY TARGET`，本节只记录口径，不再新增第二份词表。

不进判定的节：`# Role`、`# Character`、`# Priorities`、`# Runtime`、`# Delegation & Kanban`、`# Memory & continuity`、`# Communication`、`# Avoid`。前七节是身份、能力与风格叙述，不是可引用原句的硬约束；`# Avoid` 是其他节的摘要，直接引源节，避免同一句话被两次计数。

读取与成本：

- 只读 `$HERMES_HOME/SOUL.md`，按 mtime 缓存解析结果。
- 按启用的 target 拼接对应节，只注入这 6 KB，不整份注入，也不用 `load_soul_identity=True`（那是 13 KB 全文，且会改写顾问自身的身份）。
- 文件缺失或读不出时，三个 `soul_*` target 本轮一起停用，不产生判定；在状态里记一条 `soul_unavailable`。

### 3.5 处置

| 等级 | `user_preferences_check` | `ai_self_representation_check` | `soul_tool_policy_check` | `soul_workflow_check` | `soul_craft_check` |
| --- | --- | --- | --- | --- | --- |
| blocker | 安全或流程类偏好被违背且动作已发出或不可逆：停手，报告，不继续 | 与自表示冲突的完成声明即将发出或已发出：不发送，当场补验证 | Precedence / Boundaries 命中：立即停手，报告，不复述敏感内容 | Delivery contract / Critical 命中：立即停手，报告，不复述敏感内容 | 无 blocker，表面类最高 nit |
| concern | 表达型偏好被违背（语言、结构、长度、顺序）：当场改 | 行为偏离自表示但未形成声明：当场校准 | Tool policy 程序性：当场改 | Workflow / Verification 跳步：当场改 | 代码经济：当场改 |
| nit | 措辞层面的偏差：可选 | 措辞层面的偏差：可选 | 无独立 nit，程序性最低 concern | 无独立 nit，步骤叙述最低 concern | Craft 表面类：可选，不打断任务 |

投递走既有通道：入 `advisories`，按 `severity_floor` 与投递门控决定中途出现还是边界出现。本表是投递侧 `ADVICE_HANDLING` 的五个新列；判定侧的上限在 `rules/advice_judgment.md` 的 `SEVERITY CEILINGS BY TARGET`，两处不混用，也不互相推导。

### 3.6 何时不发

- 会话历史与准绳没有可引用的冲突点，只有「风格不同」。
- 准绳本身过时（`stale_card`），这时发的是「该更新卡」，不是违例。
- SOUL 文件不可读，或引不出原句。
- 同一冲突本轮已经发过（指纹去重）；重复观测只进统计，不再投递。
- target 以 `_check` 结尾但不在 `CONSISTENCY_TARGETS` 内：拒收，记 `bad_target`，不降级成 `general`。
- 统计把该 target 排到低优先观察位，且本轮没有新的准绳变化或新的证据点（`soul_tool_policy_check` 与 `soul_workflow_check` 不受此限，见 §3.7.3）。

### 3.7 统计与反馈

原则：建议采用统计，统计的反馈影响顾问的观察。统计只回答「看哪里、先看什么、这个问题是否还在」，不回答「该不该判违例」；判定仍要四项证据齐全。

#### 3.7.1 记录面

| 数据 | 位置 | 用途 |
| --- | --- | --- |
| review 的 target 覆盖 | `adviser_reviews.coverage`（新列，JSON） | 每次 review 报告五个 target 的 `clean` / `found` / `n/a`，构成分母 |
| 建议的 target / severity / status | `advisories.target`（已有列） | 建议量、投递量、severity 分布 |
| guard 丢弃 | `adviser_reviews.drop_detail`（JSON，reason→count，落库 2,000 字符上限） | 计数键改成 `<reason>/<target>`（如 `duplicate/soul_craft_check`），shape 不变，`adviser_mode_stats` 的 `detail.items()` 聚合不用改 |
| 投递时间 | `advisories.delivered_at`（已有列） | 划定反馈窗口 |

`adviser_reviews.coverage` 的值只允许三个：`clean`、`found`、`n/a`。missing 记 `unknown`，不计入 clean 率；状态图解析失败也记 `unknown`，不影响建议投递。

#### 3.7.2 顾问看到的统计块

每次 review 的 prompt 增一段有界统计，只统计本会话最近 N 次 review（默认 8），只给计数，不给正文。下例的数值仅示意格式：

```
TARGET STATS (last 8 reviews)
target                        reviews  clean  found  n/a  adv  deliv  recur  follow-up
user_preferences_check              8      6      2    0    2      2      0  not_reobserved
ai_self_representation_check        8      7      1    0    1      0      1  not_reobserved
soul_tool_policy_check              8      8      0    0    0      0      0  -
soul_workflow_check                 8      7      1    0    1      0      0  -
soul_craft_check                    8      6      2    0    2      2      1  recurred
```

- `adv` 是入库建议数，`deliv` 是 `delivered_at` 非空的数量。
- `recur` 是投递后同指纹再次被观测到的次数（guard 的 `duplicate` 丢弃）。
- `follow-up` 每行只取一个值，按 `recurred` > `not_reobserved` > `no_followup` 取最靠前的一个。
- 不写 `acted`、`adopted` 或 `ignored`：插件看不到用户是否采纳，只能看到该问题是否被再次观测。

#### 3.7.3 反馈如何改变观察

- `recurred` 排最前：下一轮先核对上一次证据点名的文件或命令，确认旧问题是否仍在；这是复核，不是新发现，不再投递同一条。
- `not_reobserved` 降低扫描优先级：该 target 退到低优先位，只有出现新的准绳变化（用户卡写入、SOUL mtime 变化、`base.ai_self_representation` 发布）或新的证据点时才重新前置。
- `no_followup` 不构成任何结论：只表示还没有下一次 review，不当作已解决。
- 安全类不受降级影响：`soul_tool_policy_check` 与 `soul_workflow_check` 始终前置，`blocker` 不走统计门控，也不因历史 clean 而少看。
- 统计不改判定：`severity`、`severity_floor`、`max_advices`、指纹规则都不由统计调整；统计只改观察顺序与观察窗口。
- 统计不得作为压制建议的理由。任何「历史上这类问题很少」都不能阻止一条证据齐全的 blocker。

#### 3.7.4 成本与边界

- 每次 review 一次聚合查询，行数按启用的 target 固定（默认 5 行），不随会话长度增长。
- 统计块在 review 开始时生成一次，review 内不刷新。
- 只统计本会话；跨会话聚合只出现在 `minir_stats` 给 operator 看，不进顾问 prompt。
- 统计只含计数与 target 名，不含 transcript、建议正文或证据原文。

## 4. 统一建议管道

五个 target 只有一条管道，没有按 target 分叉的作业、终态列或写权限。

### 4.1 判定到投递

```
review（transcript + 准绳 + 统计块）
  → parse_review（target 白名单、四项证据）
  → guard（指纹去重、预算、severity_floor）
  → advisories（一行一条，target 字段定位）
  → 投递（中途块或回合边界）
  → 统计（coverage、drop_detail、delivered_at）
```

- 五个 target 共用同一套证据门槛、指纹、预算与投递门控；`severity_floor` 与 `max_advices` 对五者一致。
- 每条建议只带一个 target；三个 soul target 命中同一句话时按固定顺序取第一个。
- guard 的 `duplicate` 计数键带 target，重复观测只进统计，不再投递。
- 不新增 `jobs` layer、不新增 worker 分支、不新增文件写权限：顾问与 review 都不改产物。
- `rules/advice_target.md` 与 `rules/advice_judgment.md` 是唯一的处置口径；Python 侧只做词表、解析与统计。

### 4.2 会话结束

`adviser_mode.session_end` 保持现状：`session_reset` 后由 `expire_adviser_advices` 清掉 `pending` / `staged`。五个 target 都不在会话结束后补做：自表示冲突已在当轮作为建议发出，事后补做会绕开投递门控和用户对证据的核对。

### 4.3 禁令

- 建议不改 `base.ai_self_representation`、不改卡文件、不改 transcript、不改产物。
- 不引入第二个顾问通道、第二套 target 词表或平行的审计表。
- 统计不改判定，也不作为压制建议的理由。

## 5. 改动清单

| # | 文件 | 改动 |
| --- | --- | --- |
| 1 | `store.py` | `_BASE_CONCLUSION_KINDS` 的 `ai_self_representation` 改 scope=`session`；`_LONG_TERM_KINDS` 去掉该 kind；不迁移旧行 |
| 2 | `store.py` | `search` 在 `kinds` 只含会话绑定 kind 且无 `session_id` 时抛 `session_scope_required`；`scope="all"` 过滤会话绑定 kind |
| 3 | `__init__.py` | `minir_search` 分支：会话绑定 kind 不回退 `_session_id`，异常转 `tool_error` |
| 4 | `prompting.py` | 新增 `CONSISTENCY_TARGETS`；并入 `ADVICE_TARGETS`、`DELIVERED_TARGETS`、`ADVICE_TARGET_GROUPS`；`ADVICE_HANDLING` 补五 token × blocker/concern；`advice_handling_section` 出两张表 |
| 5 | `adviser_mode.py` | `DELIVERED_TARGET_NAMES` 改为取 `prompting.DELIVERED_TARGETS` |
| 6 | `rules/advice_target.md`、`rules/advice_judgment.md` | 规则正文是唯一口径：五个 token、九个 kind、severity ceiling、`coverage`、`bad_target` 已写入；代码只按这些口径登记与解析，不改规则语义 |
| 7 | `adviser_mode.py` | `parse_review` 拒收未知 `_check`；`_review_one` 解析并写 `adviser_reviews.coverage` |
| 8 | `adviser_mode.py` | 新增 `_rulers(session)`（两条准绳列各 2,000 字）、统计块、SOUL 约束块；`_prompt` 用新标签的 `data_block` 注入；标签登记进 `prompting.RESERVED_LABELS` |
| 9 | `soul_rules.py`（新） | 读 `$HERMES_HOME/SOUL.md`，按三个 target 的节映射截取八节，mtime 缓存，返回 `(sections, sha)`；读不出返回 None |
| 10 | `store.py` | `adviser_reviews.coverage`（走 `_ADDED_COLUMNS`）；`review_stats(session_id, window)` 只读 review / advisories / drop_detail |
| 11 | `adviser_guard.py` | `_drop` 的计数键带 target，`drop_detail` 仍是 reason→count |
| 12 | `__init__.py` | `minir_stats` 输出增 `targets` 段（review 覆盖、投递、重复观测） |
| 13 | `README.md`、`doc/rule-files-draft.md` | 目标计数改为按常量表述（README 现写 11，常量是 7）；补 `advice_target` / `advice_judgment` 两个规则行 |

## 6. 测试

| 场景 | 断言 |
| --- | --- |
| 缺 session id 读 `base.ai_self_representation` | 抛 `session_scope_required`，文案含 main session id，不返回空 |
| 会话发布 | 同一 session 的 `base.ai_self_representation` 行带 `session_id`，key 前缀 `session:`；`base.ai_identity_card` 仍无 session id |
| 长期表 | `_LONG_TERM_KINDS` 不含 `base.ai_self_representation`；`scope="session"` 查询看不到别的会话的该 kind |
| `scope="all"` 泛化查询 | 结果里没有 `base.ai_self_representation`，也不报错 |
| 显式点名 `base.ai_self_representation` + `scope="all"` | 报错 |
| 五 target 并列 | 同一 session 可同时产出五个 target 的建议；停用其一不影响另外四个 |
| 词汇贯通 | 系统块、顾问词表、解析器三处都认五个 target token；`ADVICE_HANDLING` 缺一格时建 prompt 抛 `KeyError` |
| soul 三 target 独立 | 一条 finding 只写 `soul_tool_policy_check` / `soul_workflow_check` / `soul_craft_check` 之一；不存在 `soul` 父 target |
| target 白名单 | 以 `_check` 结尾但不在 `CONSISTENCY_TARGETS` 的 target 被拒收，记 `bad_target`，review 不失败 |
| target 去重 | 同一句 SOUL 命中三个 soul target 时只产出一条，取顺序最靠前的 target |
| 建议证据 | 缺 target 或四项证据之一时解析器拒收 |
| `user_preferences_check` 准绳 | 建议里的 reference quote（准绳原句）能在 `base.user_peer_card` 里逐字找到 |
| 准绳块 | prompt 中两条准绳列各不超过 2,000 字；块标签在 `RESERVED_LABELS` 内；注入同名闭合标签被中和 |
| `stale_card` / `temporary_override` | 前者发「更新卡」，后者问用户；都不判违例 |
| soul 读取 | 只取三个 target 的八节；文件缺失时三个 target 停用并记 `soul_unavailable`，不产生判定 |
| soul 原句 | 引不出 SOUL 原句的建议被拒收 |
| soul 节归属 | `# Boundaries` 只进 `soul_tool_policy_check`，`# Delivery contract` 只进 `soul_workflow_check`，`# Craft` 只进 `soul_craft_check` |
| 用户覆盖 SOUL 叙述 | 用户当场指令优先，不判 `workflow_breach`；`# Boundaries` / `# Critical` / `# Delivery contract` 例外 |
| 统计块 | 第 N 次 review 的统计块与库里 review/advisories/drop_detail 的计数一致，行数等于启用的 target 数（默认 5） |
| 重复观测 | 已投递建议的同指纹再现计入 `recur`，不再投递，也不改原建议状态 |
| 反馈优先级 | `recurred` 的 target 在 prompt 中排在 clean 之前 |
| 统计不越权 | 统计块不改变 `severity_floor`、`max_advices` 或 blocker 投递 |
| 统一管道 | 五个 target 都只产生 `advisories` 行；不产生 job、不新增终态列、不申请文件写权限 |
| 会话结束 | `session_end` 只做过期清理；五个 target 的建议按原状态过期，不补做 |

对应测试文件：`tests/test_store.py`（发布 scope、可见性、错误契约）、`tests/test_provider.py`（digest 与工具入口）、`tests/test_adviser_mode.py`、`tests/test_adviser_channels.py`、`tests/test_rules.py`（规则语料 id 列表）、`tests/test_card_review.py`、`tests/test_layers.py`，新增 `tests/test_soul_rules.py`、`tests/test_review_stats.py`。

## 7. 配置

| 键 | 面 | 默认 | 用途 |
| --- | --- | --- | --- |
| `soul_targets_enabled` | `adviser_mode._DEFAULTS` | true | 关掉后不注入 SOUL 约束节，三个 `soul_*` target 一起停用 |
| `review_stats_enabled` | `adviser_mode._DEFAULTS` | true | 关掉后 review prompt 不注入统计块，判定照旧 |
| `review_stats_window` | `adviser_mode._DEFAULTS` | 8 | 统计块回看的 review 次数，按会话 |

`adviser_mode._cfg()` 只合并 `provider._config["adviser_mode"]`（adviser_mode.py:693-699），所以新增的 reviewer 键必须同时写进 `_DEFAULTS`，否则默认值不生效；本稿不再新增 provider 级配置键。

## 8. 未决

1. `coverage` 状态图是否强制返回。本稿取可缺省，missing 记 `unknown`、不计入 clean 率；强制会让弱模型多一次失败面。
2. 统计窗口按会话还是按顾问跨会话。本稿按会话，跨会话只进 `minir_stats`；若要做跨会话的 target 画像，需要另定会话边界。
3. 五个 target 名较长，投递标签是否要短名映射。本稿取不映射，靠 `render_block` 截断；短名会引入第二套词表。
4. `base.ai_self_representation` 是否要在产生它的会话内每轮注入，还是只在顾问里读。

## 9. 风险

| 风险 | 缓解 |
| --- | --- |
| 准绳块挤占 transcript | 两条准绳列各 2,000 字上限；prompt 仍按 24,000 截断，超限先丢最旧事件；实测块大小写进测试 |
| 顾问整会话对账的 token 成本 | 只在该轮判定需要时读会话检索，且限定窗口；SOUL 只注入 6,063 字节约束节；统计块按启用的 target 固定行数 |
| SOUL 变成挑措辞的教条 | 只有能引用原句、且落在八节之内的才判；表面类最高 nit |
| 偏好判定变成挑格式毛病 | `violated` 必须能引用卡里原句；表达型偏好只到 concern |
| 自表示漂移无人修 | `ai_self_representation_check` 在冲突当轮发出建议，动作方当场补验证；同一冲突靠指纹去重，不重复投递 |
| 五个 target 互相顶替 | 一条建议只写一个 target；并列与停用的测试按 target 隔离 |
| Advisers 章变长 | 处置表拆成现有 7 列加 Consistency 5 列两张；每个 target 的用途句压到一行，长名不在正文重复解释 |
| 统计变成压制建议的开关 | 统计只改观察顺序；blocker 不受门控；`severity_floor`、预算、指纹不随统计变化；安全类 target 不降级 |
| 反馈误判成用户采纳 | 只记 `recurred` / `not_reobserved` / `no_followup`，不记 `acted` / `adopted`；`no_followup` 不作结论 |
| target 名称漂移 | 单一 `CONSISTENCY_TARGETS` 常量、解析器 `_check` 校验、规则正文与统计共用同一组名字 |
