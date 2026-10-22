# conclusions 单表化 + 终答注入 + minir_todo（定稿 v1）

状态：定稿，待实施。
基线：`main` `4036df0`。
取代 `doc/minir-todo-draft.md`（v4：JSONL 多列表）：todo 改为 conclusions 单行，`kind="todo"`。

## 1. 范围与非目标

范围：

1. 删除 `notes` 表，conclusions 成为唯一记忆表；`kind` 成为唯一类型轴。
2. 工具面的 `type` 参数直接是 conclusions kind；`note_id` 改名 `conclusions_id`。
3. provider 新增 `transform_llm_output_inject`，hook 触发时把队列拼到最终回答末尾。
4. `minir_todo`：两级 todo，落 conclusions，prefetch 注入，终答追加 md 表。
5. `on_session_end` / `on_session_finalize` / `on_session_reset` / `compress` 四个边界把校准后的 base 6 个结论写进 conclusions，每会话只写一次。
6. `compress()` 产出的 `session_summary` 走 `save_conclusion`，与 base 6 条无关。
7. conclusions 缩表：删 `from_ts`/`to_ts`/`from_turn`/`to_turn`，结论不再带窗口语义；窗口只属于 `base_refreshes`。
8. `base_revisions` 更名 `dialectic_refreshes`，并删 `advices` 列。

非目标：

- 不改 `layers.CATEGORIES` 和 knowledge 页面的 `category`（那是 layer artifact JSON 的字段，与 conclusions 无关）。
- 不迁移 notes 旧行（按拍板：直接 DROP，丢旧 marker 元数据）。
- 不做 v4 的 `blocked`、`due_at`、多列表、归档、跨会话 `scope="all"`。
- 不改宿主 `todo_list`，两端不互相同步。

## 2. 术语

- conclusions kind：`conclusions.kind` 的取值。`dialectic.review`、`session_summary`、`window_summary` 等旧 kind 不改名；`base.user_card`/`base.ai_card` 被 §4.8 的 6 个字段 kind 取代。
- 工具 kind：`note`、五个 value marker kind、`todo`。只有这七种能被 `minir_remember` / `minir_forget` / `minir_search(type=)` 使用。
- 注入项：`(type, content_md)`，`type` 也是 conclusions kind。

## 3. 契约与现状锚点

| # | 现状 | 目标 |
| --- | --- | --- |
| 1 | `notes` 表建表 `store.py:74-83`，`_ADDED_COLUMNS["notes"]`、`idx_notes_session` | 全部删除；`_DROPPED_TABLES`（`store.py:340`）加 `"notes"` |
| 2 | `remember()` 双写 `store.py:712-734` | 删除；`minir_remember` 直接 `save_conclusion` |
| 3 | `forget()` 查 notes 的 marker_type `store.py:736-752` | 删除；改 `forget_conclusion(kind, conclusions_id)` |
| 4 | `forget_by_content()` `store.py:754-764` | 删除（`on_memory_write` 的 remove 分支改走 id，见 §4.3） |
| 5 | `search()` 结果回查 notes `store.py:2027-2039` | 删除；结果只来自 conclusions |
| 6 | `marker_notes()` `store.py:1430-1437`、`session_markers()` `store.py:2084-2092` | 改查 conclusions，字段改 `conclusions_id/kind/content/created_at` |
| 7 | `stats()` 的 `notes`/`markers` `store.py:2310-2317, 2345` | 删除；新增 `conclusions_by_kind(session_id)` |
| 8 | `VALUE_MARKER_TYPES` `store.py:68-71` | 删除，改 `CONCLUSION_KINDS`（§4.1），不留别名 |
| 9 | `put_conclusion()` `store.py:791-803` | 删除；`save_conclusion()` 成为唯一公共写入口 |
| 10 | 无 `transform_llm_output` | 新增方法 + hook（§4.5） |
| 11 | 无 `minir_todo` | 新增工具（§4.4） |
| 12 | conclusions 带 `from_ts/to_ts/from_turn/to_turn` `store.py:124-127`，`_ADDED_COLUMNS["conclusions"]` `store.py:318-319` | 四列删除；窗口语义只留在 `base_refreshes`（§4.7） |
| 13 | base 结论由 artifact 拆 4 条 `store.py:1237-1244` | 改由校准后的 6 列发布，每会话一次（§4.8） |
| 14 | `session_summary` 用 `put_conclusion`，key 带 sid+run，status=pending `context_engine.py:268-274` | 改 `save_conclusion`，key `session_summary:<index>`，status=active（§4.10） |
| 15 | `base_revisions` 表 + `advices` `store.py:159-183` | 更名 `dialectic_refreshes`，只删 `advices`，其余列与窗口保持不动（§4.9） |
| 16 | conclusions `UNIQUE(kind, key)` `store.py:130`、`ON CONFLICT(kind, key)` `store.py:778` | 保持不动；`session_id` 只入列 |

