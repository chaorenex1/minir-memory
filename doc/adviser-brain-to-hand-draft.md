# 顾问的脑与手：行动路径草稿

状态：P0-P4 已实现（2026-10-25）；claim 统计回灌 card_review、重复 claim 抑制与 blame 计数均已接线。

依据：HEAD `30a4b09`；工作树 3 个未提交修改（`rules/markers-category.md`、`rules/markers-org.md`、`tests/test_session_finalisation.py`）；`memory.db`（本地 2026-10-25 01:xx）；本会话此前两轮的分析（全局卡播种、adviser 现状统计）。行号以本次核对为准。

## 0. 一句话

手已经在动：5 天 1713 次评审、395 条建议。但 92% 是通用动作与产物，卡片覆盖 2.3%，知识引用 0。要让手替大脑指，不是加权限或加频率，而是补三段：selector（选 claim）、authority（定力度）、timing（挪到动手前）。

## 1. 基线（10-20 至 10-24）

| 指标 | 数值 |
| --- | --- |
| 评审 | 1713：静默 1262（74%）、有建议 364（21%）、失败 45、running 35、dropped 7 |
| 建议 | 395：blocker 69、concern 326 |
| 送达 | 302：中途注入 58、turn 结束 244（81%） |
| blocker 送达 | 66：中途 24、turn 结束 42、未送达 3 |
| 动作类 blocker | 47：中途 15、turn 结束 30、未送达 2 |
| target 分布 | current_step 171、output_surface 59、tool_choice 43、final_answer 33、user_intent 27、soul_tool 24、code_economy 22、general 10、soul_workflow 4、soul_craft 2 |
| 份额 | 动作与产物 92%（365）、SOUL 8%（30）、卡片 0、知识 0 |
| 卡片检查覆盖 | user_preferences 41/1713（2.4%：35 clean、6 n/a）；ai_self 39/1713（2.3%：27 clean、12 n/a）；命中 0 |
| 知识引用 | 0/395 |

两条读数：

- 手不是不说话，是不看大脑。1713 次评审里，卡片被真正检查的不到 3%，知识一次没被引用。
- 手也不是只在收尾说话。动手类 blocker 有 30/45 是 turn 结束才到，说明发现时机晚于动作。

## 2. 目标模型

### 2.1 claim 六元组

大脑不是卡和页的文本，而是一组可判定条目：

| 字段 | 含义 |
| --- | --- |
| id | 稳定标识：`card:<path>:<section>:<n>` / `knowledge:<path>#<line>`；path 是身份，`(category, org, topic)` 是元数据 |
| text | 原句，逐字 |
| scope | 什么时候适用：answer_shape / delivery / tools / domain / path / tool 名 |
| authority | operator / card / knowledge / inference |
| status | active / superseded / retired |
| test | 什么观察会违反它，例如 `draft_write`、`shell_text`、`unverified_final` |

术语与知识稿对齐：claim 是一行断言，unit 是一张页（path），conclusion 是表行（kind,key）。claim id 用 path，不用三元组；`(category, org, topic)` 随 claim 一起带，用于 scope 与引用。

### 2.2 三类 claim，三类问题

| 来源 | 回答 | 例子 | 首选通道 |
| --- | --- | --- | --- |
| 规范（人格偏好、SOUL） | 该不该做 | 草稿轮不落地代码；不擅自提交 | gate |
| 事实（知识页、结论） | 做得对不对 | 这个方案已被否决；这个字段名与契约不符 | nudge |
| 身份（用户/AI 身份卡） | 是不是这件事 | 这是 PKI 工作面；用户要的是可交付 | recall / nudge |

### 2.3 权威阶梯

