# 输出预算规则文件草稿

状态：草稿，未实施。规则文件本体在 §4，代码未动，测试清单未改。
依据：本次会话的需求原文（§1），以及工作区 `plugin/minir/` 的当前代码（工作区有 8 个未提交改动，与本文相关的行号按当前磁盘内容标注）。

当前结论

- 已定：新增一份共享规则 `plugin/minir/rules/output-budget.md`，只写流程；每个 pass 的预算数字仍留在它自己的所有者里。
- 已定：B3 子调用、压缩路径、卡复审三条今天没有 `minir` toolset 的路径，必须补上工具，否则提示词里的「请调用」不可执行。
- 未决：无。范围外的事实见 §10。

## 1. 原文

> 所有agent的提示词需要预算校验的使用minir_content_valid工具完成文本或者token预算校验

## 2. 一句话

凡是提示词里写了字符或 token 预算的 agent，返回前必须调 `minir_content_valid`，按预算单位选 `mode`，超了就按 `over` 删最弱的行再量；这段流程进一份规则文件，数字留在原处。

## 3. 机制核对

| 原文说法 | 实际机制 | 照原样实现的后果 |
| --- | --- | --- |
| 用 `minir_content_valid` 完成文本或 token 预算校验 | `content_valid.measure` 数 Unicode code point 或 `rules.estimate_tokens`（`plugin/minir/content_valid.py:26-30`）；`handle` 返回 `ok/measured/limit/over/remaining`（`:56-65`），不截断、不改写 | 预算单位可以直接映射到 `mode`，计数不再是模型自估 |
| 每个 agent 都能调 | 分派在 `self._store is None` 检查之前（`plugin/minir/__init__.py:1721-1723`）；无 store 的用例见 `tests/test_content_valid.py:66-71` | 不受初始化顺序限制；一个没建 store 的 pass 也能量 |
| 工具对所有 agent 可见 | 注册时 `toolset=TOOLSET`，而 `TOOLSET="minir"`（`plugin/minir/tools.py:45,295-300`）；`agent_toolsets` 固定补 `file/minir/skills`（`:250-265`） | 只写「请调用」但 toolset 里没有 `minir` 的 pass，提示词是空话 |
| B1/B2 可以自检 | base route 用 `tools.agent_toolsets([session_search.TOOLSET])`（`plugin/minir/worker.py:750-756`），`minir` 已在 surface 上 | 能力已有，但提示词只写了 token 上限（`agents/b1_user_card.md:84-86`、`agents/b2_ai_card.md:73-74`），没有自检步骤 |
| B4 可以自检，B3 不行 | B1 的 CHILD CALLS 指定 B3 `["file","session_search"]`、B4 `["file","minir","session_search"]`（`agents/b1_user_card.md:44,47`）；B3 失败后的重试在代码里另写了一份同样的列表（`worker.py:1015-1017`） | B3 的 tool surface 声明在两处，只给 B3 加规则会造出一段永远调不到工具的提示词 |
| B3 能读到自己的预算数字 | 子 agent 的 system prompt 由 `minir_agents._system_prompt` 原样读 `$HERMES_HOME/plugins/minir/agents/b3_window_summary.md`（`plugin/minir/minir_agents.py:247-270`），不做 `$VAR` 替换；同一资产经 `layers.load_core` 才会被 `prompt_vars()` 填值（`plugin/minir/layers.py:86-102`） | 实测子 agent 的正文是字面 `$MAX_TOTAL_CHARS`（§9），规则要求「按提示词写的数字量」，B3 没有数字可用 |
| 压缩路径与 B3 用同一份预算 | `compress_pass.system_prompt` 拼 `layers.load_core('b3_window_summary')`（`plugin/minir/compress_pass.py:36-39`），`summarize` 调 `agent_loop.run` 不传 toolsets（`:97-99`），空列表即无工具（`plugin/minir/agent_loop.py:302,320`） | 同一份提示词，一个路径能调工具、一个不能；压缩路径只能靠代码截断（`compress_pass.py:25,84`） |
| 卡复审能自检 | `review_cards` 走 `_complete`（`worker.py:831-834`），`_complete` 默认是 `call_model` 一次性客户端（`worker.py:670`）；`run_review` 用 `complete(system, body)`（`plugin/minir/card_review.py:485`） | 四个复审 pass 的核心写了「body 不超过预算」（`agents/card_review_ai.md:50`、`agents/card_review_user.md:49`），模型只能自估；代码硬限是 `CARD_TEXT_LIMIT=8000`（`layers.py:28,220`） |
| 规则文件可以带每个 pass 的数字 | `rule_text` 只去掉 front-matter，正文原样返回（`plugin/minir/rules.py:1-9,65-84`） | 规则不能参数化；数字必须留在提示词或常量里，规则只写流程 |
| 新规则文件可以直接加 | `test_the_packaged_rules_load_without_front_matter` 钉死 9 个 id，并要求每份正文含 `CONTRACT` 与 `REVIEW CHECKS`（`tests/test_rules.py:24-33`） | 只加文件不改测试，套件直接红 |
| 这是新机制 | 已有两处自检：session summary 6000 字符（`agents/session_summary.md:33-35`）、knowledge page 2000 字符（`agents/knowledge_render.md:54-57`）；两处代码另有截断（`plugin/minir/session_summary.py:28,67`、`plugin/minir/extraction.py:41`） | 规则是把这两处的流程抽成一份，不引入新计数 |
| 顾问复审也在范围内 | 它的预算 `$MAX_ADVICES` / `$MAX_NOTES` 是条数（`plugin/minir/prompts/adviser/review_budget.md:2-3`） | 条数不是文本长度，`minir_content_valid` 量不了，规则范围外 |