## 4. 设计

### 4.1 kind 集合

```python
CONCLUSION_KINDS: tuple[str, ...] = (
    "note", "entities", "trajectories", "experiences", "tools", "projects", "todo")
```

- `note` 是兜底 kind：`on_memory_write` 镜像写入、内部层缺 kind 时落这里。
- 工具 kind 白名单就是这个元组；工具外 kind（`base.*` 等）只能由内部层写。
- 删除 `tools.py` 对 `layers.CATEGORIES` 的导入；`category` 参数与列都不存在。

### 4.2 单一写入口

```python
def save_conclusion(self, kind: str, content: str, *, key: str = "",
                    session_id: str = "", subject: str = "", evidence: str = "[]",
                    status: str = "active", created_at: float | None = None,
                    version: int = 0) -> int
```

- 不接收 from/to 与 turn 区间：结论没有窗口语义（§4.7）。
- `content` 原样写入：是文本就存文本，是 JSON 就存 JSON 字符串；不解析、不规范化、不校验格式。参数是 `str`，要存 JSON 由调用方给 JSON 字符串。
- `status` 默认 `active`；只有确有必要时才传别的值。
- 唯一约束保持 `(kind, key)`；`session_id` 只作为列写入。

- `kind` 为空抛 `ValueError`；`key` 为空时生成 `f"{kind}:{uuid4().hex}"`。
- 持锁调用 `_put_conclusion_locked` 并 commit，返回 `conclusions.id`（不是 version）。
- `_put_conclusion_locked` 保留：`put_layer_state`（`store.py:1215-1247`）和 `apply_base_revision` 在事务内多次写，仍用该原语。
- 迁移调用方：`context_engine.py:272`、`__init__.py:1158`、tests（`test_provider.py:282/988`、`test_store.py:99/319/510`、`test_worker.py:838`）。
- 新增 `forget_conclusion(kind, conclusions_id) -> bool`：`DELETE ... WHERE id=? AND kind=?`，kind 不匹配返回 False。
- 各层保存自己的 kind：`base.*` 由 §4.8 的 6 条发布路径写；`dialectic.review` 由 `_ARTIFACT_SECTIONS`（`store.py:360-364`）决定；`session_summary` 由 `context_engine` 传；`window_summary` 由 `add_chunk_summary` 写；工具写 `CONCLUSION_KINDS` 内的 kind；`minir_todo` 写 `todo`。

### 4.3 工具面

`minir_remember`
- 输入：`type`（enum=`CONCLUSION_KINDS`）、`content`。`category` 删除。
- 处理：`type` 不在白名单报错；`save_conclusion(kind=type, content, session_id=self._session_id, subject="tool")`。
- 返回：`{"stored": true, "conclusions_id": N, "type": kind, "session_id": sid}`。

`minir_forget`
- 输入：`type`（enum=`CONCLUSION_KINDS`）、`conclusions_id`（integer）。
- 处理：`forget_conclusion(kind, conclusions_id)`。
- 返回：`{"deleted": bool, "conclusions_id": N, "type": kind}`。

