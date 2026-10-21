# 会话一致性的五个并列建议目标（草稿）

状态：草稿，供评审（v5）。代码基线：`plugin/minir/` 当前 main（`9269854`）。

本稿把「会话历史是否与某个准绳一致」做成五个并列建议目标，直接进入 advice target 词表：

| 建议目标 | 准绳 | 判定类型 | 进修复轮 |
| --- | --- | --- | --- |
| `user_preferences_check` | `base.user_card` 的 `### Preferences` | `violated` / `temporary_override` / `stale_card` | 否 |
| `ai_self_representation_check` | `base.ai_self` | `false_claim` / `missing_work` / `scope_change` | 是 |
| `soul_tool_policy_check` | SOUL 的 `# Precedence`、`# Tool policy`、`# Boundaries` | `tool_policy_breach` | 否 |
| `soul_workflow_check` | SOUL 的 `# Workflow`、`# Verification & failure`、`# Delivery contract`、`# Critical` | `workflow_breach` | 否 |
| `soul_craft_check` | SOUL 的 `# Craft` | `craft_breach` | 否 |

五个目标互不包含、互不替代。`soul_tool_policy_check`、`soul_workflow_check`、`soul_craft_check` 是三个独立目标，不存在 `soul` 父目标；三个 soul 目标彼此并列，`user_preferences_check` 与 `ai_self_representation_check` 也各自独立。一条建议只写一个 target。

范围：

- AI 自我表示的会话绑定与召回报错契约。
- 五个并列建议目标的登记、解析与处置。
- 建议统计面，以及统计反馈对顾问观察的影响（§3.7）。
- 会话结束后由宿主启动的修复轮（只由 `ai_self_representation_check` 触发）。

非目标：`identity.md` / `preference.md` 的三天合并保持原样；修复轮不改自表示，不改历史 transcript；不按统计自动调整 severity、`severity_floor` 或建议预算；不引入新的顾问通道或审计表。

## 0. 术语

| 词 | 含义 | 位置 |
| --- | --- | --- |
| `ai_self` | AI 卡里的 Self Representation 一节，随会话行为漂移 | `layers.render_ai_card`（layers.py:421-426） |
| `ai_identity` | AI 卡里的 Identity 一节，稳定身份 | 同上 |
| `base.ai_card` | 整张 AI 卡的结论行，当前跨会话可召回 | `store._LONG_TERM_KINDS`（store.py:339） |
| `base.ai_self` | 拆出来的自表示结论，会话绑定 | 本稿新增 |
| `base.ai_identity` | 拆出来的身份结论，长期 | 本稿新增 |
| `base.user_card` | 用户卡结论行，含 `### Preferences` 一节 | 已有，长期 |
| `user_preferences_check` | target：会话与用户卡-用户偏好冲突 | 本稿新增 |
| `ai_self_representation_check` | target：会话与自表示不一致 | 本稿新增 |
| `soul_tool_policy_check` | target：会话违反 SOUL 的工具与授权约束 | 本稿新增 |
| `soul_workflow_check` | target：会话违反 SOUL 的流程与完成声明约束 | 本稿新增 |
| `soul_craft_check` | target：会话违反 SOUL 的 Craft 约束 | 本稿新增 |
| HERMES_SOUL | `$HERMES_HOME/SOUL.md`，operator 手写的角色与约束 | 已有，约 13 KB |
| 修复轮 | 会话结束后由宿主启动的一轮 agent 作业，补做声明了却没做的事 | 本稿新增 |

## 1. 现状 → 目标

