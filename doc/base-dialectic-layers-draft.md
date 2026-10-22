# base / dialectic 分层契约草稿

状态：草稿，供评审。本文不改代码，只固定需求解释、契约与验收口径。

> 已被 `layer-agent-contracts-draft.md` 取代。v28 起四张卡是 markdown 正文（空串=空卡），
> 没有字段集、没有 `_preset`、没有 manifest 硬字段逐字校验；本文的字段契约与预设描述都已过期，
> 仅作决策历史保留。

## 1. 结论

1. 需求里真正改变架构的只有一条：dialectic 从「base 的依赖下游」变成「独立短周期、常驻校订」。其余各条要么是字段级替换，要么是工具边界收紧。
2. 「会话开始 base 先触发，dialectic 不触发」今天已经成立，但只是依赖关系的副产品（`cold_start` 只排 base 作业，`_dispatch` 直接丢弃 dialectic 触发器）。要变成契约，必须定义无历史的新会话怎么走。
3. 新字段集（用户卡 13、AI 卡 19、窗口摘要 8、markers 两维含 topic）超出当前产物 4000 字符硬上限约 55%。预算必须先定，否则在「超限即失败」的校验下每次刷新都会失败；若退回渲染层截断，就会变成「模型漏写」的假象。
4. 身份卡按你的决定改为模型输出 + FILE 工具取证：硬字段必须能在白名单文件里逐字找到，源 hash 未变就沿用上一份，防止每次重算漂移。
5. FILE 的现状比预期宽：toolsets 缺省时 `enabled_toolsets=None` 等于全部工具（agent_loop.py:147），所以「复制 host file 工具集」实际是先收窄工具面，再加读写范围守卫。
6. 三天复盘是第三个节奏：墙钟、跨会话、可剪枝。它把两张卡落到四个 md 文件上，文件是当前状态，memory.db 只记 revision。两张卡只能有一个 durable 写者，否则 base 与复盘会互相覆盖。
7. `sessions/` 与 `knowledge/` 是两层新的只写产物：前者是审计，后者是从 markers 派生的知识。transcript 不回流 prompt；知识只以路径块回流，正文由主会话主动检索。

## 2. 需求拆解

### R1 周期错开

要求：会话开始 base 触发、dialectic 不触发；base 周期长、dialectic 周期短；两者交叉运行。

现状证据：

- `turn_counter.tick()` 每轮同时推进两层并各自判断 `due()`（turn_counter.py:190-206）。
- provider 的 `_dispatch` 丢弃 dialectic 触发器（__init__.py:717-721），review 由 base 成功发布后入队（worker.py:1321-1324）。
- 因此「dialectic 不触发」不是相位设计，而是依赖造成的；dialectic 的 interval（默认 10）只影响边界 flush 时的 watermark 记账，从不产生 review 作业。
- 会话开始只排 base（__init__.py:886-915），且仅当 `state.db` 里已有该会话的消息。

需要补定义的三件事：相位基准、同轮碰撞、空窗口。

### R2 base 层的四件产物

要求：

- 用户卡 = 自我表示 6 项（职业身份、职业技能、思维模型、兴趣爱好、沟通技巧、价值观与动机）+ 偏好 7 项（沟通、信息呈现、行动方法论、决策、技术、协作、明确禁忌）。
- AI 卡 = 自我表示 6 项（自我定位、当前任务、当前目标、自我认知、当前状态、不确定性）+ 身份卡片 13 组：唯一标识、名称、版本、类型、角色定位与核心职责、专业领域与能力等级、人格风格、沟通风格、权限范围、禁止事项、所属系统、创建者、维护者。
- 窗口摘要 = 8 固定字段。
- markers = 五类。

现状：六个 section（worker.py:91-98），其中四张卡是自由 bullet 数组（每段 480 字符、每条 200 字符、每段最多 8 条），`## Session Summary` 只投影最新 chunk（README:231-241），markers 只有组织类型五个键。

### R3 AI 卡按 profile 隔离

现状：库文件按 profile 分开（`cadence.store_path` → `$HERMES_HOME/minir-memory/memory.db`），但行级 subject 不含 profile：`user_subject() = user:{user_id or 'default'}`、`dialectic_subject() = agent:{agent_identity or 'minir'}`（worker.py:999-1005）。

- 结论：隔离目前是路径级的，不是数据级的。已定：user 键保持 `user:{user_id}`（库本身按 profile 分开）；agent 键改为 `agent:{profile}:{identity}`，把 profile 写进数据。

### R4 Value Markers 两个维度

要求：

- 维度 A（语义分类）：KNOWLEDGE / VALUE / INTENT / METHOD / GOVERNANCE，每单元恰好一个主类。
- 维度 B（组织类型）：Entities / Trajectories / Experiences / Tools / Projects。
- 附 10 对易混判定规则 + 3 条特殊规则（规范描述不等于规范本身、计划步骤要拆 I 与 M、事实/推断/假设/预测要区分）。

现状：只有维度 B，每类最多 2 条、每条 120 字符（worker.py:83-84），没有时间、关系、认识状态字段。

### R5 最终知识单元

要求：内容 + 主语义类别 + 可关联的组织对象（可多个）+ 时间与关系。

现状：marker 只有一句正文，挂在一个组织类型键下。

### R6 窗口摘要

要求：objective、current_state、decisions、constraints、verified_facts、open_issues、next_actions、references，优先保留任务连续性。

现状：`session_chunk_summary` 是每窗自由文本，语义是「压缩记录」，不是「任务状态」。

### R7 用户卡首次预设

要求：用户卡首次为空时，按「软件开发工程师」设定空白的自我表示与偏好。

现状：没有预设，第一次刷新只有窗口证据，13 个字段全空。

要定四件事：预设写什么值、写进哪里、什么时候失效、怎么防止模型把提示当成事实。

### R8 FILE 工具边界

要求：FILE 工具集复制宿主 FILE 工具集；读限制在 HERMES_HOME，写限制在 HERMES_HOME/minir-memory；写不得破坏 root 与 `minir-memory.db*`。

现状：base/dialectic 的 toolsets 取自 `auxiliary.adviser.toolsets`，缺省为空时 `enabled_toolsets=None` 等于全部工具（agent_loop.py:147、model_tools.py:219），FILE 在不在取决于配置。宿主 file 工具集只有 `read_file`、`write_file`、`patch`、`search_files` 四个（tools/file_tools.py:1395-1420），没有删除或重命名工具，所以「删除 root」的实际向量是覆盖写。

### R9 三天复盘

要求：按 3 天周期结合会话历史复盘用户卡与 AI 卡；用户卡写到 `$HERMES_HOME/minir-memory/user/identity.md`、`preference.md`，AI 卡写到 `$HERMES_HOME/minir-memory/agents/<profile>/identity.md`、`preference.md`；memory.db 分别记录两者的变更历史。

现状：没有墙钟节奏，只有轮次节奏；卡只存在于 base artifact 与 `conclusions` 里，没有可读的卡文件，也没有 revision 历史。

