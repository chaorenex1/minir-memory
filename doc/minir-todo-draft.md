# minir_todo：会话内使用的两级待办工具（草稿 v4）

> 已被 `doc/conclusions-only-draft.md` 取代：todo 改为 conclusions 单行（`kind="todo"`），
> 不再用 JSONL 文件、多列表、`due_at`、`blocked`。本文件仅保留作历史记录。

状态：草稿，供评审（v4）。代码基线：`plugin/minir/` 当前 main（`9269854`）。

v4 的口径变化：`minir_todo` 是**会话内**工具，不是跨会话目标库。工具只在主会话里可用，默认视图只看本会话；落盘是为了会话续接与崩溃恢复，不是为了把待办搬到别的会话。若你只是指「不给后台 run 用」，删掉第 1 节里 `scope="all"` 那一段即可，其余不变。

其余口径沿用 v3：工具名 `minir_todo`；一个列表一个 JSONL 文件；两级结构（列表 → 条目，无 `parent`）；`blocked` 保留；slug 显式优先、可派生、可 `todo-<hash>` 兜底；`due_at` 要做；prefetch 注入全部未完成条目；归档不移动文件；buffer 一并改 JSONL。

## 0. 与宿主 `todo_list` 的差别

| 维度 | 宿主 `todo_list` | `minir_todo` |
| --- | --- | --- |
| 生命周期 | 内存，进程结束即丢 | 落盘，同一会话重开后还在 |
| 结构 | 单列表，`parent` 任意深度 | 多列表，严格两级 |
| 读取 | 每次返回整表 | 按列表读，可过滤状态、过期与条数；prefetch 自动注入 |
| 用途 | 临时的执行清单 | 会话内的工作清单，跨回合可见、可续接 |

不再说「会话内用 `todo_list`、跨会话用 `minir_todo`」：两边都是会话内工具，差别在持久化与结构。

## 1. 使用边界

| 项 | 规则 |
| --- | --- |
| 调用者 | 只有主会话。后台 run（worker 的层任务、修复轮、卡评审）的 `agent_toolsets()` 不授予该工具 |
| 默认视图 | `lists` / `items` / prefetch 注入只看创建它的那个会话 |
| 会话 id | 来自 provider 的 `_session_id`；为空或缺失时报 `session_scope_required`，不返回空、不回退 |
| 跨会话读 | 显式 `scope="all"` 才跨会话，且同样要有效会话 id（与 ai_self 的契约一致） |
| 会话结束 | 文件保留。重开同一会话（同 id）继续可见；新会话默认看不到 |
| 写入归属 | 列表与条目都记创建会话；不提供「转移到别的会话」 |

理由：待办是主 agent 的工作状态，后台 run 往里写会造出 operator 看不到的条目；默认按会话隔离，是为了不让昨天的清单污染今天的注入。

## 2. 落盘

### 2.1 路径与 slug

```
<HERMES_HOME>/minir-memory/sessions/todo/<todo_slug>.jsonl
```

slug 取值顺序：

1. 调用方显式传 `slug`：校验 `[a-z0-9][a-z0-9-]{0,59}`。
2. 未传时由列表名派生：小写、空格下划线转 `-`、去掉其它非 ASCII 字符。
3. 派生为空（中文名）时用 `todo-<sha1(name)[:8]>` 兜底。

同一 slug 已被**另一个会话**占用时，自动加 `-<session8>` 后缀并在返回里说明；同会话重复使用同名列表报「已存在」，不静默复用。

### 2.2 行格式

每行一个 JSON 对象，UTF-8，行尾 `\n`，append-only。`v` 是格式版本，`ts` 是 epoch 秒。

```json
{"v":1,"op":"list","slug":"release-1-0","name":"发布 1.0","session":"s-abc123","ts":1750000000}
{"v":1,"op":"add","id":"a1b2c3d4","content":"跑全量测试","status":"pending","seq":1,"due_at":1750200000,"ts":...}
{"v":1,"op":"update","id":"a1b2c3d4","status":"in_progress","ts":...}
{"v":1,"op":"note","id":"a1b2c3d4","note":"等 CI 队列","ts":...}
{"v":1,"op":"move","id":"a1b2c3d4","seq":1.5,"ts":...}
{"v":1,"op":"remove","id":"ev5f6g7h","ts":...}
{"v":1,"op":"rename","name":"发布 1.0 正式版","ts":...}
{"v":1,"op":"archive","ts":...}
```

读取即重放：`add` 建状态，`update`/`note`/`move` 改字段，`remove` 打墓碑，`rename`/`archive` 改列表属性。未知 `op` 跳过并计数。

### 2.3 写入与故障