`minir_search`
- 新增可选 `type`（enum=`CONCLUSION_KINDS`）；传了就转成 `kinds=frozenset({type})` 交给 `store.search`（`store.py:1936` 已有该参数）。
- 结果条目：`{conclusions_id, kind, key, text, when, author, subject, session_id, score, rank}`。删除 `id`、`note_id`、`conclusion_id`、`marker_type`、`category`、`from_turn`/`to_turn`（结论无窗口）。
- 保留 FTS + scan 回退（短词/CJK 需要）。

`minir_stats`
- 删除 `notes`、`markers` 两个键。
- 新增 `conclusions_by_kind`：`SELECT kind, COUNT(*) ... WHERE session_id=? GROUP BY kind`，只算当前 host 会话；持久行（`session_id=''`）不计。
- 保留全局 `conclusions` 总数。

`on_memory_write` 镜像（`__init__.py:818+`）
- add/replace：`save_conclusion("note", content, session_id=...)`，返回的 id 作为对外 id。
- remove：按内容找到本会话最新一条 `kind="note"` 行，用 `forget_conclusion` 删除；`forget_by_content` 随 notes 表一起删除。

### 4.4 minir_todo

输入：

```json
{"todos": [{"id": "t1", "content": "跑全量测试", "status": "in_progress"},
           {"id": "t1a", "content": "先跑 unit", "status": "pending", "parent": "t1"}]}
```

校验：
- 顶层 `todos` 必填，0..256 条；`id` 唯一、非空、≤64；`content` 非空、≤2000。
- `status` ∈ `pending|in_progress|completed|cancelled`（对齐宿主 `tools/todo_tool.py:26-33`）。
- `parent` 只能指向同列表的一级项；被指向项自己不能再有 parent（严格两级）。
- 序列化后超过 `_MAX_CONTENT`（12000）报 `todo_too_large`，不截断。

存储：
- `save_conclusion("todo", json.dumps({"todos": [...]}, ensure_ascii=False), key=f"session:{sid}:todo", session_id=sid, subject="tool")`。
- 每会话单行 upsert，key 固定；`todos=[]` 时用 `get_conclusion("todo", key)` 取 id 再 `forget_conclusion`，不存空表。
- 存储内容是 JSON，不是渲染后的 md：prefetch 与终答各自渲染，状态永远来自同一份数据。

prefetch（`_todo_block(sid)`）：
- 不走 query 检索，有行就注入，位置紧跟 layer state 块。
- 两级缩进，全部条目都列（完成项保留，前缀 `[x]`），头部给 `open/total`。

```text
[Todos] 2 open / 4 total
  [~] t1 跑全量测试  (in_progress)
    [ ] t1a 先跑 unit
  [x] t2 写迁移说明
```

终答：
- `minir_todo` 渲染 md 表并经 `transform_llm_output_inject("todo", table)` 入队；`todo` 是状态型，同类型旧项被替换（§4.5）。

```markdown
| # | 任务 | 状态 | 父项 |
| --- | --- | --- | --- |
| 1 | 跑全量测试 | in_progress |  |
| 1.1 | 先跑 unit | pending | t1 |
| 2 | 写迁移说明 | completed |  |

Todo: 1/3 done, 1 in progress, 1 pending.
```

注册面：
- `plugin/minir/plugin.yaml`：`provides_tools` 加 `minir_todo`。
- `tools.py`：`SCHEMAS`、`TOOL_NAMES`、`_EMOJI`。
- `agent_loop.py:98` 的 `READ_ONLY_DROPPED_TOOLS` 加 `minir_todo`（后台 run 与 reviewer 不给）。
- 契约测试 `test_contract.py` 的工具清单。

### 4.5 终答注入通道

provider 状态（同一实例，加锁）：

```python
self._injects: dict[str, list[dict]] = {}   # session_id -> [{"type","content","seq"}]
self._inject_seq: int = 0
```

