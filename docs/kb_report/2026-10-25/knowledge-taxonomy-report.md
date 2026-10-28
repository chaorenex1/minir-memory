# 知识分类规则复查报告

日期：2026-10-25
范围：10-20 至 10-25 的会话历史、minir 知识页、value marker 输出、写页失败件与任务队列
对象：`plugin/minir/rules/markers-category.md`、`plugin/minir/rules/markers-org.md`

## 1. 结论

两条规则的判据都错位了。`category` 现在写的是"这句话属于哪类内容"，判的是句子形态；工程窗口里几乎每句话都能读成"X 通过 Y 实现"，所以 KNOWLEDGE 永远成立。`org` 现在把"对象的身份或属性"放在选择顺序第一位，于是每个窗口的所有内容都落到 projects 或 entities。

改成两件事：

- `category` 是一张封闭的固定映射表。marker 用 `info_type` 做一次判断，写页端只做映射，不再重判。
- `org` 是先时间形状、后对象的地址选择。dated occurrence 先归 experiences/events，projects 必须能说出目标、交付物和状态。

五天数据支持这个判断：46 张页里 38 张 KNOWLEDGE，VALUE 和 GOVERNANCE 各 0；11 次 marker 输出里 151 条单元没有一个带 category 或 org；16 条单元被写页端以 `unknown category ''` 拒收，其中包括五天里唯一的一条价值判断。

## 2. 口径与复现

会话数与消息数：`C:\Users\zarag\AppData\Local\hermes\state.db`，按 `sessions.started_at` 分组。

```sql
SELECT date(started_at, 'unixepoch', 'localtime') AS day, COUNT(*), SUM(message_count)
  FROM sessions WHERE started_at >= strftime('%s','2026-10-20') GROUP BY day;
```

知识页：`C:\Users\zarag\AppData\Local\hermes\minir-memory\knowledge\<org>\<CATEGORY>-<topic>.md`，统计 front-matter 的 `category`、`org`、`source_session`。

marker：`minir-memory\sessions\default\b4_value_markers\*.jsonl`，逐行解析 assistant 输出里的 `value_markers`。

失败件：`minir-memory\knowledge\_failed\UNIT-*.json`。

队列：`minir-memory\memory.db` 的 `jobs`、`session_extractions`（只读连接）。

统计脚本为临时脚本，未入库；上面的口径足以复算本文所有数字。

## 3. 五天实况

会话（90 个，26,847 条消息）：

| 日期 | 会话 | 消息 |
| --- | --- | --- |
| 10-20 | 9 | 1,477 |
| 10-21 | 16 | 5,887 |
| 10-22 | 27 | 10,580 |
| 10-23 | 26 | 5,967 |
| 10-24 | 11 | 2,861 |
| 10-25 | 1 | 75 |

知识页（46 张）：

| 维度 | 分布 |
| --- | --- |
| category | KNOWLEDGE 38、METHOD 6、INTENT 2、VALUE 0、GOVERNANCE 0 |
| org | projects 19、tools 15、entities 7、experiences 5、events 0、trajectories 0 |
| 来源会话日 | 10-22 23 张、10-23 23 张、其余三天 0 张 |

10-24 的窗口一张页都没有：FalkorDB/EverOS 调研（467 条消息）、证书签发、nestingdns 分流、adguard DoH、C 盘清理、minir skin 全部未落页。它们的 base 任务要么失败，要么还在 pending，不是分类规则单独造成的。

marker（11 次 B4 输出，去重 35 个 topic，累计 151 条单元）：

| info_type | 条数 | info_type | 条数 |
| --- | --- | --- | --- |
| fact | 52 | inference | 6 |
| method | 26 | experience | 4 |
| tool behaviour | 19 | need | 4 |
| measurement | 14 | risk | 2 |
| convention | 10 | value judgement | 2 |
| constraint | 10 | project state | 2 |

这批单元里带 `category` 或 `org` 的是 0。B4 的提示词写明 `info_type`"取代旧的 category + epistemic 判断，页面分类交给 knowledge_render"，而写页端只认 `(category, org, topic)`。

失败件（16 条，错误全部是 `unknown category ''`）：`minir-插件-0.17.0-部署结果`、`SKSE-插件编译器选型`、`clang-cl-用于插件的-ABI-与调试代价`、`用户画像长度预算` 等。`SKSE-插件编译器选型` 是五天里唯一一条 `value judgement`。

队列：`session_extractions` 6 行（全部 done，共出 10 页），对应 90 个会话里的 6 个。`jobs` 在 10-20 之后失败 52 次，20 条 pending 从 10-24 13:36 起未动，其中包含证书会话和这次讨论的窗口。

## 4. 三个诊断

1. 两套词表并存，规范的那套拿不到输入。B4 发 `info_type`，`layers._merge_units` 按 `(info_type, topic)` 合并，写页端按 `(category, org, topic)` 落盘。10-24 记录过一句"两套并存时改哪边都白改"，这是准确的。
2. 让模型再判一次，就必然判成 KNOWLEDGE。v2 的十条消歧对判的是句子形态；"latency is 200 ms" 与 "200 ms is too slow" 这类对照在真实窗口里很少以对照形式出现，模型看到的多是机制描述，于是 38/46。
3. `category` 是整页属性，于是它记录"整页最响的那句话"。`INTENT-Hermes-0.22.4-upgrade` 页里写着 Binding constraints，却没有对应的 GOVERNANCE 页。identity 是 `(category, org, topic)`，同 topic 出第二页本来合法，但规则没说要拆，约束被目标吞掉。

