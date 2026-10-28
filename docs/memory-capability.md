# 聊天记忆能力评估与改进路线

> 评估对象：`deepdiscord` 的对话记忆链路（长期记忆 / 重要记忆 / 世界书剧情 / 好感度）
> 评估方式：代码静态通读 + 记忆库体检（`scripts/memory_eval.py`）+ 检索质量度量（`tests/test_memory_quality.py`）
> 数据合规：只统计条数/重复度/覆盖率等聚合指标，未导出任何记忆正文

---

## 1. 记忆链路现状（先看清"记忆"到底做了什么）

| 阶段 | 实现 | 关键文件 |
|------|------|----------|
| 写入-显式 | 【记住X】→ `memories` 表（≤300 字 + 关键词） | `qqbot/memstore/local.py:save_longterm` |
| 写入-提炼 | 睡前/【结束这一天】→ LLM 提炼 → `important_memories`（affected_delta / importance / story_time / keywords / track） | `qqbot/sleep_ritual.py` |
| 写入-世界 | 世界书条目被触发 → `data/worldlog/{身份}.json` → 提炼成 `track='world'` | `qqbot/worldlog.py` + `sleep_ritual._extract_world` |
| 整理 | 超过 `sleep.max_important` → LLM 合并去重（`consolidate`） | `qqbot/sleep_ritual.py:consolidate` |
| 检索-触发 | 关键词命中 / 2-gram 重叠 / 向量分数 → 判定"这一轮需要回忆吗" | `qqbot/recall.py:judge/_local_judge/_semantic_hit` |
| 检索-召回 | 关键词(300 分制) → 重要记忆 → 长期记忆 → 本地向量 → 聊天记录兜底 → LLM 归纳 | `qqbot/recall.py` + `memstore/local.py:recall_*` |
| 检索-多路径 | 【回忆】强制回忆、跨身份一致性回忆、剧情时间窗检索 | `recall.force_recall/consistent_recall/list_important_by_time` |
| 注入 | 常驻记忆（importance≥9，≥3 条）+ 【回忆结果】块进 system prompt | `qqbot/bot_core.py`（10 块固定顺序） |
| 存储 | 每身份一个 SQLite（`data/personas/{角色}/memory.db`）+ WAL + 本地 fastembed 向量 | `qqbot/memstore/local.py` |

**一句话结论**：结构是对的（三层记忆 + 统一存储 seam + 可离线降级），
**短板在"检索排序"和"写入质量"，而不在架构**。

---

## 2. 能力评估

### 2.1 已经不错的

- `MemoryStore` seam 立得住：三后端（local / TencentDB / hybrid）职责清楚，失败语义统一（查空/写 False/不抛）。
- 零 LLM 的本地触发判定（关键词 + 2-gram）让"要不要回忆"不花钱、不阻塞。
- 剧情时间（`第N周周X时段`）独立成列，时间窗检索、Obsidian 时间线、按时间召回都能用上。
- 写入有上限（长期 200/身份，重要 100 条触发合并），不会无限膨胀。

### 2.2 关键弱点（按对体验的影响排序）

| # | 弱点 | 表现 | 现在 |
|---|------|------|------|
| 1 | **近度权重方向反了**：聊天记录兜底打分用 `min(10, n-pos)`，而条目是时间升序 → 最旧的条目得 10 分、最新的得 1 分 | "昨天说过的事"召回成第 1 周的旧事 | **已修**（`recall.py` 改为 `pos+1`） |
| 2 | **重叠项无上限**：`10 × 原始 2-gram 命中个数`，长记忆能堆到几百上千分，淹没关键词命中(300)与重要度 | 长篇流水账压过精确记忆 | **已修**（改为命中比例 ×10，封顶 10） |
| 3 | **合并是破坏性写**：读取被 `LIMIT 500` 截断、替换却整轨 `DELETE`，未回显条目直接消失，world 轨道被复制成 story | 记忆"越整理越少"、轨道串味 | **已修**（按 id 精确删除 + 全量读取 + 未采用条目保留） |
| 4 | **无写入去重**：同一件事重复提炼会重复入库，逼出合并（而合并有风险） | 重复条目、合并压力 | **已修**（`save_important`/`save_longterm` 同内容幂等，关键词合并） |
| 5 | **三路召回各自为政**：关键词/向量/聊天记录分别返回，最后拼接；没有统一打分与去重（hybrid 还只给远端 0 条配额） | 召回结果不成体系，"为什么想起这条"不可解释 | **已修**（统一 rerank + 证据合并 + 精度守门 + detail 可解释） |
| 6 | **重要度不参与生命周期**：`importance`/`affect_delta` 写进去后不再变化，没有"被想起就越重要"的强化 | 陈旧、无用的记忆长期占据注入位 | **已修**（召回强化 importance+1 / recall_count；不做删除式衰减） |
| 7 | **剧情时间只用于过滤**：检索排序里没有"越接近当前剧情时间越优先"的权重 | 第 1 周和第 5 周的事同权重 | **已修**（时间接近度作为"更近加分"，不惩罚旧记忆） |
| 8 | **常驻注入没有预算**：importance≥9 的记忆无条件进 prompt，与人设/世界书抢 token | prompt 里"记忆压人设" | **已修**（块级 + 行级预算，常驻记忆单独限额） |
| 9 | **向量索引成本高**：任写一次记忆就让所有身份索引失效 + 全量重嵌入；模型懒加载无锁、首次要联网下载 | 首轮卡顿、偶发慢 | **已修**（内容哈希增量嵌入 + 加载加锁 + 仅缓存时预热 + stats 零副作用） |
| 10 | **没有度量**：改了检索/提炼没有回归标准，好坏只能凭感觉 | "不知道该从哪里入手"的根因 | **已修**（31 条查询回归集 + 真实查询集评测入口） |

