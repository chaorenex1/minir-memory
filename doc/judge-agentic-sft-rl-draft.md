# 评审模型与 agentic 工具调用的 SFT→RL 配方（草稿）

- 状态：draft
- 覆盖：两个训练目标——(A) minir adviser 评审模型（封闭词表 JSON 判定），(B) `minir_agents` / hermes-lead 的 agentic 工具调用轨迹。给出数据契约、样本生成管道、reward、框架档位、算力估算、停训与回退条件。
- 不在范围：宿主 Hermes 本体、gateway、记忆抽取模型、dialectic 生成模型。
- 基线证据：本机 `$HERMES_HOME/minir-memory/memory.db`（22.0 MB）实测，2026-10-27：

| 表 | 行数 | 实测分布 |
|---|---|---|
| `adviser_reviews` | 2474（9.3 天，265/天） | silent 1866、advised 495、failed 103、dropped 9、running 1 |
| `advisories` | 562（60/天） | delivered 370、injected 75、expired 91、pending 17、dropped 9；source：adviser_mode 554、dialectic 8 |
| 延迟（均值） | — | silent 14.8s、advised 33.9s、dropped 126.7s、failed 32.2s |
| 评审模型 | 2474 | `Qwen/Qwen3.5-4B` 1779、`DeepSeek-V4.1-Flash` 511、`deepseek-v4-flash` 138、空 46 |
| `coverage` / `selected_claims` | 56 / 114 | 只占 2.3% / 4.6% 的 review，claim gate 实际很少被触发 |

- 词表基线：`plugin/minir/prompting.py:99` 的 `ADVICE_TARGETS` 共 14 个 delivered token（5 action + 2 artifact + 7 consistency）；判定器是 `plugin/minir/adviser_mode.py:542` 的 `parse_review`。
- 未验证：本文所有显存与吞吐均为公式估算，标 `[估算]`。本机无 NVIDIA 卡（RX 580 + AMD 集显），没有任何一段训练被实际执行过。

## 0. 结论

1. 两个目标必须分开训：评审是短上下文的结构化判定，agentic 是长轨迹。合成一个模型会让 reward 变成两类噪声的混合，且上下文预算互相挤压。
2. 评审模型的主 reward 不是"格式正确"，而是"该报的报了、不该报的没报"。只看 gate 通过率会塌成全 silent——成功 review 里已有 79.0%（1866/2361）是 silent，静默输出天然 100% 过 gate。
3. agentic 的 reward 必须是外部验收命令加策略违规，不用模型自评。
4. 数据不能等真实积累：按 60 条/天，攒 5000 条 advised 类样本要 83 天 `[实测推算]`。评审样本走"历史窗口重放 + 现役强模型生成 + 生产 gate 过滤"。
5. 现役评审是 4B（`Qwen/Qwen3.5-4B` 占 72%），79% 的 silent 里有多少是"确实没问题"、有多少是 4B 看不出来，库里无法区分。这是评测集第一个要回答的问题，也是升到 8B 的唯一正当理由。

## 基座模型（ModelScope 实测，约束全部下游选择）

### B.1 仓库与权重

基座：`Merkyor/Qwen3.8-27B-Coder390-EfficientThink-Opus5.5-GPT6Astra-Grok4.7-DSV4Pro-K3-SFT-RLOO-MTP-DFlash2`，apache-2.0，19,344 下载 / 152 stars。

| 项 | 实测值（ModelScope API + 权重清单） |
|---|---|
| 架构 | `Qwen3_5ForConditionalGeneration`，`model_type: qwen3_5`，`transformers_version 5.17.0` |
| 训练权重 | `BF16/` 子目录：`model-00001-of-00002.safetensors` 49,825 MB + `model-00002-of-00002.safetensors` 4,888 MB = 54.7 GB（≈27.36B 参数 × 2 字节） |
| MTP 头 | `BF16/mtp-bf16.safetensors` 849 MB，独立文件；`mtp_num_hidden_layers: 1` |
| 草稿模型 | `BF16/DFlash2-FP8/model.safetensors` 2.41 GB（投机解码用，不参与训练） |
| 量化包 | 同仓另有 FP8（静态/动态）、NVFP4、INT8、INT4、GGUF 子目录，只能推理，不能训练 |
| 文本主干 | 64 层，hidden 5120，head_dim 256，24 Q 头 / 4 KV 头，vocab 248320，`max_position_embeddings 262144` |
| 注意力 | **混合**：`layer_types` 每 4 层一个 `full_attention`，其余 `linear_attention`；`mamba_ssm_dtype float32`，`linear_num_value_heads 48`，`linear_conv_kernel_dim 4` |
| 位置编码 | `mrope_interleaved`，`mrope_section [11,11,10]`，`partial_rotary_factor 0.25`，`rope_theta 1e7` |
| 视觉塔 | 27 层，hidden 1152，patch 16，`out_hidden_size 5120`；`image_token_id 248056`、`video_token_id 248057` |
| 其他 | `use_cache: false` 写在 config 里、`tie_word_embeddings: false`、`eos_token_id = bos_token_id = 248044` |