| Surface | 现状 | 目标 |
| --- | --- | --- |
| AI 卡发布 | `## AI Card` 整节 → 一条 `base.ai_card`，scope=agent | 按 `### Self Representation` / `### Identity` 拆成 `base.ai_self`（session）与 `base.ai_identity`（agent） |
| 跨会话召回 | `base.ai_card` 在长期 kind 表内，`scope="all"` 可返回 | `base.ai_self` 不进长期 kind 表，`scope="all"` 永不返回 |
| 召回缺 id | 工具入口把入参与 provider 的 `_session_id` 做 or 回退，空值静默返回空结果 | 会话绑定 kind 缺主会话 id 时报错，错误回主会话，要求提供会话 id |
| 用户偏好核对 | 无。顾问摘要里有用户卡，但不逐条对账 | target `user_preferences_check`，走既有 advisories 通道 |
| 自我一致性判定 | 无。顾问只审当前动作，不读 AI 卡 | target `ai_self_representation_check` |
| 角色约束核对 | 无。顾问的 run 不加载 SOUL（`load_soul_identity` 默认 False） | 三个独立 target：`soul_tool_policy_check`、`soul_workflow_check`、`soul_craft_check`，各读一组约束节 |
| 建议的生成依据 | 只看当前 review 窗口内的即时事件，历史判定结果不参与 | 每次 review 注入统计块；统计反馈改变下一轮观察（§3.7） |
| 会话结束 | `on_session_end` → `_finish_session` → `adviser_mode.session_end`，只做过期清理（adviser_mode.py:1471） | 过期前钉住待修建议并排 `jobs(layer="repair")` |
| 修复 | 无 | 空闲窗口触发后由 worker 消费，进程内 agent loop，每会话一次 |

## 2. 召回侧：会话绑定与报错

### 2.1 发布拆节

`render_ai_card` 已经把两节渲染成 `### Self Representation` 与 `### Identity`（layers.py:421-426），artifact 解析处按 `## ` 切节、把整张 AI 卡映射成 `base.ai_card`。拆法不改正文，只改映射：

```
"## AI Card" → base.ai_self     scope=session
               base.ai_identity scope=agent
```

- `_ARTIFACT_SECTIONS` 增两条子节规则；解析器在 `## AI Card` 内再按 `### ` 切一次。
- `_LONG_TERM_KINDS` 只留 `base.ai_identity`（以及原有的 `base.user_card`）。
- 会话侧 kind 集合收 `base.ai_self`。
- 旧库迁移：已发布的 `base.ai_card` 行按子节一次性拆成两条；拆不开的行保持原样并标记 `superseded`，不阻塞启动。

### 2.2 召回规则

| 请求 | `base.ai_self` | `base.ai_identity` |
| --- | --- | --- |
| `scope="session"` + 有效 id | 返回该会话的行 | 返回 |
| `scope="session"` + 空 id | 报错 | 正常返回（不依赖会话） |
| `scope="all"` | 不返回，显式点名时报错 | 返回 |

「显式点名」指查询条件里只有会话绑定 kind。泛化查询（不限 kind）在 `scope="all"` 下静默排除，不报错，否则每个跨会话查询都会失败。

### 2.3 错误契约

错误在唯一读入口抛出，所有调用方继承：`store._search`。工具层只负责把异常转成 `tool_error`，不吞。

```
错误码：session_scope_required
错误文案：AI self representation is session-scoped. Provide the main session id
         (minir_search session_id or the provider session) to read it.
附带字段：kind、requested_scope、resolved_session_id
```

- 不回退到别的会话，不降级为空数组，不静默换成 `base.ai_identity`。
- 错误必须以工具结果的形式回到主会话，而不是只写日志。工具入口现在是入参与 `_session_id` 做 or 回退，会话绑定 kind 这条路要去掉回退。
- 五个 target 都要读会话历史；历史读同样要主会话 id，拿不到时顾问收到错误，再向主会话要 id。

## 3. 五个并列建议目标

### 3.0 并列关系与分工

五个目标各自独立；下表一行一个建议目标。三个 `soul_*` 是三个目标，不是一个 `soul` 目标下的三个 check。

| target | 准绳 | 判定类型 | 进修复轮 |
| --- | --- | --- | --- |
| `user_preferences_check` | `base.user_card` 的 `### Preferences` | `violated` / `temporary_override` / `stale_card` | 否 |
| `ai_self_representation_check` | `base.ai_self` | `false_claim` / `missing_work` / `scope_change` | 是 |
| `soul_tool_policy_check` | SOUL 的 `# Precedence`、`# Tool policy`、`# Boundaries` | `tool_policy_breach` | 否 |
| `soul_workflow_check` | SOUL 的 `# Workflow`、`# Verification & failure`、`# Delivery contract`、`# Critical` | `workflow_breach` | 否 |
| `soul_craft_check` | SOUL 的 `# Craft` | `craft_breach` | 否 |