```python
def transform_llm_output_inject(self, inject_type: str, content_md: str,
                                *, session_id: str = "") -> None
```

- `session_id` 缺省取 `self._session_id`；空则丢弃并记 debug。
- `content_md` strip 后为空则丢弃。
- `inject_type` 不在 `INJECT_ORDER` 时按首次入队顺序排在已知类型之后。
- 状态型类型 `INJECT_STATEFUL = frozenset({"todo"})`：入队前删掉本会话同类型的旧项，保证终答只有一张最终表；其余类型保留同类型入队序。
- 上限：每会话 20 项 / 累计 32 KB，超出拒绝并记 warning（不做静默截断）。

渲染顺序：`INJECT_ORDER` 中已知类型的声明顺序 → 未知类型按首次 seq → 同类型按 seq。

```python
INJECT_ORDER: tuple[str, ...] = ("todo",)
```

hook：

```python
def _on_transform_llm_output(self, response_text: str = "", session_id: str = "", **_) -> str | None
```

- 进入即 `pop(session_id)`：**触发即清空**，即使最终不返回内容。
- 队列为空或 `response_text` 为空返回 `None`（宿主保留原文本）。
- 否则返回 `response_text.rstrip() + "\n\n" + "\n\n".join(chunks)`。
- 宿主按 (session, turn) 去重，所以正常只调用一次；插件侧仍以“取出即清空”保证不会重复追加。

### 4.6 可见性、权重、prefetch

- `_visibility_sql`（`store.py:1919-1934`）：durable 分支由 `(kind='note' AND session_id='')` 改为 `(session_id='' AND kind IN (_LONG_TERM_KINDS + CONCLUSION_KINDS))`；`_LONG_TERM_KINDS` 改为 4 个卡片 kind（§4.8）。
- `_KIND_WEIGHT`（`store.py:374-383`）：加五个 marker kind `0.9`、`todo 1.2`；`note 0.7` 不变；6 个 base kind 用 §4.8 的值。
- `_PREFETCH_KINDS`（`__init__.py:119`）：`{"note"}` → `{"note","entities","trajectories","experiences","tools","projects"}`；`todo` 不进 query recall，走 §4.4 的无条件注入。
- `__init__.py:1555-1556` 的 prefetch 行标签改用 `kind`；`__init__.py:1360-1375` 的结果整形删除 `marker_type/note_id`。
- `__init__.py:1014-1030` 的 extraction snapshot：`{"conclusions_id", "kind", "content", "created_at"}`；`extraction.py:160-175` 只是把该 JSON 原样放进 prompt，不需要改解析。
- `card_review.py:416`：`{"type": row.get("kind"), "content": ...}`，删 `category`。

### 4.7 conclusions 缩表

最终列：

```text
id, kind, key, session_id, subject, content, evidence, version, status,
created_at, updated_at
```

删除 `from_ts`、`to_ts`、`from_turn`、`to_turn`，结论不再表达“覆盖哪段窗口”。落点：

- DDL `store.py:124-127` 四列删除。
- `_ADDED_COLUMNS["conclusions"]`（`store.py:318-319`）整项删除。
- `_put_conclusion_locked` / `save_conclusion` 去掉区间参数。
- `put_layer_state` 的拆分路径不再带窗口（`store.py:1237-1244`）。
- `add_chunk_summary`（`store.py:838-865`）不再写区间；key 固定为 `chunk:<sid>:<from_ts:.6f>`，幂等改按该 key 查（原来按 `from_ts` 查）。
- `search` 的 SELECT 与结果字段去掉 `from_turn/to_turn`；`session_markers` 同步。
- 窗口读取整体搬到 `base_refreshes`：