| authority | 来源 | severity 上限 |
| --- | --- | --- |
| operator | SOUL；front matter `source: operator_edit` 的卡（含用户发起 `remember_mode=card` 的写入） | blocker |
| card | 四文件默认（`source: review`）、base 行 | concern；只有 operator 行可 blocker |
| knowledge | 页面、结论、边 | concern（页面 `status: decided` 且动作反向时可 blocker） |
| inference | 窗口摘要、模式推断 | nit |

card 写入走 `write_cards`；用户发起时 `source=operator_edit`，review 播种是 `source: review`。authority 由 front matter 决定，不由写入者口头声明。

要让一条偏好能拦，把它写成 operator 文本：手改 front matter `source: operator_edit`，或写进 SOUL。卡片的默认权限是 concern。这条规则同时解决了"卡片自己给自己授权"的问题。

### 2.4 三段式

    selector（每步，便宜，确定性）
      输入：用户意图 + 当前 tool 与 args + 最近的错误/改动路径
      输出：0-3 条 claim + why now
      没有 claim 就返回空，reviewer 不跑大脑那一遍

    reviewer（只在有 claim 时跑）
      只判断选中的 claim；逐条报 clean/found/n/a；每条建议带 claim 引用与逐字原句

    channel
      动手前：operator 规范 → 确定性 gate，直接拒绝（不调模型）
      工具后/中途：card 或 knowledge → concern，指出矛盾/重复/先例 + 最便宜的修法
      收尾前：knowledge → recall，"这件事已经决定过/已经知道"

### 2.5 接入现有评审管线

现有管线（每个 review）：

    事件 → maybe_schedule(session, trigger)              adviser_mode.py:1272
         → 队列 → _review_one                            1731
             → _review_brief(cfg)     系统提示             174-197
                 core + review + budget + boundary
                 + rule_text("advice_target")             194
                 + rule_text("advice_judgment")           195
             → _prompt(session, ...)  用户提示             1568
                 digest / rulers / knowledge seed / stats / transcript
             → agent_loop.run(...)    评审模型
             → parse_review           537
             → parse_coverage         624
             → _validate_knowledge_refs 1815
             → _admit                 1954（EmissionGuard、预算）
             → store：adviser_reviews + advisories
             → _deliver_host          2163（interrupt / steer / continuation / 结束聚合）
    下一次 llm_request：sync_backlog wait → 注入
    下一次 pre_tool：block_reason       2374 → 有 blocker 就拒绝

四个插入点：

| 阶段 | 插在哪 | 改什么 | 不变什么 |
| --- | --- | --- | --- |
| P0 | `_review_one` 的 `_prompt` 之前 | `_select_claims` 选 claim，写 `selected_claims` | 提示、解析、投递 |
| P1 | `_prompt` 的用户提示；`parse_review` 之后 | `active_claims`、`claim` 字段、`claim_coverage`、`apply_ceiling` | `_review_brief`、`_admit`、EmissionGuard、投递 |
| P2 | `_review_brief` 的系统提示；`_rulers` | 两节规则、两个 target token、身份 ruler | 评审模型、解析、投递 |
| P3 | `pre_tool` 的 `block_reason`；`post_tool` 的 `maybe_schedule` | 确定性 gate、强制评审、`sync_backlog: wait` | 队列、评审、`_deliver_host` |
| P4 | `_admit` 之后；`card_review.build_prompt` | `claim_id` / `outcome`、`claim_stats` | 现有表、评审、投递 |

一句话：评审循环、规则装载、解析、去重、预算、投递、闸门都没换。草稿加的是"评审前选什么""评审时看什么""评审后怎么夹紧"以及"动手前的那一下"。

P0 的 `claims.py` 不判断，只选择和提供文本。唯一的代码判断是 P3 的确定性谓词和 P1 的 severity 上限，两者都窄且可测。

### 2.6 判断分四层

