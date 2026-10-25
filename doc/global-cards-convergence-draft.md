# 全局卡片语义与收敛草稿

状态：B1-B6 已实现（2026-10-25）；四个全局卡文件已按本稿语义播种（见 §8）。

依据：HEAD `30a4b09` 的只读核对；工作树 3 个未提交修改（`rules/markers-category.md`、`rules/markers-org.md`、`tests/test_session_finalisation.py`）；`memory.db`（本地 2026-10-25 01:49）。行号以本次核对为准。

## 0. 一句话

用户卡是"跨会话仍成立的人 + 对他的稳定承诺"；AI 卡是"跨会话仍成立的我 + 行为边界"。当前实现的结构朝这个方向，运行路径却把它变成"上一会话的 base 行 + manifest 摘要"：断在输入、生命周期、消费者三处。

## 1. 语义

### 1.1 用户卡

| 文件 | 回答什么 | 该写 | 不该写 |
| --- | --- | --- | --- |
| `user/identity.md` | 我在跟谁说话 | 领域、技能深度、思考习惯、协作方式、优化目标 | 当前任务、项目状态、会过期的计数 |
| `user/preference.md` | 他默认要我怎么干 | 语言、答案形状、方法、工具链、协作、禁忌 | 只在一轮里成立的场景约束 |

判据三条：换个项目仍成立；能改变下一次动作；不随版本或计数过期。

### 1.2 AI 卡

| 文件 | 回答什么 | 该写 | 不该写 |
| --- | --- | --- | --- |
| `agents/<profile>/identity.md` | 我是谁、我被授权做什么 | 身份、职责、声音、边界 | 当前任务/状态；版本与生命周期清单 |
| `agents/<profile>/preference.md` | 我被要求怎样工作 | 阅读与检索、推进、验证与报告 | 权限；工具清单；会话观察 |

AI 卡的价值在"上下文丢失后仍能约束下一次回答"，不在复述 manifest。当前 `ai_identity` 的 Deployment 段（minir 0.17.0、生命周期、工具）属于部署事实，版本一变就过期，且 live prompt 已经描述了工具。

判据同样是三条：换会话仍成立；能改变下一次动作；不随部署漂移。

### 1.3 卡与其它层

- 卡片是索引，不是转录。
- 当前任务与下一步属于 `window_summary`。
- 项目事实、命令、路径属于 knowledge 页。
- 结论与边属于 conclusions 表。
- 四者不互相代写。

## 2. 五天证据

### 2.1 数据边界

- 10-20 至 10-24：44 条 base 刷新、约 43 个会话、395 条 advisories。
- 10-23 00:39 至 10-25 01:35：32 个有用户输入的 host 会话、178 条用户输入。只有这段有 transcript。
- 10-23 之前只剩 conclusions 与 knowledge 页；更早的细节无法复原。

### 2.2 该进用户卡的信号（至少两次）

| 信号 | 证据 | 归属 |
| --- | --- | --- |
| 草稿先于实现，草稿轮不落代码 | 10-23 18:02「还回滚你改的代码」；21:49「回退 为什么不写草稿」 | preference |
| 一句话授权即全程执行 | 「提交」「部署插件」「继续」 | preference |
| 不相关的变化单独提交 | 10-23 19:33「提交并推送」；既有用户卡已有「不相关的变化作为单独的提交落地」 | preference |
| 证据优先于速度与观感 | SOUL `# Priorities` 第 3 条（operator 声明） | preference |
| token 预算意识 | 10-23 16:01「注意测试长度，避免token膨胀」 | preference |
| 契约先行，先修术语 | 10-23 11:44 objective 语义；10-25 00:26 value markers 分类 | identity |
| 拒为校验而校验 | 10-23 11:44「删除正在编辑的草稿对于md校验的设定」 | identity |
| 跨会话记忆可用性 | 10-24 23:50「上一个会话聊的内容，下一个会话找不到相关记忆」 | identity |
| 领域跨度 | 插件约 18 个会话；PKI/OCSP；BGS/Skyrim；OMP/Hermes UI；Windows 环境 | identity |

### 2.3 该进 AI 卡的信号