## 4. 新规则文件正文

目标路径：`plugin/minir/rules/output-budget.md`

```markdown
---
id: output-budget
version: 1
applies_to: [base:B1, base:B2, base:B3, base:B4, card_review, knowledge_render, session_summary, compression]
slot: body
max_tokens: 700
sections: [contract, review_checks]
---

## CONTRACT

WHAT
A budget is the number this prompt states for the text you return. It is a ceiling, not a target,
and the count comes from a tool, not from your own estimate.

WHEN IT APPLIES
- The prompt states a character budget: measure with mode="string".
- The prompt states a token budget: measure with mode="token".
- The prompt states no number: make no call and invent no limit.
- A count budget (advice objects, list items) is not a text budget: this tool does not measure it.

HOW
1. Call minir_content_valid with content, limit and mode. It returns ok, mode, unit, measured,
   limit, over and remaining. It never truncates, rewrites or stores the text.
2. Measure the string the budget names. A per-body budget measures that body alone; a
   whole-reply budget measures the reply as it will be sent, JSON envelope included; a budget
   over several strings is measured once on their concatenation, in returned order.
3. When ok is false, delete or compress the weakest lines by the reported `over`, then measure
   the edited text again. A line the prompt marks as non-droppable (a goal, a request, its
   outcome) is not the line to cut; cut elsewhere.
4. Return only after ok is true. If two rounds still leave ok false, return the best effort and
   add one line naming the budget still over. A code-side cap remains the hard gate.

WHAT IT DOES NOT DO
- It does not check content, shape, schema, truth or safety. One length question, one answer.
- A count inside the limit is not permission to fill the limit. Shorter is better.
- It does not override the output contract appended after this rule.

## REVIEW CHECKS
- A budgeted body is returned with no minir_content_valid call.
- The mode does not match the unit: characters measured as tokens, or the reverse.
- The measured string is not the string the budget names.
- The reply returns over budget with no line saying so.
- A non-droppable line was cut to fit a cap.
- The text was edited after the last measurement and never re-measured.
```

## 5. 组装点与改动清单