| 原消费者 | 原字段 | 新来源 |
| --- | --- | --- |
| `add_chunk_summary` 幂等 | `from_ts` | `conclusions.key = chunk:<sid>:<from_ts>` |
| `latest_chunk_end()` `store.py:878-885` | `MAX(to_ts)` | `MAX(window_to_ts)`，`base_refreshes WHERE session_id=?` |
| `chunk_turn_range()` `store.py:887+` | `from_turn/to_turn` | `base_refreshes` 按 `(session_id, window_from_ts)` 取 `window_from_turn/window_to_turn` |
| `latest_chunk_turn_before()` | 同上 | `base_refreshes WHERE window_from_ts < ?` 取最大 `window_to_turn` |

- 窗口语义只保留在 `base_refreshes`（`window_from_ts`、`window_to_ts`、`window_from_turn`、`window_to_turn`）；别的表已有的窗口列不动（turns、chunk 相关的 jobs 等）。

迁移：对 `PRAGMA table_info(conclusions)` 里存在的四列逐个 `ALTER TABLE conclusions DROP COLUMN`。`conclusions_fts` 只索引 `content`（`store.py:545-569`），触发器不引用这些列，删完后 `INSERT INTO conclusions_fts(conclusions_fts) VALUES('rebuild')` 即可。

### 4.8 校准后 base 的 6 条结论

`_BASE_REVISION_COLUMNS`（`store.py:369-372`）本来就是 6 个字段，结论逐字段发布：

| base_refreshes 列 | kind | scope |
| --- | --- | --- |
| `user_representation` | `base.user_representation` | user |
| `user_peer_card` | `base.user_peer_card` | user |
| `ai_self_representation` | `base.ai_self_representation` | agent |
| `ai_identity_card` | `base.ai_identity_card` | agent |
| `window_summary` | `base.window_summary` | session |
| `value_markers` | `base.value_markers` | session |

`publish_base_conclusions(session_id, *, user_subject="", ai_subject="") -> int`（provider 按 worker 的 `user_subject()`/`dialectic_subject()` 同规则传入）：

1. `latest_base_refresh(session_id)`；没有行返回 0。
2. 该会话已有 `conclusions_published_at` 非空的行则返回 0（**只能被调用一次**，按会话判定）。
3. 逐字段 `save_conclusion(kind, content, key=_scope_key(scope, user_subject, session_id, ai_subject), session_id=("" if scope != "session" else sid), subject=row["subject"], version=1)`。
   - `content` 原样取列值，不解析、不渲染：`value_markers` 是 JSON 就存 JSON 字符串。空字段跳过。
4. `UPDATE base_refreshes SET conclusions_published_at = ? WHERE id = ?`（写最新行；守卫扫全会话的该列，所以后到的 refresh 也不会再触发）。
5. 返回写入条数。

`conclusions_published_at` 通过 `_ADDED_COLUMNS["base_refreshes"]` 加列。它比内存标志可靠：provider 重启、两个 provider 实例同时触发都只会真正写一次；并发窗口里即使双方都写，6 个 key 也是幂等 upsert。

触发点（四个入口都调同一个带守卫的方法，先到者胜）：

| 入口 | 位置 | 调用时机 |
| --- | --- | --- |
| `on_session_end` | `__init__.py:927` → `_finish_session` | `_flush_boundary(close=True)` 之后，尾段已被推理 |
| `on_session_finalize` | `_on_session_finalize` `__init__.py:1702` | `TRACKER.stop` 之后 |
| `on_session_reset` | `_on_session_reset` `__init__.py:1684` | 用 `old_session_id` 发布（结束的是旧会话） |
| `compress` | `_prepare_compression` `__init__.py:885` | `_worker.drain_session(...)` 之后、`TRACKER.reset_layers` 之前；此时仍在被压缩的旧会话 |

压缩路径里两件事彼此独立：`publish_base_conclusions` 只写 base 6 条；`session_summary` 由 `compress()` 自己 `save_conclusion`，既不是第 7 条，也不参与 `conclusions_published_at` 守卫。

同时移除 `_ARTIFACT_SECTIONS["base"]`（`store.py:353-359`）：base 的结论只由上面 6 条发布，`put_layer_state` 不再为 base 拆 4 条重复行。`_ARTIFACT_SECTIONS` 只保留 dialectic 的 `## Base Revision`。