### B.2 由此强制的六条约束

1. **训练只用 BF16 子目录**。FP8 静态包带 `weight_scale_inv`，常规 SFT/RL 栈不在训练时反量化；拿 FP8 包开训等于先给自己造一个不支持的格式。
2. **栈被架构唯一化**。verl 代码库检索 `qwen3_5` 零命中；ms-swift 有 `examples/ascend/train/qwen3_5/*`（LoRA/FSDP2）与 `examples/ascend/grpo/fsdp2/qwen3_5_4b_lora.sh`；vLLM 有 `vllm/model_executor/models/qwen3_5.py` 与 `qwen3_5_mtp.py`。SFT/GRPO 走 ms-swift，rollout 走 vLLM；verl 与 OpenRLHF 不作为选项。检索命中的 ms-swift 例子都在 Ascend 路径下，CUDA 路径未核实。
3. **MTP 头不训**。权重单列且 `mtp_num_hidden_layers: 1`：加载时排除 `mtp.*`，SFT loss 只算主头；否则 849 MB 参数会以 unexpected keys 报错或被无意义更新。
4. **`use_cache: false` 必须显式覆盖**。rollout 与推理侧打开，否则长窗口生成退化成二次重算。
5. **格式约束用 JSON schema，不能用 token 白名单**。用模型自带 tokenizer（tokenizers 0.22.2）实测：`{"action":"silent"}` 是 5 token（`{"` / `action` / `": "` / `silent` / `"}`），但 `"blocker"` 是 4 token（`"`/`block`/`er`/`"`）、`"concern"` 4、`"nit"` 3、`soul_craft_check` 5（`s`/`oul`/`_c`/`raft`/`_check`）。按"单 token 枚举"做掩码必然漏值，走 xgrammar/outlines。
6. **系统提示已吃掉大半窗口**。同 tokenizer 实测：`review.md` 649 + `rules/advice_target.md` 2203 + `rules/advice_judgment.md` 2063 + `review_budget.md` 90 = **5005 token**。6144 预算只剩 1139 token 给事件，8192 只剩 3187。要放大窗口必须同期改线上配置，否则训练分布与线上不一致。

### B.3 规模与延迟

全参常驻状态按 16 B/param（bf16 权重 2 + bf16 梯度 2 + fp32 master 4 + Adam m/v 8）`[估算]`：

| 配置 | bf16 权重 | 全参状态 | 8 卡均摊 | 16 卡均摊 | 判定 |
|---|---|---|---|---|---|
| 27B 全参 | 50.3 GiB | 402.3 GiB | 50.3 GiB | 25.1 GiB | 8×80GB 只剩激活空间，建议 16 卡或 ZeRO-3 offload |
| 27B + LoRA 0.5% | 50.3 GiB，可训 135M（Adam 1.5 GiB） | 51.8 GiB | — | — | 单张 80GB 可跑；48GB 不够 |

延迟：模型卡的思考长度 P50（GPQA 3,196 / MMLU 154 / LCB 5,073 reasoning tokens），按 DFlash2 单请求约 180 tok/s 折算 `[估算]`：GPQA 17.8s、LCB 28.2s、MMLU 0.9s。现役 4B 线上均值已经是 silent 14.8s / advised 33.9s。27B 开着思考跑内联评审，等于把每个 turn 抬到几十秒级——**在线继续 4B/8B，27B 用于离线复核、难例标注与 teacher**。

### B.4 与"目标约 2× 现役"的冲突