- 5 天 395 条 advisories。target 分布：`current_step` 198、`output_surface` 76、`tool_choice` 74、`user_intent` 58、`final_answer` 42、`code_economy` 27、`soul_tool_policy_check` 24、`soul_workflow_check` 4。
- 典型条目：`head-40` 截断输出、`find`/`ls`/`head` 代替 search_files/read_file、把未验证说成已验证、草稿轮落地代码、报告里编造不存在的测试文件名（10-23 15:13，adviser 指出 `test_adviser_row_capture.py` 不存在而实际是 `test_adviser_row_callback.py`）。
- 这些条目证明"准则值得前置"，但不新增准则。卡片只写 operator 已声明的文本（SOUL、dispatch）；advisories 是佐证，不是授权。

### 2.4 该排除的内容

- 版本号、插件生命周期、工具清单：manifest 会漂移。
- 当前任务、active 状态、未验证结论：属于 `ai_self` 或 window summary。
- 项目计数：曾写进用户卡的「290 个有效 SKSE 插件、273 个 NG」会随 modlist 变化。
- 一次性的任务目标：JEV、OCSP 部署、C 盘清理都只属于对应窗口。

## 3. 现状核对

### 3.1 播种前

- `minir-memory/user/` 与 `minir-memory/agents/` 不存在；`card_revisions` 0 行。
- `layer_state` 的 `base/user:default` 共 4128 字符：用户卡 1687（33 行，`user_self` 985 + `user_pref` 658），AI 卡 2206（26 行，`ai_self` 722 + `ai_identity` 1443），Window Summary 与 Value Markers 为空。
- `ai_self` = "idle / no active goal"。来源是 row 71（session `20261024_115417_6a00c5`，窗口 12:01-12:14）的 210 字符模板摘要；该会话继续到 15:09，全会话 job 为 2 done、5 failed、5 pending。
- base jobs：105 done、52 failed、15 pending、3 running（插件 10-23 安装以来的全量）。
- review anchor：`last_success_at` = 2026-10-23 00:40:04（安装时刻），`next_attempt_at` = 2026-10-26 00:40:04。

### 3.2 播种后（本次）

- 四个文件由 `card_review.write_cards` 写出，revision ids 1-4，`source=review`、`reviewer=masia`。
- 文件 token：user identity 345、user preference 367、AI 两文件合计 534；均低于 2000。
- `load_global_cards` 返回三个键：`user_self_representation` 527 字符、`user_preferences` 446 字符、`ai_identity` 637 字符（identity 与 preference 已合并）。
- `_preference_prompt_block` 产出 578 字符，从下一个会话起进入 system prompt。本会话的 system prompt 已构建，看不到它。
- 新会话的 base 刷新会走 `source="global"`（`worker.py:1261` 的代码路径；`source` 只进 prompt，不落库）；首窗口的预设基线也会带上全局卡（`worker.py:1341-1344`）。

### 3.3 关键路径

| 行为 | 锚点 |
| --- | --- |
| 跨会话继承最新 base 行 | `store.py:1524` |
| base 读全局卡并选 source | `worker.py:1248-1264` |
| 新会话不接上一会话的窗口摘要 | `worker.py:1265-1270` |
| 预设基线使用全局卡 | `worker.py:1332-1363` |
| AI 卡输入来自两个 operator 文件 | `history.py:274-293` |
| layer pass 不加载 SOUL | `worker.py:876-877`，`agent_loop.py:288-291` |
| review 写四个文件 | `card_review.py:234-278` |
| review 生命周期 | `card_review.py:499-558` |
| 全局卡装载 | `card_review.py:200-217` |
| 偏好注入 system prompt | `__init__.py:558-579` |
| prefetch 只注入 ai_self + window + todo | `__init__.py:655-701` |

## 4. 断点

### B1 全局层从未落盘，anchor 却记成功

- 现象：`card_revisions` 0 行、四个文件不存在，而 `card_review` 的 `last_success_at` 已是安装时刻。
- 锚点：`ensure_anchor`（`card_review.py:520-527`）把安装时间写成 `last_success_at`；`due`（529-535）只在有 state 时看 `next_attempt_at`。
- 根因：锚点的语义是"已复核一次"，实现却把它当"已注册"。新 home 即使一张卡都没有，也要等满三天。
- 影响：`source` 永远是 `session` 或 `preset`；"全局"只等于上一会话的 base 行（`store.py:1524` 的注释自己承认）。
- 已缓解：本次手工播种让文件存在，新会话立即走 `source="global"`。这是绕过，不是修复。

