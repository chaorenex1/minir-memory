# adviser 会话模式（research / implement）

状态：草稿 v2（2026-10-26）。v1 的 `multagents` 模式与设计历程已废弃，本文件只描述现行设计，不再保留被推翻的说法。旧档归档在 `cache/scratch/adviser-multi-agent-draft.v1-20261026.md`。

依据：机制事实逐条标了行号，取自本机 Hermes 源码与插件仓库。未实测的列在 §9。

## 1. 一句话

给一个会话定模式：`research` 只做分析与验证，`implement` 按指定草稿写代码。模式存在会话上，通过一条注入消息告诉主会话，由它加载 `minir:adviser-playbook` 取得该模式的行为规则。

## 2. 范围

| 项 | 内容 |
| --- | --- |
| 要解决的场景 | 同一会话里把「研究 / 验证」与「按草稿实现」分阶段，避免边想清楚边改代码 |
| 非目标 | 不做工具级强制；不替代 `hermes-dispatch` 的流程纪律；不做多智能体调度（U1 未决） |
| 做成的信号 | 切模式后下一轮出现 `skill_view`；research 轮 `git diff` 无源码改动 |
| 不适用 | 单点问答；没有草稿的一次性小改 |

## 3. 行为

### 3.1 命令

```
/adviser research      别名：/adviser 研究
/adviser implement     别名：/adviser 实现
```

- 解析：命令词之后的全部文本进 handler（`cli.py:1222`）；handler 签名只有 `raw_args`（`plugins.py:651`），拿不到 session。
- 会话绑定：`pre_command` 钩子提供 `session_key`（`cli.py:1191-1195`），插件缓存当前会话，handler 读它。
- 幂等：同模式重复执行只回显，不重复注入。
- 非法参数：回显用法，不改模式。
- 命名冲突：注册只查内置命令（`plugins.py:658-663`）；斜杠解析顺序是 quick_commands → plugins → bundles → skills（`cli.py:1216-1232`），插件命令会遮住同名技能。
- 模式值入库用 ASCII（`research` / `implement`），中文只在命令层做别名。

### 3.2 切模式后依次发生

1. `set_mode(session, mode)` 写会话模式。
2. 落盘到 `session_modes`。
3. 注入一条 `role="user"` 的消息：一行模式声明 + 要调的 `skill_view`。
4. 下一轮主会话执行 `skill_view`，拿到该模式的行为规则。

```
会话模式=research。请调用 skill_view(name="minir:adviser-playbook")，按其中 research 一节工作；
本模式不修改代码，结论写入用户指定的草稿。
```

```
会话模式=implement。请调用 skill_view(name="minir:adviser-playbook")，按其中 implement 一节工作。
```

`skill_view` 对插件技能只认限定名（`tools/skills_tool.py:680`），裸名找不到。角色用 `user`，因为其它角色会被加 `[role]` 前缀（`plugins_injection.py:17`），重复标记会露出来。

### 3.3 命令返回值

只做终端回显一行（`会话模式 = research`）。它不进模型上下文（`cli.py:1284-1286`），不能用它通知主会话。

### 3.4 恢复与 /branch

- 恢复：从 `session_modes` 读回模式，并重放一次 §3.2 第 3 步。只读回不重放，就是「状态 research、行为 default」，且无信号。
- `/branch`：继承源会话模式。现状是两处用配置默认值重开（`__init__.py:1590,1600`），要改。

### 3.5 投递与存活

- CLI：注入进 `_pending_input`，回合循环把它当输入送进 agent（`cli_loops_mixin.py:218`、`cli.py:1291-1292`）。所以命令之后通常立刻跑一轮：读指令 → `skill_view` → 规则进上下文。用户的下一条输入到来时，模式与规则都在历史里。
- 桌面 / TUI：`inject_message` 的第一条分支是「有 `_cli_ref` 且 `origin` 为空 → 进 CLI 队列」，而 TUI 的 slash 影子 CLI 不读这个队列（`tui_gateway/slash_worker.py:90`；`session_notifications.py:232` 称其 armed-but-dead）。这条路径返回 True 却永不投递。要投到 TUI，必须传 `session_key` 且不传 `origin`，让 TUI injector 接住（`plugins_injection.py:42-45`）。
- 存活：模式声明与 `skill_view` 的结果都只是历史里的普通消息。压缩会把它们和旧对话一起压掉，长会话里模型会「忘记」模式。因此模式不能只注入一次：每轮由 `pre_llm_call` 盖一行 `[session.mode: research]`（插件已有该钩子，adviser 走的就是它），技能正文按需重取 —— 模式一行要一直在，技能正文贵可以重取。

## 4. 状态

### 4.1 载体

