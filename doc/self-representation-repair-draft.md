# AI 自我表示的会话隔离与一致性 target 族（草稿）

状态：草稿，供评审（v3）。代码基线：`plugin/minir/` 当前 main（`9269854`）。

本稿把「会话历史是否与某个准绳一致」做成一个 target 族，三把准绳并列：`user_pref`（用户卡-用户偏好）、`self_model`（AI 自我表示）、`soul`（HERMES_SOUL 约束）。机制共用，准绳、判定类型、是否进修复轮各自不同。若其中某条只是替换关系，删对应小节即可，共享部分不动。

范围：

- AI 自我表示的会话绑定与召回报错契约。
- target 族：`user_pref`、`self_model`、`soul`。
- 会话结束后由宿主启动的修复轮（只由 `self_model` 触发）。

非目标：`identity.md` / `preference.md` 的三天合并保持原样；修复轮不改自表示，不改历史 transcript；不引入新的顾问通道或审计表。

## 0. 术语

| 词 | 含义 | 位置 |
| --- | --- | --- |
| `ai_self` | AI 卡里的 Self Representation 一节，随会话行为漂移 | `layers.render_ai_card`（layers.py:421-426） |
| `ai_identity` | AI 卡里的 Identity 一节，稳定身份 | 同上 |
| `base.ai_card` | 整张 AI 卡的结论行，当前跨会话可召回 | `store._LONG_TERM_KINDS`（store.py:339） |
| `base.ai_self` | 拆出来的自表示结论，会话绑定 | 本稿新增 |
| `base.ai_identity` | 拆出来的身份结论，长期 | 本稿新增 |
| `base.user_card` | 用户卡结论行，含 `### Preferences` 一节 | 已有，长期 |
| `user_pref` | target：会话与用户卡-用户偏好冲突 | 本稿新增 |
| `self_model` | target：会话与自表示不一致 | 本稿新增 |
| `soul` | target：会话违背 SOUL 的约束段 | 本稿新增 |
| HERMES_SOUL | `$HERMES_HOME/SOUL.md`，operator 手写的角色与约束 | 已有，13,238 字节 |
| 修复轮 | 会话结束后由宿主启动的一轮 agent 作业，补做声明了却没做的事 | 本稿新增 |

## 1. 现状 → 目标

| Surface | 现状 | 目标 |
| --- | --- | --- |
| AI 卡发布 | `## AI Card` 整节 → 一条 `base.ai_card`，scope=agent | 按 `### Self Representation` / `### Identity` 拆成 `base.ai_self`（session）与 `base.ai_identity`（agent） |
| 跨会话召回 | `base.ai_card` 在长期 kind 表内，`scope="all"` 可返回 | `base.ai_self` 不进长期 kind 表，`scope="all"` 永不返回 |
| 召回缺 id | 工具入口把入参与 provider 的 `_session_id` 做 or 回退，空值静默返回空结果 | 会话绑定 kind 缺主会话 id 时报错，错误回主会话，要求提供会话 id |
| 用户偏好核对 | 无。顾问摘要里有用户卡，但不逐条对账 | target `user_pref`，走既有 advisories 通道 |
| 自我一致性判定 | 无。顾问只审当前动作，不读 AI 卡 | target `self_model` |
| 角色约束核对 | 无。顾问的 run 不加载 SOUL（`load_soul_identity` 默认 False） | target `soul`，只读 SOUL 的约束节 |
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
- 三条准绳都要读会话历史；历史读同样要主会话 id，拿不到时顾问收到错误，再向主会话要 id。

## 3. 一致性 target 族

### 3.0 一个机制，三把准绳

| token | 准绳 | 读取方式 | 判定类型 | 进修复轮 |
| --- | --- | --- | --- | --- |
| `user_pref` | `base.user_card` 的 `### Preferences` | minir 工具读整行 | `violated` / `temporary_override` / `stale_card` | 否 |
| `self_model` | `base.ai_self` | 会话绑定读，需主会话 id | `false_claim` / `missing_work` / `scope_change` | 是 |
| `soul` | `$HERMES_HOME/SOUL.md` 的约束节 | 只读文件，按 mtime 缓存 | `boundary_breach` / `process_breach` / `surface_breach` | 否（默认） |