### R10 锚点的推进条件

要求：安装时初始化锚点；首跑成功后记录时间；再按 3 天周期运行。

与上一版的差别：上一版在入队时推进锚点（对齐 base 的 dispatch 语义）。这里改为成功才推进，失败要能重试。

### R11 作用域拆分

要求：会话级的四张卡内容按会话隔离存在 memory.db，并且跨会话保留；复盘的四个卡文件是全局累积。

现状：base artifact 本来就是按会话的行，但没有「全局卡」这一层，跨会话只能靠上一份 artifact 间接继承。

### R12 首刷的基线来源

要求：base 首次刷新有全局卡就参考全局；没有全局卡就按工程师身份初始化用户卡，不读其他会话。

现状：durable 合并只来自同 profile 最新的上一份 artifact，不读全局文件，也不会主动去看别的会话。

### R13 Transcript 落盘

要求：`TranscriptBuffer` 把 host 会话历史异步写入 `sessions/<session_id>.json`；本插件 agent 的会话历史异步写入 `sessions/<layer_level>/<layer_session_id>.json`。

现状：`TranscriptBuffer` 只在内存里按会话缓冲，进程退出即丢；aux run 不落任何文件，`AgentRun` 也丢掉了 `messages`（agent_loop.py:58-70）。

### R14 知识生产管道

要求：独立线程、独立队列的异步管道，按 base 层的 Value Markers 生产知识文件，存放在 `knowledge/`。

现状：markers 只进 artifact 与 FTS，没有任何派生知识文件。

### R15 文件预算与语言

要求：用户卡、AI 卡、知识文件的 token 预算 2k，可配置；文件内容用英语，语言可配置。

现状：文件层还没有预算概念，字符上限只约束 artifact（3900 / 4000 字符）；语言跟随对话，没有独立的输出语言设置。

## 3. 与现有契约的差异

| 项 | 现状（证据） | 新要求 | 改动面 |
| --- | --- | --- | --- |
| 触发拓扑 | tick 同时推进两层，dialectic 触发器被丢弃（__init__.py:600, 717-721） | 两个独立 watermark，同轮 base 先 | turn_counter、_dispatch |
| review 入队 | base 发布成功后入队（worker.py:1321-1324） | 按自身周期入队 | provider、worker |
| 产物结构 | 6 个 section、自由 bullet | 4 个 section（两张卡 + 摘要 + markers）、40 个字段 | prompts、render、校验 |
| 窗口摘要 | 最新 chunk 投影（README:231-241） | 8 字段滚动状态 | prompt、合并输入 |
| markers | 5 键、无时间与关系 | 两维 + 关系 + 认识状态 | prompt、JSON、校验 |
| AI 身份卡 | 模型从运维文本推导；file 工具取决于配置（worker.py:1044） | 模型输出 + FILE 固定分配 + 源 hash 栅栏 | toolset、prompt、校验 |
| profile 隔离 | 路径级，subject 不含 profile | 数据级 | subject 键、迁移 |
| 产物预算 | 3900 / 4000 字符（worker.py:77、store.py:64） | 40 字段 | 预算重排 |
| 用户卡首次 | 无预设，13 字段全空 | 软件工程师预设：1 个 occupation 初值 + 12 个空字段 | prompt、校验 |
| FILE 工具 | toolsets 缺省 = 全部工具（agent_loop.py:147）；file 取决于配置 | 固定 file 四项 + 读写范围守卫 | toolset、hook |
| 模型路由 | 四层共用 `auxiliary.adviser` | 按 pass 类别三条 `auxiliary.minir_*`（base 与 dialectic 共用 `minir_dialectic`，复盘 `minir_card_view`，知识 `minir_knowledge`），可各自换模型 | 配置、worker 调用 |
| 卡存储 | 只有 DB artifact | 四份 md 为当前状态，DB 记 revision | 文件层 + 表 |
| 复盘 | 无 | 3 天墙钟、跨会话、允许剪枝 | 新 layer、worker 分支 |
| 卡作用域 | 只有会话级 artifact | 会话快照在 DB，全局卡在四个 md | store、prompt |
| Transcript | 仅内存缓冲 | host 与 aux 都异步落盘 | transcript、worker |
| 知识管道 | 无 | 独立线程按 markers 产出知识文件 | 新模块 |
| 文件预算 | 无（只有 artifact 字符上限） | 每张卡 2k tokens、每个知识文件 2k tokens，可配置 | 写入校验 |
| 文件语言 | 跟随对话 | 默认英语，`content_language` 可配 | prompt、校验 |

明确不改：adviser 链路（review → guard → ledger → delivery）、`prefetch` 只注入最新 `## Base Revision` 的契约、jobs 表结构、notes 表结构。`minir_remember` 只加 `category` 参数，表不变。

## 4. 建议契约

### 4.1 周期：两个独立 watermark，同轮 base 先

- 两层各自维护 watermark，互不派生：base 在 `turns - base.last_trigger_turn >= B` 触发；dialectic 在 `turns - dial.last_trigger_turn >= D` 触发。`D < B` 只做配置校验，不参与相位计算。
- 会话开始：base 的基准作业先跑。有全局卡就以全局卡为基线；没有全局卡就按 4.6 的工程师预设初始化；有历史时窗口覆盖历史。dialectic 的 watermark 初始化为当前轮次，所以会话开始只有 base 触发。
- 同轮碰撞：一个 tick 同时到期时，base 先入队，dialectic 紧随其后入队。单个 worker 线程按队列 FIFO 消费（store 的 oldest-first），base 作业先完成，dialectic 读到的是刚发布的产物。
- 链式跟随：同轮时 base 的作业先完成，dialectic 读到的是刚发布的产物；它的证据窗口仍从自己的 watermark 起算。不要改成从 base 的 `window_to_ts` 起算，否则 16 到 24 这段会被跳过。
- 首作业允许空窗口：会话首个 base 作业即使没有消息也要跑，只生成卡片基线（全局卡或预设）。其余空窗口作业维持 no-op，成本不变。
- 身份源 hash 变化时允许补一个只跑身份阶段的作业：只更新身份卡，其余 section 继承上一份产物，不改 base 的 watermark。
- dialectic 的前置条件只有「本会话存在一份已发布的 base 产物」。首作业保证它成立；base 作业失败时 dialectic 降级为 no-op 并记 `no_base`。
- 边界（session end / pre-compress）：两层都 flush，`_dispatch` 接受 dialectic 触发器；base 先入队，顺序由 FIFO 保证。
- 进程重启：watermark 从会话状态恢复；缺失时按当前轮次初始化，不补跑。

时间表（B=24，D=8）：

| 轮次 | base | dialectic |
| --- | --- | --- |
| 0 | base #1（首作业，允许空窗口） | 不触发（watermark 初始化在 0） |
| 8 | — | #1 |
| 16 | — | #2 |
| 24 | base #2 | #3（同轮，链式跟随） |
| 32 | — | #4 |
| 40 | — | #5 |
| 48 | base #3 | #6 |

