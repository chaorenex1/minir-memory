# 窗口摘要模板草稿

状态：已实现（2026-10-23，T2/T3/T5）。依据 `docs/draft/base-dialectic-layers-draft.md` 4.5 与 `docs/draft/layer-agent-contracts-draft.md` B3。

## 1. 定位

窗口摘要回答一个问题：任务现在进行到哪里，下一步靠什么继续。

它不写用户画像、不写知识单元、不写历史日志。三条边界写死：

- 用户卡管「这个人是谁」，AI 卡管「助手在做什么」，摘要只管「任务进行到哪」。
- 凡是用户的目标都进 `objective`，大小目标与杂项都列；`current_state` 只对它们做状态分组，不做更细的跟踪。
- 摘要不引入窗口外的证据，除非那是上一份摘要已经承载的状态。

## 2. 形态

窗口摘要只有 markdown 一种形态。B3 直接写出 `## Window Summary` 全文，没有 JSON 正文，也没有渲染器；外层 JSON 只是运输封装（B1 把这段 markdown 作为字符串放进 `window_summary` 键）。

代码不解析摘要正文，也不校验标题或字段：所谓"按模板"由提示词负责。现有 `layers.artifact_from_columns` 只对列做一次 `json.loads` 解出 markdown 字符串（`layers.py:416-420`），`window_summary.validate` 只检查是字符串（`window_summary.py:92-96`），所以取消 JSON 正文不需要迁移。

## 3. 字段契约

| 字段 | 类型 | 空值语义 | 条目上限 | 常见错误 |
| --- | --- | --- | --- | --- |
| objective | markdown 列表，每个目标一行 | 首轮可空 | 不设条数上限，每行 ≤100 字 | 编号；带状态/来源；按重要性删目标 |
| current_state | 三个子列表 Done / Doing / Todo | 三组都可空 | 三组合计等于 objective 条数 | 写进展；写与 objective 无关的事项；同一目标出现在两组 |
| decisions | array | 可空 | ≤4 条 | 只写决定不写理由；把待定写成已定 |
| constraints | array | 可空 | ≤5 条，每条 ≤100 字 | 把偏好写成硬约束 |
| verified_facts | array | 可空 | ≤5 条 | 无证据的事实；把推断写成已验证 |
| open_issues | array | 可空 | ≤5 条，每条 ≤100 字 | 把已解决项留在这里 |
| next_actions | array | 可空 | ≤5 条 | priority 缺省；写成多个动作的复合句 |
| references | array | 可空 | ≤5 条 | 用描述代替可重读的定位符 |

`priority` 枚举：`high`、`normal`、`low`。`kind` 枚举：`file`、`log`、`config`、`url`、`issue`、`other`。

`objective` 是当前会话用户的目标集合：用 `- ` 逐条列出，包含每轮输入与轮中途输入，大小目标与杂项都列，不编号、不带状态、不带来源。`current_state` 用 Done、Doing、Todo 三组把同一批目标分类：每条就是一个目标，不写进展、不改写。

## 4. Markdown 模板

标题固定英文，顺序固定，值跟会话语言。这个顺序就是 artifact 里的呈现顺序，也是下一轮 prompt 的读取顺序。

```markdown
## Window Summary

### Objective
- <goal>
- <goal>

### Current State
#### Done
- <goal>

#### Doing
- <goal>

#### Todo
- <goal>

### Decisions
- <decision> — <reason>

### Constraints
- <item>

### Verified Facts
- <fact> — evidence: <evidence>

### Open Issues
- <item>

### Next Actions
- [high] <action>
- [normal] <action>

### References
- <kind>: <ref>
```

空模板：八个标题全部保留，列表为空，不写「无」「暂无」「待补充」。空列表本身就是信号。

## 5. 示例

```markdown
## Window Summary

### Objective
- 把 minir 的 base 与 dialectic 改成两个独立节拍
- 让 prefetch 只注入两列
- 验证分层产物
- 顺手确认顾问链路的配置没被改坏

### Current State
#### Done
- 验证分层产物
#### Doing
- 把 minir 的 base 与 dialectic 改成两个独立节拍
- 顺手确认顾问链路的配置没被改坏
#### Todo
- 让 prefetch 只注入两列

### Decisions
- 两个独立 watermark — base 周期与 review 周期互不派生，同轮 base 先
- 复盘不写全局卡 — 首刷只生成会话卡，全局卡由复盘累积

### Constraints
- 具体知识不进 prefetch 正文，只注入路径

### Verified Facts
- agent_loop.run 默认 toolsets 为 None 等于全部工具 — evidence: agent_loop.py:147 与 model_tools.py:219

### Open Issues
- D8 的写权限仍需确认

### Next Actions
- [high] 写规则加载器的失败分支
- [normal] 补窗口摘要的验收断言

### References
- file: docs/draft/base-dialectic-layers-draft.md
- log: pytest -q 输出
```

## 6. 合并规则

每次刷新输入上一份摘要，输出新的全量，不输出补丁。

- 继续成立的条目原样保留；被窗口解决的从 `open_issues` 与 `next_actions` 移除。
- 完成的目标移到 `current_state.done`，不改目标文本；`next_actions` 里不再重复它。
- `current_state` 是 `objective` 的划分：每个目标恰好出现在 done、doing、todo 之一，三组合起来就是 `objective`；不写进展，不写与目标无关的事项。
- 新决定覆盖旧决定时，旧条目删除，不并列保留；冲突未定时两说都进 `open_issues`，不写进 `decisions`。
- `verified_facts` 只保留仍然有效的验证；失效的删除，不写「已失效」。
- 总量优先：宁少不滥。超过上限的列表按重要性取舍，必须在同一次输出里完成，不能留给渲染器截断。
- `objective` 例外：它是会话的目标账本，跨窗口与压缩都保留；完成的目标不删，放弃的目标从 `objective` 移除（它不再是目标）。
- `objective` 不编号。同一目标被重述时改写那一行；方向改变时替换那一行，不并列两条意思相同的目标。
- 轮中途输入必须落到 `objective`：新目标追加一行，不标来源；只改方向的改写对应行。
- `objective` 不按条数删；总预算超了压缩措辞，不删目标。

## 7. 输出约定

- B3 的提示词要求按本文模板输出；没有内容的章节保留标题、留空。
- 代码不解析、不校验摘要正文，不因格式失败、不重试。格式靠提示词约束。

## 8. 版本

- 模板带 `template_version`，写进 artifact 元数据。改标题、改顺序、改枚举都要 bump。
- 旧 artifact 不按新模板重写；模板只对本次新产物生效。
- 字段语义以 B3 提示词为准，模板与上限以本文为准。

## 9. 预算

`objective` 与 `current_state` 不设条数上限，也不计入 880；880 是其余六节的提示词目标。超预算时由 B3 压缩措辞、不删目标；代码不判失败、不重试。注入层的字符上限见生命周期草稿 §2。