现役 4B，27B 是 6.75×。规模本身没有数据支撑，所以管线内建归因：同一数据集在 8B 与 27B 上各跑一次，比 held-out 一致率 κ。只报 27B 的数字，无法区分"数据改善"和"参数变大"。

## 1. 目标 A：评审模型（judge）

### 1.1 输入输出契约

输入侧沿用生产 prompt 的三段结构（`plugin/minir/prompts/adviser/review.md` + 两个 rule 文件 + budget）：

```
system : review.md 契约 + rules/advice_target.md + rules/advice_judgment.md + review_budget
user   : live-turn events（oldest→newest，最后一个事件是当前状态）
         + long-term context（卡片摘要，可能过期）
         + ACTIVE CLAIMS 列表（可选，带 id）
输出   : {"action":"silent"}
         或 {"action":"advise","advices":[{"severity","target","text","decision",
                                          "evidence":[...],"claim","candidate_id","deferred"}]}
         外加可选的 coverage 映射（7 个 consistency target → clean|found|n/a）
```

SFT 样本就是这一对 (system+user, assistant JSON)。token 预算 6k 级：事件窗口 + 两份 rule 文件占大头，输出通常 <400 token。

### 1.2 词表漂移：先清洗历史，再谈训练

`advisories` 里有 6 个**不在 14 token 词表内**的历史 token，共 17 行：`claim_gate` 9、`ai_self_representation` 2、`user_representation` 2、`session_chunk_summary` 2、`ai_identity_card` 1、`user_peer_card` 1。它们不以 `_check` 结尾，`parse_review` 会把它们降级成 `general`；直接拿历史行当 label 会教模型写过期 token。

处理：迁移或丢弃这 17 行，并在训练集构造时用 `DELIVERED_TARGET_NAMES` 做白名单断言，出现集外 token 直接抛错，不静默通过。

### 1.3 类别缺口：稀有类必须合成

实测覆盖（`advisories` 全表）：

| target | 行数 | 结论 |
|---|---|---|
| `current_step` / `concern` | 182 | 主力类，充足 |
| `output_surface` / `concern` | 66 | 充足 |
| `tool_choice` / `concern` | 56 | 充足 |
| `output_surface` / `blocker` | 11 | 偏少 |
| `knowledge_claim_check` | 1 | 几乎为 0 |
| `user_preferences_check` | 1 | 几乎为 0 |
| `soul_craft_check` | 2 | 几乎为 0 |
| `user_identity_check` | 0 | 完全缺 |
| `ai_self_representation_check`（新名） | 0 | 完全缺（只有 legacy 名 2 行） |
| 严重度 `nit` | 0 | delivered 里只有 blocker 68 / concern 302，nit 全在 pending/expired（默认 floor 就是 concern） |

直接按自然分布 SFT，等于放弃 4 个 consistency target。训练集必须按 target 分层采样，并对 0 行类别用规则化合成（引用行 + 观测 + kind 四字段齐全，见 `rules/advice_target.md` 的 EVIDENCE AND BUDGET）。

### 1.4 样本生成管道

（本节只给形状；可执行的七阶段管线、gate 真实语义、分层配额与断言清单见 1.7。）

```
历史窗口 (session_id, from_seq..to_seq)   ← adviser_reviews 2474/2474 行有 to_seq、2342 行有 from_seq，最大 to_seq 1989；read_messages 按 timestamp 取，需要 seq→ts 定位或给它加 seq 参数
   ↓ 重放 events（history.read_messages，roles 需含 tool）
   ↓ teacher 生成 1..k 个候选 JSON（temperature 0.7，k=4）；teacher 必须强于现役 4B（本机已跑过 DeepSeek-V4.1-Flash，或经 u2 授权的更强 provider）
   ↓ 生产 gate：parse_review(dropped=...) + parse_coverage
   ↓ 通过者入 SFT 正样本；被拒者入负样本池（带 drop 原因）
```

两个硬约束：

- 事件组装目前是私有实现（`adviser_mode._render_event`，`plugin/minir/adviser_mode.py:726`；task 组装在 reviewer 类方法内）。重放要么抽出一个 `build_review_input(session_id, from_ts, to_ts) -> (system, task)` 的缝，要么在 transcript 上复刻——后者会和线上 prompt 漂移，不建议。
- `advisories.turn_id` 实测全部为空（562 行里 0 行有值），只能靠 `review_id` 连回 review 行；不要假设 turn 粒度可用。
- 重放规模够用：宿主 `state.db`（1.17 GB）实测 user 4014 / assistant 57533 / tool 77795 条消息，`history.read_messages` 已支持 `roles=("user","assistant","tool")` 取到工具事件。

