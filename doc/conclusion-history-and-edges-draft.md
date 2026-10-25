# conclusions 历史化 + 关系边 + 可选向量召回（草稿 v1）

状态：核心与实体合并候选均已实现（2026-10-25）：版本表/as-of、edges、remember_mode、merge/unmerge、scheduler_runs、折叠与归档、向量索引与降级、三档合并候选。§4.10 的 note 抽取复用尚未接线，extraction 仍走既有写页路径。
基线：`main` `30a4b09`，`pyproject.toml` 0.17.0，新库 `PRAGMA user_version = 4`（实测）。
来源：FalkorDB / Graphiti / EverOS 三仓评估的落地版；结论是"不引入任何嵌入式图数据库"。

## 1. 范围与非目标

范围：

0. `minir_remember` / `minir_forget` 增加 `remember_mode` 参数（`conclusion` 默认 / `knowledge` / `card`），
   三个模式写入不同的存储位置；`conclusion_versions` 只覆盖 `conclusion` 模式。
1. 新增 `conclusion_versions`：结论的追加式历史行 + 双时间窗（`valid_from` / `valid_to`），支持 as-of 查询。
2. 新增 `edges`：结论间的有向关系边，供检索做 1–2 跳扩展。
3. `search()` 增加三个可选参数 `as_of` / `expand` / `include_superseded`；默认值必须让现有行为逐字节不变。
4. 迁移：`user_version` 4 → 5，建表 + 把现有 `conclusions` 里 kind ∈ `CONCLUSION_KINDS` 且非 `todo` 的行各回填一条开区间版本行。
5. 版本表只对 `conclusion` 模式下 `kind ∈ CONCLUSION_KINDS` 且 `kind != "todo"` 的结论追加；每结论保留最近 32 版。
6. 实体合并分三档：高相似度自动合并（可逆重定向），中段写 `advisories` 人工确认，低分忽略。
7. 向量召回默认开启：`provider=siliconflow`、`model=BAAI/bge-m3`、`dim=1024`；`sqlite-vec` 写入依赖，
   外部调用失败时全程降级纯 bm25。
8. 调度器新增执行历史表 `scheduler_runs`，任务结果落库并可查询、可保留。
9. 换向量维度走后台迁移任务，不在启动路径阻塞。

非目标：

- 不引入嵌入式图数据库或任何常驻进程。理由与实测见 §7。
- 不在热路径调用 LLM。
- 不把 embedding 变成必需依赖：不装 embedding 时行为与现在完全一致。
- 不改"Markdown 与文件是真源、索引可弃"这条定位。`conclusion_versions` 是结论日志，不是第二真源。
- 不改现有工具签名，只加可选参数；不动 base / dialectic 两层。
- 知识页（`knowledge/`）的文件历史不在本草案范围。
- 边不做时间旅行：`edges` 单行可变，只保留当前状态。

## 2. 术语

- 版本行：`conclusion_versions` 的一行，描述某结论在 `[valid_from, valid_to)` 区间内的事实内容。
- 开区间：`valid_to IS NULL` 的版本行，即在当前时刻仍成立的那一版。每个结论最多一条（由部分唯一索引强制）。
- as-of 查询：给定时刻 T，返回 T 时刻成立的版本行。
- 扩展：从检索命中行出发，沿 `edges` 走 1–2 跳，把邻居行作为补充候选加入结果。
- 知识单元（unit）：页面的 path，即 `knowledge/` 下的文件路径。`(category, org, topic)` 是 front matter 元数据，用于索引、检索与归类，不参与身份；同一 path 是同一单元，页面就是单元本身。
- 结论：`conclusions` 表的一行，身份是 `(kind, key)`；与知识单元不是同一个东西，一个结论可以被写成一张页。
- claim：卡片或知识页正文里的一行断言，最小引用单位；一张页的 `content` 由 3–8 条 claim 组成。
- marker：B4 输出的单元对象；落页后成为知识单元（path），marker 本身只在本次输出里存在。
- `remember_mode`：`minir_remember` / `minir_forget` 的路由参数，决定写入 conclusions、知识页，还是 card 文件。
- marker kinds：`_MARKER_KINDS`（store.py:82）的五个价值标记 kind —— `entities`、`trajectories`、
  `experiences`、`tools`、`projects`。`note` 与 `todo` 属于 `CONCLUSION_KINDS`（store.py:90）但**不在** marker kinds 里。
- b4：base 刷新里的价值标记子趟（`b4_value_markers`，worker.py:958-964）；`note` 记录后由它异步提取为知识页。b4 自己是知识写者：产出单元后调 `minir_kb_write` 落页，返回 `page_writes`，不再委托第二个知识 agent。结果经 `transform_llm_output_inject`（`__init__.py:125`）注入一条提示忽略的消息。
- card part：四份全局 card 文件（user/identity.md、user/preference.md、agents/&lt;profile&gt;/identity.md、
  agents/&lt;profile&gt;/preference.md），见 card_review.py:106-115。
- `same_as` 边：自动合并写下的别名边 `(src, 'same_as', dst)`，`edges.meta` 里存合并前的 status；
  `minir_unmerge` 删这条边即撤销合并。

## 3. 契约与现状锚点

| # | 现状 | 目标 |
| --- | --- | --- |
| 1 | `save_conclusion()` 是唯一公共写入口，`store.py:885` | 保持入口不变，在同一事务内追加版本行 |
| 2 | `_put_conclusion_locked()` 用 `ON CONFLICT(kind,key) DO UPDATE` 覆盖 `content`/`status`/`version+1`，`store.py:941` | 覆盖前把旧状态落成闭区间版本行 |
| 3 | `conclusions` 上三个触发器只做 FTS 镜像，`store.py:731-741` | 不动；`conclusion_versions` 不建 FTS |
| 4 | `search()` 纯 bm25 + `LIKE` 兜底，`store.py:2190` | 增加 as-of / 扩展 / 已废弃三参数，默认关闭 |
| 5 | 迁移由 `_migrate_once()` + `PRAGMA user_version` 门控，`store.py:537` | 追加 4 → 5 段：建表 + 回填，可重入 |
| 6 | `advisories` 与 `adviser_reviews` 表已存在（新库实测 29 张表） | 合并候选复用 `advisories`，不新建表；行带 `source='merge'`、`target='merge_candidate'`，与顾问建议分开统计 |
| 7 | `conclusions` 现状唯一键 `(kind,key)`，`store.py:126` 起 | 不动；版本表以 `conclusion_id` 外键关联 |
| 8 | `_MARKER_KINDS` 只有五个价值标记 kind，`store.py:82-83` | 版本表按它判定"是否是结论"时不完整，见下一行 |
| 9 | `CONCLUSION_KINDS = ("note", *_MARKER_KINDS, "todo")`，`store.py:90` | 版本覆盖 `note` + 五个 marker kind，排除 `todo` |
| 10 | `note` 写入后由 b4 异步提取为知识页，`worker.py:958-964` | b4 只写页，不改写 note 行；note 的版本行只在 note 本身更新时追加 |
| 11 | card 只有四个 part，文件路径由 `card_paths()` 决定，`card_review.py:106-115` | `card` 模式走 `card_review.write_cards`；用户发起写入 `source=operator_edit`，不进 conclusions |
| 12 | base 层取值词汇：`CATEGORIES`（含默认 `METHOD`）与 `ORGS`，`layers.py:32-33` | `knowledge` 模式按这套词汇入队，由 B4 落页，不进 conclusions |