| 项 | 做法 |
| --- | --- |
| 追加 | 进程内按 slug 加锁；`open(..., "a")` 一次写一个整行，行 ≤4 KB |
| 失败 | 直接返回错误。待办是状态，不能像审计那样丢掉 |
| 半行 | 解析失败的末行停止重放并计数；追加前若文件不以 `\n` 结尾先补一个 |
| 压缩 | 行数 >400 或文件 >128 KB：写临时文件（`list` 行 + 每条存活条目一行），`os.replace` 覆盖 |
| 并发 | 同 slug 多进程靠 O_APPEND 行级原子性；顺序语义不支持，但不会写出坏行 |
| 保留 | 工具不删文件；归档只追加 `archive` 行 |

## 3. 工具接口

```text
minir_todo(action, ..., scope="session")
```

| action | 参数 | 返回 |
| --- | --- | --- |
| `lists` | `include_archived=false`, `scope` | slug、显示名、未完成数、完成数、过期数、归档时间 |
| `create_list` | `name`, `slug?` | slug 与路径；重名报错 |
| `rename_list` | `list`, `name` | 新显示名，文件名不变 |
| `archive_list` | `list` | 归档；未完成条目保留 |
| `add` | `list`, `items[]` | 新条目 id、seq；每条可带 `due_at` |
| `items` | `list`, `status?`, `limit=50`, `scope` | id、seq、content、status、note、due_at、overdue、updated_at |
| `update` | `id`, `content?`, `status?`, `note?`, `due_at?` | 更新后的条目 |
| `move` | `id`, `list`, `before_id?` | 跨列表或重排；`seq` 取相邻中点，压缩时归一 |
| `remove` | `id` | 追加墓碑；只用于录入错误 |

约定：

- `list` 接受 slug 或显示名（精确匹配），都不中时报错并给三个候选，不自动建列表。
- `status` 过滤额外接受 `overdue`（`due_at < now` 且状态不在 `completed` / `cancelled`）。
- `due_at` 是 epoch 秒，范围 0 到 now+10 年；`null` 表示清除。
- `add` 一次最多 50 条；`update` 是唯一状态写入口，`completed` 只接受从 `in_progress` 转。
- 只能改本会话创建的列表与条目；跨会话条目在 `scope="all"` 下可读，不可写。

## 4. 状态机

```text
pending ──→ in_progress ──→ completed
   │             │
   │             ├──→ blocked ──→ in_progress
   └──→ cancelled ←──────────────┘
completed ──→ pending        （重开，清 done_at）
```

- `blocked` 必须带 `note`。
- `completed` 写 `done_at`；离开 `completed` 的转换清空它。
- 单列表同时 `in_progress` 上限 3。
- 过期不改变状态，只影响 `overdue` 标记、排序与注入显示。

## 5. 注入

默认开启（`todo_inject=true`），由 prefetch 拼进 `<dynamic-context>`，只注入本会话的列表与条目。

内容：本会话所有未归档列表的全部未完成条目（`pending` / `in_progress` / `blocked`）。已完成的不逐条注入，只给计数。

```text
[Todos] release-1-0 发布 1.0  (2 open, 1 overdue)
  ! a1b2 等 CI 队列        [in_progress, due 2026-10-24]
  - c3d4 写迁移说明        [pending]
[Todos] docs 文档整理  (0 open)
```

- 过期条目前置，前缀 `!`，行尾带 `due`。
- 每个列表 200 条上限仍生效；注入侧默认不再截断（`todo_inject_chars=0`）。
- 硬安全线 32 KB：超过按列表截断，末尾写 `…还有 N 条未完成、M 个列表`。100 列表 × 200 条 × 400 字的最坏情况是 600 KB，不设线会挤掉别的注入。
- 归档列表不注入；别的会话的列表不注入。

## 6. buffer 改 JSONL

### 6.1 目标格式

> 本节已被 `doc/transcript-jsonl-draft.md` 取代：最终路径多一层 `<profile>`，part 只增不删，
> 内存缓存与 `last_cache` 标记也在该稿定义。下面的旧版描述仅保留作历史记录。

```
<persist_dir>/<layer>/<session_id>.jsonl
<persist_dir>/<layer>/<session_id>.part2.jsonl
```

- 一行一条消息，字段沿用现有消息字典，加 `v` 与 `seq`；aux 记录各占一行，带 `kind:"aux"`。
- 从整文件重写改为逐条追加：现有 30 秒 / 50 条的重写节奏（`DEBOUNCE_SECONDS`、`DEBOUNCE_MESSAGES`）不再需要。
- 轮转仍是 2000 条一个文件（`MESSAGES_PER_FILE` → `EVENTS_PER_PART`）；写满开 `part2`。**删除最旧整份的旧规则作废**：最终契约永久保留，读接口按 `seq` 跨 part 续读。
- 追加失败仍重试两次后丢弃并计数：审计用途，宁可少一条，不阻塞会话。
- 追加前若文件不以 `\n` 结尾先补换行。

### 6.2 迁移与影响

- 旧 `.json` 保留原样，不转换、不删除；新会话只写 `.jsonl`，不双写。
- 读侧（operator 脚本、事后分析）要同时认两种后缀。
- 崩溃丢失窗口从 30 秒降到单条；单个 part 内文件只增长，不再有全量重写。
- 仓库里目前没有读回这两个文件的代码，所以格式切换主要影响外部查看方式。