骨架（示意，不是可运行实现）：

```python
# file: tools/train/build_judge_sft.py
from plugin.minir.adviser_mode import parse_review, parse_coverage
from plugin.minir.history import read_messages, state_db_path

def build_one(row, gen):
    msgs = read_messages(state_db_path(HERMES_HOME), row["session_id"],
                         row["created_at"] - row["window_s"], row["created_at"],
                         roles=("user", "assistant", "tool"))
    if not msgs:
        return None
    system, task = build_review_input(msgs)          # 需要抽出的缝
    for cand in gen(system, task, n=4, temperature=0.7):
        dropped = {}
        rows = parse_review(cand, active_claims=row["active_claims"], dropped=dropped)
        if dropped:
            yield {"kind": "neg", "input": [system, task], "output": cand, "drop": dropped}
        if rows or '"silent"' in cand:
            yield {"kind": "pos", "input": [system, task], "output": cand}
```

### 1.5 负样本：八类注入

gate 的拒绝分支就是负样本生成器，逐一构造（字段名来自 `parse_review` 实测）：

| 注入 | 触发 | 期望 |
|---|---|---|
| `bad_target` | 造一个 `soul_precedence_check`（不在 7 个 consistency 内） | 整条被拒，不降级 |
| `unclaimed` | 有 ACTIVE CLAIMS 但不写 `claim` | 整条被拒 |
| `unselected_claim` | 写一个集外 `claim` id | 整条被拒 |
| `below_floor` | `severity_floor=concern` 时给 `nit` | 静默丢弃 |
| `duplicate` | 同 turn 两条 `text` 相同 | 只保留一条（实测 history 里有 `duplicate/<target>` 计数） |
| `digest_quote` | `user_preferences_check` 引用 320 字符摘要而非卡片正文 | 训练目标：判为无效证据 |
| `illegal_severity` | `knowledge_claim_check` 配 `blocker`、`soul_tool_policy_check` 配 `nit` | gate 会放行，语料断言必须拦下——这是必须补进 reward 的一项 |
| `truncated_tail` | 同一窗口给 8 条 advice（超过 `max_advices`） | 尾部被截断且无计数器，语料侧自检条数 |

注意：负样本不是"训练模型学会沉默"，是"训练它学会正确地报"。这两者在数据里必须可区分，否则就是 1.6 的塌陷。

### 1.6 塌陷风险：silent 是最优解

现役成功 review 里 79.0%（1866/2361）是 silent。任何以 gate 通过率为唯一 reward 的训练都会收敛到常输出 `{"action":"silent"}`：它 100% 合法、0 违规、还最短。

约束：

- SFT 阶段：正样本按 `silent : advise` 分层，不要按自然分布（自然分布下 silent 占 4:1，模型会先学沉默）。
- RL 阶段：reward 分两段，格式合规 + 注入故障集上的召回。没有召回项的 reward 一律不接受。

### 1.7 数据集生成管线（S0→S6，可执行版）

落地为 `tools/train/` 下的部署脚本，全部只读线上库，产出到 `data/judge/`。每一阶段都有输出、验收与失败恢复三项。

#### 先固定 gate 的真实语义（逐行读 `parse_review:542` / `parse_coverage:630`）

| 行为 | 代码事实 | 对语料的要求 |
|---|---|---|
| silent 别名 | `action ∈ {silent, none, noop}` 都当静默 | 正样本统一写规范形式 `{"action":"silent"}` |
| severity 强制转换 | 未知值改成 `concern`；`critical/warning/info` 映射成三档 | label 侧显式断言 ∈ {blocker,concern,nit}，否则别名混进语料 |
| 低于 floor | 非 blocker 且低于 floor → 无计数器、直接丢弃 | 按 drop 计数过滤的语料会漏掉这一类 |
| target | 未知且以 `_check` 结尾 → `bad_target` 拒收；其他未知 → **降级成 `general`** | 语料必须白名单断言，不能靠 gate 的降级 |
| claim | 有 `active_claims` 时，除 `user_preferences_check` 外没写 claim → `unclaimed`；claim 不在 active 集 → `unselected_claim` | 负样本主要来源 |
| 去重 | `text.lower()` 指纹，无计数器 | 语料自检去重 |
| 条数上限 | 超过 `max_advices` 截断尾部，无计数器 | 语料自检 ≤ 上限 |
| deferred | 非 blocker 才可能 `deferred=true` | blocker + deferred 是非法组合，assert |
| coverage | 只收 7 个 consistency key 的 `clean/found/n/a`；畸形 → `__malformed`；缺项按 unknown | 单独校验，不混进 advice 断言 |

