# 规则文件与动态组装草稿

状态：草稿。依据 `doc/base-dialectic-layers-draft.md` 与 `doc/layer-agent-contracts-draft.md`。

目标：把用户卡、AI 卡、Value Markers 两个维度的判定规则从提示词正文里抽出来，变成 6 份独立规则文件，由加载器在每次调用时组装进对应层的 system prompt。

## 1. 六个文件

`2 + 2 + 2` 拆分：两种卡各拆自我表示与偏好/身份，markers 按两个维度拆。

| 文件 | 内容 | 消费者 |
| --- | --- | --- |
| `rules/user-self.md` | 用户自我表示 6 字段 | B1 全文；dialectic 与复盘读 REVIEW CHECKS |
| `rules/user-preferences.md` | 用户偏好 7 字段 | B1 全文；dialectic 与复盘读 REVIEW CHECKS |
| `rules/ai-self.md` | AI 自我表示 6 字段与状态枚举 | B2 全文；dialectic 读 REVIEW CHECKS |
| `rules/ai-identity.md` | AI 身份卡 13 字段与硬字段取证 | B2 全文；dialectic 与复盘读 REVIEW CHECKS |
| `rules/markers-category.md` | 维度 A：K/V/I/M/G 覆盖范围、四态认识状态、10 对判定（带正反例）、3 条特殊规则、topic 与 unit 身份 | B3 全文 |
| `rules/markers-org.md` | 维度 B：六种组织类型的核心问题与覆盖、选择顺序、category×org 组合示例、objects、relations | B3 全文 |
| `rules/advice_target.md` | advice target 词表、准绳、判定类型、coverage 与知识引用 | `adviser:review` 全文 |
| `rules/advice_judgment.md` | severity 上限、blocker 校准、证据与发出门槛、去重、统计 | `adviser:review` 全文 |

窗口摘要与知识页规则不进这 8 份。前者是 8 个字段的结构约定，跟着 B3 的 OUTPUT 走；后者是单层格式，跟着 knowledge_render 走。它们没有跨层复用的需要。

## 2. 文件格式

每份文件是 markdown，带 front-matter：

```yaml
---
id: user-self
version: 1
applies_to: [base:B1]
slot: fields
max_tokens: 350
sections: [contract, review_checks]
---
```

字段含义：

- `id`：文件名去掉扩展名，加载器按它索引。
- `version`：内容版本，进 artifact 的 `rules_hash`。
- `applies_to`：允许注入的层与 pass。加载器校验，不匹配就拒绝，防止 markers 规则被塞进复盘提示词。
- `slot`：注入位置。本期只有 `fields` 一种，插在层核心之后、OUTPUT 之前。
- `max_tokens`：该文件正文的上限，加载器按估算值校验。
- `sections`：必须存在的章节名。缺一个即视为无效文件。

正文固定两个二级标题：

```markdown
## CONTRACT
（拥有该产物的 pass 用的完整规则）

## REVIEW CHECKS
（dialectic 与复盘用的缺陷清单，5 到 8 条，每条一句话）
```

这样同一份文件能给两种消费者：写作者看 CONTRACT，审查者看 REVIEW CHECKS。否则要么审查者拿不到判定标准，要么同一条规则在两处各写一份然后漂移。

## 3. 组装契约

### 3.1 映射表

| 层 | 规则文件 | 选区 | 规则预算 |
| --- | --- | --- | --- |
| base B1 | user-self, user-preferences | CONTRACT | 700 tokens |
| base B2 | ai-self, ai-identity | CONTRACT | 800 tokens |
| base B3 | markers-category, markers-org | CONTRACT | 1 900 tokens（实测） |
| dialectic | 四份卡规则 | REVIEW CHECKS | 450 tokens |
| card_review | 四份卡规则 | CONTRACT | 1200 tokens |
| knowledge_render | markers-category, markers-org | 全文 | 1 900 tokens（实测） |

dialectic 不读 markers 规则，因为它不写 markers（分层草稿 2.3）。复盘读四份卡规则的全文，因为它是唯一允许剪枝的角色，需要完整判定标准而不是缺陷清单。knowledge_render 读 markers 全文，因为合并后的这个 pass 自己决定页面的 category 与 org，不再由 B4 代劳。

### 3.2 组装函数