连带：`_LONG_TERM_KINDS`（`store.py:345-348`）改成 4 个卡片 kind；`_KIND_WEIGHT` 用新 kind 名（user/ai 卡片 1.5/1.4，window_summary/value_markers 1.0）。

### 4.9 base_revisions → dialectic_refreshes

- 只做两件事：表名 `base_revisions` → `dialectic_refreshes`；删 `advices` 列（代码里本来就只写 `"[]"`，`store.py:1488-1490`）。
- 数据库列一个不动：6 个内容列、四个窗口列、`base_refresh_id`/`changed_fields`/`source_job_id`/`is_latest`/`created_at` 全部保留，列名、类型、顺序不变；按 `window_to_ts` 的历史排序不变。唯一删掉的是 `advices`。
- 索引随表保留原名（SQLite 自动跟随 RENAME）。FTS 表与触发器必须重建：`content=` 是模块参数，不会跟随表名，只能指向新表；对象名同步成 `dialectic_refreshes_fts`，索引列由 7 个（含 `advices`）变成 6 个内容列。
- Python 方法名不改：`base_revisions()`、`search_base_revisions()`、`_ensure_base_revisions_fts()` 继续用旧名，指向新表。
- 迁移：`ALTER TABLE base_revisions RENAME TO dialectic_refreshes` → 先删旧 FTS 与触发器 → `ALTER TABLE dialectic_refreshes DROP COLUMN advices`（SQLite ≥ 3.35；Python 3.11+ 自带）→ 按新表名重建 FTS 与触发器。索引与 rowid 原样保留。若目标环境不支持 DROP COLUMN，退化到新建表 + `INSERT ... SELECT` 复制非 advices 列，再 DROP 旧表。两张表都在时只删旧表。
- 最终列：`id, session_id, subject, base_refresh_id, user_representation, user_peer_card, ai_self_representation, ai_identity_card, window_summary, value_markers, window_from_ts, window_to_ts, window_from_turn, window_to_turn, changed_fields, source_job_id, is_latest, created_at`。

### 4.10 compress 后的 session_summary

- 仍由 `ContextEngine.compress()` 自己写，但改走 `save_conclusion`：

  ```python
  save_conclusion("session_summary", summary,
                  key=f"session_summary:{index}", session_id=session,
                  subject="session", evidence=json.dumps([run_id]),
                  status="active")
  ```

- `index` 由 `store.next_session_summary_index(session_id)` 计算：`COUNT(*) WHERE kind='session_summary' AND session_id=?` + 1，就是**本会话**的第几次压缩。`session_id` 取自 `compress()` 当前的 host 会话，作为参数传给 `save_conclusion`，不拼进 key。
- `status=active` 从写入起生效：不再有 pending 两阶段。`promote_summary_run`（`store.py:825-834`）与 `context_engine.py:189` 的调用删除；`expire_pending_handoffs` 只保留 legacy `handoff` 清理。
- 载体消息仍带 `#<run_id>` 标记，用于折叠；`run_id` 只进 `evidence`，不再进 key。
- 与 base 6 条完全无关：不共享 key、不共享 `conclusions_published_at` 守卫、不依赖先后顺序。

## 5. 改动清单