| 层 | 放哪 | 谁执行 | 例子 | 怎么改 | 能做什么 |
| --- | --- | --- | --- | --- | --- |
| 规则文件 | `rules/advice_target.md`，经 `_review_brief` 进系统提示（`194`） | reviewer 模型 | `user_identity_check` 的 entry / evidence / kinds | 改规则，随评审生效 | 影响判断，不保证行为 |
| claim 数据 | `active_claims`，进用户提示（每轮变） | reviewer 模型引用 | 卡片行"草稿轮不落地代码"；页面 claim"方案已被否决" | 改卡片或知识页 | 提供判据，不改代码 |
| predicate | `claims.py` 的 `PREDICATES` | 代码，同步 | `draft_write`、`secret_read`、`commit_unasked` | 改代码 | 同步拦截，不调模型 |
| ceiling | `apply_ceiling` | 代码 | card→concern、operator→blocker | 改代码 | 夹紧 severity，不能放宽 |

三条理由：

1. 规则文件只能影响模型，不能保证行为；需要零模型、同步的拦截只能写成谓词。
2. claim 是数据，大脑更新时不该改规则文件；否则每次改卡片都要动规则，来源又会混。
3. 系统提示保持稳定（规则文件），每轮变化的 claim 放用户提示；不破坏提示缓存，也让评审知道"这一轮为什么选它"。

评审模型仍做真正的语义判断：这条 claim 是否适用于这一步、动作是否违反它、最便宜的修法是什么。规则给框架，claim 给标准，模型做比对；代码只负责选择和夹紧。

如果全塞进规则文件，等于把卡片内容硬编码进规则，大脑不能独立更新；如果全写成代码，"这个动作是否与那页的结论矛盾"又表达不出来。分层是按判断的性质拆的。

## 3. 行动路径 P0-P4

每阶段独立可提交、可回退、有停止条件。顺序的理由：先量（P0），再接线（P1），再命名（P2），再把时机前移（P3），最后闭环（P4）。P0 不出现在任何 prompt 里，风险为零。接入位置见 §2.5，判断分层见 §2.6。

### P0 影子：claim 索引与 selector

目的：证明"从当前动作能选出相关 claim"，不改任何行为。

改动：

- 新增 `plugin/minir/claims.py`：
  - `Claim` 数据类（六元组 + source）。
  - `card_claims(hermes_home, profile, base_row)`：解析四个文件，按 `##` 段映射 scope；claim id 用文件 path；`read_card` 的 meta 决定 authority；文件缺失时退回 base 行（authority=card）。
  - `soul_claims(hermes_home)`：解析 SOUL 的 `# Boundaries`、`# Priorities`、`# Delivery contract`、`# Critical`，authority=operator。
  - `knowledge_claims(query, root, limit=3)`：用 `minir_kb_search` / `kb_search` 按 query 选页，返回 path、`(category, org, topic)` 元数据、summary 与段首句；claim id 用 path#line；authority=knowledge。依赖 marker 稿 §4.5b 的返回结构；未落地时用 `select_paths` + `read_page` 兜底。
  - `select(turn, claims, limit=3)`：确定性打分。谓词命中 10，scope 命中 3，关键词重叠 0-2；>0 才返回。
  - `PREDICATES` 第一批：`draft_write`、`shell_text`、`unverified_final`、`commit_unasked`、`secret_read`。
- `adviser_mode._select_claims(session, trigger, fresh)`：组装 turn 信息，调用 `select`，结果写进 `adviser_reviews.selected_claims`（新增列，`store.py:608-645` 的迁移模式）。
- 配置 `adviser_mode.claims`：`off`（默认）/ `shadow` / `on`。P0 只允许 `shadow`。
- `_prompt` 不变；`parse_review` 不变；投递不变。

测试：

- `test_claims_parse_card_sections_and_scope`
- `test_a_review_sourced_card_is_card_authority`
- `test_an_operator_edited_card_is_operator_authority`
- `test_soul_claims_are_operator_authority`
- `test_the_selector_matches_draft_write`
- `test_the_selector_is_silent_for_an_unrelated_turn`
- `test_shadow_logs_selection_without_changing_the_prompt`