## 4. 详细设计

### 4.1 conclusion_versions

```sql
CREATE TABLE IF NOT EXISTS conclusion_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conclusion_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 0,
    valid_from REAL NOT NULL,
    valid_to REAL,
    superseded_by INTEGER,
    recorded_at REAL NOT NULL
);
-- 每个结论最多一条开区间行
CREATE UNIQUE INDEX IF NOT EXISTS ix_cv_open
    ON conclusion_versions(conclusion_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS ix_cv_key  ON conclusion_versions(kind, key, valid_to);
CREATE INDEX IF NOT EXISTS ix_cv_asof ON conclusion_versions(valid_from, valid_to);
```

不变量：

1. 同一 `conclusion_id` 恰好一条 `valid_to IS NULL`（部分唯一索引已实测生效）。
2. `valid_from < valid_to`（闭区间时）；插入与闭合在同一事务，失败则整笔回退。
3. 版本行只追加，不更新 `content`；唯一允许的更新是把 `valid_to` 从 NULL 改成时间戳。
4. `recorded_at` 是写入时刻，`valid_from` 是事实生效时刻；as-of 用 `valid_from`。

版本范围（按现有常量推导，不另造白名单）：

```python
from .store import CONCLUSION_KINDS   # ("note", entities, trajectories, experiences, tools, projects, todo)

def _versioned(kind: str) -> bool:
    return kind in CONCLUSION_KINDS and kind != "todo"
```

- `note` 在内：它是 fallback kind，也是被 b4 异步提取为知识页的那一类（§4.8）；历史必须留，但 note 行本身不被 b4 改写。
- 五个 marker kind 在内：它们就是 `_MARKER_KINDS`，base 层合并的目标。
- `todo` 排除：每会话任务清单，改一次追加一版只会把表撑爆。
- `base.*`、`dialectic.review`、`session_summary`、`window_summary` 本来就不在 `CONCLUSION_KINDS` 里（store.py:85-90），版本表不覆盖。

预算：折叠与归档都自动化，由统一调度器驱动（§4.9）。折叠条件：该结论最早版本距今 > 180 天**且**版本数 > 32，
保留最近 32 版，更早的折成一条 `status='collapsed'`（`content` 保留被折叠区间的正文，`valid_from`/`valid_to` 覆盖整个区间）。

忘记语义：`forget_conclusion()` 不删版本行，只把开区间行闭合（`valid_to = now`，`status = 'deleted'`）。
"这条曾经存在过"本身是事实，删掉会让 as-of 丢失依据。

### 4.2 写入路径（未接入）

```python
# store.py，_put_conclusion_locked 内部，UPDATE 之前
def _append_version_locked(self, row: sqlite3.Row, now: float) -> None:
    if not _versioned(row["kind"]):   # CONCLUSION_KINDS 且非 todo，见 §4.1
        return
    prev = self._conn.execute(
        "SELECT id FROM conclusion_versions WHERE conclusion_id = ? AND valid_to IS NULL",
        (row["id"],)).fetchone()
    if prev is not None:
        self._conn.execute(
            "UPDATE conclusion_versions SET valid_to = ? WHERE id = ?", (now, prev["id"]))
    self._conn.execute(
        "INSERT INTO conclusion_versions (conclusion_id, kind, key, session_id, subject,"
        " content, evidence, status, version, valid_from, recorded_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (row["id"], row["kind"], row["key"], row["session_id"], row["subject"],
         row["content"], row["evidence"], row["status"], row["version"], now, now))
```

调用点只有一个：`_put_conclusion_locked` 里 `ON CONFLICT` 之前读出旧行后调用。`forget_conclusion()` 不删版本行（删除是物理删除，历史保留；`status` 置 `deleted` 由实现决定，见 §9 问题 1）。

### 4.3 as-of 查询

```sql
SELECT content, version, valid_from, valid_to
  FROM conclusion_versions
 WHERE conclusion_id = :cid
   AND valid_from <= :t
   AND (valid_to IS NULL OR valid_to > :t)
 ORDER BY valid_from DESC
 LIMIT 1;
```

同一模式可用于全库 as-of（去掉 `conclusion_id` 条件，加 `kind`/`key` 过滤与 `LIMIT`）。

实测（新库，三条版本行，时间戳手工给定）：

| 查询时刻 | 返回 |
| --- | --- |
| T+1 | `minir 用 SQLite FTS5` |
| T+150 | `minir 用 SQLite FTS5 加 LanceDB` |
| T+300 | `minir 用 SQLite FTS5 加 LanceDB，不引图库` |

追加三次版本行（含建表与提交）共 0.025s。

### 4.4 edges

```sql
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_kind TEXT NOT NULL, src_key TEXT NOT NULL,
    rel TEXT NOT NULL,
    dst_kind TEXT NOT NULL, dst_key TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    meta TEXT NOT NULL DEFAULT '{}',
    valid_from REAL NOT NULL, valid_to REAL,
    source_conclusion_id INTEGER, created_at REAL NOT NULL,
    UNIQUE(src_kind, src_key, rel, dst_kind, dst_key)
);
CREATE INDEX IF NOT EXISTS ix_edges_src ON edges(src_kind, src_key);
CREATE INDEX IF NOT EXISTS ix_edges_dst ON edges(dst_kind, dst_key);
```

端点用 `(kind, key)` 而不是 `conclusions.id`：`key` 是稳定标识，`id` 在重建库时会变。`source_conclusion_id` 只做溯源记录，可以为空。

边的来源（全部离线派生，零模型）：

1. base 发布时已有的 `subject`/`topic` 结构。
2. 知识页 front-matter 的 `org` / `category` / `topic`（父子、同 org）。
3. 人工调用新增的 `minir_link(src, rel, dst)` 工具直接写。

边的类型统一小写，目标词表与 `rules/markers-org.md` v5 一致（v5 待扩 `derived_from`、`same_as`）：
`part_of`（事件或经历 → 项目）、`derived_from`（经验 → 它来自的事件）、`instance_of`（单元 → 具名实体或工具）、
`caused_by`、`supersedes`、`contradicts`、`updates`、`same_as`（合并别名）。

代码同步扩 `layers.RELATION_TYPES`（layers.py:35-36）与 `rules/markers-org.md` v5，加入 `derived_from`、`same_as`；
`layers.py:307` 的 marker 校验随词表一起接受这两类。页参与的边用 `('page', path)` 作为端点；结论边仍用 `(kind, key)`。

页面就是单元（身份是 path）：正文留在 Markdown，front-matter 的 `org` / `category` / `topic` / `objects` / `relations`
是元数据与边的声明源，`edges` 表由它派生，删表可重建。图是索引，不是第二真源。

1–2 跳扩展：

```sql
WITH RECURSIVE hop(kind, key, depth) AS (
    SELECT 'projects', :start_key, 0
    UNION ALL
    SELECT e.dst_kind, e.dst_key, hop.depth + 1
      FROM hop JOIN edges e
        ON e.src_kind = hop.kind AND e.src_key = hop.key
     WHERE hop.depth < :max_depth AND e.valid_to IS NULL
)
SELECT DISTINCT h.kind, h.key, h.depth
  FROM hop h WHERE h.depth > 0;
```

实测：5 条边的小图，2 跳扩展 0.0004s。同时暴露一个必须处理的情况——边可以指向不存在的结论（实测中 `proj/lancedb` 有边无行），扩展结果里 `content` 为 NULL。处理方式：扩展候选先 `JOIN conclusions` 过滤；`minir_link` 写入时校验目标 `(kind,key)` 必须存在于 `conclusions`，否则拒绝写入；悬空边由 §4.9 的维护任务清理。