| # | 文件 | 改动 |
| --- | --- | --- |
| 1 | `plugin/minir/store.py` | 删 notes 建表/index/迁移列、`remember`、`forget`、`forget_by_content`、`marker_notes`、`put_conclusion`；加 `CONCLUSION_KINDS`、`save_conclusion`、`forget_conclusion`、`conclusions_by_kind`、`next_session_summary_index`；`search` 去 notes 回查、结果改字段；`session_markers`/`marker_conclusions` 改查 conclusions；`stats` 去 notes/markers；`_DROPPED_TABLES` 加 notes；`_visibility_sql`/`_KIND_WEIGHT` 调整；conclusions 删四个窗口列并迁移；`publish_base_conclusions` + `conclusions_published_at`；`base_revisions` → `dialectic_refreshes`（只删 advices，窗口列保留，FTS/索引跟随表名重建，方法名不变）；删 `promote_summary_run` |
| 2 | `plugin/minir/tools.py` | `minir_remember`/`minir_forget`/`minir_search` schema 改 kind 与 `conclusions_id`；新增 `minir_todo`；删除 `CATEGORIES` 导入 |
| 3 | `plugin/minir/__init__.py` | 工具分发改 `save_conclusion`/`forget_conclusion`；`minir_stats` 加 `conclusions_by_kind`；`transform_llm_output_inject` + `_on_transform_llm_output` + hook 注册；prefetch 加 `_todo_block`；`on_memory_write` 改单表；snapshot/结果整形/行标签改字段；`_finish_session`、`_on_session_finalize`、`_on_session_reset`、`_prepare_compression` 调 `publish_base_conclusions` |
| 4 | `plugin/minir/plugin.yaml` | `provides_tools` 加 `minir_todo`；`provides_hooks` 加 `transform_llm_output`；版本 0.15.0 → 0.16.0 |
| 5 | `plugin/minir/agent_loop.py` | `READ_ONLY_DROPPED_TOOLS` 加 `minir_todo` |
| 6 | `plugin/minir/card_review.py` | marker 证据改读 `kind` |
| 7 | `plugin/minir/context_engine.py` | `put_conclusion` → `save_conclusion`；summary key 改 `session_summary:<index>`、status=active；删 `promote_summary_run` 调用 |
| 8 | `README.md` + 本目录文档 | 表清单、工具清单、store 说明去掉 notes |
| 9 | `tests/` | 见 §6 |

## 6. 测试

只测草稿改动的契约，一条用例对一个断言组；复用现有 `db` / `provider` fixture，不新增端到端流程测试。

| # | 用例 | 断言 |
| --- | --- | --- |
| 1 | conclusions 形状 | 表集合无 `notes`；`PRAGMA table_info(conclusions)` 无四个窗口列；FTS 仍可查 |
| 2 | 单表写读 | `save_conclusion("entities", …)` 后 search 命中，带 `conclusions_id`，无 `note_id/marker_type/category` |
| 3 | forget | 用 `kind="note"` 的行：kind+id 正确时删行；kind 不符返回 False 且行还在 |
| 4 | search type | `type="tools"` 只回 tools 行；不传回全 kind |
| 5 | stats | `conclusions_by_kind` 只数本会话；键里没有 `notes`/`markers` |
| 6 | 原样 content | JSON 字符串与纯文本都逐字节回读一致 |
| 7 | 旧库迁移 | `notes` 消失；`base_revisions` 变 `dialectic_refreshes`，无 `advices`，窗口列与行数保留 |
| 8 | base 6 条 + 一次 | 6 个 kind 各一行，user/agent 行 `session_id=''`、window 行带 sid；第二次触发返回 0 |
| 9 | summary | 同一会话两次压缩得 `session_summary:1` / `:2`；status=active |
| 10 | 注入顺序 | 入队 entities 后 todo：终答中 todo 在前，未知类型最后 |
| 11 | 注入清空 | 触发一次后队列为空；`response_text=""` 返回 None 且仍清空 |
| 12 | todo 校验 | 三级嵌套、孤儿 parent、重复 id 报错且不落库 |
| 13 | todo 单行 | 两次调用只有一行 `key=session:<sid>:todo`，内容取最后一次；`todos=[]` 删行 |
| 14 | todo 渲染 | prefetch 有 `open/total` 与两级缩进；终答表行列与汇总正确 |
| 15 | 只读 reviewer | 工具集不含 `minir_todo` |

不测，避免膨胀：

- 宿主 hook 调度、真实 transcript 落库、跨进程重启。
- 网络与 LLM 调用。
- 不为每个 kind 复制一套用例；kind 只取 `entities`、`tools`、`note` 三个代表。
- 现有测试只改被本次契约破坏的断言，不新增端到端链路。