`SessionCounter.mode`（`turn_counter.py:92-104`）：每会话一份、单进程内存态，`/branch` 由 `inherit` 带过去（181 行）。当前没有 setter、不落盘、也没有任何代码读它改变行为。

| 缺口 | 补法 | 落点 |
| --- | --- | --- |
| G1 无 setter | `TurnTracker.set_mode(session_id, mode) -> bool`：校验取值域、同值幂等、未知 session 返回 False（不隐式创建）、复用已有锁；`begin()` 只在新建时写 mode，reopen 不覆盖 | `turn_counter.py` |
| G2 不落盘 | 新表 `session_modes(session_id PK, mode, updated_at)` + `CREATE TABLE IF NOT EXISTS` 迁移；provider 在 set 后写、在会话开始/恢复时读回。Tracker 保持「无 I/O」 | `store.py` + `__init__.py` |
| G2' 恢复 | 见 §3.4 | `__init__.py` 恢复路径 |
| G3 消费者 | ①命令幂等；②adviser 的 brief 选择（research 走研究 brief，接现成的 `mode_brief` 分支）；③台账增 `session_mode` 字段 | `adviser_mode.py` + `store.py` |

术语分家：`session.mode`（本文）与 `adviser.mode`（`adviser_mode.py:1001-1004`，取值 watch|adviser_only，config-only，session 参数不生效）是两回事；另有 `history.mode`、`count.mode`。

### 4.2 session_mode 退出配置

配置键、env `MINIR_SESSION_MODE`、manifest 字段、README 说明一并移除；初值保留常量 `default` 作哨兵。

理由：配置是「建会话时盖一次」，模式是运行期状态。同一个键承担两件事，必然出现「改了配置这个会话不生效」。

| 位置 | 现状 | 改成 |
| --- | --- | --- |
| `__init__.py:305-306` | 读 config 并支持 env 覆盖 | 删除，只留常量 |
| `__init__.py:504-505` | 构造 tracker 时给 `default_mode` 并 `begin` | `default_mode` 用常量；`begin` 只作用于新会话 |
| `__init__.py:596` | manifest 声明 `session_mode` 字段 | 删除该字段 |
| `__init__.py:1590,1600` | `/branch` 与新开会话用配置默认值重开 | 继承源会话模式（从 store 读回） |
| `store.py:128+` | 无会话表 | 新增 `session_modes` 与迁移 |
| `verify_against_host.py:68,278` | 断言「从 config 读到 session_mode」并在测试配置注入该键 | 断言改为「不再读配置」；注入行删除 |
| `tests/test_provider.py:654,706` | 用配置设模式 | 改为运行期 `set_mode` |
| `README.md:561,1134` | 文档描述配置键 | 改为描述命令与持久化 |

风险：

- 旧 `config.yaml` 里残留的 `session_mode` 会被静默忽略 → doctor 需要「未知键」提示。
- 恢复重放若挂在多条路径上会重复注入 → 只留一个入口。
- `1590/1600` 改语义后 `/branch` 行为变化 → 一条测试锁住「继承源会话」。

回退：恢复配置读取即回旧行为；`session_modes` 表保留不影响旧代码。

## 5. 技能 adviser-playbook

落点与现有插件技能同构（对照 `plugin/minir/skills/data-layout/`）：

```
plugin/minir/skills/adviser-playbook/
  SKILL.md                  ≤ 60 行
  references/research.md    ≤ 120 行
  references/implement.md   ≤ 120 行
```

frontmatter，description 前 57 字符要能独立成句（技能索引只显示这一行）：

```yaml
name: adviser-playbook
description: "Use when the session mode is research or implement. How to work in that mode."
version: 0.1.0
author: Masia
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [minir, adviser, session-mode, research, implement]
```

SKILL.md 六节，顺序写死：

| # | 节 | 内容 | 行数上限 |
| --- | --- | --- | --- |
| 1 | 模式路由 | 三行表：research / implement / default 各读哪节、产出是什么、不读哪节 | 8 |
| 2 | 共性规则 | 模式以注入文本为准、不自行推断；不猜草稿路径，没有就停；收尾固定三件（改了什么、证据、残余风险） | 10 |
| 3 | research | 目标、允许、禁止、交付、自检各一行；末尾指向 `references/research.md` | 20 |
| 4 | implement | 入口必须有目标草稿；交付按该草稿 §4 验收表逐条打勾；自检每条带命令与退出码；指向 `references/implement.md` | 20 |
| 5 | 换模式 | 用户改模式后，先按新一节复述约束再动手 | 5 |
| 6 | 不适用声明 | default 模式下本技能不适用，读完即停 | 3 |