### 4.5 检索融合

`search()` 签名增量：

```python
def search(self, query: str, limit: int = 5, *, session_id: str = "",
           as_of: float | None = None, expand: int = 0,
           include_superseded: bool = False) -> list[dict]:
```

- `as_of=None`：读 `conclusions`（现状）。给了时间：读 `conclusion_versions` 的对应版本行，`limit` 与 scope 规则不变。
- `expand=0`（默认）：不扩展。`expand=1|2`：对 top-5 命中做扩展。
- `include_superseded=False`（默认）：维持现有 active 过滤。
- 页检索走 `minir_kb_search`，返回 `file` / `path` / `sha256` / `lines` / `content`（见 marker-schema §4.5b）；本文的 `search()` 只查结论，两者不共享 `expand`。

融合用 RRF，不做分数归一化（bm25 与跳数是不同量纲）：

```
score(row) = 1/(60 + rank_bm25) + 0.5/(60 + rank_expand) + 0.7/(60 + rank_vec)
```

缺某一路时该项为 0。默认（向量开、`expand=0`）是 bm25 + 向量两路融合；向量降级时退回纯 bm25。
`expand` 的候选上限：深度 ≤ 2、总候选 ≤ 50、每命中扇出 ≤ 10。

### 4.6 向量召回（默认开启）

前置事实（本机实测）：`sqlite-vec` 0.1.9 在 Python 3.12 / SQLite 3.47.1 / Windows 上加载成功，`vec0` 建表、插入、`MATCH` 距离查询正常。向量留在同一个 db 文件里，不需要 LanceDB，也不需要第二个进程。

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS conclusions_vec USING vec0(
    conclusion_id INTEGER PRIMARY KEY,
    embedding FLOAT[1024]
);
```

配置（下表默认值即出厂行为）：

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `memory.minir.vector.enabled` | `true` | 默认开启，与上一版草案相反 |
| `memory.minir.vector.provider` | `siliconflow` | |
| `memory.minir.vector.model` | `BAAI/bge-m3` | 硅基流动支持该模型，最大输入 8192 token（官方文档） |
| `memory.minir.vector.dim` | `1024` | bge-m3 原生维度；硅基流动的 `dimensions` 参数只对 Qwen3 系列生效，bge-m3 不接受该参数，配错维度会让 `vec0` 插入失败 |
| `memory.minir.vector.base_url` | `https://api.siliconflow.com/v1` | 可整体替换成任何 OpenAI 兼容端点 |
| `memory.minir.vector.api_key` | 空 | 空时回落到环境变量 `SILICONFLOW_API_KEY` |

命名空间以现有读取路径为准：`__init__.py:182-185` 读的是 `load_config()["memory"]["minir"]`，
所以向量配置挂在 `memory.minir.vector.*` 下，插件不用新开一条配置读取路径。草案上一版写的 `memory.vector.*` 是错的，已统一。

`api_key` 可配置的代价要说明白：写在 `config.yaml` 里就是明文留在 `HERMES_HOME`，优先级高于环境变量。
默认建议留空走 `SILICONFLOW_API_KEY`（或 gopass），配置项存在的意义是换端点时能一起换 key。

端点默认 `https://api.siliconflow.com/v1/embeddings`（Bearer 认证，官方文档示例）；`base_url` 可整体替换，
拼接规则是 `base_url + "/embeddings"`，所以换成本地 vLLM / LM Studio 时只改配置、不改代码。
key 解析顺序：`memory.minir.vector.api_key` → `SILICONFLOW_API_KEY` → 无 key（走降级）。

默认开启必须一起接受的三个后果：

1. `sqlite-vec` 从可选变成实际必需 —— 写进依赖；启动自检失败时打 warning 并整体降级纯 bm25，不阻塞启动。
2. 每次 `search` 多一次网络 embedding。同一 query 的向量做进程内 LRU 缓存，命中不重复请求。
3. 首次启动与升级要回填：按 `conclusions.id` 游标分批，可中断可续跑，限速可配。

降级：provider 不可达、密钥缺失、HTTP 非 2xx、返回维度与 `dim` 不符 —— 记 warning，本次检索走纯 bm25，不抛错、不写坏 vec 表。

写路径：结论写入并提交后异步补 embedding，不阻塞 `remember` 返回；补失败的留空，由回填任务补齐。

### 4.7 实体合并候选（离线，人工确认）

问题：`key` 与 `subject` 是精确匹配，同一实体的两种写法会变成两行/两页。

三档分带（对 `subject` / 知识页 `topic` 先归一化：小写、去标点、去空白；相似度用 trigram）：

| 相似度 | 动作 | 可逆性 |
| --- | --- | --- |
| ≥ 0.92 | 自动合并 | 保留全部行，改写为 alias |
| 0.85 – 0.92 | 写 `advisories`（`merge_candidate`），走已有 review 流程 | 不改数据 |
| < 0.85 | 忽略 | — |

自动合并只做"重定向"，不删任何行、不合并正文。写入前有一道前置校验：`resolve_alias(dst)` 若指回 `src`，
说明这条边会成环，直接拒绝这次合并。

```python
def merge_into(conn, src: tuple[str, str], dst: tuple[str, str], now: float,
               score: float | None = None) -> None:
    """把 src 折成 dst 的别名。可逆：只改 status、写 same_as 边、闭合 src 的开区间。
    edges.meta 记下合并前的 status，供 minir_unmerge 恢复。"""
    row = conn.execute("SELECT id, status FROM conclusions WHERE kind = ? AND key = ?", src).fetchone()
    if row is None:
        raise ValueError("merge source not found")
    if resolve_alias(conn, dst[0], dst[1]) == src:
        raise ValueError("merge would create a cycle")
    meta = json.dumps({"prev_status": row["status"], "score": score, "merged_at": now})
    conn.execute(
        "INSERT OR IGNORE INTO edges(src_kind, src_key, rel, dst_kind, dst_key, weight, meta,"
        " valid_from, created_at) VALUES (?, ?, 'same_as', ?, ?, ?, ?, ?, ?)",
        (src[0], src[1], dst[0], dst[1], score if score is not None else 1.0, meta, now, now))
    conn.execute("UPDATE conclusions SET status = 'merged', updated_at = ? WHERE id = ?", (now, row["id"]))
    conn.execute(
        "UPDATE conclusion_versions SET valid_to = ?, status = 'merged'"
        " WHERE conclusion_id = ? AND valid_to IS NULL", (now, row["id"]))

def resolve_alias(conn, kind: str, key: str, depth: int = 3) -> tuple[str, str]:
    """沿 same_as 边找到最终代表行；无别名时返回自身。depth 上限用来防环。"""
    for _ in range(depth):
        row = conn.execute(
            "SELECT e.dst_kind, e.dst_key FROM edges e JOIN conclusions c"
            "  ON c.kind = e.src_kind AND c.key = e.src_key"
            " WHERE e.rel = 'same_as' AND e.src_kind = ? AND e.src_key = ?"
            "   AND c.status = 'merged' LIMIT 1", (kind, key)).fetchone()
        if row is None:
            return kind, key
        kind, key = row[0], row[1]
    return kind, key
```