## 5. 规则改动

### 5.1 markers-category v2 → v3

- 类别定义改成"读者拿它做什么"：理解 / 取舍 / 校准方向 / 照做 / 遵守。
- 新增 13 个 `info_type` 的封闭词表与固定映射（含 epistemic 的归属）。表外标签记为规则缺口，不再临时判一个类别。
- 删除十条消歧对，它们是"再判一次"的入口；只保留最容易混的三对标签选择（fact/value judgement、need/convention、method/constraint）。
- 新增 IDENTITY AND SPLIT：一页只携带一个 category；同一 topic 的另一个 category 是另一张页，不允许并进"最响的那条"。
- REVIEW CHECKS 改为可机检的不一致：类别与映射不符、表外标签、KNOWLEDGE 缺 epistemic、页面类别只覆盖正文两个主张之一。

映射表：

| info_type | category | epistemic |
| --- | --- | --- |
| fact / measurement / tool behaviour / project state / experience | KNOWLEDGE | fact |
| inference | KNOWLEDGE | inference |
| hypothesis | KNOWLEDGE | hypothesis |
| prediction | KNOWLEDGE | prediction |
| method | METHOD | 无 |
| need | INTENT | 无 |
| value judgement | VALUE | 无 |
| convention / constraint | GOVERNANCE | 无 |

### 5.2 markers-org v3 → v5

v4 把 events 的触发条件写成"日期本身是检索入口"，实操上把工作记录几乎全滤掉了；同日修正为 v5：

- 判定改为两步：先问"这条单元是否记录了一件发生的事（动作 + 对象 + 结果）"，是则按 occurrences / sequence 归 events、experiences、trajectories；不是才进对象族。
- events 是工作记录：谁对什么做了什么、结果是什么。读者会问的每个动作都记一条，不要求按日期检索才成立。
- experiences 只在这次发生改变了做法时成立：写结论、写坑，不写动作。既有记录又有结论时写两条单元。
- 删掉选择顺序第一位的"identity or a property of an object: entities"。这一条吞掉了所有落不到别处的单元。
- projects 增加门槛：必须能说出目标、交付物、当前状态；缺一项就不是 project 单元，按它实际陈述的内容归档。
- 示例换成库里的真实单元（用户画像长度预算 → entities + GOVERNANCE；升级目标与约束 → projects + 两个单元；搜索工具条数上限 → tools + KNOWLEDGE；证书签发 → events）。
- OBJECTS、RELATIONS、BOUNDARIES 保留原文。

### 5.3 配套改动

- `tests/test_session_finalisation.py` 里对旧文案的断言（`GOVERNANCE: what binds a subject.`）改为新文案的稳定锚点（`GOVERNANCE: obeys it.`）。测试的意图是"打包规则进入写页提示词"，与具体措辞无关。
- 版本号：markers-category v3，markers-org v5。
- token 预算：category 实测 1,185（原上限 1,000，已改为 1,200）；org v5 实测 1,173（上限 1,200 不变）。

### 5.4 验证

- `python -m pytest tests/test_rules.py tests/test_knowledge.py tests/test_session_finalisation.py -q`：54 passed。
- `rules.rule_text()` 读取正常，front-matter 不进正文；`estimate_tokens` 数字见上。
- 两个规则文件已同步到 `%LOCALAPPDATA%\hermes\plugins\minir\rules\`，`diff -rq` 无差异。规则在每次 pass 时读取，不需要重启 Gateway。

## 6. 仍未解决

1. B4 仍然只发 `info_type`。写页端按映射表落 category/org，这条链路现在能闭环；若要让 B4 直接发 category/org，需要改 `layers.validate_value_markers` 的键集合与 B4 的 agent 提示词，本次未动代码。
2. 吞吐缺口：90 个会话只有 6 个进过抽取队列，52 次分层任务失败，20 条 pending 从 10-24 13:36 起未处理。映射表再准，也要窗口先跑完才有效果。
3. 10-24 的 467 条消息会话有两次 base 任务失败，错误是 payload 缺键（`user_self_representation` 等），属于契约问题，不是分类问题。
4. 本报告只覆盖规则语义。页的合并、超预算拆页、同名冲突合并仍按现有代码行为执行；规则里新增的"一页一个 category"是对这条行为的显式约束，代码未变。

## 7. 附录：org 判定顺序

按顺序问，第一个成立的就是答案。

1. 这条单元记录了一件发生的事吗（动作 + 日期 + 对象 + 结果）？记录不了，进第 4 步。
2. 是，且是两个以上时间点的状态序列：trajectories。
3. 是，且是一次动作：events，工作记录本身成立，不等教训；若这次动作改变了做法，另写一条 experiences 或 method。
4. 标准单元：可调用的能力，tools；有身份的主体，entities；能说出目标、交付物、当前状态的工作，projects。
5. 平局时取以后会话先输入的那个名字。