并列规则：

- 五个 target 之间没有优先级，也没有替代关系。三个 `soul_*` 目标彼此并列，不共享父节点；停用其一不影响另外两个，也不影响前两个目标。
- 同一处观测只写一条建议。跨 target 的冲突各自成条；三个 `soul_*` 目标命中同一句话时，按 `soul_tool_policy_check` > `soul_workflow_check` > `soul_craft_check` 取第一个，不重复计数。
- target 就是 advice row 的 `target` 字段，没有独立的 check 字段。五个值直接进 `ADVICE_TARGETS`，解析、投递、统计都只认这一层。
- 只有 `ai_self_representation_check` 进修复轮；`user_preferences_check` 当场可改，三个 `soul_*` 的多数问题是边界，事后补不了。

共享规则：

| 项 | 规则 |
| --- | --- |
| 载体 | `advisories`（store.py:201），用已有的 `target` 列；不加列、不加表 |
| 投递 | 既有通道：中途块或回合边界，受 `severity_floor`（默认 concern）与投递门控约束 |
| 字段 | `target`，加上四项证据：`turn_uid`、准绳原句、实际观测（工具名与结果）、判定类型 |
| 缺证据 | 任一项缺失即不发。宁可不发，不做泛泛的「不一致」 |
| 顾问输入 | 会话历史（会话检索，需主会话 id）+ 准绳 + 统计块（§3.7） |
| 去重 | 沿用 advisories 的指纹逻辑，同一冲突不重复发；重复观测计入统计，不再投递 |

### 3.1 登记面

一处登记，多面同步（`prompting.py` 现有结构的设计意图）：

| 面 | 位置 | 改动 |
| --- | --- | --- |
| 元组 | `ADVICE_TARGETS` | 新增 `CONSISTENCY_TARGETS`（五个 target token），并入 `ADVICE_TARGETS` |
| 系统块 | `ADVICE_TARGET_GROUPS` | 增 `("Consistency targets:", CONSISTENCY_TARGETS)`，组内五条即出现在主 agent 的 Advisers 章 |
| 投递词表 | `adviser_mode.DELIVERED_TARGET_NAMES`（adviser_mode.py:46） | 改为直接取 `prompting.DELIVERED_TARGETS`；后者并入 `CONSISTENCY_TARGETS`。不并会让 `parse_review` 把五个一致性 target 降级成 `general` |
| 处置矩阵 | `ADVICE_HANDLING` + `advice_handling_section` | 五个 target 在 blocker / concern / nit 三档各一格；现有 7 列一张表不动，Consistency target 另起一张 5 列表，避免 12 列长表 |
| 顾问词表 | `advice_target_glossary()` | 自动同步；用途句写进元组 |
| 顾问指令 | `adviser_prompts.py` | 五段 target 指令，各自写准绳、判定类型、证据要求 |
| 解析 | `parse_review`（adviser_mode.py:313） | target 校验沿用 `DELIVERED_TARGET_NAMES`；以 `_check` 结尾但不在 `CONSISTENCY_TARGETS` 的值拒收并记 `bad_target`（防止拼错后降级成 `general`）；无 check 字段；同一 payload 的 `coverage` 状态图另行解析并写 review 行 |
| 投递渲染 | `render_block`（adviser_mode.py:370） | 标签显示 target 与 severity；长 token 由 `MAX_BLOCK_CHARS` 截断兜底 |
| 覆盖状态 | `adviser_reviews.coverage`（新列，JSON） | 五 target × `clean` / `found` / `n/a` |

五个 token 直接作为 target，不再拆 check：`soul_*` 三个是建议目标，不是 `soul` 的子检查。长 target 名会撑大投递标签，但不引入短名映射，避免同一目标出现两套词表；投递预算仍由 `render_block` 的截断处理。

### 3.2 `user_preferences_check`：会话 vs 用户卡-用户偏好

准绳：`base.user_card` 的 `### Preferences` 一节；卡里没有时退到全局 `user-card/preference.md`。