与检索的关系：`status='merged'` 的行本来就被现有 active 过滤挡在默认检索外，所以自动合并会立刻消掉重复命中；
edges 扩展如果走到别名，先用 `resolve_alias` 折到代表行再加分。合并写进 `same_as` 边，反向恢复就是删这条边 + 把 status 改回。

不加新调度器，不在热路径增加开销：候选生成仍在已有的空闲抽取阶段里跑。

### 4.8 remember_mode（remember / forget 对称）

参数：`remember_mode: str = "conclusion"`，取值 `conclusion` / `knowledge` / `card`。两个工具共用同一套取值，
方向相反（记住 / 忘记）；未知取值直接报错，不静默 fallback。

| 模式 | 落点 | 进 conclusions | 进 conclusion_versions | forget 语义 |
| --- | --- | --- | --- | --- |
| `conclusion`（默认） | `conclusions` 行 | 是 | 是（`todo` 除外） | 现有删除路径 |
| `knowledge` | 入队；由 B4 落成 `knowledge/` 下的 markdown | 否 | 否 | 同步归档：移到 `knowledge/_archive/`，front-matter 加 `archived_at`；归档目录不进 kb 检索 |
| `card` | 四份 card 文件之一 | 否 | 否（历史归 `card_revisions`） | 清空对应 card 段，不删文件 |

`conclusion` 模式：`kind ∈ CONCLUSION_KINDS`。`note` 记录后由 b4 异步提取为知识页；b4 自己是写者（§4.10），
用 `minir_kb_write` 落页，不改写 note 行。因此 note 的版本行只在 note 本身更新时追加，不再假设"异步改写会落到同一行"。
结果经 `transform_llm_output_inject` 注入一条提示 AI 忽略的消息。
异步的执行入口按 §4.10 抽成一个方法，窗口级刷新和 `note` / `minir_*` 的异步调用都走它。

`knowledge` 模式：`kind` 用 base 层的取值词汇 —— `ORGS`（`entities`/`trajectories`/`experiences`/`events`/`tools`/`projects`）
作为 org 目录，`CATEGORIES`（`KNOWLEDGE`/`VALUE`/`INTENT`/`METHOD`/`GOVERNANCE`）作为分类（layers.py:32-33）。
分类缺失/非法时：只有明确的方法/步骤类回落 `METHOD`；事实、价值、意图、治理类缺失时写页失败，B4 重试，不静默降级。
不写 `conclusions`；工具只入队，页由 B4 独占写（§4.10），避免一次 remember 写两遍。
`forget` 仍同步归档（页已存在时）；不存在则记一条 pending delete 交给 B4。

`card` 模式：`kind` 用四个 card part 名 —— `user_self_representation` / `user_preferences` / `ai_identity` /
`ai_preference`。映射：`user/identity.md`、`user/preference.md`、`agents/<profile>/identity.md`、
`agents/<profile>/preference.md`。`ai_self_representation` 是 base 层的会话自我状态，不是 card part；
`user_representation`、`user_peer_card`、`ai_identity_card` 是存储侧列名，工具层不接受。
写入必须走 `card_review.write_cards`（或保留其余三份正文的单 part 包装），不直接改文件；
`write_cards` 增加两个参数：`source`（默认 `review`，用户写入传 `operator_edit`）与 `parts`（默认四份；
单 part 写入只渲染与记录该 part，其余三份用当前正文补齐校验）。
用户发起的写入把 front matter 的 `source` 设为 `operator_edit`，`reason` 写进证据与 revision。
`content` 传整段内容，`reason` 单独传，作为 `card_view`（`auxiliary.minir_card_view`，adviser.py:32）审查时的证据。

校验：

| 条件 | 结果 |
| --- | --- |
| `remember_mode` 不在三个取值内 | 报错 |
| `conclusion` 模式且 `kind ∉ CONCLUSION_KINDS` | 报错（现状行为） |
| `knowledge` 模式且 `kind ∉ ORGS` | 报错 |
| `knowledge` 模式且分类缺失/非法 | 方法/步骤类回落 `METHOD`；其余分类缺失写页失败，B4 重试 |
| `card` 模式且 kind 不在四个名称内 | 报错 |
| `card` 模式未走 `write_cards` | 报错；单 part 包装必须保留其余三份正文 |
| `card` 模式且 `reason` 为空 | 报错（无证据的 card 改动不接受） |
| `todo` 出现在非 `conclusion` 模式 | 报错 |

对版本表的影响：三模式里只有 `conclusion` 产生版本行；`knowledge` 的正文历史由页面的 `revisions`/sha 承担（B4 不再改写 note 行），`card` 的历史由 `card_revisions` 承担，本草案不另建。

### 4.9 维护：折叠、归档与悬空边清理

三个 pass 挂在统一调度器的同一个 time task 上（`add_time`，scheduler.py:109-121），
耗时与结果自动进调度账本（`_record`，scheduler.py:236）：

```python
scheduler.add_time("conclusion_history_maintenance", every=6 * 3600,
                   run=self._maintain_conclusion_history, first_delay=60)
```

pass A 折叠（条件：最早版本距今 > 180 天 且 版本数 > 32）。折叠用 Python 而不是纯 SQL：
合并后的 `evidence` 要取折叠区段里最新那版，SQL 的 `MAX()` 是字典序，会给错的证据。

```python
def fold_versions(conn, cid: int, now: float) -> int:
    """把 cid 的历史折到 32 版，返回被折掉的行数。事务与 180 天条件由调用方负责。"""
    rows = conn.execute(
        "SELECT id, content, evidence, version, valid_from, valid_to FROM conclusion_versions"
        " WHERE conclusion_id = ? AND status != 'collapsed' ORDER BY valid_from DESC",
        (cid,)).fetchall()
    if len(rows) <= 32:
        return 0
    old = rows[32:]                     # 按时间倒序，old[0] 是折叠区段里最新的一版
    conn.execute(
        "INSERT INTO conclusion_versions (conclusion_id, kind, key, session_id, subject,"
        " content, evidence, status, version, valid_from, valid_to, recorded_at)"
        " SELECT conclusion_id, kind, key, session_id, subject, ?, ?, 'collapsed', ?, ?, ?, ?"
        "   FROM conclusion_versions WHERE id = ?",
        (" ".join(r["content"] for r in reversed(old)), old[0]["evidence"], old[0]["version"],
         old[-1]["valid_from"], old[0]["valid_to"], now, old[0]["id"]))
    conn.execute(
        "DELETE FROM conclusion_versions WHERE id IN (%s)" % ",".join("?" * len(old)),
        [r["id"] for r in old])
    return len(old)
```

折叠后的行 `valid_to` 必然非 NULL（被折的区段不含当前开区间），所以不会与 `ix_cv_open` 冲突；
`status='collapsed'` 又让下一次折叠跳过它。

pass B 归档（条件：`valid_to < now - 180 天`）。
归档库按日期分文件：`<data dir>/conclusion/archive/conclusion_archive_<YYYYMMDD>.db`
（每次维护写一档；同一天重跑复用同一个文件）。DDL 与热表同构，首列换成 `original_id`，并加 `archived_at`：