gate 不查的两件事，语料侧必须补：词表声明的 `(target, severity)` 合法性（`knowledge_claim_check` 无 blocker、`soul_tool_policy_check` / `soul_workflow_check` 无 nit、`soul_craft_check` 无 blocker），以及 severity 别名归一化。

#### S0 采集 `collect.py`

- 输入：`adviser_reviews`（status ∈ {silent, advised}）× `advisories`（`review_id` 关联）。
- 过滤：集外 target 行（17 条）、窗口内事件数 0 的行、宿主 state.db 里找不到 session 的行。
- 输出 `data/judge/raw_windows.jsonl`：`{review_id, session_id, from_seq, to_seq, ts_lo, ts_hi, active_claims, coverage_expected, model, created_at}`。
- 验收：行数与每条过滤原因计数写进 `manifest.s0.json`；总数 = 2361 − 被过滤数。
- 失败恢复：缺 session 记 `lost_session` 跳过，不重试。

#### S1 重建 `replay.py`

- 前置：`adviser_mode` 导出 `build_review_input(session_id, ts_lo, ts_hi) -> (system, task)`。现状私有（`_render_event:726` 加 reviewer 类方法内组装），**这是唯一需要改线上代码的地方**，且必须是导出，不是在训练脚本里复刻 prompt。
- 消息取用：`history.read_messages(state_db_path(hermes_home), session_id, ts_lo, ts_hi, roles=("user","assistant","tool"))`。
- 输出 `data/judge/inputs.jsonl`：`{review_id, system, task, claims, n_events, prompt_tokens}`，`prompt_tokens` 用 B.2 那个 tokenizer 数。
- 验收：`prompt_tokens ≤ 6144`；超出的按线上截断规则裁掉并记 `truncated`；system 必须逐字节等于线上渲染结果（抽 50 条 diff）。
- 失败恢复：空事件 → `empty_window` 丢弃；system diff 非空 → 停管线，说明 prompt 已漂移。

#### S2 teacher 生成 `generate.py`

- teacher：`DeepSeek-V4.1-Flash`（本机已跑过 649 次 review，是现成可用的强于 4B 的教师），第二教师需授权（见 6 的 u2）。
- 采样：每窗口 k=4，temperature 0.7，top_p 0.95；prompt 显式要求 coverage 段（现役只有 2.3% 带 coverage，teacher 不要求就永远补不齐）。
- 规模 `[估算]`：2361 窗口 × 4 = 9,444 次调用；输入约 60.6M token（5005 系统 + 约 1400 事件），输出约 3.6M token（每条 380）。
- 输出 `data/judge/candidates.jsonl`：`{review_id, k_index, answer, stop_reason, usage}`。
- 验收：每窗口恰好 4 条；单条失败重试 1 次，仍失败记 `gen_failed`。
- 失败恢复：窗口 4 条全失败 → 整窗丢弃，不降 k（降 k 会让多数投票失去意义）。

#### S3 gate 过滤与多数投票 `label.py`

- 唯一裁判：`parse_review(answer, active_claims=..., dropped=...)` + `parse_coverage(answer)`，直接 import 线上函数，不重写词表。
- 分类：`pos_advise`（≥3/4 通过且 target 集合一致，取其中最短一条）、`pos_silent`（≥3/4 静默）、`neg_<reason>`（按 `dropped` 的键归档）、`ambiguous`（2/2 分歧，丢弃只计数）。
- 补充断言：severity 归一化、`(target,severity)` 合法性、blocker+deferred 非法。
- 输出 `data/judge/labeled.jsonl` + `manifest.s3.json`。
- 验收：打印三类比例与每个 target 的条数；`bad_target` 与 `unselected_claim` 计数必须 >0，否则负样本注入没生效。
- 失败恢复：`ambiguous > 40%` 说明 teacher 判定不稳 → 降 temperature 或换教师，**不许放宽 gate**。