`references/research.md` 五节：分析范围（现状 / 边界 / 非目标 / 失败样本）、验证手段（只读命令）、结论形态（可被草稿直接引用的证据格式）、边界情况（贴代码片段算不算写代码）、退出条件。

`references/implement.md` 五节：读草稿顺序（§0 一句话 → §2 槽位 → §4 验收）、分批提交与回退、与 `hermes-dispatch` 的分工（只引用不复制）、验收证据格式、失败处理。

刻意不写进技能的三样：

- `session.mode` 的存储与持久化（属插件代码，写进技能就是双份真源）
- 命令语法（技能只认注入文本给的模式名，命令属于入口）
- `hermes-dispatch` 已有的流程纪律（何时停、怎么验证），只引用不复制

## 6. 权限与失败

- 明确不设工具门禁：research 与 implement 放行同一套工具。「不写代码」是规则，写在技能与注入文本里，不是权限。
- 因此唯一可验证形态是事后检测：research 轮结束时 `git diff --stat` 不该出现源码改动。能被发现，不能被阻止。
- 最小台账（S8）：轮末记一行 `session_mode` + `diff --stat` 摘要，不阻断、不告警。没有这一行，research 模式没有验收面。
- 失败面：
  - 模型不调 `skill_view` → 模式声明生效、行为不变，无告警。
  - 注入消息与用户输入竞争 → 切换被吞。
  - 恢复只读回不重放 → 状态与行为分裂。

## 7. 机制依据

| 结论 | 证据 | 实测 |
| --- | --- | --- |
| 命令可注册，冲突只查内置命令 | `plugins.py:647-672`、`658-663` | 读码 |
| 斜杠解析顺序：quick_commands → plugins → bundles → skills | `cli.py:1216-1232` | 读码 |
| `user_args` 是命令词之后的剩余文本 | `cli.py:1222` | 读码 |
| handler 只拿到 `raw_args`，无 session | `plugins.py:651` | 读码 |
| `pre_command` 带 `session_key` | `cli.py:1191-1195` | 读码 |
| 命令返回值只进终端 | `cli.py:1284-1286` | 读码 |
| `inject_message` 在 CLI 进 `_pending_input`（空闲）或 `_interrupt_queue`（回合中） | `plugins_injection.py:20-23` | 实跑 probe，EXIT=0 |
| `role != "user"` 会加 `[role]` 前缀 | `plugins_injection.py:17` | 实跑，出现重复标记 |
| 插件技能需限定名 | `tools/skills_tool.py:680` | 读码 |
| `SessionCounter.mode` 无消费者 | `turn_counter.py:92-104`、`181`，全仓检索 | 读码 |
| CLI 的 `_pending_input` 会被回合循环消费 | `cli_loops_mixin.py:218`、`cli.py:1291-1292` | 读码 |
| TUI 的 slash 影子 CLI 不读该队列 | `tui_gateway/slash_worker.py:90`、`session_notifications.py:232` | 读码 |
| TUI 投递需 `session_key` 且 `origin` 为空 | `plugins_injection.py:20-23`、`42-45` | 读码 |

## 8. 未决与待审

| 编号 | 内容 | 现状 |
| --- | --- | --- |
| U1 | 「多智能体协作」归 research 阶段，还是被这两级整体替代 | 需用户定 |
| U1 建议答案 | 委派式多体进 implement：每个写者一个 `git clone --shared` 目录 + `feature/N` 分支；只读体共用主树；门禁在 merge 不在 commit；不用 git worktree | 待你确认 |
| 建议 | `/adviser implement` 是否必须带目标草稿路径（如 `/adviser implement docs/draft/xxx-draft.md`） | 我建议必须带；多份草稿时不给路径只能猜 |
| V1 | 研究阶段「贴代码片段」算不算写代码 | 初值：算，只描述位置与改法 |
| V2 | 模式切换判据：只由用户决定，还是允许模型建议 | 初值：只由用户决定，模型收尾可提一句 |
| V3 | default 模式下读到本技能后是否立刻停 | 初值：停，不执行任何一节 |

## 9. 未验证

- 注入到 `_pending_input` 的文本会被回合循环送给 agent（消费点已确认）。未观察的是时序：命令返回后立刻起一轮，还是等你下一次回车才被取出。这决定命令回显之后屏幕上会不会马上多一段输出。
- 技能与命令同名的运行时表现（注册不报错、插件命令优先）只读了代码顺序，未实跑。
- 本文件未实现任何东西：未注册命令、未写技能、未改调用点、未动 `config.yaml`。

## 10. 更改记录

| 日期 | 变更 | 作者 |
| --- | --- | --- |
| 2026-10-26 | v2 重写：废弃 `multagents` 与设计历程章节，改为现行设计的单份规格；旧档归档到 scratch | Masia |