### B2 AI 卡的输入自证，SOUL 不在场

- 现象：`ai_identity` 1443 字符里 Deployment 段是 manifest 转写；唯一有价值的 "Executor..." 行按 `rules/ai-identity.md` 的 GROUNDING 没有来源。
- 锚点：`history.identity_paths`（`history.py:274`）把 `agents/<profile>/{identity,preference}.md` 当 operator 文件；`worker.py:501` 的 `_base_ai_prompt` 把它们标成 "Operator-authored ... (trusted)"；三个调用点 `worker.py:979`、`1315`、`1494-1495` 都传 `self._identity`；`worker.py:876-877` 关闭 `load_soul_identity`。
- 根因：review 的产物就是这两个文件。下一轮 B2 把 review 的结论当 operator 声明，形成闭环。真正的 operator 声明（SOUL + dispatch）没有进入 B2。
- 次生问题：`history.load_identity` 与 `card_review.build_prompt`（`card_review.py:356-364`）都把 front matter 原样传入，`source: review` 会作为正文的一部分出现。

### B3 ai_preference 没有自己的规则

- 现象：四个全局文件里，AI preference 只能为空或复制 AI identity。
- 锚点：`CARD_SCOPE_RULES["ai_preference"] = ("ai-identity",)`（`card_review.py:80-85`）；`rules/ai-identity.md` 的 BOUNDARIES 禁止写 operator 未声明的权限、能力、禁止项。
- 根因：AI 侧只定义了一个规则文件，preference 借用 identity 的边界。identity 管"我是谁"，preference 管"我被要求怎样工作"，两者不是同一种内容。
- 影响：`agents/default/preference.md` 结构性空转，四张卡实际只有三张在工作。

### B4 用户身份卡没有消费者

- 现象：`user/identity.md` 只被卡片 pass 自己读；主 agent 看不到它。
- 锚点：`_preference_prompt_block` 只读 `USER_PREFERENCE`（`__init__.py:558-579`）；`prefetch` 只拼 `ai_self`、`window_summary`、todo（`__init__.py:655-701`）。
- 根因：注入面按"能不能改变下一次动作"设计，身份卡被当成只给 review 看的基线。
- 影响：即使写出完美的全局用户身份，也不会改变下一个会话的行为，只会影响下一次写卡。这是"写而不读"。

### B5 状态字段随 B3 塌陷，job 失败切断裂

- 现象：空摘要推出 "idle"；会话 6a00c5 的 10 个 job 里 5 failed、5 pending，卡片停在 12:14。
- 锚点：`validate_summary_pass` / `summary_template` 接受只有标题的模板（row 71，210 字符）；B2 的 `ai-self` 规则没有"无证据时写 unknown"的约束；`_MAX_ATTEMPTS = 2`（`worker.py:85`），失败窗口是否被下一窗口覆盖未验证。
- 影响：`ai_self` 会在一段活跃工作后显示 idle；base 工件里的 Window Summary 与 Value Markers 为空。
- 用户已提出要求（10-24 23:50）：「base 窗口失败要在下一会话的记忆块里留 stale 信号，现在是静默为空，看起来像'从来没聊过'。」
- 未验证：52 个 failed job 是否导致证据被跳过。需要一次窗口覆盖测试，不能凭计数下结论。

### B6 没有周期剪枝，计数与项目状态混入

- 现象：用户卡含 modlist 计数；AI 卡含版本与生命周期；两者都没有周期剪枝。
- 锚点：B1 只做 merge（`worker.py:1272-1275`）；剪枝只在 review 的规则文本里（`rules/user-self.md` REVIEW CHECKS、`rules/ai-identity.md` REVIEW CHECKS），而 review 从未运行。
- 影响：卡片逐步变成项目简报，预算被过期事实占用。
- 注：用户卡曾在约 24 小时内从 1087 涨到 2292 字符（10-23 00:55 到 10-24 01:30），10-24 切中文后压到 985（内容相同）。增长是累积，不是幻觉；语言切换掩盖了体积问题。

