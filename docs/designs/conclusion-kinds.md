# CONCLUSION_KINDS 重定义

状态：已落地（2026-10-28）。

落地记录：
- 迁移在代码里是 schema 版本 7：`_SCHEMA_VERSION` 在本设计写就时已经是 6（v5→v6 的注释在
  `store.py`），只有写成 7 才会对现有库运行。语义与下表一致。
- `_RETIRED_SPECS` 中 `session_chunk_summary` 与 `handoff` 的权重记为 `None`：两者从无行、
  也从未出现在权重表里，写入 0.5 会让权重表变成 18 项，与 T2 的 16 项冲突。
- 验收测试：`tests/test_conclusion_kinds.py`（T1-T10）。

七条硬约束：

1. 词表里没有标记（`MARKER_KINDS` 的 5 项）。
2. 不设"工具能写什么"的词表，`TOOL_KINDS` 整个概念删除。
3. 没有 `note`，也没有兜底种类：每个 kind 都有明确的生产者，未登记就写不进来。
4. `minir_remember` 与 `minir_forget` 取消 `conclusion` 模式。
5. `minir_search` 的 `type` 过滤枚举 = 新的 `CONCLUSION_KINDS`。
6. `base.window_summary` 与 `window_summary` 合并，统一取 `window_summary`。
7. `minir_search` 只保留 FTS 检索，向量子系统删除。

## 1. 定义

```
CONCLUSION_KINDS (9)
  todo                          会话任务清单
  base.user_representation      用户自表示卡（持久）
  base.user_peer_card           用户同侪卡（持久）
  base.ai_identity_card         AI 身份卡（agent 作用域，持久）
  base.ai_self_representation   AI 自表示（会话，绑定）
  base.value_markers            base 产物里的值标记快照（会话，绑定）
  dialectic.review              辩证层修订记录（会话）
  session_summary               一次压缩的会话摘要（会话）
  window_summary                窗口的规范 chunk 摘要（会话，含被取代的行）
```

现库分布（只读 memory.db，2026-10-28）：9 个结论 kind 共 433 行；`window_summary` 121 行。另有待迁移的 18 行 `base.window_summary`（§2）、退役的 12 行 `note`（§6）与 6 行标记（§3）。

| kind | 行数 | 带 session |
|---|---:|---:|
| window_summary | 121 | 121 |
| base.ai_self_representation | 79 | 79 |
| base.value_markers | 79 | 79 |
| base.user_representation | 64 | 61 |
| dialectic.review | 64 | 64 |
| session_summary | 16 | 16 |
| todo | 5 | 5 |
| base.user_peer_card | 3 | 0 |
| base.ai_identity_card | 2 | 0 |

## 2. 合并 base.window_summary

base 层的文档写清了主从关系："The artifact's `## Window Summary` becomes the newest chunk projection: older chunks stay as independent records"（worker.py:1361-1366）。chunk 行（kind `window_summary`）是窗口摘要的规范载体，`publish_base_conclusions` 又把同一份文本发布成 `base.window_summary`，是第二份。

数据核对（18 行 `base.window_summary`）：

| 情况 | 行数 | 处理 |
|---|---:|---|
| 同窗口已有 chunk，正文逐字相同 | 16 | 删除 base 行 |
| 同窗口没有 chunk（base id 707，窗口 0.013 秒） | 1 | 转成 chunk 行，key 用该窗口的 `window_from_ts` |
| 找不到对应的 `base_refreshes` 行（base id 678，正文 1170 字符） | 1 | 留在退役登记，只读 |

合并后的规则：

- `_BASE_CONCLUSION_KINDS` 由 6 项变 5 项（`window_summary` 条目删除），`publish_base_conclusions` 每会话写 5 行而不是 6 行。
- `base_refreshes.window_summary` 列保留：下一次刷新要用它做 `previous_summary`（worker.py:1394）。
- 修正路径不变：辩证层改写 window_summary 段时，用同一个 key 就地更新 chunk 行（worker.py:1566-1573）。
- `type="base.window_summary"` 不再合法；`rules/advice_target.md:55` 的 `missing_work` 目标改为"该会话最新的 `window_summary` 行"。
- `window_summary` 权重取 1.0（继承被删 base 行的角色，见 r3）。

## 3. 工具面只剩两个目的地