```python
def build_system(layer: str, pass_id: str, consumer: str) -> tuple[str, str]:
    parts = [CORES[(layer, pass_id)]]
    for rule_id in RULES_FOR[(layer, pass_id)]:
        rule = RULES.load(rule_id, section=consumer)  # contract | review_checks
        parts.append(f'<rules id="{rule_id}" version="{rule.version}">\n{rule.text}\n</rules>')
    parts.append(OUTPUT_CONTRACT[(layer, pass_id)])
    return "\n\n".join(parts), rules_hash(parts)
```

三条硬约束：

1. `<rules>` 块必须排在 OUTPUT 之前。输出契约永远最后一段，规则不能压过它。
2. 规则是运行时读取，不是编译进常量。改文件即生效，不需要重启。
3. 每次组装返回 `rules_hash`，写进 artifact 元数据。回滚或对账时用它确认当时用的是哪一版规则。

### 3.3 加载器

```python
class RulesLoader:
    def load(self, rule_id: str, section: str) -> Rule: ...
```

- 路径：`plugin/minir/rules/<id>.md`；存在 `$HERMES_HOME/minir-memory/rules/<id>.md` 时优先，供 operator 覆写。
- 缓存键：`(path, mtime, size)`。命中即返回解析结果，miss 才读盘。
- 校验顺序：front-matter 完整 → `applies_to` 含请求的层 → `sections` 齐全 → 正文 ≤ `max_tokens × 1.5` → 只允许 `contract` 与 `review_checks` 两个二级标题。
- 覆写文件校验失败：记 warn，回退到随包默认文件。不因为一份坏规则让所有 pass 停摆，但必须在日志里可见。
- 随包默认文件缺失或校验失败：本次调用失败，报 `rules_error`，不写产物。默认文件是代码资产，缺了就是安装坏了，不能静默降级。

### 3.4 预算

规则进入 system prompt，和 artifact、证据抢同一份上下文。当前的分配：

| 项 | 预算 |
| --- | --- |
| base B1 核心 | 900 |
| B1 规则 | 700 |
| B1 窗口 | 约 2400 |
| 合计 | 约 4000 tokens，等于 16000 字符上限 |

B3 是最紧的一层：核心 700、规则 1900、窗口 2400，已经超出 4000 的目标值。这是刻意的：判定规则要写细，只能从窗口里挪预算。代价落在 B4 上，`_MAX_PROMPT_CHARS`（16000 字符）不变，`_fit_window` 最旧优先裁窗口，规则正文从 3271 字符涨到 7047 字符，等于每次少留约 3800 字符证据。要收回预算，先删 `markers-org` 的 EXAMPLES 段和 `markers-category` 的 CATEGORIES 覆盖词，不动窗口。

表里没算基线卡与 `global_cards`。它们在窗口之外，实际余量只有 300 到 500 tokens，所以上表的规则预算是硬上限，不是目标值。

估算方式与分层草稿一致：CJK 一字一 token，其余四字符一 token，再留 10% 余量。不引入 tokenizer。

## 4. 六份文件的正文草案

### 4.1 `rules/user-self.md`

```yaml
---
id: user-self
version: 1
applies_to: [base:B1]
slot: fields
max_tokens: 350
sections: [contract, review_checks]
---
```

CONTRACT 章节：

- WHAT：这张卡描述的是这个人本身，不是他的任务，也不是他想要助手怎么说话。
- FIELDS：六个字段各一段，逐字段给「收什么 / 证据门槛 / 反例」。
  - occupation：角色与领域，一个短句，未说明就空。
  - skills：本人使用的语言、框架、基础设施；学习中的必须标「learning」。
  - thinking_models：反复出现的推理方式，一次措辞不算。
  - interests：长期兴趣与正在探索的领域。
  - communication：表达与协调能力，是能力不是偏好。
  - values_motivation：优化什么、想达成什么。
- EVIDENCE：必须由窗口明说或多次展示。沉默不是证据，一次示例不是模式。
- BOUNDARIES：与 user-preferences 的分界（能力 vs 期望）；会话目标不写进这张卡。
- QUALITY：可自包含、保留精确名称与版本；不重复、不概括成空话。

REVIEW CHECKS 章节（给 dialectic 与复盘）：

- 是否把一次示例写成了长期模式。
- 是否把「助手应该怎样」写进了 communication。
- 是否把当前任务目标写进了卡。
- 是否同一事实出现在两个字段。
- 是否出现无出处的角色或技能。

### 4.2 `rules/user-preferences.md`

CONTRACT：