## 5. 改动方案

### 5.1 B1：让 review 的生命周期与"文件存在"对齐

改：

- `CardReviewScheduler.ensure_anchor` 只写 `started_at`、`attempts`、`next_attempt_at`；不写 `last_success_at`。
- `due()`：四文件任一缺失 → True；否则按 `next_attempt_at`。
- `tick()` 的 `since`：有 `last_success_at` 用它，否则用 `started_at`。
- 不迁移历史 state：文件缺失检查已经覆盖"安装后从未复核"的存量。

契约反转：`test_the_scheduler_waits_three_days_and_advances_only_on_success`（`tests/test_card_review.py:262-297`）锁定 `ensure_anchor` 返回 `last_success_at = now`、1 天后 `due` 为 False。要改成"文件缺失时立即可跑；成功复核后才等三天"。

测试：

- `test_a_missing_card_runs_the_first_review_immediately`
- `test_a_successful_review_starts_the_three_day_period`
- 存量 home 的回归：`card_review` state 只有安装锚点、文件缺失 → `due` 为 True。

回滚：只动 `card_review.py` 的调度段与对应测试。

### 5.2 B2：把 operator 声明、review 结论、manifest 分开

改：

- `worker.py` 新增 `soul_identity` 读取（`HERMES_HOME/SOUL.md`，上限 4000 字符；缺失为空），传给 `_base_ai_prompt`。
- `_base_ai_prompt`（`worker.py:501`）改成三段带来源的输入：
  - `## Operator declarations (SOUL, trusted)`：可覆盖卡片。
  - `## Prior review card (review rank, may be revised)`：不是 operator。
  - `## Manifest (facts)`：只陈述部署事实。
- `history.load_identity`（`history.py:281`，调用点 `__init__.py:404`）返回 `(meta, body)`，正文去掉 front matter；`card_review.build_prompt` 也只传正文。
- `_refresh_identity_only`（`worker.py:1309-1318`）与 dialectic（`worker.py:1494-1495`）同步改调用。

契约反转：B2 现在把两个 operator 文件标成 trusted。改动后，review 产物降为 review 级。

测试：

- `test_the_ai_card_prompt_separates_soul_review_and_manifest`
- `test_front_matter_never_reaches_the_identity_input`
- `test_a_review_sourced_card_is_not_labelled_operator`

回滚：`worker.py`、`history.py`、`card_review.py` 的读取段；不影响存储。

### 5.3 B3：给 ai_preference 一份自己的规则

改：

- 新增 `plugin/minir/rules/ai-preferences.md`：`id: ai-preferences`，`applies_to: [base:B2, dialectic, card_review]`。
- CONTRACT 写"被要求的工作方式"：阅读与检索、推进、验证与报告；BOUNDARIES 仍禁止新增权限、工具清单、会话观察。
- `CARD_SCOPE_RULES["ai_preference"] = ("ai-preferences",)`（`card_review.py:80-85`）。

契约反转：`test_each_review_file_has_its_own_agent_rule_and_output`（`tests/test_card_review.py:149-169`）现在断言 `ai_preference` 拿到 ai-identity 的规则文本。要改成它自己的规则。

测试：

- `test_ai_preference_reads_its_own_rule`
- `test_the_two_ai_rules_have_distinct_contracts`

回滚：删规则文件，恢复映射；已写出的 AI preference 卡保留。

### 5.4 B4：让用户身份进入下一会话

改：

- `_preference_prompt_block` 改成 `_cards_prompt_block`：读 `USER_IDENTITY` 与 `USER_PREFERENCE`，分别输出 `# User Identity`、`# User Preferences`。
- 预算：身份 ≤1200 字符（当前 527），偏好沿用 4000（`_MAX_PREFERENCE_CHARS`）。
- `system_prompt_block`（`__init__.py:554-556`）换调用。
- 每会话构建一次，行为不变（`__init__.py:552-553`）。

测试：

- `test_system_prompt_injects_the_user_identity_card`
- `test_an_empty_identity_card_adds_no_block`
- `test_both_cards_empty_returns_empty_block`