回滚：删 `claims.py`，停用 `claims` 配置；`selected_claims` 列留着无害。

停止条件：24 小时 shadow 后，selector 非空率 <10%，或人工抽查 20 条选中里可接受 <60%，停下来修 claim 切分与 scope，不进 P1。

验收：非空率 ≥25%；抽查可接受 ≥80%；建议数量与 target 分布相对基线 ±10% 以内。

### P1 接线：把 claim 交给 reviewer

目的：让 reviewer 看见 0-3 条 claim，并要求逐条交代。

改动：

- `_prompt`（`adviser_mode.py:1568`）加 `active_claims` 块：≤3 条、≤1200 字，每条给 id、authority、text、why_now。只在 `claims=on` 且选择非空时出现。
- `parse_review`（`537`）接受每条 advice 的 `claim` 字段；`claim` 不在 active 集合 → 丢弃并计 `unselected_claim`；P2 引入的两个新 target 若没有 claim → 丢弃。
- `user_preferences_check` 没有 claim 仍允许（legacy），计 `unclaimed`，severity 按 card 夹紧；新 target 必须带 claim。
- 新增 `parse_claim_coverage(answer, active_ids)`；结果写 `adviser_reviews.claim_coverage`（新增列）。
- 新增 `apply_ceiling(notes, claims)`：在 `_review_one` 的 `parse_review` 之后（`1810-1816` 之间）按 §2.3 夹紧 severity。
- `_validate_knowledge_refs`（1815）不变；知识引用仍走现有后缀。

契约反转：现在 `user_preferences_check` 的 blocker 格允许 blocker。P1 后，`source: review` 的卡片行最高 concern；只有 `source: operator_edit` 或 SOUL 行可 blocker。`ADVICE_HANDLING` 文案不改，力度由 `apply_ceiling` 执行；规则文件要写明升级路径。风险是偏好变软，处置是把硬约束写成 operator 文本。

测试：

- `test_the_prompt_lists_only_selected_claims`
- `test_an_advice_with_an_unselected_claim_is_dropped`
- `test_an_unclaimed_preference_advice_is_capped_at_concern`
- `test_a_knowledge_claim_cannot_be_a_blocker`
- `test_an_operator_claim_can_block`
- `test_claim_coverage_is_recorded_per_claim`
- `test_a_card_claim_is_clamped_to_concern`

回滚：`claims` 改回 `off`；prompt 与解析回到现值；两列留着。

停止条件：建议总量相对基线上涨 >25%，或知识引用率 <5%，先调 selector，不进 P3。

验收：有 active claim 的评审里，claim coverage ≥50%（基线 2.3%）；相关页存在时，每 10 条建议至少 1 条知识引用（基线 0）；建议总量涨幅 ≤10%。

### P2 词汇：两个新 target 与身份入队

目的：让新发现能有名字，并让用户身份成为可检查的 ruler，而不只是 320 字摘要。

改动：

- `prompting.CONSISTENCY_TARGETS`（`87`）加 `user_identity_check`、`knowledge_claim_check`。
- `ADVICE_HANDLING`（`157`）补两列：blocker / concern 格子。`knowledge_claim_check` 的 blocker 格写"不使用；派生 claim 只能 concern"。
- `rules/advice_target.md` 加两节：entry condition、四种 evidence 字段 + `claim_ref`、kinds。
  - `user_identity_check`：wrong_domain、wrong_deliverable、wrong_assumption。
  - `knowledge_claim_check`：contradicts、duplicates、supersedes、recall_miss。
- `_rulers`（`1398`）加 `user_self_representation`（2000 字），读文件优先于 base 行。
- `parse_coverage` 自动接受新 token（`624` 只看 `CONSISTENCY_TARGETS`）。

测试：