```python
ARCHIVE_DDL = """
CREATE TABLE IF NOT EXISTS {a}.conclusion_versions (
    original_id INTEGER PRIMARY KEY, conclusion_id INTEGER NOT NULL,
    kind TEXT NOT NULL, key TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '', content TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active', version INTEGER NOT NULL DEFAULT 0,
    valid_from REAL NOT NULL, valid_to REAL, superseded_by INTEGER,
    recorded_at REAL NOT NULL, archived_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS {a}.ix_arc_asof ON conclusion_versions(valid_from, valid_to);
CREATE INDEX IF NOT EXISTS {a}.ix_arc_concl ON conclusion_versions(conclusion_id, valid_from);
"""

def attach_archives(conn, data_dir: str, today: str, keep: int = 1) -> list[str]:
    """只挂载最近 keep 档归档库（含今天这一档，缺文件就建），按日期倒序；返回别名列表。
    先把已挂载的 arc* 全 detach，保证别名与文件顺序一一对应；不能在事务内调用。"""
    for (name,) in conn.execute(
            "SELECT name FROM pragma_database_list WHERE name LIKE 'arc%'").fetchall():
        conn.execute(f"DETACH DATABASE {name}")
    base = os.path.join(data_dir, "conclusion", "archive")
    os.makedirs(base, exist_ok=True)
    paths = set(glob.glob(os.path.join(base, "conclusion_archive_*.db")))
    paths.add(os.path.join(base, f"conclusion_archive_{today}.db"))
    aliases = []
    for i, path in enumerate(sorted(paths, reverse=True)[:max(1, keep)]):
        alias = f"arc{i}"
        conn.execute(f"ATTACH DATABASE ? AS {alias}", (path,))
        conn.executescript(ARCHIVE_DDL.format(a=alias))
        aliases.append(alias)
    return aliases

def archive_versions(conn, alias: str, cutoff: float, now: float) -> int:
    """把比 cutoff 更早的闭区间版本行搬进归档库；只删 (id, valid_from) 已确认落档的行。"""
    conn.execute(
        f"INSERT OR IGNORE INTO {alias}.conclusion_versions"
        " (original_id, conclusion_id, kind, key, session_id, subject, content, evidence,"
        "  status, version, valid_from, valid_to, superseded_by, recorded_at, archived_at)"
        " SELECT id, conclusion_id, kind, key, session_id, subject, content, evidence,"
        "  status, version, valid_from, valid_to, superseded_by, recorded_at, ?"
        "  FROM main.conclusion_versions WHERE valid_to IS NOT NULL AND valid_to < ?",
        (now, cutoff))
    return conn.execute(
        "DELETE FROM main.conclusion_versions"
        " WHERE valid_to IS NOT NULL AND valid_to < ?"
        f"   AND EXISTS (SELECT 1 FROM {alias}.conclusion_versions a"
        "                WHERE a.original_id = main.conclusion_versions.id"
        "                  AND a.valid_from = main.conclusion_versions.valid_from)",
        (cutoff,)).rowcount
```

表名必须带别名限定：不限定会落到 `main`，而热表同名，`IF NOT EXISTS` 会静默跳过，归档库最后没有表（实测踩过）。

删除条件必须按 `(original_id, valid_from)` 这一对来判，不能只判 id 存在：

- 先 `INSERT OR IGNORE` 再无条件 `DELETE` → 主键冲突时没落档的行被一起删掉。
- 只判 `id IN (SELECT original_id ...)` → 归档里已有同 id 的**另一版**时仍会误删：实测 777 号行
  在归档里存在同 id 不同正文，`INSERT OR IGNORE` 被跳过，但按 id 判存在照样把它从热表删了。
  改成 `(id, valid_from)` 后，冲突行留在热表，干净行正常搬走。
- 搬移整体包在一个 `BEGIN IMMEDIATE` … `COMMIT` 里。

`executescript` 会隐式提交当前事务，所以 `attach_archives` 的 ATTACH + 建表必须发生在 `BEGIN IMMEDIATE` **之前**；
顺序是「挂载 → BEGIN → archive_versions → COMMIT」。首次运行没有归档文件时，`attach_archives` 会把今天这一档建出来，
`archive_versions` 直接写它。

挂载上限固定 1 档（`keep=1`）：比最近一档更早的时间点在 as-of 里查不到，`version_at` 返回 `None`，不抛错。
理由是半年以前的版本很容易过时，为它常驻挂载不划算。那些文件仍在磁盘上（路径可预测），排查时手工 `ATTACH` 即可；
归档文件本身永远不删。

pass C 悬空边清理：

```sql
DELETE FROM edges
 WHERE NOT EXISTS (SELECT 1 FROM conclusions c
                    WHERE c.kind = edges.dst_kind AND c.key = edges.dst_key);
```

as-of 需要跨归档，统一入口（热表优先，再按挂载顺序逐个归档库）：

```python
def version_at(conn, cid: int, t: float) -> dict | None:
    """返回 t 时刻成立的版本行；热表未命中时依次查已挂载的归档库。"""
    sql = ("SELECT content, version, status, valid_from, valid_to FROM {tbl}"
           " WHERE conclusion_id = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
           " ORDER BY valid_from DESC LIMIT 1")
    row = conn.execute(sql.format(tbl="conclusion_versions"), (cid, t, t)).fetchone()
    if row is not None:
        return dict(row)
    for (alias,) in conn.execute(
            "SELECT name FROM pragma_database_list WHERE name LIKE 'arc%' ORDER BY name"):
        row = conn.execute(sql.format(tbl=f"{alias}.conclusion_versions"), (cid, t, t)).fetchone()
        if row is not None:
            return dict(row)
    return None
```

归档库别名来自 `pragma_database_list`，不是外部输入，拼进 SQL 不构成注入面。

命中 `status='collapsed'` 的行时返回整段区间的合并正文，不假装能还原到具体某天（已定，可接受）。

幂等：pass A 条件排除 `status='collapsed'`；pass B 靠 `original_id` 唯一键 + 只删已落档行；
三个 pass 可重入，中断后重跑不产生重复、不丢行。

kb 检索边界：`knowledge/_archive/` 不纳入 `minir_kb_search` 的检索范围 —— 归档页不该继续污染召回。

### 4.10 b4 值标记提取的复用

现状：`b4_value_markers` 只是 base 刷新的窗口级子趟（worker.py:958-964），`note` 的写入点在 `__init__.py:987-996`，
两者没有共用入口。

改法：从 worker 里抽出

```python
def run_value_markers(self, session_id: str, subject_ids: list[int],
                      *, source: str) -> list[dict]:
    """给定若干结论或入队单元，产出价值标记、写页并返回 page_writes；调用方只负责入队。"""
```

调用点三处，行为一致：

1. base 刷新窗口级（现状路径改成调用它）；
2. `minir_remember` 写 `note` 之后的异步任务；
3. B4 队列消费：`remember_mode="knowledge"` 的工具只入队，单元由 B4 取到后调用它写页。

约束：

- 方法内部不 commit，事务归调用方；这样 `note` 路径能把「写结论行 + 追加版本行 + 排队异步」放在一个事务里。
- 自带幂等键（`source` + `session_id` + 页 path），重复触发不重复写页；窗口级与 note 级命中同一 path 时按 sha 判 unchanged。
- 结果注入仍走 `transform_llm_output_inject`（__init__.py:125），消息文本带"提示 AI 忽略"的标记。

### 4.11 调度器的执行历史表

现状：`Scheduler._record`（scheduler.py:236-244）只把 `runs` / `failures` / `last_*` 记在内存的 `Task` 对象上，
进程重启即失，`snapshot()`（scheduler.py:248-251）看到的也只是最后一条。