| 类型 | 含义 | 建议指向 |
| --- | --- | --- |
| `violated` | 行为或声明违背卡里写明的偏好 | 当场改，或停手确认 |
| `temporary_override` | 本次会话用户另一句话与卡冲突 | 问清是本次覆盖还是要改卡 |
| `stale_card` | 用户本会话表达了新偏好，卡里还没有 | 更新卡，不算违例 |

证据里的「准绳原句」= 卡里那一行原文。顾问现有摘要是用户卡 320 字截断，可能截掉偏好原句，所以 `user_preferences_check` 的建议必须来自完整读一次 `base.user_card`（用 minir 工具按 kind 读）。摘要不扩容、不改。

### 3.3 `ai_self_representation_check`：会话 vs AI 自我表示

准绳：`base.ai_self` 的当前文本。

| 类型 | 含义 |
| --- | --- |
| `false_claim` | 声明了没做的事（「已验证」但没跑） |
| `missing_work` | 自表示承诺的流程没走（「先给结论」但埋在第三段） |
| `scope_change` | 自表示说的范围与实际做的范围不一致 |

这是唯一进修复轮的 target。修复只改行为与产物，不改 `base.ai_self`；自表示的更新仍由发布链路负责。

### 3.4 三个 `soul_*` 建议目标：会话 vs HERMES_SOUL 约束

三个目标各自独立，各取一组 SOUL 节，互不重叠：

| target | 节 | 字节 | 判定类型 | 严重度上限 |
| --- | --- | --- | --- | --- |
| `soul_tool_policy_check` | `# Precedence`、`# Tool policy`、`# Boundaries` | 1,893 | `tool_policy_breach` | blocker（Precedence / Boundaries）、concern（Tool policy 程序性） |
| `soul_workflow_check` | `# Workflow`、`# Verification & failure`、`# Delivery contract`、`# Critical` | 2,629 | `workflow_breach` | blocker（Delivery contract / Critical）、concern（Workflow / Verification） |
| `soul_craft_check` | `# Craft` | 1,537 | `craft_breach` | nit（表面）、concern（代码经济） |

三个目标合计 6,059 字节，约为整份 SOUL 的一半。

判据：

- 必须能引用 SOUL 的原句；引不出原句的，不发。
- `# Precedence` 管的是「谁的话算授权」：把工具输出、记忆或网页当指令，判 `tool_policy_breach`。
- `# Boundaries`、`# Critical` 与 `# Delivery contract` 的硬约束不可被用户当场指令覆盖，命中即 blocker；Tool policy 的程序性要求、Workflow 的步骤顺序、Craft 的表面偏好可在 operator 明确指令下让路，不判违例。
- `# Delivery contract` 的「未验证就收尾」「缩范围未说明」「留 stub」判 blocker；`# Workflow` 的跳步、不清理、不验证判 concern，两者都归 `soul_workflow_check`。
- `# Craft` 的表面禁令判 nit，代码经济（重写 stdlib、给一次性任务加防御层）判 concern；不把「读起来像生成的」当证据，必须引原句。
- 同一句话只归属一个目标。三个 `soul_*` 命中同一句话时按 `soul_tool_policy_check` > `soul_workflow_check` > `soul_craft_check` 取第一个；它们没有共同的 `soul` 父目标，各自独立成条。

不进判定的节：`# Role`、`# Character`、`# Priorities`、`# Runtime`、`# Delegation & Kanban`、`# Memory & continuity`、`# Communication`、`# Avoid`。前七节是身份、能力与风格叙述，不是可引用原句的硬约束；`# Avoid` 是其他节的摘要，直接引源节，避免同一句话被两次计数。

读取与成本：

- 只读 `$HERMES_HOME/SOUL.md`，按 mtime 缓存解析结果。
- 按启用的 target 拼接对应节，只注入这 6 KB，不整份注入，也不用 `load_soul_identity=True`（那是 13 KB 全文，且会改写顾问自身的身份）。
- 文件缺失或读不出时，三个 `soul_*` target 本轮一起停用，不产生判定；在状态里记一条 `soul_unavailable`。

### 3.5 处置