回滚：函数名与调用点；两个文件不动。

### 5.5 B5：空摘要不许推出 idle

改：

- `summary_template` 增加"全空模板"判定：有标题、无正文 → 无效。`validate_summary_pass` 调用它。
- `rules/ai-self.md` 增加一条：窗口摘要没有 objective/current_state 时写 `unknown`，不写 `idle`，不猜。
- `prefetch` 的 layer block 在 base 窗口失败时注入一行 `stale: 上一个窗口未摘要`（带 job 时间），不再静默为空。这是 10-24 23:50 已提出的要求。
  - 落点：base job 最终失败时写 `layer_failures`（marker 稿 §1.3）一行（subject、layer=base、session、window_to、error）；prefetch 的 `## Minir memory gaps` 块按 subject 渲染，新 base 行成功后清掉。不新建 `base_stale`。
- 失败窗口的覆盖行为先测后改：如果 `since` 不回退，failed 窗口的证据会丢；如果回退，只需要重试上限。

测试：

- `test_a_heading_only_summary_is_rejected`
- `test_ai_self_marks_unknown_without_window_evidence`
- `test_a_failed_base_window_leaves_a_stale_signal`
- `test_a_failed_base_window_keeps_its_evidence`（先写红，确认现状）

回滚：`summary_template`、`rules/ai-self.md`、prefetch 的 stale 分支；job 行为不动。

### 5.6 B6：剪枝写进规则，不写 lint

改：

- `rules/user-self.md` 与 `rules/ai-identity.md` 的 REVIEW CHECKS 各加两条：
  - 会过期的计数、版本号、项目状态。
  - 同一事实同时出现在身份与偏好。
- `card_review.build_prompt` 已经包含当前卡（`card_review.py:356-364`，`Current ... card (canonical, merge into it)`），review 有东西可剪。

不做：代码层正则 lint。理由：用户明确拒绝为校验而校验；剪枝需要判断"这条事实会不会过期"，正则判不了。

测试：

- 规则文件被 `rule_text` 读到即可，不新增行为测试。

## 6. 验收

1. 四文件存在；`load_global_cards` 返回三键；新会话首条 base 行的用户卡包含 `## 领域` 段（不是预设的单行 Occupation），说明全局卡接管了基线。`source` 不落库，用单测断言，不靠日志。
2. 首次真实 review 在 24h 内运行，写出 `card_revisions` 的 `source=review` 行；播种卡被当作 prior card，不再被当作 operator 文本。
3. 新会话 system prompt 含 `# User Preferences`；若采纳 5.4，也含 `# User Identity`。
4. 空摘要不再产生 `idle`。
5. 受影响测试与全量 `pytest` 通过；新增测试合计不少于 12 条（本稿列出 14 条）。

## 7. 非目标

- 不改窗口摘要的累积模型与 chunk 链。
- 不改四文件存储格式与 front-matter schema。
- 不做知识页、结论边、向量检索的迁移。
- 不给卡片加检索；卡片只做基线。
- 不改 operator 手写 `user/*.md` 的优先权。

## 8. 播种记录（本次已做）

- 用 `card_review.write_cards` 一次写出四个文件，通过同一验证边界；revision ids 1-4。
- 文件：`user/identity.md`、`user/preference.md`、`agents/default/identity.md`、`agents/default/preference.md`。
- front matter：`schema: minir-card/v1`、`source: review`、`reviewer: masia`、`generated_at: 2026-10-24T17:49:02Z`（UTC）。
- 正文语言：中文。依据是本 profile 的五天会话与 SOUL 的原文；未引入用户未声明的权限。
- 验证：`load_global_cards` 三键齐全；`_preference_prompt_block` 非空（578 字符）；token：345 / 367 / AI 两文件合计 534。
- 这批卡是 review 级，不是 operator 声明。首轮真实 review 可以改写它们，但不能把它们当 operator 原文。

## 9. 顺序与风险

推荐顺序：B1 → B2 → B3 → B5 → B4 → B6。