每 24 轮 1 次 base 加 3 次 review。相对现状（每周期 1 次）review 成本 ×3，这是独立 watermark 的直接代价，`dialectic_depth` 是唯一旋钮。

### 4.2 独立之后 dialectic 的作业语义

独立不等于可以任意选窗口。建议固定为「常驻校订」：

- 作业载荷 = `base_refresh_id` + 证据窗口 `[artifact.window_to_ts, now]`。
- 只做三件事：纠正与该窗口证据冲突的字段；补齐产物中为空且本窗口有证据的字段；把窗口摘要推进到当前。
- 不得：清空有内容的字段（除非新证据明确否定并引用 uid）；对自己没有证据的 durable 字段下判断；引用窗口外证据（`adviser_session_search` 例外，但必须标注 uid）。
- 写回前做版本栅栏：`base_refresh_id` 仍是该 session 的 latest 且 `window_to_ts` 未变才写；否则丢弃本次改动，记 `stale_base` 事件，不写半截。
- base 从未成功发布时，dialectic 作业降级为 no-op 并记 `no_base`，不得自己充当 base。
- base 作业失败时，其 dispatch watermark 已经前进，但内容未覆盖。dialectic 的证据窗口必须从「最后一次成功产物的 `window_to_ts`」起算，不能从失败作业的 watermark 起算，否则会漏掉失败作业本该覆盖的轮次。

一句话：base 累积，dialectic 只把最新证据并进现有产物；权威不反转。

替代方案（不推荐）：dialectic 只产 diff，由下一次 base 合并。代价是修正最多滞后 B 轮，而 `prefetch` 注入的仍是旧卡。

`## Base Revision` 的内容契约随之调整：不再是一段散文，而是每个被改字段一行的字段路径（如 `User Card/Preferences/technology`），按重要性排序，整体仍受 1200 字符上限约束。review 频率翻倍后，`prefetch` 只会注入最新一份，旧 revision 仍可 `minir_search`。

### 4.3 产物与字段

四个 section 替代现有六个。子块一律用 `###`，因为 `_drop_sections` 只按 `^## ` 切分（worker.py:717-723），用 `##` 拆子块会让跨会话丢弃逻辑失效：

```
## User Card
### Self Representation   (6)
### Preferences           (7)
## AI Card
### Self Representation   (6)
### Identity              (13)
## Window Summary         (8)
## Value Markers          (units)
```

列映射（不改 schema）：

| section | 现有列 |
| --- | --- |
| User Card / Self Representation | user_representation |
| User Card / Preferences | user_peer_card |
| AI Card / Self Representation | ai_self_representation |
| AI Card / Identity | ai_identity_card |
| Window Summary | session_chunk_summary |
| Value Markers | value_markers |

跨会话规则（对应 worker.py:710-714 的 `_SESSION_SECTIONS`）：

- 保留：User Card 两半、AI Identity。
- 丢弃：AI Self Representation、Window Summary、markers（按会话重开）。
- 「首次刷新」= 该 profile 从未发布过任何 base 产物。基线优先级：全局卡文件 > 上一份会话卡（跨会话保留） > 4.6 的工程师预设。
- 会话切换只影响 AI 自我表示、窗口摘要与 markers；User Card 与 AI Identity 跨会话保留，全局文件存在时全局优先。`_SESSION_SECTIONS` 仍只丢弃那三项。
- 「本会话累计」解释为「按窗口合并、跨会话保留」，不因 `/new` 清空用户卡；否则画像永远只有一轮会话，与 profile 隔离的前提矛盾。

字段形状（JSON）：

```json
{
  "user_self": {
    "occupation": "", "skills": [], "thinking_models": [],
    "interests": [], "communication": [], "values_motivation": []
  },
  "user_preferences": {
    "communication": [], "presentation": [], "method": [],
    "decision": [], "technology": [], "collaboration": [], "taboos": []
  }
}
```

```json
{
  "ai_self": {
    "positioning": "", "current_task": "", "current_goal": "",
    "known": [], "unknown": [], "state": "", "uncertainty": []
  },
  "ai_identity": {
    "id": "", "name": "", "version": "", "type": "",
    "role_duties": [], "domains_level": [], "persona_style": [],
    "communication_style": [], "permissions": [], "prohibitions": [],
    "system": "", "creator": "", "maintainer": "",
    "_sources": ["plugin.yaml", "minir-memory/agents/default/identity.md"]
  }
}
```

`ai_self.state` 取固定枚举 `idle|executing|reasoning|reviewing|blocked|done`，对应需求里的待命、执行、推理、审查、阻塞、完成；自由文本会让状态无法聚合。

窗口摘要：8 个键都必须存在；`current_state` 用 `{done, doing, todo}`，`decisions` 用 `[{decision, reason}]`，`verified_facts` 用 `[{fact, evidence}]`，`next_actions` 用 `[{action, priority}]`，`references` 用 `[{kind, ref}]`。空列表合法，`objective` 首轮可空字符串；禁止用 `N/A`、`未知`、`待补充` 占位。

字段边界（防止同一句话写两处）：

- 用户卡「沟通技巧」是能力，偏好「沟通偏好」是期望的表达方式，两者不得重复同一条。
- 用户「价值观与动机」是本人取向；AI「人格风格」是助手表现，不得互相推断。
- 「职业身份」「技能」与窗口摘要 `objective` 分属 durable 与 session，会话结束后不得把任务结论写进用户卡。

Identity 卡来源（已定：模型输出 + FILE 工具）：

- 身份卡由模型输出，代码不装配、不改写。base 的 pass 2 生成，dialectic 可按 `_DIALECTIC_FIELDS` 校订（worker.py:135-140）。
- FILE 工具集固定分配给 base 与 dialectic 路由。现状只取 `auxiliary.adviser.toolsets` 再补 `session_search`（worker.py:1044-1047），`file` 有没有取决于配置。改为代码层并集，不依赖 operator 配置；one-shot 回退没有工具，提示词不得假设有工具（worker.py:119-120 的注释仍适用）。
- 可读白名单写进 tool hint 与 system：本 profile 的 `$HERMES_HOME/minir-memory/agents/<profile>/identity.md`、`preference.md`（history.py:238-242）、`plugin.yaml`。禁止 config.yaml、其他 profile 目录、会话库与密钥文件。
- 取证：`ai_identity._sources` 列出本次实际读取的文件（相对 `$HERMES_HOME`）。校验器要求非空、每个路径在白名单内、文件存在。
- 硬字段防伪造：`id`、`name`、`version`、`type`、`system`、`creator`、`maintainer` 每个非空值必须是 `plugin.yaml` 内容的大小写敏感子串，否则该字段丢弃并记 warn。只认 `plugin.yaml`，不认 identity.md，否则三天复盘可以把版本号写进 identity.md 再被 base 逐字匹配（见 4.8）。软字段（`role_duties`、`domains_level`、`persona_style`、`communication_style`、`permissions`、`prohibitions`）是转述，只做 `_sources` 路径校验，残余风险由 hash 栅栏限制。
- 防漂移：hash 栅栏。pass 前对白名单文件做 sha256，存到 `layer_state` 的 `('identity_meta', user_subject)` 行；hash 未变就沿用上一份身份卡、不重算，hash 变化才重新取证生成。模型仍是唯一作者，代码只决定何时重算。
- 防泄漏：file 工具输出不经宿主 redactor，而产物会落 SQLite 并被 prefetch 注入每个会话。存前用宿主 `redact_for_export`（adviser_mode.py:191-206 的用法，fail-closed）扫一遍 AI Card，出现差异即本次刷新失败、保留上一份产物并记 warn。
- profile 隔离：只读本 profile 目录，缺文件时字段留空，不回退到 default 或其他 profile。