共享规则：

| 项 | 规则 |
| --- | --- |
| 载体 | `advisories`，`target` 列是自由文本（store.py:208），不加表 |
| 投递 | 既有通道：中途块或回合边界，受 `severity_floor`（默认 concern）与投递门控约束 |
| 证据 | 每条四项：`turn_uid`、准绳原句、实际观测（工具名与结果）、判定类型 |
| 缺证据 | 任一项缺失即不发。宁可不发，不做泛泛的「不一致」 |
| 顾问输入 | 会话历史（会话检索，需主会话 id）+ 准绳 |
| 去重 | 沿用 advisories 的指纹逻辑，同一冲突不重复发 |

### 3.1 登记面

一处登记，三面同步（`prompting.py` 现有结构的设计意图）：

| 面 | 位置 | 改动 |
| --- | --- | --- |
| 元组 | `ADVICE_TARGETS` | 新增一组 `Consistency targets:`，三个 token 同组 |
| 系统块 | `ADVICE_TARGET_GROUPS` | 自动生成，组内三条即出现在主 agent 的 Advisers 章 |
| 处置矩阵 | `ADVICE_HANDLING` | 三个 token 在 blocker / concern / nit 三档各一行 |
| 顾问词表 | `advice_target_glossary()` | 自动同步；用途句写进元组 |
| 顾问指令 | `adviser_prompts.py` | 三段检查指令、证据要求、准绳边界 |

### 3.2 `user_pref`：会话 vs 用户卡-用户偏好

准绳：`base.user_card` 的 `### Preferences` 一节；卡里没有时退到全局 `user-card/preference.md`。

| 类型 | 含义 | 建议指向 |
| --- | --- | --- |
| `violated` | 行为或声明违背卡里写明的偏好 | 当场改，或停手确认 |
| `temporary_override` | 本次会话用户另一句话与卡冲突 | 问清是本次覆盖还是要改卡 |
| `stale_card` | 用户本会话表达了新偏好，卡里还没有 | 更新卡，不算违例 |

证据里的「准绳原句」= 卡里那一行原文。顾问现有摘要是用户卡 320 字截断，可能截掉偏好原句，所以 `user_pref` 的建议必须来自完整读一次 `base.user_card`（用 minir 工具按 kind 读）。摘要不扩容、不改。

### 3.3 `self_model`：会话 vs AI 自我表示

准绳：`base.ai_self` 的当前文本。

| 类型 | 含义 |
| --- | --- |
| `false_claim` | 声明了没做的事（「已验证」但没跑） |
| `missing_work` | 自表示承诺的流程没走（「先给结论」但埋在第三段） |
| `scope_change` | 自表示说的范围与实际做的范围不一致 |

### 3.4 `soul`：会话 vs HERMES_SOUL 约束

准绳取 SOUL 的约束段，不取叙述段。按节标题切，只读这五节：

| 节 | 约束内容 | 判定类型 |
| --- | --- | --- |
| `# Precedence` | 冲突时的优先级（系统 > brief > 用户 > 项目规则 > 派生记忆 > 工具输出） | `boundary_breach`（把工具输出或记忆当授权） |
| `# Boundaries` | 不打印密钥、不推断隐私、不可逆或外部动作要确认、诚实报告阻塞 | `boundary_breach` |
| `# Delivery contract` | 不虚构、不减范围、不留 stub、不假完成 | `process_breach` |
| `# Critical` | 未验证不得声称完成、不得编造工具输出 | `process_breach` |
| `# Craft` | 表面与代码经济的具体禁令（英文破折号、加粗堆砌、单实现接口等） | `surface_breach` |

不进判定的节：`# Role`、`# Character`、`# Runtime`、`# Workflow`、`# Delegation & Kanban`、`# Communication` 的叙述部分。它们是风格与流程描述，不是可引用原句的硬约束。

判定规则：

