# knowledge → advice：建议附带记忆引用（草稿）

状态：契约待确认，未动代码。

## 结论

关系是 knowledge → advice。adviser 用 file 工具（`search_files` / `read_file`）按建议的
target 在知识根下检索，读到相关页面后把路径写进建议文本：

    使用 xx 工具，能更快找到目标，记忆引用:<路径1> <路径2> <路径3>

注入的 seed 索引只是起点，检索以 file 工具为准。

## 现状（实测）

- reviewer 只拿到 `base.user_card`（`adviser_mode.py:779` `_digest`）；`__init__.py:621` 注入主
  agent 的知识块不进事件环（`adviser_mode.py:653` 只记 `user|intent|tool|final`）。
- file 读根已经是 `$HERMES_HOME` 加项目根（`file_guard.py:197`），知识目录天然可读；写只允许
  `minir-memory/scratch`（`file_guard.py:12`）。所以 adviser 读得到、写不了，守卫不用改。
- reviewer 现在是 5 个 iteration（`adviser_mode.py:927`）：一次检索、最多两页读、作答，还留一次
  余量。
- 本 profile 还没有知识目录（`$HERMES_HOME/minir-memory/knowledge` 不存在），现在检索与注入都
  是空，功能要等页面产生后才激活。
- 提示词第 89 行的读取边界写的是「项目根」，比实际守卫窄；知识根要写进提示词，别让模型猜。
- `adviser_mode.py:313` `parse_review` 只保留 `text`；`adviser_prompts.py:105` 的 advice 说明
  没有引用格式。

## 契约

### R1 seed 索引：省掉一轮检索

- `_digest(session, query="")` 末尾加 knowledge 块：首行 `root: <知识根绝对路径>`，随后最多
  5 行 `<topic> · <绝对路径>`，块上限 700 字符，超出丢整行、绝不截断路径。
- 行来自 `select_paths(query, root, limit=5, max_chars=700)`；行内 `path` 是相对的
  （`knowledge.py:673`），接根后变绝对。
- 查询词：review 用最新 user 文本，consult 用提问原文。目录不存在时整块不出；目录在但无命中
  时只出 `root:` 行。

### R2 输出：建议文本带引用

- 一条建议若依据了知识页面，`text` 末尾写 `，记忆引用:<路径1> <路径2> <路径3>`；最多 3 条，
  没有依据就不写这个后缀。
- 空格分隔（知识目录下的路径不含空格），重复路径只留一次；标签 `记忆引用:` 固定中文。
- 路径逐字取自 file 工具读到的页面或 R1 的 seed，不许拼、不许猜、不许引用读不到的文件。

### R3 提示词：按 target 检索

- 给建议前按该建议的 target 选检索词：`code_economy` 找方法/工具类页，`user_intent` 找偏好/
  目标类，`current_step` 找流程/经验类，`output_surface` 找规范/风格类；用 `search_files` 在
  R1 的 root 下检索，最多一轮（可一次发多条 pattern），随后 `read_file` 最多两页，只引用读过
  的页。
- 检索不到相关页就不写引用，不为了凑引用去读无关页。
- 边界句（`adviser_prompts.py:89`）改为：项目根与 R1 给出的知识根可读，其余仍不读；写入仍
  禁止。

### R4 校验与落库

- `parse_review` 之后、`_admit` 之内解析末尾后缀，逐条校验：路径必须在知识根下（`realpath`
  加 `commonpath`，与 `file_guard` 同法）且文件存在；不满足就剥掉并计入 `verdict.dropped`
  （`invalid knowledge ref`）。
- 超过 3 条按书写顺序留前 3 条（`over citation budget`）；去重后为空就整段删掉；有效路径逐
  字保留，不改写。
- 不要求命中 seed：file 工具检索到的页面同样合法，这是与上一版的关键差别。
- 落库仍是 `advisories.content`，不加列。反查用 `content LIKE '%记忆引用:%'` 再比对路径。

### R5 只读与交付

- adviser 只读页面，不写；`_digest` 的块走 `data_block`，是数据不是指令。
- 交付块 `[adviser — advisory only]` 的形态与目标词表不变，引用只是文本的一部分。

## 预算

- seed 5 行、700 字符，约占 reviewer prompt（`adviser_mode.py:54` 的 12000）的 6%。
- 检索吃 iteration：一次 search（可并发多条 pattern）+ 至多两页 read + 作答，5 个足够且留一
  次余量；检索仍限一轮，避免把预算耗在找页上。
- 3 条绝对路径约 285 字符，`text` 的 800 字符截断（`adviser_mode.py:335`）之后正文剩约 500。
- 不新增模型调用，每次 review 多 1 到 2 次工具调用，已包含在 iteration 预算内。

## 验证

1. `python -m pytest tests/test_adviser_mode.py -q` 新增：seed 块含 `root:` 行；根下存在但不在
   seed 的路径被接受；根外路径剥离；不存在的文件剥离；第 4 条剥离；重复去重。
2. 端到端：先造一页 `$HERMES_HOME/minir-memory/knowledge/<org>/<CATEGORY>-<slug>.md`（本 profile
   现在没有该目录），跑 `review_once`，断言 prompt 含 root，`advisories.content` 以
   `记忆引用:<path>` 结尾，且该路径可 `read_file`。
3. 反向：知识目录不存在时不出现空块，建议不写引用。

## 风险与回退

- 检索烧 iteration：5 轮之后仍掉作答，问题在提示词不在轮数，先收紧检索词再谈加轮。
- 路径过期：页面改名或删除后引用会被存在性校验剥掉，不会留下打不开的路径。
- 回退：删 R1 调用即回到只读 `user_card`；无根时引用一律剥掉，行为自动降级。

## 不做

知识页面反写、引用列、图存储、向量检索、把引用做成 JSON 字段。