## 7. 迁移与风险

- 迁移：`_DROPPED_TABLES` 加 `"notes"`；打开旧库时 DROP。旧 marker 行在 conclusions 里已经存在（`note:<id>`，kind=`note`），但失去 marker_type/category，按拍板接受。
- 风险 1：注入内容会进入持久 transcript 和下一轮历史，模型的下一轮会看到自己上一轮的 todo 表。这是“注入最终回答”的定义，不是 bug。
- 风险 2：`transform_llm_output` 在 assistant 行落库前执行；若宿主在流式结束后再次调用（它按 session+turn 去重），队列已空，不会重复。
- 风险 3：`minir_todo` 每次调用都会改写注入项；同一回合调用多次只保留最后一张表。
- 风险 4：`_visibility_sql` 改为“durable = session 为空且 kind 在 `_LONG_TERM_KINDS + CONCLUSION_KINDS`”，会让以后新增的 session 级 kind 默认不跨会话，这是想要的保守方向；新增 durable kind 必须同时改这个谓词。
- 风险 5：base 6 条按会话只发布一次。压缩会自动创建新会话，`on_pre_compress` 又在 `compress()` 之前触发，所以发布用的是被压缩的旧会话；该会话不会再有后续校准，“先到者胜”就是它的最后一次。
- 风险 6：base 结论只在边界出现，会话中途 `minir_search` 搜不到当轮 base 内容。实时注入走 `layer_state` 与 `base_refreshes`，不受影响。
- 风险 7：旧库里的 `base.user_card` / `base.ai_card` 行不删除，但 `_LONG_TERM_KINDS` 不再包含它们，跨会话检索不再返回；它们只是惰性历史行。
- 风险 8：summary 一写入就是 active。若压缩结果最终没提交到 transcript，这行仍会被检索到；原来的 pending + promote 两阶段正是为规避这种情况。按“status=active”接受。

## 8. 已定口径

1. todo 的“两级”是父项/子项（`parent`，宿主 `todo_list` 同形），不是 v4 的多列表。三级嵌套、孤儿 parent、重复 id 都报错。
2. prefetch 注入全部条目，含 completed / cancelled；状态用前缀表示，头部给 `open/total`。
3. `minir_search` 的 `type` 是单值字符串，过滤 `kind = ?`；不传则全 kind。
4. `_ARTIFACT_SECTIONS["base"]` 移除，base 结论只来自 §4.8 的 6 条；`_ARTIFACT_SECTIONS` 只保留 dialectic 的 `## Base Revision`。

## 9. 实施顺序

每步结束跑 `pytest`，通过再进下一步。迁移前备份 `$HERMES_HOME/minir-memory/minir.db`。

1. store.py 表结构：conclusions 删四列；`_DROPPED_TABLES` 加 `notes`；`dialectic_refreshes` 迁移；`CONCLUSION_KINDS`；`save_conclusion` / `forget_conclusion` / `conclusions_by_kind` / `next_session_summary_index`；删 notes 路径、`put_conclusion`、`promote_summary_run`。
2. store.py 读路径：`search`、`session_markers`、`marker_conclusions`、`_visibility_sql`、`_KIND_WEIGHT`、`_PREFETCH_KINDS`；`publish_base_conclusions` + `conclusions_published_at` + `_ADDED_COLUMNS["base_refreshes"]`。
3. tools.py：三个工具 schema 改 kind / `conclusions_id`，新增 `minir_todo`。
4. `__init__.py`：工具分发、`minir_stats`、注入通道与 hook、`_todo_block`、`on_memory_write`、snapshot 与结果整形、四个边界的 `publish_base_conclusions`。
5. `plugin.yaml`、`agent_loop.py`、`card_review.py`、`context_engine.py`。
6. tests：按 §6 的 15 条改/补，只碰被本次契约破坏的断言；README 与本文的表清单同步。
7. 跑现有 `pytest` 全集做回归；不新增端到端用例，不写手工冒烟脚本。