### 4.4 markers 两维契约

```json
{
  "units": [
    {
      "id": "k1",
      "org": "experiences",
      "category": "KNOWLEDGE",
      "epistemic": "fact",
      "topic": "依赖版本不兼容导致部署失败",
      "content": "版本不兼容导致部署失败；锁定依赖后恢复",
      "time": "2026-10-18",
      "relations": [{"type": "caused_by", "ref": "k2"}],
      "objects": ["tools:Docker", "projects:OMP 部署"]
    }
  ]
}
```

校验规则：

- `category` 五枚举必填，恰好一个主类；次类只能通过 `relations` 表达，禁止双主类。
- `epistemic` 仅 K 允许，枚举 `fact|inference|hypothesis|prediction` 且必填；非 K 出现即失败。
- `org` 五枚举必填（主组织对象）；其他关联放 `objects`（`type:name`），可多个。
- I 与 M 同时出现且各自独立时拆成两个单元（特殊规则 2）；引用规范内容按 G 处理（特殊规则 1）。
- 合并键 = `category + org + topic`；同键取新证据更新正文、`time` 与 `relations`，措辞变化不产生新条目。`id` 只在本次输出内有效，不要求模型跨刷新复用。
- `topic` 必填：2 至 40 字的名词短语，说明这条知识讲的是什么（如「Docker 镜像清理」「base 与 dialectic 的相位」）。它是去重键与知识文件名，禁止用整句、疑问句、「其他」「杂项」。
- `relations` 只能引用同一窗口内出现的单元；跨窗口关系用 `objects` 与正文表述，避免模型记忆旧 id。
- 显式 marker：`minir_remember(type, content, category)` 的 `category` 必填，取值同一套 K/V/I/M/G；`type` 仍是五个组织类型。存量 note 没有 category 时按 `null` 展示，不回填、不猜。显式 marker 不进知识管道，因此不需要 `topic`。
- 每轮上限：16 个单元、正文 160 字符、每个单元最多 3 条 relations。

判定矩阵与三条特殊规则放进 prompting.py 的一个常量（现有 `MARKER_TYPES` 旁边），base pass3 与 dialectic 共用同一份。现在 prompt 内联在 worker.py，两处各写一份必然漂移。

### 4.5 窗口摘要

- 语义是单一滚动状态，不是窗口日志。
- 每次刷新输入上一份 8 字段 + 本窗口，输出新的 8 字段全量，不输出增量补丁。
- 存储仍写 `conclusions` 的 `session_chunk_summary` 链（保留审计与 dialectic 证据），但 artifact 只投影最新一份，合并输入不再取整条链。
- 首次刷新允许 `objective` 为空、`current_state` 为空列表；禁止用占位词填充。
- 渲染按 `doc/window-summary-template-draft.md` 的固定模板：八个标题、固定顺序、`####` 三个子状态。标题与顺序是解析契约，改模板要 bump `template_version`；旧 artifact 不重渲染。

### 4.6 用户卡角色预设（首次空白）

- 预设 id：`software-engineer`，只作用于该 profile 的第一次刷新：既没有全局卡，也没有可继承的会话卡。
- 首次初始化 = 工程师预设。只用预设骨架生成用户卡；本会话窗口里有证据就按窗口补，没有就留空，不读其他会话，也不编造。
- 初始化只写会话 artifact，不写全局卡。全局卡只由 3 天复盘产生；首刷到首次复盘之间，新会话继承上一份会话卡。
- 已有全局卡或可继承的会话卡时，预设不生效。
- 初始内容：`occupation` 按会话语言渲染（中文会话写「软件开发工程师」，英文会话写 "Software Development Engineer"）；其余 12 个字段为空；`_preset` 记录来源。它进的是 artifact，所以跟对话语言，不跟 `content_language`。空字段渲染为无 bullet 的标题，不写「待补充」。
- 读法：预设给 occupation 一个初值、其余 12 项空白。若你要的是「全空骨架 + 角色化提示」，删掉初值这一句即可，其余条款不变。
- 字段提示（每类字段该找哪些证据：语言与框架、基础设施、调试与权衡习惯、评审标准、协作方式）放进常驻 system 提示，不进产物。预设只管那一个初值。
- 证据优先：第一份 base 若在 identity.md、preference.md 或窗口里读到别的角色，直接覆盖 occupation 并清掉 `_preset`；这不算 clear，不受「不得清空」规则约束。
- 只在首次生效：已有全局用户卡就不再注入；预设写过的值以 `_preset` 标记，证据覆盖后清标记；改预设不回写历史卡。
- 反抄写：提示里的示例词若出现在卡值中，该次刷新判失败。模型把提示当事实是这类模板最常见的退化。
- 预设不进入 AI 卡、窗口摘要、markers。

### 4.7 FILE 工具的边界

工具面：

- 复制宿主 file 工具集的四个工具与 schema：`read_file`、`write_file`、`patch`、`search_files`（tools/file_tools.py:1395-1420）。不新增工具、不改名、不换 schema。这里的「复制」指同一套定义与 handler 由同一 registry 提供、插件侧只加范围守卫，不重新实现四个工具。
- 缺省 `path` 的工具先按 cwd 解析再判定，落在读根外即拒绝。
- base 与 dialectic 的 `toolsets` 固定为 `("file", "session_search")`，不得传 None。`enabled_toolsets=None` 意味着全部工具（agent_loop.py:147、model_tools.py:219），这是当前配置缺省时的实际行为，也是本次要关掉的缺口。
- 不授予 `terminal`、`shell`、`computer_use`、`browser`：任何一个都能绕过路径守卫。

范围：