| 等级 | `user_preferences_check` | `ai_self_representation_check` | `soul_tool_policy_check` | `soul_workflow_check` | `soul_craft_check` |
| --- | --- | --- | --- | --- | --- |
| blocker | 安全或流程类偏好被违背且动作已发出或不可逆：停手，报告，不继续 | 与自表示冲突的完成声明即将发出或已发出：不发送，当场补验证；进修复轮 | Precedence / Boundaries 命中：立即停手，报告，不复述敏感内容 | Delivery contract / Critical 命中：立即停手，报告，不复述敏感内容 | 无 blocker，表面类最高 nit |
| concern | 表达型偏好被违背（语言、结构、长度、顺序）：当场改 | 行为偏离自表示但未形成声明：当场校准；进修复轮 | Tool policy 程序性：当场改 | Workflow / Verification 跳步：当场改 | 代码经济：当场改 |
| nit | 措辞层面的偏差：可选 | 措辞层面的偏差：可选 | 无独立 nit，程序性最低 concern | 无独立 nit，步骤叙述最低 concern | Craft 表面类：可选，不打断任务 |

投递走既有通道：入 `advisories`，按 `severity_floor` 与投递门控决定中途出现还是边界出现。

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
| guard 丢弃 | `adviser_reviews.drop_detail`，每条增 `target` | `duplicate`（重复观测）、`rate-limit`、`noise` 按 target 归因 |
| 修复结果 | `jobs(layer="repair")` 的终态 | `fixed` / `cannot_fix` / `unresolved` |
| 投递时间 | `advisories.delivered_at` | 划定反馈窗口 |

`adviser_reviews.coverage` 的值只允许三个：`clean`、`found`、`n/a`。missing 记 `unknown`，不计入 clean 率；状态图解析失败也记 `unknown`，不影响建议投递。

#### 3.7.2 顾问看到的统计块

每次 review 的 prompt 增一段有界统计，只统计本会话最近 N 次 review（默认 8），只给计数，不给正文。下例的数值仅示意格式：

```
TARGET STATS (last 8 reviews)
target                        reviews  clean  found  n/a  adv  deliv  recur  follow-up
user_preferences_check              8      6      2    0    2      2      0  not_reobserved
ai_self_representation_check        8      7      1    0    1      0      1  repair: unresolved
soul_tool_policy_check              8      8      0    0    0      0      0  -
soul_workflow_check                 8      7      1    0    0      0      0  -
soul_craft_check                    8      6      2    0    2      2      1  recurred
```

- `adv` 是入库建议数，`deliv` 是 `delivered_at` 非空的数量。
- `recur` 是投递后同指纹再次被观测到的次数（guard 的 `duplicate` 丢弃）。
- `follow-up` 每行只取一个值，按 `recurred` > `repair: unresolved` > `not_reobserved` > `no_followup` 取最靠前的一个；修复类只在有修复 job 时出现。
- 不写 `acted`、`adopted` 或 `ignored`：插件看不到用户是否采纳，只能看到该问题是否被再次观测。

#### 3.7.3 反馈如何改变观察

- `recurred` 排最前：下一轮先核对上一次证据点名的文件或命令，确认旧问题是否仍在；这是复核，不是新发现，不再投递同一条。
- `repair: unresolved` 次之：`ai_self_representation_check` 优先复核同一处，不自动开第二轮修复。
- `not_reobserved` 降低扫描优先级：该 target 退到低优先位，只有出现新的准绳变化（用户卡写入、SOUL mtime 变化、`base.ai_self` 发布）或新的证据点时才重新前置。
- `no_followup` 不构成任何结论：只表示还没有下一次 review，不当作已解决。
- 安全类不受降级影响：`soul_tool_policy_check` 与 `soul_workflow_check` 始终前置，`blocker` 不走统计门控，也不因历史 clean 而少看。
- 统计不改判定：`severity`、`severity_floor`、`max_advices`、指纹规则都不由统计调整；统计只改观察顺序与观察窗口。
- 统计不得作为压制建议的理由。任何「历史上这类问题很少」都不能阻止一条证据齐全的 blocker。

#### 3.7.4 成本与边界