- `test_the_two_new_targets_are_delivered_and_handled`（扩展 `test_consistency_targets_are_delivered_and_handled`，`tests/test_adviser_mode.py:818`）
- `test_user_identity_is_a_ruler_not_a_digest`
- `test_knowledge_claim_target_requires_a_claim_ref`
- `test_the_advice_handling_table_has_both_new_columns`

回滚：删两个 token、两列、规则两节；coverage 里旧数据不影响。

停止条件：`bad_target` >0，或有 active claim 的评审 coverage <30%，停下来修 prompt 与规则。

验收：两个 target 出现在 coverage 与建议里；`bad_target` 为 0；身份类建议至少 1 条/天（当前为 0）。

### P3 时机：动手前的确定性 gate

目的：把"该不该做"从事后评论挪到动手前；把判断类 claim 的评审提前到下一步之前。

改动：

- `_on_pre_tool_call`（`__init__.py:2051`）把 `kwargs.get("args")` 传给 `block_reason`。
- `block_reason`（`2374`）先跑确定性 gate：选 operator claim 中 `test` 命中 tool+args 的，直接返回 block，不调模型；现有的 pending-blocker 路径保留。
- `maybe_schedule`（`1272`）：在 mutating tool 的 **post** 事件上，若命中判断类 claim，强制一次评审（不等 cadence）。当前这一步已经发生，确定性 gate 负责真正的动手前拦截；这次评审保证 blocker 在下一步之前到位。
- 把 `sync_backlog` 设为 `wait`（当前默认 off，`wait_ms` 上限 1500），让下一次 `llm_request` 有界等待并注入。
- 新增 `late_blocker` 计数：动作类 blocker 若在 turn 结束才送达（`injected_at` 为空、`delivered_at` 非空），记一次；数据来自 advisories，不新增时间字段。

测试：

- `test_a_draft_request_blocks_write_file_without_a_model_call`
- `test_a_secret_path_read_is_blocked_by_an_operator_claim`
- `test_a_knowledge_claim_triggers_a_review_before_the_next_step`
- `test_an_action_blocker_delivered_at_turn_end_is_counted_late`
- `test_the_gate_does_not_block_non_mutating_tools`

回滚：`claim_gate` 设 false；`block_reason` 回旧签名调用；`maybe_schedule` 的强制分支删除。

停止条件：出现一次误拦（operator 明确说"这是对的"），关掉 `claim_gate`，只保留 shadow，重做谓词。

验收：动手类 blocker 的 turn 结束送达从 30/45 降到 ≤15/45；误拦 ≤1 次/周；非 mutating tool 零拦截。

### P4 闭环：把手的引用回灌大脑

目的：让反复被引用、被采纳或被无视的 claim 影响卡与知识页，而不是每次重新指。

改动：

- `advisories` 加 `claim_id TEXT DEFAULT ''`、`outcome TEXT DEFAULT ''`；`store.py:182-204` 加列，迁移同 P0。
- `block_reason` 消费 blocker 时写 `outcome='blocked'`；同一 claim 在同一会话再次出现时写 `outcome='repeated'`。
- `store.claim_stats(session, window)`：只统计 `source='adviser'` 且 `claim_id != ''` 的行（合并候选 `source='merge'` 不计），按 claim_id 聚合 cited / blocked / repeated。
- `card_review.build_prompt` 加一段 claim 统计：高频被引用且 blocked 的卡行保留；高频 repeated 的卡行进入改写候选；retire 的页 section 不再选。
- `review_stats` 不动，新增方法，避免影响现有表格。

测试：

- `test_claim_stats_counts_cited_blocked_and_repeated`
- `test_the_card_review_sees_top_claims`
- `test_a_repeated_claim_is_not_re_emitted_in_the_same_session`

回滚：停用 `claim_stats` 读取；两列留着；card_review 的段删除。

验收：同一 claim 的重复建议率下降 30%；card_review 的 prompt 含 claim 统计；review 的 revision 里能看到"因 repeated 而改写"的记录。