| pass | 预算（数字与所有者） | 今天挂 `minir` | 规则入口 | 还要做的改动 |
| --- | --- | --- | --- | --- |
| B1 用户卡 | 整条回复 3200 tokens，只在提示词正文（`agents/b1_user_card.md:84-86`） | 是（`worker.py:750`） | `_system(core, OUTPUT_B1, [...])` 追加 `self._rule("output-budget")`（`worker.py:981-982`） | BUDGET 段保留 3200，把「量一次、删最弱行」换成一行指向规则；回报前量整条回复（含 JSON） |
| B2 AI 卡 | 整条回复 2000 tokens（`agents/b2_ai_card.md:73-74`） | 是（同 route） | 两处 `_system` 都追加规则（`worker.py:1043-1045`、`1426-1428`） | 同 B1 |
| B3 窗口摘要（子 agent） | 六个 section 合计 880 字符（`plugin/minir/window_summary.py:36-41`；经 `layers.load_core` 填进 `agents/b3_window_summary.md:32,39-49`，子 agent 路径不填，见下一行） | 否（`agents/b1_user_card.md:44`；重试列表 `worker.py:1017`） | 子 agent 的 system 侧：`_system_prompt` 按 agent 名追加规则正文（`minir_agents.py:247-270`） | B3 的 `tool_sets` 两处都加 `minir`；`_system_prompt` 对随包核心填 `$MAX_*`，否则规则拿到的是字面占位符；正文写明量的是六节拼接块，每行 100 字符仍留在正文 |
| B4 价值标记（子 agent） | 每单元 content 1200 字符（`layers.py:50-55`，`agents/b4_value_markers.md:32-33`） | 是（`b1_user_card.md:47`） | 同 B3 的 system 侧注入 | 正文加 SELF-CHECK：每个 unit 的 content 单独量 |
| 压缩校准 | 今天只有代码截断 8000 字符（`compress_pass.py:25`），提示词沿用 B3 的 880 | 否（无 toolsets，`compress_pass.py:97-99`） | `compress_pass.system_prompt` 追加 `rule_text("output-budget")` | 挂 `toolsets=tools.agent_toolsets()`；加一行「整条回复 ≤8000 字符」，把折叠摘要的预算与 880 的窗口摘要分开 |
| 卡复审（4 个 pass） | 每文件 body，代码硬限 8000 字符（`layers.py:28,220`）；提示词只说「per-file budget」（`agents/card_review_ai.md:50`、`agents/card_review_user.md:49`） | 否（`worker.py:670,831-834`） | `build_prompt` 的 system 列表追加规则（`card_review.py:443-446`） | 复审改走带工具的 agent route（`agent_loop.run` + `tools.agent_toolsets()`，用 `CARD_VIEW_TASK` 的配置）；一次性客户端只保留为 route 故障时的退路，那时代码硬限是唯一闸门；正文把 8000 写成数字 |
| session summary | 6000 字符（`plugin/minir/session_summary.py:28`，提示词 `agents/session_summary.md:32-35`） | 是 | `session_summary.system_prompt` 追加规则 | 保留 6000，内联流程改指规则；加断言把提示词的 6000 与 `MAX_CHARS` 钉在一起 |
| knowledge page | 每页 2000 字符（`agents/knowledge_render.md:46-47,53-57`） | 是（`extraction.py:61-66,214`） | `extraction.system_prompt` 在核心之后追加规则 | 保留 2000 并移到 `prompt_vars()`（`layers.py:75-83`）；与存储截断 6000（`extraction.py:41`）分工写清楚 |
| dialectic | 没有数字预算（`agents/dialectic.md` 无 caps） | 是（`worker.py:1610-1614`） | 不追加 | 无需改动；它不属于「有预算」的 pass |
| 顾问复审 | 条数预算（`prompts/adviser/review_budget.md:2-3`） | 是 | 不追加 | 范围外 |

数字仍留在原所有者：B3 上限在 `window_summary.py:36-41`，B4 在 `layers.py:50-55`，卡复审在 `layers.py:28`，session summary 在 `session_summary.py:28`；本规则不复制任何数字。

子 agent 的两条改动落在同一个函数：`_system_prompt` 先按 `plugins/minir/agents/` 判断是否为随包核心，是则用 `layers.prompt_vars()` 填 `$MAX_*`，再按小映射追加规则正文（`{"b3_window_summary": ("output-budget",), "b4_value_markers": ("output-budget",)}`）；operator 放在 `.agents/` 下的同名文件原样返回，不填值也不追加。

## 6. 连带改动

| 位置 | 现状 | 要改成 |
| --- | --- | --- |
| `tests/test_rules.py:24-33` | 钉死 9 个规则 id | 加 `output-budget`；`CONTRACT` / `REVIEW CHECKS` 的断言自动覆盖新文件 |
| `README.md:393-397` | 只说明规则正文原样注入 | 加一句：预算自检的流程在 `rules/output-budget.md`，各 pass 只保留数字 |
| `plugin/minir/layers.py:75-83` | `prompt_vars()` 不含卡与页面预算 | 加 `MAX_CARD_BODY_CHARS`（= `CARD_TEXT_LIMIT`）与 `MAX_PAGE_BODY_CHARS`（2000） |
| `agents/session_summary.md:33-35`、`agents/knowledge_render.md:54-57` | 各自内联一遍流程 | 保留数字，流程改指规则；否则同一段话有两个版本 |
| `tests/test_layers.py:146-148` 的同类断言 | 只钉 B3 的上限出现在核心 | 按同一写法补 session summary、卡复审、knowledge page 的数字断言，防止提示词与常量漂移 |
| `plugin/minir/worker.py:1015-1017` | B3 重试的 `tool_sets` 是代码里硬编码的第二份 `["file","session_search"]` | 与提示词那份一起加 `minir`；只改一处会留下一条静默无工具的退路 |

## 7. 换来 / 放弃

换来