- 每次 review 一次聚合查询，行数按启用的 target 固定（默认 5 行），不随会话长度增长。
- 统计块在 review 开始时生成一次，review 内不刷新。
- 只统计本会话；跨会话聚合只出现在 `minir_stats` 给 operator 看，不进顾问 prompt。
- 统计只含计数与 target 名，不含 transcript、建议正文或证据原文。

## 4. 会话结束后的修复轮

### 4.1 触发链

```
on_session_end(session)            __init__.py:819
  → _finish_session(session)       __init__.py:837
      → adviser_mode.session_end(session)
          1. 冻结：本会话 target='ai_self_representation_check'、severity in (blocker, concern)、
             未被消费的行改成 status='repair'，expires_at 推到远期
          2. 其余行照旧 expire_adviser_advices（adviser_mode.py:1471）
          3. 有冻结行时排一个 jobs(layer='repair', session_id, from_ts, to_ts)
      → 空闲窗口 arm（__init__.py:243-256 的既有语义）
          · 窗口内用户开口 → 取消 arm，job 留在队列
          · 定时器触发 → worker 认领 repair job
```

其余四个 target 不进这条链：`user_preferences_check` 当场可改，三个 `soul_*` 多数是边界问题，事后补不了。要并入就把第 1 步的过滤条件改成 `target IN ('ai_self_representation_check','user_preferences_check','soul_tool_policy_check','soul_workflow_check','soul_craft_check')`，一处改动。

target 就是判定单元，冻结条件只用 `ai_self_representation_check`，不存在 target/check 两套值。

不在 `on_session_end` 里同步跑 agent loop：那会阻塞会话拆除，而且在用户可能立刻回来的时候写盘。空闲窗口是宿主已有的「人不在键盘前」信号，复用它。

### 4.2 作业形状

| 项 | 取值 |
| --- | --- |
| 队列 | 复用 `jobs` 表（store.py:84），`layer='repair'`；不新建 repair 表 |
| 输入 | 冻结的建议行（含 evidence JSON）、会话 id、工作目录、本会话变更过的文件清单 |
| 执行 | `agent_loop.run`，toolsets = `agent_toolsets()`（file / minir / adviser），不是新会话、不是子进程 |
| 预算 | `max_iterations` 显式（建议 24）；wall clock 10 分钟；超预算即 `unresolved` |
| 产物 | 每条建议写回终态：`fixed` / `cannot_fix` + 原因 |
| 重试 | 不自动重试。`unresolved` 是终态，留一条 note 说明缺什么 |

### 4.3 禁令

修复轮 prompt 里写死，且由工具面兜底：

- 只碰证据点名的文件及其直接依赖；越界路径由 file guard 拦。
- 不 commit、不 push、不改卡文件、不改 `.env` 或凭据。
- 不改 `ai_self`（按你定的口径，修行为不修自表示）。
- 不安装依赖、不联网、不删数据。
- 允许跑验证类命令（pytest / ruff / tsc / mypy 等）；其它命令拒绝。

### 4.4 幂等

- 每会话最多一个 repair job，键为 `(session_id, layer='repair')`，重复入队即忽略。
- 修复轮不产生新的建议：判定只读会话 transcript，修复轮没有 transcript。
- 进程重启后 job 仍在 `jobs` 表，状态 `pending`，由 worker 正常认领；不会因为重启丢判定。

## 5. 改动清单