## 4. 验收总表（基线 → 目标）

| 指标 | 基线 | P1 后 | P3 后 | P4 后 |
| --- | --- | --- | --- | --- |
| 有 claim 时的 coverage | 2.3% | ≥50% | ≥60% | ≥60% |
| 知识引用率 | 0 | ≥1/10 建议 | ≥2/10 | ≥2/10 |
| 动作类 blocker 结束送达 | 30/45 | 30/45 | ≤15/45 | ≤15/45 |
| 通用建议占比 | 92% | ≤85% | ≤70% | ≤60% |
| 建议总量 | 395/5d | +≤10% | 不增 | 不增 |
| selector 非空率 | 无 | 已测 | 已测 | 已测 |

## 5. 非目标

- 不做向量检索；selector 先用确定性规则。命中率不够再评估。
- 不为 selector 增加一次模型调用。
- 不改知识页 schema；claim 从现有 front matter 与段首句派生。
- 不改 reviewer 的模型与路由。
- 不让 adviser 写卡或写知识页；保持只读。
- 不做 UI；统计走现有表与日志。

## 6. 风险与处置

| 风险 | 处置 |
| --- | --- |
| selector 漏选，大脑仍被忽略 | shadow 先量非空率与抽查；补 scope 同义词；P0 停止条件先行 |
| selector 误选，噪声上升 | 最多 3 条；claim 引用必须逐字；同一 claim 一轮一次；建议涨幅 >25% 停止 |
| severity 上限让卡片变软 | 需要硬约束时改用 `source: operator_edit` 或写进 SOUL；这是显式升级，不是默认 |
| prompt 膨胀 | active claims ≤1200 字；rulers 已有 2000 字/项；超限截断 |
| 迁移风险 | 只加列，有默认值；不做回填；旧行读作空 |
| 额外延迟 | 确定性 selector 目标 <5ms；pre_tool 不调模型；判断类评审走现有异步队列与有界等待 |
| 评审成本上升 | 只在 claim 命中时强制评审；其余仍按 cadence；P1 停止条件卡住总量 |

## 7. 灰度与开关

- `adviser_mode.claims`：`off`（默认）/ `shadow` / `on`。P0 手动设为 shadow，P1 起用 on。
- `adviser_mode.claim_gate`：默认 false；P3 只对 operator claim 开启。
- 一键回退：`claims: off` 回到当前行为；数据列保留。
- 观察面：`adviser_reviews.selected_claims`、`claim_coverage`；`advisories.claim_id`、`outcome`。不新增日志系统。

## 8. 与全局卡草稿的接口

- 四文件是大脑的规范层；P0 的 authority 直接读 front matter：`source: review` → card，`source: operator_edit` → operator。SOUL 始终 operator。
- 本路径不依赖卡片草稿的 B1-B6。这里的上限判断读文件 meta，不读 B2 的标签，所以 B2 未修时也不会把 review 产物当 operator；B2 修好后行为不变。
- 卡片草稿的 B4 把身份注入主 agent；本路径 P2 把身份注入 reviewer。同一文件，两个读者，不冲突。
- 文件缺失时 claim 退回 base 行（authority=card），adviser 仍可工作；这时 identity 只有 320 字摘要，P2 的"身份入队"就是修复点。

## 9. 执行顺序与停止点

P0 → P1 → P2 → P3 → P4，每阶段单独提交。

- P0 结束：看 shadow 数据，决定 claim 切分是否需要重做。
- P1 结束：看 coverage 与引用率，决定是否值得继续投 P3。
- P2 结束：看新 target 是否产生有效建议；没有就停，不进入 P3。
- P3 结束：看误拦与 late blocker；误拦一次就关 gate。
- P4 只有在前三步稳定一周后才做；它改的是大脑，不是手。

## 10. 校准记录（2026-10-25）