| 工具 | 入口 | `type` 的含义 | 写入 |
|---|---|---|---|
| minir_remember | `remember_mode=knowledge` | org 名 | 知识页单元（进 B4 writer） |
| minir_remember | `remember_mode=card` | 卡片 part 名 | `card_review.write_cards` |
| minir_forget | `remember_mode=knowledge` | 不参与（用 path） | 归档页面 |
| minir_forget | `remember_mode=card` | 卡片 part 名 | 清空该 part |
| minir_search | `type` 可选 | `CONCLUSION_KINDS` 的 9 项 | 只读，纯 FTS |
| minir_todo | `todos` | 固定 `todo` | 唯一的工具侧结论写入 |

- `remember_mode` 必填，枚举 `["knowledge", "card"]`，默认值 `conclusion` 取消。
- `minir_forget` 的 `conclusions_id` 随 conclusion 分支一起删除；`required` 由 `type` 改为按模式校验。
- 没有"传个 kind 先存着"的路径。

后果：

- 模型不再写任何结论行，也不再写任何标记行（r0 已定：不新增 `marker` 模式）。标记 5 个 kind 只剩 6 行历史，读路径与 `base.value_markers` 快照照常工作；标记词表保留。
- 12 行 `note` 退役为只读；宿主记忆镜像 `on_memory_write`（__init__.py:1151-1171）是它唯一的自动写入者，随之删除。代价：宿主 memory 工具的条目不再出现在 `minir_search` 里，宿主自己的存储不受影响。`newest_conclusion_by_content`（store.py:1075）只服务这个镜像，随之成为孤儿。
- 记忆写入的实际去向只剩两个：知识页与卡片。

## 4. 检索通道：只保留 FTS

`minir_search` 只剩词面一条通道（store.py:3079-3190）：

- 候选：`_terms` 产出 ≥3 字符的 term（store.py:608），FTS5 trigram、`"term"` OR、bm25 排序、上限 `_MAX_SCAN=200`；FTS5 不可用或 term 全短于 3 字符时落到 LIKE 扫描兜底（按 `updated_at` 倒序，上限 200）。
- 评分：`overlap + _KIND_WEIGHT[kind]`，排序 `(score, -rank, when, id)`，`overlap=0` 丢弃。
- 扩展：`expand>0` 的邻居按 `expand_rank` 追加在直接命中之后；`limit` 截到尾部时会丢，expand 只在直接命中不足 limit 时可见。
- 删除：`VectorIndex.recall` 调用（__init__.py:2105-2112）、`vector_ranks` 参数（store.py:3084）、`_fuse` 与 RRF（store.py:3192-3240）、tests/test_store.py:1102 与 1116。
- 返回值：`score` 从 RRF 分变为词面分；`rank` 语义不变。
- 后果：向量通道绕过 `scope`/`kinds` 的泄漏随之消失（探针已证：`scope="session"` 能带回别的会话的 `session_summary`）；代价是没有语义召回。

LIKE 兜底算词面通道的一部分，保留：中文两字查询靠它（trigram 需要 3 字符）。

向量子系统整体删除（r5 已定）：`vector.py`、`__init__` 的接线（79、425-428、514-517、1691、1986）、`plugin.yaml:18` 注释、`scheduler.py` 的向量任务（235 附近的降级判定）、`vector_meta` 表（store.py:411，v6 迁移里 DROP）、`tests/test_vector.py` 与 `test_scheduler.py` 里的向量用例。embedding 写入与 backfill 一并停止，`minir_stats` 不再报向量计数。

## 5. 注册表（9 行，唯一真相）

```
_CONCLUSION_SPECS: dict[str, _Spec]   # 行序即词表序
_Spec = (meaning, writer, recall, bound, weight, live_when_superseded)
writer ∈ {"operator", "layer"}
recall ∈ {"cross_session", "session"}   # 决定 durable 谓词
bound  : 是否受会话绑定约束（search 的 scope=all 剔除 + SessionScopeError）
```

| kind | writer | recall | bound | weight | 已取代仍可检索 | 写入点 |
|---|---|:--:|:--:|---:|:--:|---|
| todo | operator | session | 否 | 1.2 | | minir_todo __init__.py:1007 |
| base.user_representation | layer | cross_session | 否 | 1.5 | | publish_base_conclusions store.py:2411 |
| base.user_peer_card | layer | cross_session | 否 | 1.5 | | 同上 |
| base.ai_identity_card | layer | cross_session | 否 | 1.4 | | 同上 |
| base.ai_self_representation | layer | session | 是 | 1.4 | | 同上 |
| base.value_markers | layer | session | 是 | 1.0 | | 同上 |
| dialectic.review | layer | session | 否 | 1.4 | | _split_sections 发布 store.py:1023、2197 |
| session_summary | layer | session | 否 | 0.8 | | 压缩 context_engine.py:281；收尾 __init__.py:1522 |
| window_summary | layer | session | 否 | 1.0 | 是 | add_chunk_summary store.py:1729；worker.py:1414、1569 |