| 操作 | 根 | 额外拒绝 |
| --- | --- | --- |
| 读 | HERMES_HOME，排除 `config.yaml`、`.env`、`credentials*` | `..` 逃逸、UNC、`\\?\` 前缀、盘符后冒号（ADS）、保留设备名、解析后落在根外的 symlink 或 junction |
| 写 | HERMES_HOME/minir-memory | 写根目录本身；`**/minir-memory.db*`（含 -wal、-shm、-journal）；`agents/`、`user/`、`knowledge/`、`sessions/`、`rules/` 五个目录整体；唯一允许模型写的是 `scratch/` |

- 卡、知识、transcript 三类产物都只允许对应的代码路径写，模型侧工具一律只读，唯一例外是 `scratch/`。放开写等于让模型先改文件再引用它，身份卡的子串校验会退化成自证；knowledge/ 可写则等于可以自己制造知识。
- 宿主 file 工具集没有删除与重命名，所以「不能删除 root」等价于「不能覆盖写 root 与 DB 文件」。

实现与失败行为：

- 强制点是 `pre_tool_call` hook，返回 `{"action": "block", "message": ...}`，message 成为该次工具结果（`_get_pre_tool_call_directive_details`，hermes_cli/plugins.py）。
- 作用域用 ContextVar 标记 minir aux run。工具执行线程提交任务时会复制 context（tool_executor.py:944、1410），守卫因此有机会在工具线程里仍认得出 minir；这条不能假设，P3 的探针必须先证明「aux 内拦截、主 agent 不拦截」。未标记时 hook 直接放行。
- `set_thread_tool_whitelist` 是 `threading.local`（plugins.py:1948），只约束同线程路径，不能当唯一屏障，只能作为工具名白名单的补充。
- 同一 pass 连续 3 次 block 即终止并判 pass 失败，不写产物；hook 在 minir 作用域内抛异常按拒绝处理（fail closed）。
- 路径判定顺序：展开 `~` → `realpath` → `normcase` → 与根做 `commonpath` 全等比较。不要用字符串前缀比较，`C:\x\minir-memory-2` 会被 `C:\x\minir-memory` 前缀误判。

残余风险：

- 密钥文件已从读根排除（`config.yaml`、`.env`、`credentials*`）。`redact_for_export` 仍是最后一道，因为身份与偏好文件里也可能被手写进密钥。
- 写根覆盖 `minir-memory/agents/`，不拒绝 identity.md 与 preference.md 就等于身份链自证（D8）。

### 4.8 三天复盘（用户卡与 AI 卡）

定位：第三个节奏，按墙钟 3 天、跨会话。它不发布 base artifact，只改 durable 卡文件并写变更历史。

触发与窗口：

- 锚点 `last_success_at` 存 `layer_state('card_review', user_subject)`，安装时初始化为当前时间，所以第一次复盘落在第 3 天，不对空历史做复盘。
- 调度是独立线程，不挂在 session start 或 turn 边界上：provider initialize 时起一个 daemon 线程（`card_review_scheduler`），每 10 分钟醒一次，判断 `now - last_success_at >= 3d`。到期且没有 pending/running 的 card_review 作业时才入队（按 layer + subject 去重）。作业 `layer='card_review'`、`session_id=''`，窗口 `[last_success_at, now]`。jobs 表的 `session_id` 有默认空串，不需要为跨会话作业改 schema。
- 线程在 shutdown 时 join。进程不在就没有复盘，下一次启动立刻补一次，窗口合并。
- 停机跨过多个周期只补一次，窗口合并，不补跑多次。
- 成功发布后写回 `last_success_at = 作业开始时间`，attempts 归零。失败不推进锚点，attempts+1，按 30 分钟、2 小时、6 小时退避重试；连续 3 次失败停止重试，等下一个 3 天窗口。这是与 base 的 dispatch 语义刻意不同的一点：复盘宁可晚，不可跳。

输入：

- 基线是四份现有卡文件（含 front-matter）。复盘在基线上合并，不重建，否则手改内容会被抹掉。
- 主输入是窗口内发布的 base artifact，每会话取最新一份，字段为用户卡、AI 卡、窗口摘要、markers，用来提取窗口内的新证据。
- 次输入是显式 markers 与 `conclusions` 行。
- 会话历史按需取，且有上限：最多 20 个会话、单会话最多 200 条消息、总字符 120k。超出按时间倒序截断，并在 revision 里标 `truncated`。
- 只读本 profile。

输出与存储：

作用域：

| 数据 | 作用域 | 存储 | 写者 |
| --- | --- | --- | --- |
| 四张卡的会话快照 | session | memory.db `base_refreshes` 的四列 | base |
| 四份卡文件 | profile（全局） | `user/`、`agents/<profile>/` 下四个 md | 3 天复盘 |
| 卡变更历史 | profile | `card_revisions` | 3 天复盘；人工编辑记 `operator_edit` |
| 知识文件 | profile | `knowledge/` | 知识管道 |
| host 与 aux 会话原文 | session | `sessions/` | TranscriptBuffer |

- 会话卡是快照，不是 durable：新会话从全局文件起步，不从上一会话的 artifact 继承。

| 卡 | 文件 | 内容 |
| --- | --- | --- |
| 用户卡 | `$HERMES_HOME/minir-memory/user/identity.md` | 自我表示 6 字段 |
| 用户卡 | `$HERMES_HOME/minir-memory/user/preference.md` | 偏好 7 字段 |
| AI 卡 | `$HERMES_HOME/minir-memory/agents/<profile>/identity.md` | 身份卡 13 字段 + 自我定位 |
| AI 卡 | `$HERMES_HOME/minir-memory/agents/<profile>/preference.md` | 人格、沟通、权限与禁止、协作方式 |

- 文件头加 front-matter：`schema`、`scope`、`part`、`generated_at`、`source`（operator|review|operator_edit）、`evidence`（会话与 artifact id）、`reviewer`。没有它，base 分不清「运维声明」与「复盘结论」。
- 会话态字段（当前任务、当前目标、当前状态、不确定性、窗口摘要）只留在 artifact，写进文件即校验失败。
- 复盘是唯一允许删除条目的角色。每次删除要记 reason 与证据；base 与 dialectic 的「不得清空」不适用于它。

文件是当前状态，DB 是历史：

- 新表 `card_revisions`：`id`、`scope`（user|ai）、`part`（identity|preference）、`path`、`reviewed_at`、`before_hash`、`after_hash`、`diff`、`changed_fields`、`source`、`evidence`、`reviewer`、`status`。索引 `(scope, part, reviewed_at desc)`。
- 正文不再全量存一份 DB。文件是 canonical，DB 只存 diff 与 hash。「分别记录」由 `scope + part` 过滤实现，不建两张表。
- 写入顺序：先原子写文件（temp + `os.replace`），再插 revision 行。中途崩溃时，下次打开用文件 hash 与最后一条 `after_hash` 比对，不一致就补一条 `source=recovered` 记录，不重写文件。

人工编辑：

- 复盘前算文件 hash。与最后一条 `after_hash` 不同说明 operator 手改过，本次以文件为基线合并，不覆盖，记 `source=operator_edit`。
- operator 手改的字段优先于复盘推断，除非窗口证据明确否定且 change 里写了理由。
- 首次接管：目标文件已存在且没有 front-matter（旧的人工文件）时，先复制为 `<name>.declared.md` 并记一条 `source=operator_edit` revision，再写新文件。之后 base 读 `<name>.md`，`.declared.md` 只留作人工审计，不参与合并。

与 R8、base 的边界：

- 复盘走代码路径写文件，不走模型的 file 工具，D8 继续成立；D8 的拒绝清单补上 `user/identity.md` 与 `user/preference.md`。
- base 读这四份文件作为 durable 输入；`user/` 两份是新路径，history.py 的 `identity_paths` 要扩展或新增加载器。
- base 刷新的 durable 基线是一条优先级链：全局卡文件 > 上一份会话卡 > 4.6 初始化。artifact 里的卡是「基线 + 本会话证据」的快照。
- 全局文件存在时它就是唯一基线，不再比较版次；全局文件缺失时靠会话卡跨会话继承，直到第一次复盘产出全局卡。
- 身份硬字段（id、name、version、type、system、creator、maintainer）的校验只认 `plugin.yaml`，不认 identity.md。否则复盘可以先把版本号写进 identity.md，base 再逐字匹配到它，形成闭环。

失败行为：

- 复盘失败不影响 base/dialectic；锚点不推进，按上面的退避重试，只记 error。
- 任一文件校验失败（缺 front-matter、含会话态字段、字段名不在 schema 内）：本次整体失败，四个文件都不写。
- 文件写成功但 revision 插入失败：不回滚文件，下次触发时补记。

- 成本：每 3 天一次，读 3 天的 artifact 加最多 20 个会话，写 4 个文件。单次比 base 刷新重，频率低。
- 复盘 pass 有自己的输入预算（120k 字符），不套用 base 的 `_MAX_PROMPT_CHARS = 16000`，需要在配置里单列，否则预算检查会误判。

### 4.9 Transcript 落盘

两个来源、两个路径：

- host 会话：`$HERMES_HOME/minir-memory/sessions/<session_id>.json`。
- 插件 agent 会话：`$HERMES_HOME/minir-memory/sessions/<layer_level>/<layer_session_id>.json`，`layer_level` ∈ base | dialectic | card_review，`layer_session_id` 由插件生成（`<job_id>-<attempt>` 或 uuid），并在文件头记录。

格式与写入：

- 单个 JSON 对象：`{session_id, layer, started_at, updated_at, model, provider, toolsets, system_message, messages[]}`。去抖 30 秒或累计 50 条消息原子重写（temp + `os.replace`）；session end 与 pre_compress 立即 flush。
- 单文件保留最近 2000 条消息，超出写 `<session_id>.part2.json` 分片，不截断已写内容。
- 独立写线程 + 有界队列（2000 条），满时丢最旧并计数，永不阻塞回合。写失败重试 2 次，仍失败丢该批并记 error。文件是审计，不是事实来源，state.db 才是。
- `AgentRun` 目前不带 `messages`（agent_loop.py:58-70），要加一个字段，或提供 `run_capture()`。`conversation_loop` 的返回值已包含 messages（agent_loop.py:159-166 只取了 final_response 与 api_calls），不拿就只剩 prompt 与 answer，没有工具调用过程。
- 保留期 30 天，由 3 天复盘的清理步骤删除过期文件。模型工具不能删（4.7 的拒绝清单）。
- 隐私：文件里是完整 prompt，含身份文件与 markers 原文；读根是 HERMES_HOME，所以模型能读。接受，或把 `sessions/` 加进读拒绝清单。

### 4.10 知识生产管道

- 触发：base 发布成功后，对比该次 artifact 的 markers，新增或内容变化的单元入队；同 topic 的单元不入队。是否已生产以文件存在性判断，不额外建表。
- 独立线程 + 独立队列：每 profile 一个消费者线程，队列上限 512，满时丢最旧并计数。base 发布不等待它，管道失败不影响 base、dialectic、复盘。
- 文件：`$HERMES_HOME/minir-memory/knowledge/<org>/<category>-<topic-slug>.md`，例如 `tools/METHOD-Docker-镜像清理.md`。文件名必须说明主题：`<category>` 是 K/V/I/M/G，`<topic-slug>` 由 `unit.topic` 规范化得到，保留中英文与数字，空格转 `-`，去掉路径分隔符与控制字符，避开 Windows 保留名与冒号，长度上限 60。首行 `# <topic>`。front-matter 写 id、category、epistemic、org、objects、time、relations、summary、source_session、source_artifact、produced_at；正文是 unit.content。`summary` 是一句话，供注入块使用。
- 幂等：`(category, org, topic)` 是单元身份，同一身份只保留一个文件。证据更新时原子重写并递增 `revisions`；证据更旧则跳过并计数。
- 主题分裂：`Docker` 与 `docker` 这类差异由规范化消除；仍分裂成两个主题页就都保留，不做相似度合并，避免引入 embedding 依赖。
- 失败：重试一次，仍失败写到 `knowledge/_failed/<category>-<topic-slug>.json`（含错误与原始 unit），不阻塞队列。
- 索引：v1 不做索引，`search_files` 直接扫目录；数量过千再谈索引。
- 边界：只有知识线程写 `knowledge/`；复盘不碰它；模型侧工具对 `knowledge/` 只读。