- 必须能引用 SOUL 的原句；引不出原句的，不发。
- operator 当场指令与 SOUL 的叙述冲突时，按 SOUL 自己的 Precedence，用户当前指令优先，不算违例；`# Boundaries` 与 `# Critical` 不可被用户指令覆盖，这两节违反一律 blocker。
- 顾问是在核对，不扮演主 agent：SOUL 只作为准绳注入，不改变顾问自己的身份与输出格式。

读取与成本：

- 只读 `$HERMES_HOME/SOUL.md`，按 mtime 缓存解析结果。
- 只把上面五节拼进顾问的 `system_message`（约 4 到 5 KB），不整份注入，也不用 `load_soul_identity=True`（那是 13 KB 全文，且会改写顾问自身的身份）。
- 文件缺失或读不出时，`soul` 这个 target 本轮停用，不产生「不一致」判定；在状态里记一条 `soul_unavailable`。

### 3.5 处置

| 等级 | `user_pref` | `self_model` | `soul` |
| --- | --- | --- | --- |
| blocker | 安全或流程类偏好被违背且动作已发出或不可逆：停手，报告，不继续 | 与自表示冲突的完成声明即将发出或已发出：不发送，当场补验证；进修复轮 | 违反 `# Boundaries` 或 `# Critical`：立即停手，报告，不复述敏感内容；不进修复轮 |
| concern | 表达型偏好被违背（语言、结构、长度、顺序）：当场改 | 行为偏离自表示但未形成声明：当场校准；进修复轮 | 违反 `# Delivery contract` 的过程要求（未验证就收尾、缩范围未说明）：当场补做；可进修复轮 |
| nit | 措辞层面的偏差：可选 | 措辞层面的偏差：可选 | `# Craft` 表面类：可选，不打断任务 |

投递走既有通道：入 `advisories`，按 `severity_floor` 与投递门控决定中途出现还是边界出现。

### 3.6 何时不发

- 会话历史与准绳没有可引用的冲突点，只有「风格不同」。
- 准绳本身过时（`stale_card`），这时发的是「该更新卡」，不是违例。
- SOUL 文件不可读，或引不出原句。
- 同一冲突本轮已经发过（指纹去重）。

## 4. 会话结束后的修复轮

### 4.1 触发链

```
on_session_end(session)            __init__.py:819
  → _finish_session(session)       __init__.py:837
      → adviser_mode.session_end(session)
          1. 冻结：本会话 target='self_model'、severity in (blocker, concern)、
             未被消费的行改成 status='repair'，expires_at 推到远期
          2. 其余行照旧 expire_adviser_advices（adviser_mode.py:1471）
          3. 有冻结行时排一个 jobs(layer='repair', session_id, from_ts, to_ts)
      → 空闲窗口 arm（__init__.py:243-256 的既有语义）
          · 窗口内用户开口 → 取消 arm，job 留在队列
          · 定时器触发 → worker 认领 repair job
```

`user_pref` 与 `soul` 默认不进这条链：前者当场可改，后者多数是边界问题，事后补不了。要并入就把第 1 步的过滤条件改成 `target IN ('self_model','user_pref','soul')`，一处改动。

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
| 4 | `prompting.py` | `ADVICE_TARGETS` 增 `Consistency targets:` 组与三个 token；三档矩阵各补三行 |
| 5 | `adviser_prompts.py` | 三段检查指令（偏好、自表示、SOUL）、四项证据要求、`stale_card` 与 `temporary_override` 的区分、SOUL 不可覆盖的边界 |
| 6 | `soul_rules.py`（新） | 读 `$HERMES_HOME/SOUL.md`，按节标题截取五节，mtime 缓存，返回 `(sections, sha)`；读不出返回 None |
| 7 | `adviser_mode.py` | 顾问 `system_message` 追加 SOUL 约束节；`session_end` 冻结待修行并入队；`toolsets` 放开会话检索（默认值待核）；主会话 id 透传 |
| 8 | `worker.py` | repair job 消费：预算、禁令 prompt、终态写回 |
| 9 | `__init__.py` | `_finish_session` 与空闲窗口衔接：取消 arm 时 job 保留 |