`bound` 与 `recall` 分开的原因（r1 已定）：会话绑定保持今天的行为，只有两个 base 会话 kind 受约束；`dialectic.review`、`session_summary`、`window_summary`、`todo` 仍然在 `scope="all"` 下可跨会话读到。这是已知并接受的状态，不是本设计要改的东西。

结论不入版本表：今天 `_versioned` 为真的 6 项里结论只有 `note`，它退役了。

## 6. 退役登记（4 项，只读）

```
_RETIRED_SPECS: dict[str, _Retired]   # 不可写；只为读路径与迁移存在
_Retired = (recall, weight)
```

| kind | recall | weight | 现状 |
|---|---|:--:|---|
| note | cross_session | 0.7 | 12 行（5 持久 / 7 会话），可检索，不再新增 |
| base.window_summary | session | 1.0 | 合并后剩 1 行孤儿（id 678），正文无法对应任何窗口 |
| session_chunk_summary | session | 0.5 | v5 迁移已改名为 window_summary，0 行 |
| handoff | session | 0.5 | 过期清理后 0 行 |

退役不是"降级种类"：没有生产者、不进任何工具入参、不进词表语义；它是历史行的只读登记，让旧数据以今天的可见性和权重继续可读。

## 7. 派生（取代六处手写清单）

| 规则 | 今天 | 新来源 |
|---|---|---|
| 搜索 `type` 过滤 | 7 项（`TOOL_KINDS`，tools.py:97、__init__.py:1817） | `CONCLUSION_KINDS` 9 项 |
| durable 谓词 | 3 卡 ∪ 7 工具 kind（store.py:3072） | `recall == "cross_session"` 的 3 项 + 标记 5 + 退役 note 1 = 9 |
| 会话绑定 | 3 项（549-550，派生自 base 列 scope） | `bound` 字段的 2 项（合并掉 base.window_summary 后少一项） |
| 检索权重 | 16 行字面表（516-533） | 结论 9 + 标记 5 + 退役 2 = 16；`window_summary` 由 0.6 改 1.0，是唯一数值变化 |
| 版本化 | 6 项（97-99；迁移回填 794-803 另写一遍） | 标记 5 项；结论不参与 |
| 已取代仍可检索 | SQL 硬编码 kind（3139） | `live_when_superseded` 的 1 项 |
| `_LONG_TERM_KINDS` | 3 项（491-495），唯一用途是被 3072 拼进 durable | 删除 |
| `TOOL_KINDS` | 7 项（94），跨两套词表 | 删除，不设替代 |
| base 发布 | 6 行/会话（536-545） | 5 行/会话 |
| 向量通道 | `_fuse` 三通道 RRF | 删除，embedding 与索引一并停 |

`_BASE_CONCLUSION_KINDS`（536-545）保留为"列 → kind → key scope"的发布映射；bound 不再从它的 scope 派生。

## 8. 与今天的行为差异（逐条）

1. 检索只剩 FTS：向量与 RRF 删除；`score` 变词面分；`expand` 邻居降级为尾部追加；embedding 不再写。
2. `minir_search` 的 `type`：7 项变 9 项结论。`type="entities"`、`"note"`、`"base.window_summary"` 被拒；不带 `type` 的查询仍返回标记行与退役行。
3. `minir_remember` / `minir_forget`：没有 `conclusion` 模式，`remember_mode` 必填；`conclusions_id` 删除。
4. 标记无写入口（r0），6 行历史只读。
5. 12 行 note 只读；宿主 memory 工具写入不再进 Minir。
6. base 发布每会话 5 行；16 行重复的 `base.window_summary` 删除，1 行转 chunk，1 行退役。
7. `rules/advice_target.md:55` 的目标改为最新的 `window_summary` 行。
8. 版本表不再新增结论行或标记行。
9. 权重 16 项，唯一数值变化是 `window_summary` 0.6 → 1.0；durable 9 不变。
10. 会话绑定 3 → 2（合并的副作用，不是收紧）：`scope="all"` 仍能读到 4 类会话结论，保持今天的行为。

## 9. 落地顺序