#### S4 分层配额与合成 `assemble.py` + `synth.py`

advise 类目标 4,580 条，按词表而非自然分布分配（自然分布里 `current_step` + `concern` 一条就占 182/562）：

| 组 | token 与目标条数 | 小计 |
|---|---|---|
| ACTION | `current_step` 900 / `tool_choice` 650 / `user_intent` 300 / `final_answer` 250 / `general` 100 | 2,200 |
| ARTIFACT | `output_surface` 450 / `code_economy` 80 | 530 |
| CONSISTENCY | `knowledge_claim_check` 400 / `soul_tool_policy_check` 300 / `soul_workflow_check` 300 / `ai_self_representation_check` 300 / `user_preferences_check` 250 / `user_identity_check` 200 / `soul_craft_check` 100 | 1,850 |

缺口补齐：

- 0 行 / 近 0 行类别（`user_identity_check`、`user_preferences_check`、`knowledge_claim_check`、`soul_craft_check`）：按规则文件四字段（kind / quote / observation / inference）造 3 套模板 × 5 变体，再让 teacher 在真实事件上改写，过 S3 同一 gate。
- `nit` 档：从 6 条真实 pending nit 扩写；不足部分用 `severity_floor=nit` 的注入样本补，标签显式记 `severity=nit`。
- 上限：任何单类不得超过 advise 集 25%（现配额最大 19.7%）。

#### S5 组装与断言 `build_dataset.py`

- 格式：chat JSONL，`messages=[{system},{user},{assistant}]`，assistant-only loss；模板用模型自带 `chat_template.jinja`（TRL 侧有 `qwen3_5_think_training.jinja`，用现成的，不自己拼）。
- 断言（任一失败即中止）：
  1. `target ∈ DELIVERED_TARGET_NAMES`（直接 import 常量）
  2. `severity ∈ {blocker, concern, nit}`，且 `(target, severity)` 合法
  3. `claim` 非空时必须 ∈ 该窗口 `active_claims`；`user_preferences_check` 例外
  4. JSON 可解析，且 `parse_review` 重新解析后与标签逐字段一致（round-trip）
  5. 同窗口文本指纹唯一；条数 ≤ `MAX_ADVICES`
  6. `prompt_tokens ≤ 6144`
- 切分：按 `created_at` 日期 70/15/15，不随机切（同日事件高度相关，随机切会漏时间泄漏）。
- 输出 `data/judge/{train,val,test}.jsonl` + `manifest.json`（每类条数、来源占比、drop 原因分布、token 总量、sha256）。
- 规模 `[估算]`：advise 4,580 + silent 4,000 + neg 1,600 = 10,180 条 ≈ 61.1M token；8 卡 @2000 tok/s 约 63.6 分钟/epoch。

#### S6 发布 `release.py`

- 产出 dataset card（条数、类别表、生成日期、teacher 版本、gate 函数 git blame）+ `SHA256SUMS`。
- 验收：重跑 `build_dataset.py` 得到同一 sha256。
- 与评测表绑定：manifest 记录 held-out 冻结日期。

## 2. 目标 B：agentic 工具调用

### 2.1 轨迹契约

```
样本 = (goal, 可用工具清单, 环境快照, 轨迹[step...], 终态)
step = {call: {tool, args}, result_summary, files_touched}
reward = w1·验收命令通过 + w2·(1 - 策略违规) + w3·步数预算内 + w4·(1 - 回归破坏)
```

- 验收命令：任务自身的 test/lint/断言，必须由外部进程执行（不是模型自述）。
- 策略违规：`plugin/minir/file_guard.py` 的 `check(tool_name, args, hermes_home)` 是现成的路径围栏（写盘限于项目根、排除秘密文件与 `.git`），直接当越界写/越界读检测器，不用新写一套。
- 回归破坏：改动后跑既有测试集，破坏数扣分。
- 步数：超预算直接截断并记 0，不给"接近完成"的部分分。

### 2.2 数据来源

- `minir_agents` 的真实分发记录（Kanban / worktree 分支）是主来源。
- 冷启动不足时用强模型在沙箱里合成任务，但合成任务的验收必须真的能跑（先跑通再入池）。