---

## 3. 本次建立的"度量基线"（改之前先能量出来）

### 3.1 检索质量回归集（已扩到 31 条查询）

```bash
python -m unittest tests.test_memory_quality -v
```

- 15 条重要记忆 + 3 条长期记忆 + 长干扰项；**31 条查询**，分 6 类：
  关键词直命中 / 改写 / 长期记忆 / 抗干扰 / 剧情时间窗 / 多主题；
- 当前基线：**Hit@1 = 0.94，Hit@3 = 1.00，MRR = 0.97**（关键词路径，向量关闭）——
  且度量走的是机器人**真正的统一重排路径**（不是单独的打分函数）；
- 同时锁住：长干扰项不得压过精确记忆、时间窗查询回到当天、近度方向、
  写入去重、合并保留、重叠项有界 + 长度归一。

### 3.2 记忆库体检（只读）

```bash
python scripts/memory_eval.py                  # 当前人格
python scripts/memory_eval.py --query "过山车"  # 检索冒烟（默认不打印正文）
python scripts/memory_eval.py --show-samples    # 需要看重复样例时才加
```

输出规模、**重复度**、**关键词覆盖率**、**剧情时间覆盖率**、长度分布、合并阈值压力、向量重建成本。
本机当前人格实测：重要记忆 21 条（story 20 / world 1），重复组 0，关键词覆盖 20/21，
剧情时间覆盖 20/21，平均 57 字，距合并阈值还有 80 条 —— 属于"健康但很薄"的状态
（记忆总量少 → 检索质量好坏暂时看不出差别，正是需要先补度量的阶段）。

### 3.3 用你**真实**的查询评测（新增）

把"你真实问过、且期望它想起某件事"的句子写成 JSON（不必写代码）：

```json
[
  {"query": "那段视频还在吗", "expect": "拍了一段视频"},
  {"query": "上周三晚上我们在做什么", "expect": "戚风蛋糕", "identity": "主人"}
]
```

```bash
python scripts/memory_eval.py --queries my_queries.json --top 3
# Hit@1 / Hit@3 / MRR + 未命中清单（走机器人真正的召回重排；不写库、不强化）
python scripts/memory_eval.py --queries my_queries.json --vector   # 需要时再启用语义检索
```

这是把"改动是否更好"从主观变成数字的关键一步：**先写 20~30 条真实查询，
再动任何检索参数**。

---

## 4. 已实施的改进（本轮全部完成）

### 第 1 步｜度量（已完成）
- 回归集扩到 **31 条查询 / 6 类**，度量走真实重排路径：`tests/test_memory_quality.py`；
- 新增**真实查询集**评测入口：`scripts/memory_eval.py --queries my_queries.json`。

### 第 2 步｜统一召回打分 + 证据合并 + 精度守门（已完成）
`qqbot/recall.py` 新增 `RecallHit / gather_candidates / _merge_hits / rerank / recall_block`：

```
score = 0.5·来源(重要>长期>向量>记录)
      + 3.0·min(3, 关键词精确命中)
      + 1.0·向量余弦
      + 0.8·max(0, 剧情时间接近度 − 0.4)      ← 只奖励"更近"，不惩罚旧记忆
      + 0.5·importance/10 + 0.5·|affect_delta|/10
      + 0.3·命中比例 × 长度归一(BM25 风格)
      + 0.2·召回强化 + 1.0·聊天记录自带分
```

- **证据合并**：同一件事被关键词 + 向量同时命中 → 合并成一条（两路一致），
  而不是两条互相挤占名额（旧实现会让"关键词先命中"直接短路返回）；
- **语义精度守门**：向量引擎给出有信心的命中时，其余记忆必须有真实字面重叠
  （`overlap ≥ 0.5`）才算数——防止标注错的关键词污染注入块（可用 `recall.vector_guard=false` 关闭）；