### 4.11 知识注入：advice_target=knowledge，只注入路径

- 具体知识不进入 prefetch 正文。prefetch 只多一个路径块，正文由主会话按需读取。
- 新目标：`prompting.py` 增加 `RETRIEVAL_TARGETS = ("knowledge",)`，并加入 `DELIVERED_TARGET_NAMES`（adviser_mode.py:46）。它不进 `ADVICE_TARGET_GROUPS`，因为 adviser 模型不该生成知识指针；它是机器生成的路由标签。
- 渲染：独立的 `render_knowledge_block`，头尾 `[knowledge — retrieve on demand]` / `[end knowledge]`，每行 `- <category>/<org> · <topic> · <相对路径>`，例如 `- METHOD/tools · Docker 镜像清理 · knowledge/tools/METHOD-Docker-镜像清理.md`。上限 6 条、700 字符，超出按分数截断。
- 选择：对当前 user 消息做确定性打分，`2×objects 命中 + 1×summary 词重叠 + 0.5×时效`，取前 6。只读 front-matter，不读正文，否则每轮都要扫全部知识文件。不做 embedding，不引入新依赖。
- 去重：同一路径连续 3 轮不重复出现，除非文件在此期间被重写（内容指纹变化）。
- 位置：与 `## Base Revision` 同一注入点、独立成块，不混进 `[adviser]` 块。advice 块有 severity 与 floor 过滤，指针没有严重性，混进去会被当 nit 丢掉，还会挤占 `MAX_ADVICES = 3`。
- 账本：指针投递记 `kind=pointer` 事件供审计，不参与 adoption，不触发 guard 或 escalation。
- 主会话侧：块内只有路径没有正文，指令写明「相关才读」，读取用 `read_file`（读根含 HERMES_HOME，`knowledge/` 可读）。
- 没有知识文件时不出块，不占预算。

### 4.12 文件预算与语言

预算口径：