```sql
CREATE TABLE IF NOT EXISTS scheduler_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL,
    trigger TEXT NOT NULL,          -- turn | time
    outcome TEXT NOT NULL,          -- ok | error | retry | skip
    detail TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    finished_at REAL NOT NULL,
    ms REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sched_task ON scheduler_runs(task, id DESC);
CREATE INDEX IF NOT EXISTS ix_sched_finished ON scheduler_runs(finished_at);
```

注入点：`Scheduler(record=store.record_scheduler_run)`，`_record` 末尾在锁**外**调用一次。
写历史失败只记 warning —— 观测层不能把调度拖死。

保留：每个 task 最近 200 行 + 全局 90 天，清理语句挂在 §4.9 的同一个维护任务里：

```sql
DELETE FROM scheduler_runs WHERE finished_at < :cutoff;
DELETE FROM scheduler_runs WHERE id IN (
    SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY task ORDER BY id DESC) AS rn
                      FROM scheduler_runs) WHERE rn > 200);
```

`minir_stats` 改成从这张表读"最近 N 次执行"，不再只靠内存快照。

### 4.12 换向量维度：后台迁移任务

维度写在配置里（`memory.minir.vector.dim`），但 `vec0` 的维度是建表时钉死的，换模型/换维度必须重建。
做法是把迁移做成一个调度任务，不在启动路径上阻塞：

```sql
CREATE TABLE IF NOT EXISTS vector_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- key: dim | model | provider | updated_at | progress_id
```

```python
scheduler.add_time("vector_dim_migration", every=3600,
                   run=self._migrate_vector_dim, due=self._vector_dim_due, first_delay=0)

def _vector_dim_due(self) -> bool:
    """元数据里的维度与配置不一致（或 vec 表还不存在）时才跑。"""
    row = self._conn.execute("SELECT value FROM vector_meta WHERE key = 'dim'").fetchone()
    return row is None or int(row[0]) != self._settings.dim
```

迁移步骤（每一步都可中断，重复执行安全）：

1. `DROP TABLE IF EXISTS conclusions_vec`，按新维度重建；
2. `vector_meta.progress_id` 归零，按 `conclusions.id` 游标分批重嵌，每批提交后写回游标；
3. 全部完成后写 `vector_meta.dim = 新维度`、`model`、`provider`，清 `progress_id`。

迁移期间向量路不可用 → `search` 走 §4.6 的降级路径（纯 bm25），不报错、不返回半套结果。
任务失败时游标保留，下一个 tick 续跑；调度器单线程执行，不会有两个迁移同时在跑。

### 4.13 `minir_unmerge`：撤销一次合并

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `minir_unmerge` | `kind`、`key`、`reason`（必填） | 恢复合并前的 status、追加开区间版本行、删除 `same_as` 边 |

```python
def unmerge(conn, kind: str, key: str, reason: str, now: float) -> dict:
    """撤销一次 same_as 合并。合并期间的历史保留，时间线不倒流。"""
    if not (reason or "").strip():
        raise ValueError("unmerge 需要 reason 作为证据")
    edge = conn.execute(
        "SELECT meta, dst_kind, dst_key FROM edges"
        " WHERE rel = 'same_as' AND src_kind = ? AND src_key = ?", (kind, key)).fetchone()
    row = conn.execute("SELECT id, status, content, evidence FROM conclusions"
                       " WHERE kind = ? AND key = ?", (kind, key)).fetchone()
    if edge is None or row is None or row["status"] != "merged":
        return {"unmerged": False, "reason": "not_merged"}
    prev = json.loads(edge["meta"] or "{}").get("prev_status") or "active"
    conn.execute("UPDATE conclusions SET status = ?, updated_at = ? WHERE id = ?", (prev, now, row["id"]))
    conn.execute(
        "INSERT INTO conclusion_versions (conclusion_id, kind, key, session_id, subject, content,"
        " evidence, status, version, valid_from, recorded_at)"
        " SELECT id, kind, key, session_id, subject, ?, ?, ?,"
        "        (SELECT COALESCE(MAX(version), 0) + 1 FROM conclusion_versions WHERE conclusion_id = ?),"
        "        ?, ?"
        "   FROM conclusions WHERE id = ?",
        (row["content"], row["evidence"], prev, row["id"], now, now, row["id"]))
    conn.execute("DELETE FROM edges WHERE rel = 'same_as' AND src_kind = ? AND src_key = ?", (kind, key))
    return {"unmerged": True, "restored_status": prev, "into": [edge["dst_kind"], edge["dst_key"]]}
```

语义要点：

- 幂等：已经不是 merged 时返回 `{"unmerged": false}`，不报错。
- 版本行只追加不重开：合并那段时间在 as-of 里仍然是 `merged`，恢复从 `now` 开始算。
- 需要 `reason`，与 card 模式同一套证据要求 —— 撤销也是改写事实，得留下理由。
- 只管 conclusion 行；知识页（`remember_mode="knowledge"`）的合并仍是人工，没有对应工具。

## 5. 迁移与回填

`_migrate_once()` 尾部追加新的一段（现有门控机制不变），`user_version` 4 → 5：

1. `CREATE TABLE IF NOT EXISTS` + 三个索引（幂等）。
2. 回填：每个 `kind ∈ CONCLUSION_KINDS` 且 `kind != "todo"` 的 `conclusions` 行各写一条开区间版本行，
   `valid_from = created_at`、`valid_to = NULL`，条件 `NOT EXISTS (SELECT 1 FROM conclusion_versions WHERE conclusion_id = c.id)`。
3. 不删除、不改写任何 `conclusions` 行。

失败恢复：第 1、2 步都可重复执行；若第 2 步中断，重跑只补缺失的行。回滚 = 先 `PRAGMA user_version = 4`，再 `DROP TABLE conclusion_versions`（`edges` 独立，不依赖版本表）。

## 6. 测试清单

