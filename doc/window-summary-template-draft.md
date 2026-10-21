# 窗口摘要模板草稿

状态：草稿。依据 `doc/base-dialectic-layers-draft.md` 4.5 与 `doc/layer-agent-contracts-draft.md` B3。

## 1. 定位

窗口摘要回答一个问题：任务现在进行到哪里，下一步靠什么继续。

它不写用户画像、不写知识单元、不写历史日志。三条边界写死：

- 用户卡管「这个人是谁」，AI 卡管「助手在做什么」，摘要只管「任务进行到哪」。
- 已解决的事项从摘要移除，不留「已完成」的历史账本；历史在 artifact 与 transcript 里。
- 摘要不引入窗口外的证据，除非那是上一份摘要已经承载的状态。

## 2. 双形态

| 形态 | 由谁产出 | 用途 |
| --- | --- | --- |
| JSON | B3 模型输出 | 机器契约、校验、合并 |
| Markdown | 渲染器按本模板生成 | artifact 的 `## Window Summary` 段，给人看，也给下一轮 prompt 读 |

模型不写 markdown。渲染器只做格式，不改内容。两边字段集合必须完全一致，校验器按 8 个键比对，多一个少一个都算格式失败。

## 3. 字段契约

| 字段 | 类型 | 空值语义 | 条目上限 | 常见错误 |
| --- | --- | --- | --- | --- |
| objective | string | 首轮可空 | 1 句，≤120 字 | 写成多个目标；写成过程描述 |
| current_state | object | 三个数组都可空 | 每数组 ≤5 条，每条 ≤80 字 | 把已完成写成 todo；状态写散文 |
| decisions | array | 可空 | ≤4 条 | 只写决定不写理由；把待定写成已定 |
| constraints | array | 可空 | ≤5 条，每条 ≤100 字 | 把偏好写成硬约束 |
| verified_facts | array | 可空 | ≤5 条 | 无证据的事实；把推断写成已验证 |
| open_issues | array | 可空 | ≤5 条，每条 ≤100 字 | 把已解决项留在这里 |
| next_actions | array | 可空 | ≤5 条 | priority 缺省；写成多个动作的复合句 |
| references | array | 可空 | ≤5 条 | 用描述代替可重读的定位符 |

`priority` 枚举：`high`、`normal`、`low`。`kind` 枚举：`file`、`log`、`config`、`url`、`issue`、`other`。

## 4. JSON 形状

```json
{
  "objective": "",
  "current_state": {"done": [], "doing": [], "todo": []},
  "decisions": [{"decision": "", "reason": ""}],
  "constraints": [],
  "verified_facts": [{"fact": "", "evidence": ""}],
  "open_issues": [],
  "next_actions": [{"action": "", "priority": "normal"}],
  "references": [{"kind": "file", "ref": ""}]
}
```

## 5. Markdown 模板

标题固定英文，顺序固定，值跟会话语言。这个顺序就是 artifact 里的呈现顺序，也是下一轮 prompt 的读取顺序。

```markdown
## Window Summary

### Objective
<one sentence>

### Current State
#### Done
- <item>

#### Doing
- <item>

#### Todo
- <item>

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

## 6. 填充示例

```markdown
## Window Summary

### Objective
把 minir 的 base 与 dialectic 改成两个独立节拍，并验证分层产物。

### Current State
#### Done
- 分层草稿定稿，6 条决策全部落定
#### Doing
- 规则文件动态组装
#### Todo
- 三天复盘的调度线程

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
- file: doc/base-dialectic-layers-draft.md
- log: pytest -q 输出
```

## 7. 合并规则

每次刷新输入上一份摘要，输出新的全量，不输出补丁。

- 继续成立的条目原样保留；被窗口解决的从 `open_issues` 与 `next_actions` 移除。
- 已完成的事项进 `current_state.done`，不再重复列在 `next_actions`。
- 新决定覆盖旧决定时，旧条目删除，不并列保留；冲突未定时两说都进 `open_issues`，不写进 `decisions`。
- `verified_facts` 只保留仍然有效的验证；失效的删除，不写「已失效」。
- 总量优先：宁少不滥。超过 5 条的列表按重要性截断，但必须在同一次输出里体现，不能留给渲染器。

## 8. 渲染与校验

渲染：

- 标题层级固定为 `##` 与 `###`，`current_state` 用三个 `####`。不要改成 `### Current State (done)` 这类变体，解析器按固定路径读。
- `decisions` 用 `—` 连接决定与理由；`verified_facts` 用 `evidence:` 标记证据。
- `next_actions` 前缀 `[priority]`，缺省即格式失败。
- 单行超过字段上限即判格式失败，重试一次。渲染器不做任何截断，也不做省略号。

校验：

- 8 个键齐全，类型正确，枚举合法。
- `verified_facts` 的 `evidence` 不得为空；`references` 的 `ref` 必须是可重读的定位符（路径、URL、命令、日志位置），不是描述。
- 禁用词：`N/A`、`unknown`、`TBD`、`待补充`、`暂无`、`无`。出现即判失败。
- 总字符上限 880（与 artifact 预算一致）。超限判失败并重试一次，不截断。
- 与上一份摘要相比，字段集合不变；字段值可以全空。

## 9. 版本

- 模板带 `template_version`，写进 artifact 元数据。改标题、改顺序、改枚举都要 bump。
- 旧 artifact 不按新模板重渲染。渲染器只对本次新产物生效。
- 字段语义以 B3 提示词为准，渲染与上限以本文为准。两处字段集合不一致时，校验器按本文的 8 个键判失败，避免静默漂移。