- `card_token_budget = 2000`：**每个卡文件** 2000 tokens，四份各自独立（`user/identity.md`、`user/preference.md`、`agents/<profile>/identity.md`、`agents/<profile>/preference.md`）。不设「一对合计」，也不允许文件之间借用额度：一份超了就是这份超了。
- `knowledge_token_budget = 2000`：每个知识文件 2000 tokens，与卡文件同一口径。
- 这几个键与 `context_cadence`、`dialectic_cadence` 同级，改配置即生效，不需要迁移。

计数方式：

- 不引入 tokenizer 依赖。估算 `tokens = ceil(cjk_chars + other_chars / 4)`，CJK 按 1 字 1 token，其余按 4 字符 1 token，再留 10% 余量。content_language 默认英语，这个估算偏保守。
- 精确值不是目标，预算是防膨胀的闸门。宿主 venv 里有 litellm 的计数器，将来要精确再换，不现在背依赖。

执行与失败：

- 写入前校验。超标先重试一次压缩（按字段权重砍低价值条目），仍超标判本次写入失败，保留旧文件，不写半截。
- 知识文件按主题整体重写，超预算时先删最旧的证据行，再压正文；`revisions` 与 front-matter 不受影响。
- 读取侧：四份文件不会整份进 prompt，否则 8k tokens 起步。base 注入的是投影：每份文件最多 `card_injection_tokens = 400`，按字段取前 N 条，块尾注明「完整卡在文件里，可用 read_file 读取」。投影额度单独可配，和文件预算互不影响。
- 四份投影合计约 1600 tokens，留在 `_MAX_PROMPT_CHARS = 16000`（约 4k tokens）以内。若把 `card_injection_tokens` 调到 600，四份就是 2400 tokens，这时必须把上限提到 24000（约 6k tokens）。两个参数联动，改一个要看另一个。
- 知识块本身仍受 700 字符上限，与文件预算无关。

语言：

- `content_language = "en"` 默认。可设 `zh` 等，取值只影响值的语言。
- 标题与字段名永远用英语（`## Occupation`、`## Preferences`），因为解析器按固定键读。翻标题等于把 schema 也翻了。
- 值的语言跟随 `content_language`；预设的初值也是（`occupation: "Software Development Engineer"`）。
- 知识文件的 `topic` 同样用 `content_language` 生成，否则文件名与正文语言不一致。正文由知识管道调一次模型把 unit 内容渲染成该语言，一个新主题一次调用；重复生产不重复调用。
- artifact 与对话语言不受这条约束，按原样跟随用户。

## 5. 预算

两套预算不要混：本节表格是 **artifact 的字符预算**（每轮注入 prompt 的快照）；文件层的 **token 预算**（每对卡 2k、每个知识文件 2k）在 4.12。

现状硬上限：worker `_MAX_STATE_CHARS = 3900`，store `_MAX_CONTENT = 4000`（worker.py:77、store.py:64）。section 级上限 480 字符、summary 660、markers 1180。

按最小可用预算估算新字段集：

| 区块 | 预算 |
| --- | --- |
| 用户卡 13 字段 × 80 | 1040 |
| AI 自我表示 6 字段 × 80 | 480 |
| AI 身份卡：7 个配置字段 × 30 | 210 |
| AI 身份卡：6 个内容字段 × 100 | 600 |
| 窗口摘要 8 字段 × 110 | 880 |
| markers 16 × 180（含 topic） | 2880 |
| identity `_sources` 路径 | 120 |
| 合计 | 约 6210 |

超出 4000 约 55%。三个选项：

1. 提高上限到 8000（store 与 worker 同步改）。影响所有 conclusions 内容行，FTS 与搜索不受影响。
2. 压缩字段数：identity 只存 6 个关键字段，markers 上限 8，用户卡每字段 60 字符。
3. 分层存储：artifact 只放摘要，完整卡另存并在 prompt 中按需取。

建议先量一次真实字符分布再定，不拍脑袋；默认倾向 1 + markers 上限 8。

无论选哪个，上限都由校验器执行：产物超出上限即该次刷新失败、保留上一份产物并重试一次，不靠渲染层截断。40 个字段静默截断会变成「模型漏写」的假象，比失败更难查。

其他预算风险：

- 判定矩阵与三条特殊规则进 system prompt 后，base 的 system 体积上升，需与 `_MAX_PROMPT_CHARS = 16000` 的 user prompt 分开统计。
- 40 个字段如果每个都要求「有内容」，模型会开始编造。校验必须是「允许空、禁止占位词」，不是「必须非空」。
- FILE 工具把可读面扩到本机文件系统，白名单只是提示词与产物校验的双重约束，`redact_for_export` 是最后一道。不要因为「模型需要更多上下文」放宽白名单。
- 磁盘：`sessions/` 与 `knowledge/` 是新的增长点。按单会话 2000 条、30 天保留估算；knowledge 按单元数增长，重复单元不新增。过期清理挂在 3 天复盘上，不单独起线程。
- 队列：transcript 写队列与知识队列都是有界的，满时丢最旧并计数，绝不阻塞回合与 base 发布。
- 预算分层：单文件 2k 是存储与复盘口径，满额四份就是 8k tokens，绝不整份进 prompt。注入只走 400 tokens/文件的投影，`read_file` 按需取全文。否则每次 base 光读卡就超过 4k tokens。

## 6. 决定记录

已定：身份卡由模型输出 + FILE 工具取证；FILE 固定分配并收窄；首刷只写会话卡、不写全局；会话卡跨会话保留，全局卡由复盘累积；复盘是独立线程调度、成功才推进锚点；user 键 `user:{user_id}`，agent 键 `agent:{profile}:{identity}`；读根排除 `config.yaml`、`.env`、`credentials*`；知识一单元一文件，文件名带 org 与 category；知识不进 prefetch 正文，改走 `advice_target=knowledge` 的路径块。

| # | 问题 | 已定 |
| --- | --- | --- |
| D1 | 无历史的新会话 base 是否跑 | 跑。有全局卡用全局，没有全局用工程师预设，不读其他会话；空窗口也跑，只生成会话卡基线 |
| D2 | 同轮碰撞 | 两个独立 watermark；同轮 base 先，dialectic 链式跟随（读新产物），窗口仍按自身 watermark |
| D3 | dialectic 写入方式 | 就地改 + 版本栅栏 |
| D4 | 旧数据 | 直接删除。升级时删 `base_refreshes` 与其 FTS 行，不做兼容层；`conclusions` 与 notes 保留，它们是独立资料 |
| D5 | `minir_remember` 维度 A | 加。`category` 必填（K/V/I/M/G），`type` 仍是五个组织类型 |
| D8 | 卡文件写入 | 只有三天复盘走代码路径写；模型侧一律拒绝 |
| D11 | 卡文件是否拆分 | 不拆。`identity.md`、`preference.md` 单文件，复盘输出，front-matter 标 `source` |
| D15 | 2k 预算是按文件还是按卡 | 按文件。四份卡文件与知识文件各自 2k，不互相借用；注入另设每文件 400 tokens 的投影额度 |

## 7. 验收测试