- 流程只有一份：字符/ token 与 `mode` 的对应、`over` 的处理、非可删行的例外，不再在 6 个 pass 里各写一遍。
- 超预算可核查：`over` 是工具给的数，不是模型自估；B1/B2 的 agent route 没有任何输出上限（`agent_loop.run` 不收 `max_tokens`），一次性退路的 `_MODEL_MAX_TOKENS=2000`（`worker.py:87,132`）只覆盖退路，自检是主路径上唯一的闸门。
- 两处已有自检并入同一口径，规则文件成为唯一改动点。

放弃

- 每次自检多一次工具往返。B1/B2/B4 在 agent loop 里，成本是一个 tool call；卡复审从一次性调用变成 agent loop，延迟与 token 成本明显上升。
- 规则不能带数字，改一个上限仍要改它的所有者；规则不会自动跟着变。
- 规则正文约 1.3k 字符，会进每个受约束 pass 的 system prompt（B1/B2/四个复审/压缩/session summary/知识页），压缩路径与卡复审的输入因此变长。
- 卡复审的故障退路（route 不可用）仍然没有工具，那一跳只能靠代码硬限。
- 压缩路径的预算从「沿用 B3 的 880」改成 8000，折叠摘要的长度口径变了，下游要重新看一次。

## 8. 失效面

1. 工具不在 surface 上（B3 的提示词加了 `minir` 而重试列表没加，或反过来）：模型编一个数字或跳过自检，预算仍超，最后被代码截断。
2. 数字没填（子 agent 的 system prompt 仍是 `$MAX_*`）：规则要求量，`limit` 缺失，模型只能跳过。
3. 卡复审仍走一次性客户端：提示词要求调用工具，实际没有工具，模型写一句「已校验」。
4. 规则与所有者数字不一致（改了 6000 没改提示词）：两个上限并存，模型取宽的那个。
5. JSON 外壳没算进 token 预算：3200 / 2000 被低估；规则已写「整条回复，含 JSON」。
6. 量完又改文本（例如补一行摘要）：返回的比量过的长；规则要求每次编辑后重量。
7. 压缩路径按 880 而不是 8000 量：折叠摘要被过度压缩，下游看到的窗口摘要变短。

## 9. 验收

今天核过的（claim + 证据）

- 工具不依赖 store：`__init__.py:1721-1723` 在 store 检查之前分派；`tests/test_content_valid.py:66-71`。可行。
- token 模式可用：probe 调 `content_valid.handle({"content":"知识abcd","limit":3,"mode":"token"})` 返回 `measured=4, over=1`；与 `rules.estimate_tokens` 的算法一致（`rules.py:29-37`）。可行。
- `minir` toolset 是工具的唯一入口：`tools.py:45,295-300`，`agent_toolsets` 只补 `file/minir/skills`（`:250-265`）。可行。
- B3 子 agent 的 system prompt 来自安装副本且含字面占位符：probe 调 `minir_agents._system_prompt`，`prompt_source=C:\Users\zarag\AppData\Local\hermes\plugins\minir\agents\b3_window_summary.md`，正文含 `$MAX_TOTAL_CHARS`。可行（这是一个缺口）。
- 规则正文原样注入：`rules.py:54-84`。可行。
- B3 的 tool surface 有两处：提示词 `agents/b1_user_card.md:44` 与重试 `worker.py:1015-1017`，两处都是 `["file","session_search"]`。可行（这是缺口）。

未验证

- 模型是否稳定调用 `minir_content_valid`（行为，不是代码）。
- 卡复审改走 agent loop 后的延迟与成本。
- 压缩预算 8000 是否让折叠摘要长到影响下游读取。
- 六节拼接块一次量 880，与逐行量相比哪个更省调用。

第一步实施顺序

1. 写规则文件，改 `tests/test_rules.py` 的 id 清单（先让规则可加载、可测）。
2. `_system_prompt` 填 `$MAX_*` 并注入规则（B3/B4）。
3. B1 的 CHILD CALLS 给 B3 加 `minir`。
4. 压缩路径挂 `toolsets`，加 8000 字符那一行。
5. 卡复审改 agent route，保留一次性客户端作退路。
6. 各提示词的内联流程改指规则，补数字断言。

## 10. 未决 / 未验证

未决：无。范围外：顾问复审的条数预算（`review_budget.md:2-3`）；dialectic 没有数字预算；`adviser_mode.py` 的 `MAX_PROMPT_CHARS` 是输入预算，不是 agent 输出预算。

## 11. 修订记录

- 初稿：确定规则文件 `output-budget.md` 的正文与 9 个 agent 资产的组装点；点名 B3 子调用、压缩路径、卡复审三处今天没有工具。