## 7. 限额与错误

| 项 | 限额 | 越界行为 |
| --- | --- | --- |
| 列表数 | 100 个文件 | 报错，提示先归档 |
| 单列表存活条目 | 200 | 报错，提示拆列表 |
| content | 400 字 | 报错，不截断 |
| note | 200 字 | 报错，不截断 |
| 单次 add | 50 条 | 报错 |
| items limit | 200 | 报错 |
| 单行 | 4 KB | 由字段上限保证 |

错误返回 `{ok:false, error}`；磁盘错误额外带 `io:true`。会话 id 缺失返回 `session_scope_required`，文案要求主会话提供会话 id。

## 8. 改动清单

| # | 文件 | 改动 |
| --- | --- | --- |
| 1 | `todo_store.py`（新） | slug 解析与跨会话后缀、会话归属、追加、重放、压缩、半行修复、限额、状态机、due_at / overdue |
| 2 | `tools.py` | `minir_todo` 的 schema 与 handler；`agent_toolsets()` 不含该工具（后台 run 拿不到） |
| 3 | `__init__.py` | 工具分发；prefetch 拼本会话 todo 块（默认开、32 KB 硬线）；`minir_stats` 加列表数与未完成数 |
| 4 | `plugin.yaml` | `provides_tools` 加 `minir_todo`；跑 `hermes plugins validate` 的清单契约测试 |
| 5 | `transcript.py` | 扩展名与写入方式改 JSONL 追加；轮转与丢弃策略按 6.1；半行修复 |
| 6 | `tests/test_minir_todo.py`（新） | 见下 |
| 7 | `tests/test_transcript_jsonl.py`（新） | 见下 |

不建 SQLite 表：正文与状态都在文件里，与「正文落文件」的既有取向一致。

## 9. 测试

| 场景 | 断言 |
| --- | --- |
| 后台 run 调用 | `agent_toolsets()` 无 `minir_todo`，工具不可见 |
| 缺会话 id | `session_scope_required`，不返回空 |
| 别的会话的列表 | 默认 `lists` / `items` 不返回；`scope="all"` 才返回，且只读 |
| 同 slug 跨会话 | 第二个会话自动得到 `-<session8>` 后缀，返回里说明 |
| create 重名 | 同会话内报错，不覆盖 |
| 中文名不给 slug | 落到 `todo-<hash>`，`lists` 能看到显示名 |
| add 不存在的列表 | 报错并给候选，不自动建文件 |
| add 51 条 | 报错，文件无新增行 |
| 三级结构 | schema 无 `parent`；`update(parent=...)` 被拒 |
| `pending → completed` | 拒绝，必须先 `in_progress` |
| `blocked` 无 note | 拒绝 |
| 重开 completed | `done_at` 清空，状态回 pending |
| due_at 范围 | 超范围或非数字被拒；`null` 清除 |
| overdue 过滤 | `status=overdue` 只回未完成且过期的；`lists` 计数一致 |
| 重放顺序 | 同 id 多条 update 以最后一行为准 |
| 半行恢复 | 末尾半行被忽略；下次追加前补换行 |
| 压缩 | 400 行触发；前后重放一致；`os.replace` 原子 |
| 注入 | 只含本会话未完成条目，过期在前；归档与别的会话不出现；超 32 KB 截断并写计数 |
| buffer 追加 | 每条消息一行，`seq` 单调；单 part 内只增长 |
| buffer 轮转 | 2000 行开 part2；第三个 part 出现时删最旧整份 |
| buffer 迁移 | 旧 `.json` 不动；新会话只写 `.jsonl` |

## 10. 未决

1. `scope="all"` 是否要留。只做会话内的话，删掉它和对应测试即可，`lists` / `items` 变得不可跨会话。
2. `todo-<hash>` 用 sha1 前 8 位是否够；冲突概率与可读性要不要换 base32。
3. 注入硬线 32 KB 定多少；设 0 可关掉截断，代价是最坏情况 600 KB。
4. buffer 逐条追加是否保留 50 ms 合并窗口。
5. 旧 `.json` 是否需要一次性转换工具。
6. `due_at` 注入里只写日期不写时区，是否要带时区。

## 11. 风险

| 风险 | 缓解 |
| --- | --- |
| 后台 run 写入造成孤儿条目 | `agent_toolsets()` 不授予；会话 id 缺失直接报错 |
| 全部注入吃掉上下文 | 列表与条目上限 + 32 KB 硬线 + 显式计数；`todo_inject` 可关 |
| 两个会话抢同一个 slug | 自动后缀隔离；同会话内重名报错 |
| 文件被手改坏 | 重放容错、未知 op 跳过、压缩可从合法行重建 |
| 状态造假 | 只允许 `in_progress → completed`；重开清 `done_at` |
| buffer 改格式后读侧漏改 | 后缀写进改动清单；旧文件保留，回滚只需把扩展名读回来 |