- claim id 用 path；`(category, org, topic)` 是元数据。
- card part 名统一为 `user_self_representation` / `user_preferences` / `ai_identity` / `ai_preference`；`ai_self_representation` 只指 base 层会话状态。代码侧把 `user_identity` → `user_self_representation`、`user_preference` → `user_preferences`，AI 两名不变；同步 `PART_NAME`/`PART_SCOPE`/`card_paths`、`card_revisions.part` 的存量值与测试。
- 用户发起 `remember_mode=card` 的写入 `source=operator_edit`；review 播种是 `source: review`。
- `advisories` 共用：`claim_stats` 只统计 `source='adviser'`。
- B4 是唯一页写者；本稿只读页，不写页。

## 实现缺口（2026-10-25 复核）

判定：本稿没有被严格实现。P0-P4 的符号都在，但默认值停在 P0 之前，选不出 claim 时旧规则原样接管，未决的 gate 与度量按最窄路径落地。§4 的验收在默认配置下没有一行能产生数据。

向后兼容仍是默认
- `claims=off`、`claim_gate=false` 写在 `_DEFAULTS`（adviser_mode.py:90-99）；`_select_claims` 对非 shadow/on 直接返回 []（1606-1608）。稿要求 P0 手工 shadow、P1 起 on，代码没有阶段推进，也没有“P0 只能 shadow”的约束。
- 选不出 claim 时 `active or None` 关闭过滤（1892-1895）：`parse_review(active_claims=None)` 保留所有旧 target 的 blocker，`apply_ceiling` 只夹引用到 claim 的 note 或 `user_preferences_check`（claims.py:232-240）。稿 §2.3/§2.4 的 card/knowledge 上限与“没有 claim 就返回空”只在非空选择集里成立。
- 卡文件缺失时 `card_claims` 直接跳过（claims.py:91-94），没有 §P0/§8 的 base 行回退；selector 池可以为空，然后落回上一条旧路径。
- mutating tool 后的强制评审仍是 `force=errored or mutating`（2795-2797），与 claim 命中无关；`sync_backlog` 默认仍是 off（121），稿要求的 wait 没有成为默认。
- gate 的 turn 来自工具事件（tool/args，外加该事件的 text），不含用户上文；`draft_write` 要求参数文本自带“草稿/draft”（claims.py:175-186），用户口头的草稿要求不参与，只能拦住路径自带 draft 字样的写入，且拦截分支只返回字符串，不写 advisories/outcome（2491-2509）。

未决项按最小改动
- `claim_coverage` 没有逐 claim 的 review 产出：规则只要求按 consistency target 报 coverage（rules/advice_target.md:61-64），写入端把 target coverage 与 claim coverage 合成一个字典，无条件写进 `claim_coverage` 列（1900-1904，store.py:2615-2628），旧 `coverage` 列也收到同一字典。
- 重复抑制改写上一条已有 advisory 的 outcome（store.py:2424-2438），被压制的新建议不落行；cited 计数与“重复建议率”没有独立分母。
- 身份行没有可判定谓词（`_infer_test` 对描述行返回空），`###` 标题不更新 heading（claims.py:99），身份 claim 只能靠 scope 或词面重合被选中。

未实现或与稿冲突
- `late_blocker` 未实现：全仓无符号，行动类 blocker 在回合末送达时没有计数口径（稿 §P3、§4）。
- claim id 只用文件名（claims.py:102），用户卡与 AI 卡都叫 `preference.md`，两卡行共享 id；`apply_ceiling` 的 by_id 后者覆盖前者，`claim_stats` 合并计数。
- 测试只落了 4 条（tests/test_claims.py），稿点名的 26 条新测试不存在；coverage、身份 ruler、gate、claim_stats、重复抑制没有专门断言。

运行时状态：提交未部署，live DB 仍是 v4；这是状态，不改变上述实现判定。