- B1 先做，否则后面所有卡片改动都没有周期复核。
- 例外：若在意播种 AI 卡的来源标签，B2 的"标签 + 去 front matter"切片可以先单独提交，再回到 B1。它不依赖 B1，见 §10 R1。
- B2 次之，避免 AI 卡继续自证；播种卡在这之前会被 B2 当作 operator 文本，风险与处置见 §10。
- B3 补第四张卡；B5 保护连续性；B4 让用户卡进入行为；B6 是收尾。
- 每一步只动一个变量：改调度、改输入、加规则、改注入，分开提交，便于回退。

## 10. 剩余风险与处置

### R1 播种的 AI 卡会被 B2 当作 operator 声明

- 机制：`history.load_identity`（`history.py:281-293`）原样读取文件，包含 front matter；`_base_ai_prompt`（`worker.py:519-524`）把内容标成 `Operator-authored AI identity (trusted)`。播种卡的 front matter 写着 `source: review`，但标签说 trusted，两个信号矛盾，模型可能按 trusted 处理。
- 影响：下一轮 B2 可能逐字保留这两张卡，并把它们当作不可改写的声明。卡的内容本身来自 SOUL，没有新增权限，所以实际危害是"来源不可审计的硬化"，不是权限扩大。后果是以后用证据修正这些行会被当成覆盖 operator 文本。
- 规则侧已有对应检查：`rules/ai-identity.md` 的 REVIEW CHECKS 写着 "A review-sourced line is used as if the operator had declared it"。但 B2 的 prompt 标签与它冲突，冲突要在代码层解决，不能指望 review 兜底。
- 触发时机：下一次新会话的第一条 base 刷新。本会话不触发。
- 检测：新会话首条 `base_refreshes` 的 `ai_identity_card` 若与播种卡逐字相同，或只增不减且没有本窗口证据，说明标签生效；同时 AI 两文件的 revision 仍是 3、4，没有新行。
- 处置，按代价从低到高：
  1. 先落 B2 的最小切片（只改标签与去 front matter，约 20 行 + 2 条测试），再开新会话。新会话随时可能开始，所以这条只能降低概率，不能保证；要保证就先改 B2 再继续用 Hermes。
  2. 如果 B2 来不及：接受一次带错标签的 base 刷新。内容不越权；B2 落地后，下一次 review 用正确来源重写一次。
  3. 不接受的做法：为了绕开标签而删掉播种卡。那会让 B1 的断点回到原状，且 `source="global"` 再次失效。
- 失效条件：B2 的标签修复合并，且至少一次 base 刷新用新 prompt 跑过。

### R2 首轮 review 可能把 AI 卡改回 manifest 摘要

- 机制：review 的 AI 身份 pass 仍然拿到 `Manifest (trusted; ...)` 与 `rules/ai-identity.md`（`card_review.py:424-425`）。规则允许从 manifest 取身份事实，模型可能重新写回 Deployment/版本段。
- 影响：播种卡里"职责、声音、边界"被覆盖，AI 卡退回到"插件自述"。
- 检测：首轮 review 的 `card_revisions` 里 AI 身份文件出现新行后，正文是否重新出现 `Deployment`、版本号、生命周期。
- 处置：
  1. 与 B2 同批改 `rules/ai-identity.md`：把 manifest 限定为"事实来源"，禁止整段转写；`## Deployment` 这类段名直接列为反例。
  2. 规则落地前，把 `layer_state("card_review")` 的 `next_attempt_at` 推到规则落地后（只改时间，不删 anchor）。否则 2026-10-26 00:40 的首轮 review 会带着旧规则跑。
  3. 若首轮已经写回 manifest，用修订后的规则再跑一次覆盖；`card_revisions` 保留两版，可对照。
- 失效条件：B2 + ai-identity 规则落地后的第一次 review 输出不含 manifest 段。

### R3 偏好卡提前进入每个会话的 system prompt

- 机制：`_preference_prompt_block` 每会话读一次，`_MAX_PREFERENCE_CHARS = 4000`（`__init__.py:116`）；当前 446 字符。
- 影响：低。若后续 review 写长，会占用每个会话的固定上下文。
- 处置：B4 落地时给身份卡单独预算（≤1200 字符），偏好仍 ≤4000；超限截断并指向文件。
- 失效条件：不适用，属于常态预算。

### R4 播种卡里的事实会随项目漂移