- **可解释**：每条命中带 `detail`（关键词/词重叠/语义分数/记录时间），方便排查"为什么想起这条"。

### 第 3 步｜记忆生命周期（已完成）
- 表结构新增 `recall_count` / `last_recalled_at`（`migrate_conn` 幂等补列，老库自动迁移）；
- **召回强化**：真正被想起来的记忆 `importance + 1`（上限 10）+ 计数 +1 → 常用记忆自然上浮；
  不删除、不衰减任何条目（`recall.reinforce`，`mark_recalled` 故意不触发向量索引失效）；
- **剧情时间接近度**：把"第N周"换算成距当前的周差，同周 1.0、差 1 周 0.5……

### 第 4 步｜提炼字段硬门槛（已完成）
`save_important` 在字段缺失时规则兜底：
- `story_time` ← 从内容前缀解析；
- `keywords` ← `textutil.derive_keywords()`（去时间戳 → 按标点/虚词切片段 → 长片段取尾部中心词，
  如"坐了过山车"→过山车、"拍了一段视频"→视频）；
- `importance` ← 由 `|affect_delta|` 推导。
> 旧数据的批量回填仍推荐 `python memory_ui.py keywordize`（LLM 生成，质量更高）。

### 第 5 步｜注入预算（已完成）
- 新增 `BotCore._fit_blocks / _fit_block`：按**块优先级**保留整块，超预算时在**行边界**截断，
  不再 `joined[:budget]` 盲截把记忆腰斩；世界书强调块仍优先保留；
- 常驻记忆块单独限额（`recall.persistent_max_chars`，默认 600），整行丢弃 + 省略标记。

### 第 6 步｜索引与性能（已完成）
- **增量向量索引**：向量按内容哈希缓存（`_vec_cache`），写一条记忆只嵌入新增的那条
  （旧实现每次缓存失效都全量重嵌入所有身份的所有文本）；
- **模型加载加锁** + `prewarm()` 后台预热（仅当模型已缓存，不会静默联网下载）+ `loaded()` 零副作用探测
  → 管理台 5 秒轮询 `stats()` 不再触发模型加载；
- **跨身份【回忆】限流**：`recall.force_identity_max`（默认 4），避免 O(身份 × 记忆) 热点。

### 新增配置项（`config.json` → `recall` / `memory.vector`）

| 键 | 默认 | 作用 |
|----|------|------|
| `recall.reinforce` | true | 召回后 importance+1 |
| `recall.force_identity_max` | 4 | 跨身份回忆的身份数上限 |
| `recall.vector_guard` | true | 语义置信时要求其余记忆有真实字面重叠 |
| `recall.vector_guard_overlap` | 0.5 | 上述"真实字面重叠"的阈值 |
| `recall.persistent_max_chars` | 600 | 常驻记忆块字符上限 |
| `memory.vector.prewarm` | true | 启动后台预热（模型已缓存时才加载） |

---

## 5. 下一步（还没做，按收益排序）

1. **把真实查询集用起来**：跑 `--queries`，把未命中的查询逐条归因（是没记住？关键词错？还是排序问题？）。
2. **向量真正参与**：现在向量只在关键词/重叠不足时补位；本地模型已缓存后可用 `--vector` 观察增益，
   再决定是否调高 `W_VECTOR`。
3. **旧数据回填**：`python memory_ui.py keywordize --batch 20` 给历史重要记忆补关键词与时间前缀，
   覆盖率上去后关键词路径会明显更准。
4. **矛盾/失效检测**：同一主题出现新旧两条事实时，旧条目标记 `superseded`（只降权不删除）。
5. **注入预算按 token 而非字符**（用 `textutil.estimate_tokens`），并把世界书/人设也纳入统一预算。

---

## 6. 验收清单

- [x] 破坏性写路径有硬保护（合并按 id 删除、未采用条目保留、同内容幂等）
- [x] 检索打分不再有"越旧越优先""越长越优先"两个反向偏置
- [x] 有可重复运行的检索质量度量（31 条查询 / 6 类，走真实重排路径）
- [x] 有可重复运行的记忆库体检（只读，含真实查询集评测入口）
- [x] 三路召回统一 rerank + 证据合并 + 精度守门（可解释）
- [x] importance 生命周期（召回强化）+ 剧情时间接近度
- [x] 提炼字段硬门槛（story_time / keywords / importance 兜底）
- [x] 注入预算（按块/按行裁剪，不腰斩记忆）
- [x] 增量向量索引 + 模型加锁/预热 + 跨身份限额
- [ ] 旧数据关键词/时间前缀回填（用现成 `keywordize`，建议本月跑一次）
- [ ] 矛盾/失效检测与 token 级预算