## 6. 测试

| 场景 | 断言 |
| --- | --- |
| 缺 session id 读 `base.ai_self` | 抛 `session_scope_required`，文案含 main session id，不返回空 |
| `scope="all"` 泛化查询 | 结果里没有 `base.ai_self`，也不报错 |
| 显式点名 `base.ai_self` + `scope="all"` | 报错 |
| 发布拆节 | 同一 session 产出 `base.ai_self`（session）与 `base.ai_identity`（agent）两行，正文与旧 `base.ai_card` 一致 |
| 词汇贯通 | 系统块、顾问词表、解析器三处都认三个 token |
| 建议证据 | 缺四项之一时解析器拒收 |
| `user_pref` 准绳 | `preference_clause` 能在 `base.user_card` 里逐字找到 |
| `stale_card` / `temporary_override` | 前者发「更新卡」，后者问用户；都不判违例 |
| `soul` 读取 | 只取五节；文件缺失时 `soul` 停用并记 `soul_unavailable`，不产生判定 |
| `soul` 原句 | 引不出 SOUL 原句的建议被拒收 |
| 用户覆盖 SOUL 叙述 | 用户当场指令优先，不判 `process_breach`；`# Boundaries` / `# Critical` 例外 |
| 会话结束 | 有待修建议 → 一个 repair job；建议 status='repair' 后不被过期清掉 |
| `user_pref` / `soul` 违例 | 不产生 repair job |
| 重复会话结束 | 仍只有一个 repair job |
| 修复轮失败 | job `unresolved`，不重试，不产生第二轮 |
| 越界写 | file guard 拒绝，job 记 `unresolved` + 原因 |

对应测试文件：`tests/test_adviser_mode.py`、`test_adviser_channels.py`、`test_card_review.py`、`test_layers.py`（若存在），新增 `test_soul_rules.py`、`test_self_repair.py`。

## 7. 配置

| 键 | 默认 | 用途 |
| --- | --- | --- |
| `soul_check_enabled` | true | 关掉后不注入 SOUL 约束节，`soul` target 停用 |
| `self_repair_enabled` | true | 关掉后只发建议，不排修复轮 |
| `self_repair_max_iterations` | 24 | 修复轮工具调用预算 |
| `self_repair_timeout_seconds` | 600 | 墙钟上限 |
| `self_repair_daily_limit` | 3 | 全局每日上限，超出只落建议不起轮 |
| `self_repair_allow_commands` | true | 允许验证类命令；关掉则只读改文件 |

## 8. 未决

1. 三个 target 是并列还是替换关系。并列就都保留；只留其一就删对应小节，4.1 的过滤条件说一句即可。
2. token 名：`user_pref` / `self_model` / `soul` 是否采用。三处同步，先定。
3. SOUL 约束节取哪几节。本稿取 Precedence / Boundaries / Delivery contract / Critical / Craft，共约 4 到 5 KB。
4. `base.ai_self` 是否要在产生它的会话内每轮注入，还是只在顾问与修复轮里读。
5. 修复轮对「声明不成立但工作已完成」的处理：只记 note，还是同时改产物里的措辞。
6. 旧 `base.ai_card` 行的迁移：一次性拆完，还是惰性拆。

## 9. 风险

| 风险 | 缓解 |
| --- | --- |
| 无人值守写盘 | 路径白名单、验证命令白名单、无 git 写、无网络、每会话一次、失败即停 |
| 修复轮与新会话并发编辑 | 只在空闲窗口跑；用户开口即取消 arm |
| 顾问整会话对账的 token 成本 | 只在该轮判定需要时读会话检索，且限定窗口；SOUL 只注入约束节 |
| SOUL 变成挑措辞的教条 | 只有能引用原句、且落在五节之内的才判；表面类最高 nit |
| 偏好判定变成挑格式毛病 | `violated` 必须能引用卡里原句；表达型偏好只到 concern |
| 自表示漂移无人修 | `self_model` 只在冲突时发；修复轮失败留 note，下一次同类判定再触发 |