1. store：加 `_CONCLUSION_SPECS`（9）与 `_RETIRED_SPECS`（4）；`CONCLUSION_KINDS` 改 9 项；`_BASE_CONCLUSION_KINDS` 去 window_summary；删 `TOOL_KINDS`、`_KIND_WEIGHT` 字面表、`_LONG_TERM_KINDS`、`_SESSION_BOUND_KINDS` 字面定义，消费点改派生（3072、3139、97-99、794-803）；加 v6 迁移（下表，含 `DROP TABLE vector_meta`）。
2. 检索：删 `_fuse` 与 `vector_ranks`，`search` 直接返回词面排序 + 扩展尾部；删 `VectorIndex.recall` 调用；删向量用例。
3. 向量子系统：删 `vector.py`、`__init__` 接线、`plugin.yaml:18`、`scheduler.py` 向量任务、`minir_stats` 向量段、`tests/test_vector.py`。
4. 工具面：`minir_search` 的 enum 与校验改 `CONCLUSION_KINDS`；`remember`/`forget` 的模式枚举与分支删除（__init__.py:1882-1937、tools.py:122-150）；instructions（__init__.py:721）与 tools 描述改写；删镜像与孤儿方法；改 `rules/advice_target.md:55`。
5. 测试同步：tests/test_store.py:30-38 改断言；`base.window_summary` 相关用例（test_store.py:378、test_provider.py:1082）改指 `window_summary`。

迁移（v6，一次，幂等）：

| 动作 | 行数 |
|---|---:|
| 删 base 行（同窗口 chunk 正文相同） | 16 |
| 插 chunk 行（key `chunk:<sid>:<window_from_ts>`，正文=base 去首行标题） | 1 |
| 留退役（无窗口可归） | 1 |
| DROP `vector_meta` | 1 表 |
| 本机验收：`window_summary` 121 → 122，`base.window_summary` 18 → 1，9 个 kind 434 行 |

## 10. 测试

- T1 词表：`CONCLUSION_KINDS` 9 项、每 kind 唯一、无 note、无 base.window_summary、无标记成员；退役登记 4 项。
- T2 派生：权重 16（9+5+2，`window_summary` 1.0）、durable 9、bound 2、版本化 5。
- T3 发布：一次 base 刷新写 5 行结论，不再出现 `base.window_summary`。
- T4 迁移：临时库放 3 类 base 行（重复 / 无 chunk / 无 refresh），断言删除、插入、退役各按表发生，且幂等。
- T5 检索：`search` 不再接受 `vector_ranks`；`score` = overlap + 权重；扩展行排在直接命中之后；两字中文查询仍能命中。
- T6 搜索过滤：9 个结论 kind 全部可作 `type`；`type="entities"`、`"note"`、`"base.window_summary"` 被拒。
- T7 工具模式：`remember_mode` 缺省与 `"conclusion"` 被拒；`minir_forget` 不再接受 `conclusions_id`。
- T8 静态扫描：plugin/minir 内 `save_conclusion` 的 kind 字面量 ⊆ 词表 ∪ 标记 ∪ 退役；生产代码不再出现 `"note"` 作为 kind。
- T9 词表同步：`prompting.MARKER_TYPES` 的 key 集 == 标记词表。
- T10 向量删除：仓库内不再有 `vector` 模块引用（import、配置键、调度任务、stats 字段）。

## 11. 不做

- 不改任何 kind 字符串（`base.window_summary` 是整条删除，不是改名），不动其他 schema。
- 不改名 `base.value_markers`：`base.<列名>` 现在是"这一列的原样发布快照"的统一约定（r2 关闭）。
- 不定义标记词表，不定义工具能力词表。
- 不把历史的 note 或标记行改写成别的 kind。
- 不重新引入语义检索通道；要就重新立项。
- 不收紧会话绑定（r1 保持现状）；已知并接受 4 类会话结论在 `scope="all"` 下可跨会话读。

## 12. 决策记录

| 编号 | 决定 | 后果 |
|---|---|---|
| r0 | 标记不设写入口 | 标记词表只读；模型写入只剩知识页与卡片 |
| r1 | 会话绑定保持现状 | `bound` 只有 2 项；4 类会话结论仍可被 `scope="all"` 跨会话读到 |
| r2 | `base.value_markers` 不改名 | 与 `base.<列名>` 约定一致；省掉 79 行迁移 |
| r3 | 合并后 `window_summary` 权重 1.0 | 权重表唯一数值变化；canonical 摘要排序与合并前一致 |
| r4 | store 不校验 kind | 校验只在工具边界 + T8 静态扫描；测试与迁移仍可写任意 kind |
| r5 | 删除向量子系统 | embedding 成本归零；语义召回能力消失 |

已知风险：r1 保留的跨会话可读包含 `session_summary` 与 `window_summary`，它们的内容可能带用户原话片段；要收紧是下一次改动，不在本设计内。