- 机制：用户卡的领域与技能、AI 卡的职责都是当前工作面的快照。
- 影响：中。例如"C++ 在研究中"在真正交付后会过期；"维护 CA/PKI"在项目结束后会变成历史。
- 检测：现有 REVIEW CHECKS 没有"会过期的事实"这条（`rules/user-self.md` 6 条、`rules/ai-identity.md` 5 条）；B6 要新增计数、版本、项目状态两条。
- 处置：靠 B1 让 review 真正跑起来，靠 B6 的规则剪枝；不做代码层 lint。
- 失效条件：review 周期恢复后自然收敛。

## 11. 校准记录（2026-10-25）

- 卡 part 名统一为 `user_self_representation` / `user_preferences` / `ai_identity` / `ai_preference`；`ai_self_representation` 只指 base 层会话状态。代码侧把 `user_identity` → `user_self_representation`、`user_preference` → `user_preferences`，AI 两名不变；同步 `PART_NAME`/`PART_SCOPE`/`card_paths`、`card_revisions.part` 的存量值与测试。
- `remember_mode=card` 必须走 `card_review.write_cards`（单 part 包装保留其余三份正文）；用户发起写入 `source=operator_edit` 并写 `reason`。
- stale 统一用 `layer_failures`（marker 稿 §1.3），删除 `base_stale`。
- claim id 用 path；`(category, org, topic)` 是元数据。
- 合并候选复用 `advisories` 时带 `source='merge'`，与顾问建议分开统计。

## 实现缺口（2026-10-25 复核）

判定：B1-B6 都有代码落点，但不是严格实现。旧调用点保住了旧标签与旧输入，未决的标签、校验与规则接线按最窄一处落地；R1/R2 未闭合。

向后兼容仍是默认
- B2 的新标签只落在 base 一段：`_base_ai_prompt` 用 Operator declarations / Prior review card（worker.py:520-531），但 `_dialectic_prompt` 仍写 “Operator-authored AI identity/preferences (trusted)”（562-567），调用点传的正是 review 播种卡（1550-1553）。每次复盘仍把 review 卡当 operator 原文。
- `history.load_identity` 返回 (body, body)（292-304），没有 meta；调用方无法按 source 决定标签，标签只能硬编码。
- B1 的 `ensure_anchor` 对存量 state 保留 `last_success_at`（542-549），只有新 home 才不写；`due` 只查 `is_file()`，不查内容、哈希或 mtime，存在但空/坏的卡也算已播种。
- B5 的空摘要校验只接在 base：`validate_summary_pass` 用 `validate_nonempty`（layers.py:324-331），dialectic 的 `validate_review` 用 `summary_template.validate`（350），heading-only 仍可经复盘写回。
- B3 的 ai-preferences 只被 card_review 读取（card_review.py:84），B2 与 dialectic 的 system prompt 仍只有 ai-self + ai-identity（worker.py:996-997，1546-1549）。

未决项按最小改动
- `build_prompt` 仍把整份卡文件（含 front matter）拼进 “Current ... card”（card_review.py:370-374）；只有 history._card_body 与 read_card 两处剥头，review 输入没有剥。
- ai-self 规则同时写着 “idle when Doing is empty”（rules/ai-self.md:20-21）与 “never idle”（32），旧的具体指令排在前面；代码没有 idle/unknown 判定，完全靠提示词。
- R2 的 manifest 限定没有做：card_review 仍把 manifest 标成 trusted（443-445），ai-identity 规则仍写 “Identity claims come from the operator files or the manifest”（24）。
- 播种按 review 来源（source=review、reviewer=masia），没有按 R1 在代码层解决标签冲突；R1 仍依赖 review 自纠。

未实现或与稿冲突
- 计划 14 条新测试只落了 4 条；B2 三段分离、front matter 不进输入、dialectic 的 review 卡标签、双卡为空、unknown/idle、失败窗口保留证据都没有专门用例。
- 首轮真实 review 没有发生，`card_revisions` 只有播种 1-4；按 live state，下一次是 2026-10-26 00:40 (+08)，§6.2 未达成。

运行时状态：运行副本仍是旧版本（`_files_present` 0 命中）；这是状态，不改变上述判定。