| 用例 | 断言 |
| --- | --- |
| 迁移幂等 | 连续两次打开同一库，版本行数不变 |
| 老库升级 | `user_version=4` 的库升到 5 后，每条 eligible 结论恰好一条开区间行 |
| as-of 边界 | 三个时间点取到三个不同版本；`valid_from` 与 `valid_to` 端点归属明确 |
| 默认行为不变 | 不传新参数时，`search` 结果与改动前的 golden 输出逐条一致 |
| superseded | `status` 改为 superseded 后，默认搜索不命中，`as_of` 命中 |
| 版本范围 | `todo` 写入不产生版本行 |
| 边唯一 | 重复插入同一 `(src,rel,dst)` 走 upsert，不产生第二行 |
| 扩展上限 | 深度 2、扇出上限 10、候选上限 50 均生效 |
| 悬空边 | 指向缺失结论的边不进结果 |
| 向量关闭 | 未启用时 `conclusions_vec` 不存在，`search` 行为与本改动前一致 |
| `remember_mode` 路由 | `conclusion` 写 conclusions；`knowledge` 不产生 conclusions 行；`card` 只走 write_cards，不产生 conclusions 行 |
| 未知 mode | 三个工具调用点都报错，不静默降级 |
| `note` 异步 | b4 提取为页后 note 行不被改写；页的历史由 revisions/sha 承担，note 的版本行不变 |
| card 缺 reason | `card` 模式拒绝写入 |
| 折叠阈值 | 179 天 + 40 版不折叠；181 天 + 40 版折成 33 行（32 + 1 条 collapsed） |
| 折叠幂等 | 连续两次维护，`collapsed` 行数不变 |
| 归档搬移 | 热表无残留，归档库按 `original_id` 不重复；中断后重跑补齐 |
| as-of 跨归档 | `t` 落在归档窗口时能从归档库取到版本行 |
| forget 闭合 | forget 后开区间为 0，as-of 仍能命中 `status='deleted'` 的行 |
| `minir_link` 校验 | 目标 `(kind,key)` 不存在时拒绝写入 |
| 向量默认开 | 新库启动即建 `conclusions_vec`；缺省解析为 siliconflow / BAAI/bge-m3 / 1024 |
| 向量降级 | provider 401、超时、非 2xx → 本次检索纯 bm25，只记一条 warning |
| 维度不符 | 返回 768 维时拒绝写入并降级，`vec0` 表不被污染 |
| 向量配置可替换 | 改 `base_url` + `api_key` 即换端点；`api_key` 为空时回落 `SILICONFLOW_API_KEY` |
| 多归档 as-of | `keep=1`：最近一档内的时间点命中，更早的返回 None |
| 归档冲突不丢行 | 人为制造 `original_id` 冲突，热表对应行仍在 |
| kb 归档排除 | `knowledge/_archive/` 下的页面不出现在 `minir_kb_search` 结果里 |
| 自动合并高分段 | 相似度 ≥0.92：src 变 `merged`、写 `same_as` 边、开区间闭合；默认检索不再返回 src |
| 低分段走人工 | 0.85–0.92 只写 `advisories`，不改任何 status |
| 别名链 | a→b→c 解析到 c；环由 depth 上限终止，不挂死 |
| 合并拒绝成环 | `resolve_alias(dst)` 指回 src 时抛错，不写边 |
| 合并写入 meta | `merge_into` 后 `edges.meta` 含 `prev_status`、`score`、`merged_at` |
| unmerge 恢复 | 原 status 回来、`same_as` 边被删、追加一条开区间版本行 |
| unmerge 幂等 | 第二次调用返回 `{"unmerged": false}`，不动数据 |
| unmerge 缺 reason | 报错，数据不变 |
| 调度历史落库 | 每次执行写入 `scheduler_runs`；写失败只 warning，任务照常完成 |
| 历史保留 | 每 task 200 行 + 90 天；超期行被清 |
| 维度迁移 | dim 变更触发任务；迁移期间检索走 bm25；中断后从游标续跑 |

## 7. 为什么不用嵌入式图数据库（选型证据）

| 候选 | 状态 | 判定 |
| --- | --- | --- |
| kuzu 0.11.3 | 仓库 `archived: true`，最后推送 2025-10-10 | 不可选，项目已归档 |
| cozo-embedded 0.7.6 | 最后 release 2023-12，仓库最后推送 2024-12 | 不可选，停滞 |
| duckdb + DuckPGQ | 复测（duckdb 2.0.0 / Windows AMD64 / pip）：`INSTALL duckpgq FROM community` + `LOAD` + `SET active_grammar_extensions = ['duckpgq']` 后，`CREATE PROPERTY GRAPH`、`GRAPH_TABLE ... MATCH`、`ANY SHORTEST`、`pagerank` 全部执行成功；property graph 落盘后跨连接仍在。早前记录的 2.0.0 Parser Error 是漏了 `active_grammar_extensions`（2.0 起扩展语法需显式 opt-in） | 可用，但不作主存储：同一张 20k 边图，2 跳 MATCH 中位 16.8ms，SQLite 递归 CTE 0.148ms；且跨库拿不到 `conclusion_versions` 的同一事务 |
| pyoxigraph 0.5.13 | 活跃，MIT OR Apache-2.0，win_amd64 wheel 可用 | 可用，但不适合做主存储：20,000 个四元组占 21MB，跨库无原子事务 |

pyoxigraph 实测数据（如需 RDF/SPARQL 语义时的备选，本草案不采用）：内存库 2,000 条边摄入 0.004s；2 跳 SPARQL 0.0004s；落盘库 20,000 条边摄入 0.23s；从盘重建到内存 0.10s。

DuckPGQ 的其余实测（本机、duckdb 2.0.0、20k 边 / 5k 点）：冷 `INSTALL` 1.72s、暖 `LOAD` 44ms、
`CREATE PROPERTY GRAPH` 37ms；用 `sqlite_scanner` 只读挂载同一份 SQLite 文件后建 property graph，
`pagerank` 19ms、`weakly_connected_component` 19ms。它的位置因此是**离线图算法**（连通分量、中心性、
聚类系数），不是检索热路径：那些算法与本仓 1–2 跳邻域无关，而它和 SQLite 不在同一个事务里。

结论：本仓的图查询是 1–2 跳邻域，SQLite 递归 CTE 在 0.4ms 量级完成，且能和 `conclusion_versions` 放在同一个事务里——这是任何外部图库都给不了的。

## 8. 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| 版本表写放大 | `CONCLUSION_KINDS` 范围 + 180 天自动折叠 + 超龄行归档 |
| 折叠误伤 | 折叠条件排除 `status='collapsed'`，且要求最早版本 > 180 天 |
| 归档搬移中断 | `ATTACH` + 单事务；只删已落档行，`original_id` 冲突不丢历史 |
| 向量默认开引入外部依赖 | 启动自检 + 失败即降级 bm25；query 向量 LRU 缓存 |
| 每次 search 一次 embed 延迟 | LRU + 可配 timeout；失败不无限重试 |
| 归档文件增多 | as-of 只挂最近 1 档；更早的按需手工 ATTACH |
| 自动合并误判 | 阈值 0.92 + 只做重定向（不删行、不并正文）+ 记录 `same_as` 边，删边即可回滚 |
| 维度迁移期间检索退化 | 走 bm25 降级；任务失败从游标续跑 |
| 调度历史表膨胀 | 每 task 200 行 + 90 天，随维护任务清理 |
| 迁移中断留下半套表 | 建表用 `IF NOT EXISTS`，回填按 `NOT EXISTS` 补行 |
| 老库上 `user_version` 已被前移 | 迁移段自身幂等，重跑安全 |
| search 变慢 | 扩展只在 `expand > 0` 时跑；默认路径多一次 `kind` 判断 |
| 悬空边累积 | 维护任务清理；扩展结果先 JOIN conclusions |
| embedding 调用失败 | 降级纯 bm25，记 warning，不抛错 |

回滚：删三张表即可回到现状（`conclusion_versions`、`edges`、`conclusions_vec`），`conclusions` 本身不被改写。

## 9. 已定与未决

本轮已定（来自对上一版 7 个问题的回答）：