| 文件 | 断言 |
| --- | --- |
| tests/test_turn_counter.py | B=24、D=8：base 触发于 0、24、48；dialectic 于 8、16、24、32、40、48 |
| tests/test_turn_counter.py | 同轮 24：base job id < dialectic job id；dialectic 窗口仍是 16 到 24，不因同轮被清空 |
| tests/test_turn_counter.py | 无历史新会话：第 0 轮有 base 作业（预设基线），第 8 轮前没有 dialectic |
| tests/test_provider.py | 首个 base 作业允许空窗口；第二个起的空窗口作业 no-op |
| tests/test_provider.py | 边界 flush 同时排两层时，base job id < dialectic job id |
| tests/test_provider.py | `_dispatch` 接受 dialectic 触发器；compression gate 下仍然不排 |
| tests/test_worker.py | review 作业期间 base 发布新版本：旧 review 不写回，记 `stale_base` |
| tests/test_worker.py | base 从未成功：dialectic 作业 no-op，记 `no_base` |
| tests/test_worker.py | markers：缺 category、K 缺 epistemic、非 K 带 epistemic、双主类，各一条失败用例 |
| tests/test_worker.py | 窗口摘要缺键失败；空列表与首轮空 objective 通过 |
| tests/test_worker.py | 用户卡 6+7 渲染；跨会话保留用户卡与 identity，丢弃 AI self 与摘要 |
| tests/test_worker.py | identity 的 id/version 与 plugin.yaml 不一致时该字段被丢弃，一致时保留 |
| tests/test_worker.py | 源 hash 未变时沿用上一份身份卡，不发起 identity 生成 |
| tests/test_worker.py | 身份源变化 + 空窗口：只跑身份阶段，User Card 从上一份产物继承，不被清空 |
| tests/test_worker.py | `_sources` 含白名单外路径时整次刷新失败 |
| tests/test_store.py | 两个 profile 的 identity 行 subject 不同 |
| tests/test_worker.py | 首次无卡：occupation=软件开发工程师、其余 12 字段空、带 `_preset`；已有卡不注入 |
| tests/test_worker.py | 证据覆盖 occupation 时清除 `_preset`，且不计为 clear |
| tests/test_provider.py | base 与 dialectic 路由的 toolsets 恒为 `("file","session_search")`，不为 None |
| tests/test_provider.py | 守卫：读 HERMES_HOME 外拒绝；`..` 逃逸拒绝；写 minir-memory 子目录允许；写根目录、`minir-memory.db-wal`、identity.md 拒绝 |
| tests/test_provider.py | 作用域外（无 ContextVar）同样的写不被拦，证明不污染主 agent |
| tests/test_worker.py | 复盘判断：`last = now - 3d - 1s` 入队；`now - 2d` 不入队；停机 10 天只入队一次且窗口合并 |
| tests/test_worker.py | 复盘产出四个文件，front-matter 完整，不含会话态字段 |
| tests/test_worker.py | 手改文件后复盘：以文件为基线，记 `operator_edit`，不覆盖手改内容 |
| tests/test_worker.py | 任一文件校验失败：四个文件都不写，revision 无新增 |
| tests/test_worker.py | identity.md 写入 `version 9.9` 不会被 base 的硬字段校验接受，硬字段只认 plugin.yaml |
| tests/test_worker.py | 锚点：安装后 72h 内不入队；成功后推进；失败不推进且按 30m/2h/6h 退避；pending 期间不重复入队 |
| tests/test_worker.py | 新会话的 base 卡继承上一份会话卡；全局卡存在时全局优先；首刷不写全局文件 |
| tests/test_worker.py | 首刷：只用预设生成会话卡，不读其他会话，不写全局；有全局卡或会话卡时忽略预设 |
| tests/test_worker.py | `minir_remember` 缺 category 被拒绝；存量 note 以 category=null 正常读取 |
| tests/test_worker.py | 调度：没有活动会话时也按时入队；有 pending 作业时不重复入队 |
| tests/test_worker.py | 知识块：最多 6 条、700 字符、只有路径没有正文、不进 adviser 账本 |
| tests/test_worker.py | 读根拒绝 `config.yaml`、`.env`、`credentials*` |
| tests/test_store.py | user subject 为 `user:{user_id}`，agent subject 为 `agent:{profile}:{identity}` |
| tests/test_worker.py | 文件预算：单文件 2001 tokens 拒绝、1999 通过；另一份文件有空余也不能补给它；超限重试压缩一次；仍超限保留旧文件 |
| tests/test_worker.py | 语言：`content_language=en` 时值与 topic 为英语、标题仍为英语键；切到 `zh` 时值变中文、键不变 |
| tests/test_worker.py | 知识正文按 `content_language` 渲染，同一主题重复生产不再调模型 |
| tests/test_worker.py | transcript：host 与 aux 路径正确；enqueue 不阻塞（单次 < 5ms）；2000 条分片；30 天清理 |
| tests/test_worker.py | 知识：文件名由 topic 决定且稳定；同 topic 重写不新增文件；不同 topic 两个文件；证据更旧跳过；失败进 `_failed`；队列满丢最旧并有计数 |
| tests/test_store.py | `card_revisions` 的 scope 分别为 user 与 ai，按 scope 查询互不混淆 |
| tests/test_store.py | 产物超限即刷新失败并保留上一份产物，不发生静默截断 |

## 8. 实施顺序

每个阶段独立可验证、可回滚。

1. P1 周期：两个独立 watermark、同轮 base 先与链式跟随、首作业空窗口、边界 flush、worker 入队开关。不动字段契约。
2. P2 字段契约：prompting 常量、三段 base pass、用户卡预设、dialectic system prompt、校验、渲染、预算上限。
3. P3 markers 两维、`minir_remember` 的 category、identity 取证与 hash 栅栏、FILE 范围守卫、旧数据删除、subject 键。
4. P4 三天复盘：独立调度线程、锚点成功推进与退避、卡文件读写、front-matter、`card_revisions`、基线优先级链、首刷初始化、token 预算与 `content_language`。
5. P5 Transcript 落盘：两条路径、去抖与分片、`AgentRun.messages`、保留期清理。
6. P6 知识管道：独立线程与队列、`<org>/<category>-<topic>` 命名、失败隔离、按 `content_language` 渲染正文、`advice_target=knowledge` 路径块。
7. P7 README 与 `scripts/verify_against_host.py` 对齐，补一条真实宿主冒烟。

验证命令：`python -m pytest -q tests/test_turn_counter.py tests/test_provider.py tests/test_worker.py tests/test_store.py`，P4 追加 `python scripts/verify_against_host.py`（需真实 profile）。

## 9. 本次分析中确认不构成问题的一点

`TranscriptBuffer._edges` 是 per-session、取最大值（transcript.py:48、67）。两层各自 dispatch 时，后到的 dialectic 窗口会把 session edge 推到更晚，只会让新消息时间戳更靠后，不会把消息挤进已派发窗口或造成 base 漏读（base 的 from_ts 用它自己的 watermark）。因此两层共用一个 edge 是安全的，不需要按层拆分。