### 2.3 数据集生成管线（A0→A4）

- **A0 任务池** `collect_tasks.py`：真实任务来自 Kanban / worktree 分支记录；冷启动不足时合成。输出 `data/agentic/tasks.jsonl`（goal、repo、验收命令、步数预算、依赖）。
- **A1 沙箱执行** `rollout.py`：每任务在一次性 worktree 里跑，`file_guard` 全程开，记录每步 tool / args / result。输出 `data/agentic/traces.jsonl`。
- **A2 判分** `score.py`：验收命令在干净进程里跑（避免 agent 自己污染环境），加回归测试、违规计数、步数；失败样本全部保留（分母不丢）。
- **A3 过滤** `filter.py`：剔除无终态、工具报错未恢复、超步数的轨迹；失败样本保留作 neg。
- **A4 组装** `build_agentic.py`：用模型自带 `chat_template.jinja` 渲染 tool-call 格式，thinking 前缀保留与否固定一种并写进 manifest。

长轨迹吃上下文：262144 的 max_position 够，但 8 卡全参只剩 50.3 GiB/卡给参数，激活空间紧张 `[估算]`；长轨迹优先 LoRA 或 16 卡。评审（目标 A）的短上下文才是 27B 全参的主场。

## 3. 框架与算力

### 3.1 档位

| 档位 | SFT | RL | 算法 | 适用 |
|---|---|---|---|---|
| 单卡 24-80GB，≤8B LoRA | Unsloth 或 LLaMA-Factory | TRL 的 GRPO（PEFT）或 ms-swift | GRPO + LoRA | 先跑通管道、验数据 |
| 单机 8 卡，8-32B 全参 | ms-swift（`qwen3_5` 例子在 `examples/ascend/`，CUDA 待核） | ms-swift GRPO（fsdp2） | GRPO/RLVR，vLLM rollout | 目标 A 的主流档 |
| 多机，32B+ 或 MoE | Megatron 系或 ms-swift 多机 | ms-swift / slime | 异步 rollout，bounded staleness | 目标 B 的长轨迹 |
| 只有偏好对、无 verifier | — | TRL/ms-swift 的 DPO | 跳过 RL 段 | 目标 B 的兜底 |

这张表在选 27B 之后被削掉了一半：Unsloth / LLaMA-Factory / Axolotl / verl / OpenRLHF 都没有 `qwen3_5` 的适配证据（verl 检索零命中），只剩 ms-swift + vLLM 这条线（B.2 第 2 条）。档位只剩"LoRA 单卡 80GB"和"全参 16×80GB"两档，中间没有便宜选项。

现役评审是 4B（`Qwen3.5-4B` 占 72%）。判定高度结构化，8B 全参在单机 8 卡就够，而且它是**归因基线**：同一份数据集先在 8B 上跑一遍，再看 27B 的 κ 能不能更高。跳过 8B 直接上 27B，就无法区分"数据改善"和"参数变大"（B.4）。

### 3.2 显存（公式 + 实算，`[估算]`）

全参训练的常驻状态按 16 B/param 计（bf16 权重 2 + bf16 梯度 2 + fp32 master 4 + Adam m/v 8）：

| 模型 | bf16 权重 | 全参状态 | 8 卡均摊 | 判定 |
|---|---|---|---|---|
| 3B | 5.6 GiB | 44.7 GiB | 5.6 GiB | 单卡 24GB 可行（加梯度检查点） |
| 7-8B | 13.0-14.9 GiB | 104.3-119.2 GiB | 13.0-14.9 GiB | 单卡 80GB 紧；8 卡 80GB 宽裕 |
| 27B | 50.3 GiB | 402.3 GiB | 50.3 GiB | 8×80GB 只剩激活空间，建议 16 卡或 ZeRO-3 offload |
| 32B | 59.6 GiB | 476.8 GiB | 59.6 GiB | 同上，16 卡起 |

LoRA 走另一条账：7B、可训 0.5% ≈ 35M 参数，Adam 状态 0.40 GiB，权重 bf16 13.0 GiB → 24GB 卡可跑，seq 4096 + 梯度检查点。27B 的同一笔账是权重 50.3 GiB + Adam 1.5 GiB = 51.8 GiB，单张 80GB 可跑，48GB 不够（见 B.3）。