| # | 文件 | 改动 |
| --- | --- | --- |
| 1 | `store.py` | 子节解析：`## AI Card` 拆 `base.ai_self` / `base.ai_identity`；长期 kind 表去 `ai_card`；会话 kind 集合收 `ai_self`；迁移旧行 |
| 2 | `store.py` | `_search` 加会话绑定校验，抛 `session_scope_required`；`scope="all"` 过滤会话 kind |
| 3 | `__init__.py` | 工具入口去掉会话绑定 kind 的 session 回退，异常转 `tool_error` |
| 4 | `prompting.py` | 新增 `CONSISTENCY_TARGETS` 五 token；并入 `ADVICE_TARGETS`、`DELIVERED_TARGETS`、`ADVICE_TARGET_GROUPS` 与 `ADVICE_HANDLING`；`advice_handling_section` 为 Consistency 另起一张表，现有 7 列不动 |
| 5 | `adviser_prompts.py` | 五段 target 指令；JSON 合同增 `coverage` 状态图；四项证据要求；`stale_card` 与 `temporary_override` 的区分；SOUL 不可覆盖的边界 |
| 5a | `adviser_mode.py` | `DELIVERED_TARGET_NAMES` 改为引用 `prompting.DELIVERED_TARGETS`，去掉本地重组的 `ACTION_TARGETS + ARTIFACT_TARGETS` |
| 6 | `soul_rules.py`（新） | 读 `$HERMES_HOME/SOUL.md`，按三个 `soul_*` target 的节映射截取八节，mtime 缓存，返回 `(sections, sha)`；读不出返回 None |
| 7 | `adviser_mode.py` | `parse_review` 对 `_check` 后缀但不在 `CONSISTENCY_TARGETS` 的 target 拒收并记 `bad_target`；同一 payload 的 `coverage` 状态图解析后写 `adviser_reviews`；`_prompt` 注入统计块；`session_end` 冻结 `target='ai_self_representation_check'`；主会话 id 透传 |
| 8 | `worker.py` | repair job 消费：预算、禁令 prompt、终态写回 |
| 9 | `__init__.py` | `_finish_session` 与空闲窗口衔接：取消 arm 时 job 保留 |
| 10 | `store.py` | `adviser_reviews.coverage` 一列；`review_stats(session_id, window)` 按 target 聚合（不含正文）；`advisories.target` 已有，不加列 |
| 11 | `adviser_guard.py` | `_drop` 的 detail 带 `target`，让 `duplicate` / `rate-limit` 可按 target 归因 |
| 12 | `__init__.py` | `minir_stats` 输出增 `targets` 段（review 覆盖、投递、重复观测、修复终态） |

## 6. 测试

| 场景 | 断言 |
| --- | --- |
| 缺 session id 读 `base.ai_self` | 抛 `session_scope_required`，文案含 main session id，不返回空 |
| `scope="all"` 泛化查询 | 结果里没有 `base.ai_self`，也不报错 |
| 显式点名 `base.ai_self` + `scope="all"` | 报错 |
| 发布拆节 | 同一 session 产出 `base.ai_self`（session）与 `base.ai_identity`（agent）两行，正文与旧 `base.ai_card` 一致 |
| 五 target 并列 | 同一 session 可同时产出五个 target 的建议；停用其一不影响另外四个 |
| 词汇贯通 | 系统块、顾问词表、解析器三处都认五个 target token |
| soul 三 target 独立 | 一条 finding 只写 `soul_tool_policy_check` / `soul_workflow_check` / `soul_craft_check` 之一；不存在 `soul` 父 target |
| target 白名单 | 以 `_check` 结尾但不在 `CONSISTENCY_TARGETS` 的 target 被拒收，记 `bad_target`，review 不失败 |
| target 去重 | 同一句 SOUL 命中三个 soul target 时只产出一条，取顺序最靠前的 target |
| 建议证据 | 缺 target 或四项证据之一时解析器拒收 |
| `user_preferences_check` 准绳 | `preference_clause` 能在 `base.user_card` 里逐字找到 |
| `stale_card` / `temporary_override` | 前者发「更新卡」，后者问用户；都不判违例 |
| soul 读取 | 只取三个 target 的八节；文件缺失时三个 target 停用并记 `soul_unavailable`，不产生判定 |
| soul 原句 | 引不出 SOUL 原句的建议被拒收 |
| soul 节归属 | `# Boundaries` 只进 `soul_tool_policy_check`，`# Delivery contract` 只进 `soul_workflow_check`，`# Craft` 只进 `soul_craft_check` |
| 用户覆盖 SOUL 叙述 | 用户当场指令优先，不判 `workflow_breach`；`# Boundaries` / `# Critical` / `# Delivery contract` 例外 |
| 统计块 | 第 N 次 review 的统计块与库里 review/advisories/drop_detail 的计数一致，行数等于启用的 target 数（默认 5） |
| 重复观测 | 已投递建议的同指纹再现计入 `recur`，不再投递，也不改原建议状态 |
| 反馈优先级 | `recurred` / `repair: unresolved` 的 target 在 prompt 中排在 clean 之前 |
| 统计不越权 | 统计块不改变 `severity_floor`、`max_advices` 或 blocker 投递 |
| 会话结束 | 有 `ai_self_representation_check` 待修建议 → 一个 repair job；建议 status='repair' 后不被过期清掉 |
| 其余四个 target 违例 | 不产生 repair job |
| 重复会话结束 | 仍只有一个 repair job |
| 修复轮失败 | job `unresolved`，不重试，不产生第二轮 |
| 越界写 | file guard 拒绝，job 记 `unresolved` + 原因 |