- WHAT：这个人希望助手怎么工作。
- FIELDS：七个字段，逐字段给收集口径与反例。
  - communication：语言、语气、篇幅、专业程度。
  - presentation：先结论还是先分析；列表还是表格；代码块还是散文。
  - method：先规划后执行、分阶段交付、验证后推进。
  - decision：证据优先、成本优先、速度优先、稳健优先。
  - technology：工具链、部署环境、版本管理习惯。
  - collaboration：主动推进、适时确认、汇报频率。
  - taboos：明确拒绝的表达、方案、操作。
- EVIDENCE：明说优先；未明说时至少要两次一致的行为，且要排除场景偶然。
- BOUNDARIES：不推断；不从助手行为反推偏好；不写会话状态。
- QUALITY：每条都要能指导下一次行动，不能是「喜欢高质量」这类空话。

REVIEW CHECKS：

- 是否从单次行为推断出偏好。
- 是否把能力写成偏好，或把偏好写成能力。
- 是否把场景约束（这次要短）误记成长期偏好。
- taboos 是否带具体触发词或场景。
- 是否出现与 operator 声明冲突的条目。

### 4.3 `rules/ai-self.md`

CONTRACT：

- WHAT：本次会话里助手在做什么、知道什么、卡在哪。会话级，不进身份卡。
- FIELDS：positioning、current_task、current_goal、known、unknown、state、uncertainty。
- STATE：`idle|executing|reasoning|reviewing|blocked|done`，只能取一个值。
- DISTINCTIONS：known 只放窗口证明过的事实；unknown 是待验证的假设，不是「还没读到」；uncertainty 要带原因。
- BOUNDARIES：不写用户信息；不写身份、权限、版本。

REVIEW CHECKS：

- state 是否与窗口事实一致（例如已完成却写 executing）。
- known 里是否混入推断。
- 是否把会话状态写进身份卡。
- uncertainty 是否只是空泛的「可能有风险」。

### 4.4 `rules/ai-identity.md`

CONTRACT：

- WHAT：这个 profile 里助手是谁、被允许做什么。
- HARD FIELDS：id、name、version、type、system、creator、maintainer。值必须与 `plugin.yaml` 逐字一致，找不到就留空。代码会做子串校验。
- SOFT FIELDS：role_duties、domains_level、persona_style、communication_style、permissions、prohibitions。只能转述 operator 文本，不得扩权。
- SOURCES：`_sources` 列出本次实际收到的文件路径。
- PROVENANCE：front-matter `source` 为 review 时，该文件是复盘结论，与上一份卡同级，不得覆盖 operator 声明。
- BOUNDARIES：不写工具、不写已完成工作、不写会话观测。

REVIEW CHECKS：

- 硬字段是否都能在 plugin.yaml 找到原文。
- 是否新增了 operator 没写的权限或禁止项。
- 是否把会话观测（用了什么工具）写进身份。
- `_sources` 是否为空或含未提供的文件。
- review 来源的条目是否被当成 operator 声明使用。

### 4.5 `rules/markers-category.md`

CONTRACT：

- WHAT：一条知识表达的是什么。恰好一个主类，不设双主类。
- CATEGORIES：每类给覆盖范围。K 事实与认知（事实、概念、定义、原理、规律、机制、测量、可验证解释）；V 价值与评价（价值观、标准、偏好、优先级、取舍、评价）；I 目标与意图（目标、需求、计划、目的、预期、目标状态）；M 方法与行动（方法、技术、流程、策略、命令、工作流）；G 规则与规范（规则、制度、约束、权限、禁止、义务、合规要求）。category 与 org 相互独立，主类不决定 org。
- EPISTEMIC：仅 K 允许且必填，四态各一句定义：fact 本窗口观察到；inference 由本窗口证据推出；hypothesis 提出未验证；prediction 关于未来状态。「可能发生」不得记成事实，未验证的因果也不得记成事实。
- TOPIC：名词短语 2 至 40 字，命名这条知识讲什么；它是去重键与文件名。禁整句、疑问句、其他、杂项。
- UNIT IDENTITY：`category + org + topic`；同身份是修订，不新增。
- DISAMBIGUATION：10 对判定，一行一对，按顺序匹配。