### 3.3 吞吐（`[估算]`，按实测 tok/s 代入）

SFT：`epoch 时间(h) = tokens / (卡数 × tok/s/卡) / 3600`

5000 条 × 6000 token = 30M token 的参考值：

| 配置 | 每 epoch |
|---|---|
| 8 卡 @2000 tok/s | 31.2 分钟 |
| 4 卡 @2000 tok/s | 62.5 分钟 |
| 1 卡 @1500 tok/s | 5.6 小时 |

GRPO：`每步生成 token = prompts × rollouts × 平均生成长度`。4×8×1200 = 38.4k token/步，500 步 = 19.2M；按 4000 tok/s 聚合生成算 9.6 s/步纯生成时间，训练侧另计。rollout 与 trainer 分卡，别在同 8 卡上抢显存。

## 4. 评测集（先于训练）

- held-out：从 562 条 `advisories`（其中 delivered 370）里按 target 分层抽 20%，冻结，训练期间不看。
- 人工/强模型双标：现役模型在 held-out 上的判定当基线，人（或独立 AI 标记者）标一遍，算一致率 κ。
- 测试项固定四项：gate 违规率、注入故障召回、误报率、silent 率。
- 没有这张表就不开训。SFT 前定表，RL 前定阈值。

## 5. 停训与回退

停止条件（任一命中）：

| 条件 | 阈值 | 判据 |
|---|---|---|
| gate 违规（bad_target + unclaimed + unselected_claim） | held-out 必须为 0 | 任何一条 >0 回 SFT 查数据 |
| silent 率偏离现役基线 79.0% | ±10 个百分点 | 超出判 collapse，回上一个 checkpoint |
| 注入故障召回（blocker 级） | ≥ 0.85 | 未达不进入 RL 段 |
| 误报率 | ≤ 0.10 | 超阈值说明 RL 在教它乱报 |
| 与双标一致率 κ | ≥ 0.6 达标；SFT 后 <0.45 停 | 低于 0.45 是数据方向错，加训练量无用 |

回退：

- 每 N 步存 `safetensors`，只有 held-out 全绿才晋级为"可发布"。
- 晋级失败回上一个可发布 checkpoint，并在记录里写失败的那一项指标，不重跑同一配置第二次。
- 词表变更（新增 target）视为破坏性变更：先重跑 held-out 基线，再决定是否重训。

## 6. 未决

| 编号 | 事项 | 为什么必须由你定 |
|---|---|---|
| u1 | 卡型与卡数（27B 全参要 16×80GB 或 8 卡 + offload；LoRA 单卡 80GB） | 预算与可得性，我不该假设 |
| u2 | review events（含项目内容）能否送外部 provider 做 teacher | egress 政策；`adviser_mode` 有 `redact` 开关，但那是脱敏不是授权 |
| u3 | 27B 的定位：只做离线复核 / teacher，还是也要上线内联 | 延迟预算（B.3：GPQA 级思考 17.8s/次） |
| u4 | 是否接受 silent 类按 0.87:1 过采样（4,000 : 4,580） | 会改变线上沉默率，属于产品取舍 |
| u5 | agentic 是否用真实 Kanban 记录做冷启动 | 会涉及把项目内容放进训练集 |
| u6 | 是否把 review 预算从 6144 提到 8192/16384 | 系统提示已占 5005，改预算必须同期改线上配置，否则训练分布与线上不一致 |
| u7 | MTP 头是否参与训练（当前建议不训） | 只有当投机解码收益值得时才做，且 ms-swift 未必支持该头 |
| u8 | teacher 是否必须产出 coverage 段 | 现役覆盖只有 2.3%，强行要求会把语料推向"编 coverage" |

## 修订记录

- 2026-10-27 初稿：基于本机库实测基线（2474 reviews / 562 advisories / 9.3 天）与生产词表、gate 代码路径。显存与吞吐为公式估算，无实测训练。
- 2026-10-27 补齐数据集生成管线：新增基座模型节（B.1-B.4，ModelScope API + config.json + tokenizer 实测）、1.7 的 S0-S6 七阶段（含 gate 九条真实语义、分层配额、六项断言）、2.3 的 A0-A4，负样本从六类扩到八类，训练栈按 B.2 唯一化为 ms-swift + vLLM。