对应测试文件：`tests/test_adviser_mode.py`、`test_adviser_channels.py`、`test_card_review.py`、`test_layers.py`（若存在），新增 `test_soul_rules.py`、`test_self_repair.py`、`test_review_stats.py`。

## 7. 配置

| 键 | 默认 | 用途 |
| --- | --- | --- |
| `soul_targets_enabled` | true | 关掉后不注入 SOUL 约束节，三个 `soul_*` target 一起停用 |
| `review_stats_enabled` | true | 关掉后 review prompt 不注入统计块，判定照旧 |
| `review_stats_window` | 8 | 统计块回看的 review 次数，按会话 |
| `self_repair_enabled` | true | 关掉后只发建议，不排修复轮 |
| `self_repair_max_iterations` | 24 | 修复轮工具调用预算 |
| `self_repair_timeout_seconds` | 600 | 墙钟上限 |
| `self_repair_daily_limit` | 3 | 全局每日上限，超出只落建议不起轮 |
| `self_repair_allow_commands` | true | 允许验证类命令；关掉则只读改文件 |

## 8. 未决

1. `coverage` 状态图是否强制返回。本稿取可缺省，missing 记 `unknown`、不计入 clean 率；强制会让弱模型多一次失败面。
2. 统计窗口按会话还是按顾问跨会话。本稿按会话，跨会话只进 `minir_stats`；若要做跨会话的 target 画像，需要另定会话边界。
3. 五个 target 名较长，投递标签是否要短名映射。本稿取不映射，靠 `render_block` 截断；短名会引入第二套词表。
4. `base.ai_self` 是否要在产生它的会话内每轮注入，还是只在顾问与修复轮里读。
5. 修复轮对「声明不成立但工作已完成」的处理：只记 note，还是同时改产物里的措辞。
6. 旧 `base.ai_card` 行的迁移：一次性拆完，还是惰性拆。

## 9. 风险

| 风险 | 缓解 |
| --- | --- |
| 无人值守写盘 | 路径白名单、验证命令白名单、无 git 写、无网络、每会话一次、失败即停 |
| 修复轮与新会话并发编辑 | 只在空闲窗口跑；用户开口即取消 arm |
| 顾问整会话对账的 token 成本 | 只在该轮判定需要时读会话检索，且限定窗口；SOUL 只注入 6 KB 约束节；统计块按启用的 target 固定行数 |
| SOUL 变成挑措辞的教条 | 只有能引用原句、且落在八节之内的才判；表面类最高 nit |
| 偏好判定变成挑格式毛病 | `violated` 必须能引用卡里原句；表达型偏好只到 concern |
| 自表示漂移无人修 | `ai_self_representation_check` 只在冲突时发；修复轮失败留 note，下一次同类判定再触发 |
| 五个 target 互相顶替 | 一条建议只写一个 target；并列与停用的测试按 target 隔离 |
| Advisers 章变长 | 处置表拆成现有 7 列加 Consistency 5 列两张；每个 target 的用途句压到一行，长名不在正文重复解释 |
| 统计变成压制建议的开关 | 统计只改观察顺序；blocker 不受门控；`severity_floor`、预算、指纹不随统计变化；安全类 target 不降级 |
| 反馈误判成用户采纳 | 只记 `recurred` / `not_reobserved` / `no_followup`，不记 `acted` / `adopted`；`no_followup` 不作结论 |
| target 名称漂移 | 单一 `CONSISTENCY_TARGETS` 常量、解析器 `_check` 校验、五段指令与统计共用同一组名字 |