| 判定 | 归 |
| --- | --- |
| 描述对象是什么 / 评价对象是否重要 | K / V |
| 描述已存在的事实 / 描述主体想要的结果 | K / I |
| 解释为什么或是什么 / 说明如何操作实现 | K / M |
| 描述现实情况 / 规定允许禁止或必须做什么 | K / G |
| 表达值得追求的判断 / 形成有约束力的要求 | V / G |
| 表达评价取向 / 表达希望实现的状态 | V / I |
| 表达评价标准或取舍 / 表达实现方式 | V / M |
| 表达目的或预期结果 / 表达行动步骤或手段 | I / M |
| 表达想做什么 / 表达必须遵守什么 | I / G |
| 说明怎样完成任务 / 规定必须或禁止怎样做 | M / G |
- 示例：10 对判定每对在规则正文里带正反各一个短例，示例只存于正文，避免两处漂移。
- SPECIAL RULES：描述规范不等于规范。引用法规须求的是 G；讲这条规则的来源、历史、结构或成因是 K，按用途判断。计划与方法在各有独立意义时拆成两条（I 与 M）。事实、推断、假设、预测是 K 内部的不同认识状态。

REVIEW CHECKS：

- 是否出现双主类，或按内容里的名词而不是按论断选类。
- K 是否缺 epistemic，非 K 是否带了 epistemic。
- 是否把评价写成事实，或把仅计划中的能力写成既有事实。
- 计划与方法是否被塞进同一条。
- `must|only|never` 句是否被归成 K，或描述句被归成 G。
- topic 是否是句子、空泛词或超过 40 字。
- 预测或假设是否被记成已观察到的事实。

### 4.6 `rules/markers-org.md`

CONTRACT：

- WHAT：这条知识围绕什么对象组织，也就是页面落在哪个目录。category 与 org 相互独立，任意主类可以落在任意 org。
- ORGS：每条给核心问题、覆盖范围、时间属性。entities 是谁或什么（身份、属性、关系，可长期存在）；trajectories 如何随时间变化（技能、进度、决策演变、版本史，强时间序）；experiences 单次发生了什么与得到什么（一次部署、故障、诊断、复盘结论，过去且单次；留下可复用教训的归这里）；events 单次带日期的发生（发布、故障、迁移、会议、部署，有参与者、地点与结果，不承担可复用教训）；tools 有哪些可调用能力与怎么用（软件、API、CLI、模型、库，含输入输出、限制、依赖、失效场景）；projects 什么持续工作服务什么目标（目标、范围、阶段、里程碑、成员、决策、约束、风险、验收、状态，有生命周期）。
- SELECTION：按身份、时间变化、单次发生（events）、单次教训（experiences）、能力、持续工作依次匹配；两者都像时取「下个会话会去搜的那个对象」，工具用法落 tools，项目决策落 projects，带教训的事件落 experiences。
- EXAMPLES：七条 category×org 组合示例。用户→entities+K；部署项目目标与约束→projects+I+G；调用 Docker→tools+K+M；版本冲突导致部署失败→experiences+K（事件与验证后的因果）；2026-10-23 的发布无事故→events+K（纯发生）；部署从失败到恢复→trajectories+K（状态变化）；事后锁定版本→experiences 或 projects+I+M。
- OBJECTS：其他关联对象写成 `type:name` 单数，最多五个，主 org 不重复。
- RELATIONS：引用同窗口单元用 id，`updates` 可指向已有页面路径或 topic；类型限定 `supersedes|caused_by|part_of|contradicts|updates|instance_of`；`caused_by` 需要正文写明原因或已验证的诊断。
- BOUNDARIES：一个单元只有一个主 org；不同 org 不合并；org 决定归档位置，不决定内容类别。

REVIEW CHECKS：

- 是否把一次经历写成了轨迹，或把轨迹压成一次经历。
- 是否把带教训的事件写进 events，或把纯发生写进 experiences。
- 是否把工具用法写成了项目，或把项目决策写到 tools 下。
- 是否把项目写成实体，或把项目写成一堆事件。
- objects 是否没有 `type:` 前缀、用了复数，或重复主 org。
- relations 是否引用了窗口外的 id 且没有指向已有页面，或写了正文没说的因果。
- 是否同一内容在两个 org 下各存一份。
- 是否按主类反推 org（「因为是 INTENT，所以 projects」）而不是按对象选。

## 5. 与两份主文档的改动

- `doc/layer-agent-contracts-draft.md` 里 B1 的 FIELDS、B2 的 FIELDS 与 GROUNDING、B3 的 MARKERS 三节改为引用 `<rules>` 块，不再内联规则正文。各层的章节摘要表补一行 `RULES`。
- 分层草稿 4.7 的写拒绝清单增加 `rules/`：模型侧不可写，operator 可覆写。
- 规则文件随插件发布，路径 `plugin/minir/rules/`；operator 覆写目录 `$HERMES_HOME/minir-memory/rules/`，不参与知识文件、卡文件、transcript 的清理。