| # | 问题 | 决定 | 落点 |
| --- | --- | --- | --- |
| 1 | forget 时版本行怎么处理 | 闭合开区间 + 保留行，`status='deleted'` | §4.1 |
| 2 | 悬空边 | 清理；`minir_link` 写入时校验目标存在 | §4.4、§4.9 pass C |
| 3 | `evidence` 体积 | 保留全文；超过 180 天的版本行移到磁盘归档 | §4.9 pass B |
| 4 | 折叠是否自动 | 自动：180 天 + 版本数 > 32，由统一调度器跑 | §4.9 pass A |
| 5 | card 四名与代码不一致 | 四个 card part：`user_self_representation` / `user_preferences` / `ai_identity` / `ai_preference`；写入走 `write_cards`，用户发起 `source=operator_edit` | §4.8 |
| 6 | knowledge 模式 forget | 归档，不删除 | §4.8、§4.9 |
| 7 | `note` → b4 触发 | 抽出 `run_value_markers()`，窗口级与异步共用 | §4.10 |
| 8 | 向量召回默认开关 | 默认开；provider/model/dim 三个默认值定死 | §4.6 |
| 9 | 归档文件位置 | `<data dir>/conclusion/archive/conclusion_archive_<YYYYMMDD>.db` | §4.9 pass B |
| 10 | `knowledge/_archive/` 是否进 kb 检索 | 不进 | §4.9 |
| 11 | collapsed 行返回合并正文 | 可接受 | §4.9 |
| 12 | 维护节奏 | 固定数值，不做自适应 | §4.9 |
| 13 | 实体合并是否自动 | 分带：≥0.92 自动（重定向、可逆），0.85–0.92 人工，<0.85 忽略 | §4.7 |
| 14 | embedding 密钥键名 | 环境变量 `SILICONFLOW_API_KEY`（全大写） | §4.6 |
| 15 | 换向量维度 | 注册后台调度任务，按游标重嵌，中断续跑 | §4.12 |
| 16 | 调度器要记录表 | 新增 `scheduler_runs`，注入点 `Scheduler(record=...)` | §4.11 |
| 17 | 归档挂载上限 | 1 档；更早的按需手工 ATTACH | §4.9 |
| 18 | base_url / api_key | 都在 `memory.minir.vector.*` 下可替换；key 空时回落 `SILICONFLOW_API_KEY` | §4.6 |
| 19 | 撤销合并 | 新增 `minir_unmerge(kind, key, reason)`，合并前 status 存 `edges.meta` | §4.13 |

2026-10-25 追加（与规则文件对齐）：

| # | 问题 | 决定 | 落点 |
| --- | --- | --- | --- |
| 20 | 单元 identity | path；`(category, org, topic)` 是元数据；同话题不同类别是两张页 | §2、`knowledge.py` |
| 21 | 正文形态 | 要点式 3..8 行 / 120..1200 字符，禁止散文；超过就拆单元 | `marker-schema` §1.1 |
| 22 | 边与文件的关系 | 文件是正文真源，`edges` 是从 front-matter 派生的索引，可重建 | §4.4 |
| 23 | events 定义 | 工作记录：动作 + 日期 + 对象 + 结果，不要求按日期检索 | `rules/markers-org.md` v5 |
| 24 | 类别与 epistemic | 五类；epistemic 只挂 KNOWLEDGE，取 fact/inference/hypothesis/prediction | `marker-schema` §1.1 |

仍未决：

1. 合并分带的 0.92 / 0.85 是起始值，需要拿真实 `subject` 分布校准一次。
2. `unmerge` 之后可能留下失效的 `same_as` 边（链式合并时），是否要清理任务，还是靠 `resolve_alias` 的 status 判定就够。
3. `scheduler_runs` 的 200 行 / 90 天是否够用，要看维护任务实际触发频率。
4. 合并键换成 path 之后，`(category, org, topic)` 只做元数据；存量页同 topic 的多个类别仍是多张页，需要一次只读统计核对，不自动合并。

## 校准记录（2026-10-25）

- B4 是唯一知识写者：结论稿改为与 marker 稿一致，B4 自己调 `minir_kb_write` 落页，返回 `page_writes`；不再委托第二个知识 agent。
- card part 名统一为 `user_self_representation` / `user_preferences` / `ai_identity` / `ai_preference`；`ai_self_representation` 只指 base 层会话状态。代码侧把 `user_identity` → `user_self_representation`、`user_preference` → `user_preferences`，AI 两名不变；同步 `PART_NAME`/`PART_SCOPE`/`card_paths`、`CARD_SCOPE_RULES` 的键、`card_revisions.part` 的存量值（迁移或读时映射）与测试。
- 实现落点：`card_review.py` 的 `USER_SELF`/`USER_PREFERENCE` 值、`PARTS`/`PART_NAME`/`PART_SCOPE`/`PART_CARD`/`card_paths`/`CARD_SCOPE_RULES`；`write_cards` 增加 `source`/`parts`；`card_revisions.part` 迁移；`tests/test_card_review.py` 的 part 名断言。`__init__.py` 的常量名不变，只跟值走。
- card 模式必须走 `write_cards`；用户发起写入 `source=operator_edit` 并写 `reason`。
- 知识单元身份改为 path；`(category, org, topic)` 是元数据。页参与的边用 `('page', path)`。
- 关系词表扩为 8 种：`layers.RELATION_TYPES` + `rules/markers-org.md` v5 加 `derived_from`、`same_as`。
- `knowledge` 模式的工具只入队，页由 B4 独占写；`forget` 同步归档。
- 合并候选复用 `advisories` 时带 `source='merge'`，与顾问建议分开统计。
- 术语表补 claim / unit / conclusion / marker。
- 分类缺失：方法/步骤类回落 `METHOD`，其余失败重试。

## 实现缺口（2026-10-25 复核）

判定：骨架落地，但不是严格实现。检索、写入、保留期与迁移都优先保持旧行为，未决的边、向量、维度迁移按最小改动处理；§6 的 38 条用例多数没有对应测试。

向后兼容仍是默认
- 向量无 key 时 `embed` 直接返回 None，不写日志（vector.py:120-122）；`sqlite_vec` 不可用时只有一条启动 warning。检索静默退回 bm25，与 §4.6“失败留 warning”相反。
- 维护任务只在 `_reasoning` 为真时注册（__init__.py:476-480）；reasoning 关闭的 profile 不折叠、不归档、不清悬空边、不裁剪 scheduler_runs。
- 写入路径不索引：`index()` 只被 `backfill()` 调用（vector.py:183-201），新结论要等每小时一次、每次 50 条的回填；稿要求写入提交后异步补。
- as-of 只在结论仍存在时可查：`_search_as_of` JOIN `conclusions`（store.py:2902-2908），`forget_conclusion` 物理 DELETE 行（1029-1034）；删除前的版本行留在 `conclusion_versions`，但模型侧查不到，`version_at` 没有生产调用者。
- 自动合并已实现，但候选只比较 `conclusions.subject`，知识页 topic 不参与；相似度用 Dice 而非稿的阈值语义。

未决项按最小改动
- edges 只有人工 `minir_link` 与 `merge_into` 的 `same_as`（store.py:1173-1179，1223-1229）；§4.4 的 base subject/topic 与知识页 front matter 两类派生没有代码，`expand` 在真实库里无邻居可扩。
- `scheduler_runs` 的保留期复用 `retain_days=180`（store.py:1411），稿要求 90 天；每 task 200 行的窗口裁剪已做。
- 维度不符在 `ensure_table` 内直接 DROP 重建（vector.py:80-81，100-108），没有 §4.12 的 `vector_dim_migration`、游标续跑与 progress_id。
- RRF 没有按稿实现：现有分数是词面重合加 kind 权重（store.py:2880-2892），向量项只是 `+0.7/(60+rank)`（__init__.py:1984），没有 bm25 的 `1/(60+rank)`，expand 用 `score*0.5` 而不是 rank。
- §4.10 的 `run_value_markers` 不存在（全包 0 命中），note 抽取仍走既有写页路径。

未实现或与稿冲突
- `include_superseded` 没有任何测试；迁移 4→5 回填、forget 后的 as-of、维度迁移、多归档 as-of 都没有回归用例。
- 版本号未升：schema 已到 5，`plugin.yaml` 与 `pyproject.toml` 仍是 0.17.0。

运行时状态：live DB 仍是 v4，迁移未执行；这是状态，不改变上述判定。
