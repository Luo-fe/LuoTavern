# -*- coding: utf-8 -*-
"""回忆 Agent：判断 → 重要记忆优先 → 聊天记录兜底 → LLM 归纳。

- judge：每条消息 LLM 判断是否在问过去的事（可配置关闭）
- 检索优先级：重要记忆（命中即用）→ 聊天记录（跳过已纠正条目）
- 权限：主人可搜全部身份，客人只能搜自己身份
- 失败安全降级：judge 失败 → 不回忆；归纳失败 → 原始命中块
"""
import random
import re
from dataclasses import dataclass, field

from . import relative_time, textutil

RECALL_JUDGE_PROMPT = """判断这条用户消息是否在询问"过去发生的事"或"以前聊过的话"（如回忆、之前、昨天、上次、上次聊到、在学校发生了什么）。
只输出 JSON，不要其他内容：
{"needs_recall": true|false, "window": null|"昨天"|"最近几天"|"上次", "keywords": []}
- window：消息提到的时间范围，没提到就 null
- keywords：可能用于检索的中文关键词（最多3个，没有就空数组）
- 日常寒暄、当前话题、未发生的事 → needs_recall=false"""

# 本地短路关键词：消息含其一 → 纯本地判断（零 LLM）；不含 → 不回忆。
# 宁全勿漏——误报只是多一次本地检索，漏报才损失回忆质量。
RECALL_KEYWORDS = (
    "昨天", "前天", "第二天", "上次", "上回", "之前", "以前", "曾经", "当年", "当初",
    "回忆", "记得", "记不记得", "还记得", "聊过", "说过", "提到过", "提过", "谈过",
    "那时候", "那天", "那次", "那一晚", "那晚", "小时候", "第一次", "刚才", "刚刚",
    "当时", "那一次", "前几天", "昨儿", "前阵子",
    # 流动性时间关键词
    "今天", "今日", "昨日", "大前天", "昨晚", "昨夜", "前晚", "前夜",
    "今早", "今晨", "昨早",
    "上周", "上星期", "上个星期", "上礼拜",
    "这周", "本周", "这礼拜", "这个星期", "这星期",
)

# 显式剧情时间戳（"第一周周一上午"）也是回忆触发词
STORY_STAMP_RECALL_RE = re.compile(r"第[一二三四五六七八九十百\d零]+周")

# 关键词 → 检索时间窗（LLM judge 的 window 字段的本地近似）。
# 只映射明确时间词；其余模糊词（之前/以前/那次/记得…）→ 默认窗（含今天+近 7 天），
# 与 LLM judge 的 window=null → default 行为一致。
RECALL_WINDOW_WORDS = {
    "昨天": "昨天", "昨儿": "昨天",
    "前天": "前天", "前几天": "前天", "前阵子": "最近几天",
    "上回": "上次", "上次": "上次",
}

RECALL_SYNTHESIZE_PROMPT = """你是一个角色扮演助手。以下是对方问"过去的事"时检索到的聊天记录片段。
请整理成一段 250 字以内、口语化的回忆（用第一人称，像真的想起来一样），不要逐条复述，不要提"检索/记录/片段"等词。
如果片段不足以回答，就如实说记不清了。

{context}"""

@dataclass
class RecallHit:
    """统一召回候选：四路（重要记忆/长期记忆/向量/聊天记录）同场竞争。"""
    text: str
    source: str                 # important / longterm / vector / chatlog
    score: float = 0.0          # 重排后的总分
    delta: int = 0              # affect_delta（情绪强度）
    importance: int = 0
    story_time: str = ""        # 剧情时间戳（"第3周周一晚上"）
    keywords: list = field(default_factory=list)   # 命中的关键词
    overlap: float = 0.0        # 2-gram 命中比例
    vector: float = 0.0         # 向量余弦（0~1）
    recalled: int = 0           # 历史被召回次数（强化信号）
    surface: bool = False       # 是否有"字面命中"（关键词或词重叠）
    kind: str = ""              # 向量来源的 important/longterm
    stamp: str = ""             # 聊天记录条目时间戳
    speaker: str = ""           # 聊天记录说话人
    base: float = 0.0           # 来源自带分（聊天记录近度等，已归一化）
    detail: str = ""            # 可解释：为什么被选中

    def line(self, max_chars=300):
        """注入用的单行文本（与旧实现的格式保持一致）。"""
        if self.source == "chatlog":
            return "%s %s: %s" % (self.stamp, self.speaker,
                                  self.text.replace("\n", " ")[:max_chars])
        if self.source == "important" or self.kind == "important":
            return "- %s（好感度%s%d）" % (
                self.text, "+" if self.delta > 0 else "", self.delta)
        return "- %s" % self.text


@dataclass
class JudgeResult:
    needs_recall: bool = False
    window: str = "default"
    keywords: list = field(default_factory=list)


class RecallAgent:
    def __init__(self, bot):
        self.bot = bot
        cfg = bot.config.get("recall", {})
        self.enable = cfg.get("enable", True)
        self.judge_enabled = cfg.get("judge", True)
        self.judge_shortcut = cfg.get("judge_shortcut", True)  # 本地关键词短路：无关键词不调 LLM
        self.synthesize_enabled = cfg.get("synthesize", True)
        self.default_days = int(cfg.get("default_days", 7))
        self.max_days = int(cfg.get("max_days", 30))
        self.top_k = int(cfg.get("top_k", 8))
        self.explicit_top_k = int(cfg.get("explicit_top_k", 15))
        self.max_hit_chars = int(cfg.get("max_hit_chars", 300))
        self.cross_identity = bool(cfg.get("cross_identity", True))
        self.mention_top_k = int(cfg.get("mention_top_k", 3))
        self.rumination_prob = float(cfg.get("rumination_prob", 0.1))
        self.consistent_top = max(1, int(cfg.get("consistent_top", 2)))
        self.reinforce = bool(cfg.get("reinforce", True))
        self.force_identity_max = max(1, int(cfg.get("force_identity_max", 4)))
        self.vector_guard = bool(cfg.get("vector_guard", True))
        self.vector_guard_overlap = float(cfg.get("vector_guard_overlap", 0.5))
        self.semantic_min_score = float(
            bot.config.get("memory", {}).get("vector", {}).get("trigger_min_score", 0.45))

    # ---------- 判断 ----------
    def judge(self, text, identity=None):
        if not self.enable or not self.judge_enabled:
            return JudgeResult(needs_recall=False)
        if self.judge_shortcut:
            return self._local_judge(text, identity)
        data = self.bot.llm.chat_json(
            [
                {"role": "system", "content": RECALL_JUDGE_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.1,
            call_type="recall",
        )
        if not isinstance(data, dict):
            return JudgeResult(needs_recall=False)
        return JudgeResult(
            needs_recall=bool(data.get("needs_recall")),
            window=str(data.get("window") or "default") if data.get("window") else "default",
            keywords=[str(k) for k in (data.get("keywords") or [])][:3],
        )

    def _local_judge(self, text, identity=None):
        """本地替代 LLM judge：含回忆关键词 或 命中记忆关键词 或 语义相似度达标 → 需要回忆（零 LLM）。

        window 按词表映射；keywords 留空 → maybe_recall 用原文整句做 ngrams 检索
        （交集计分，可命中核心词）。关键词索引 + 语义相似度让任意措辞都能触发回忆，
        不需要预先枚举同义词（这才是通用机制，关键词只是快速通道）。
        """
        if not (any(k in text for k in RECALL_KEYWORDS) or STORY_STAMP_RECALL_RE.search(text)):
            if not (identity and self.bot.memory is not None
                    and (self.bot.memory.has_memory_trigger(identity, text)
                         or self._semantic_hit(identity, text))):
                return JudgeResult(needs_recall=False)  # 非回忆消息：零 LLM 调用
        window = None
        for kw, win in RECALL_WINDOW_WORDS.items():
            if kw in text:
                window = win
                break
        return JudgeResult(needs_recall=True, window=window, keywords=[])

    def _semantic_hit(self, identity, text):
        """语义触发：任意措辞与记忆的向量相似度 ≥ 阈值即触发回忆（本地计算，零 token）。"""
        if len(text.strip()) < 2:
            return False
        return self.bot.memory.vector_top_score(identity, text) >= self.semantic_min_score

    # ---------- 检索 ----------
    def search_important(self, identity, query, top):
        """重要记忆检索（含好感度变动）。"""
        return self.bot.memory.recall_important(identity, query, top)

    def search_memories(self, identity, query, top=3):
        """长期记忆检索：【记住X】明确灌输的事实，优先级高于聊天记录。"""
        return self.bot.memory.recall_memories(identity, query, top)

    def vector_recall(self, identity, query, top=None):
        """本地向量语义检索（P2）；模型不可用时返回 []，不影响其他路径。"""
        if not self.enable or not self.bot.memory:
            return []
        return self.bot.memory.vector_recall(identity, query, top=top)

    def consistent_recall(self, identity, query, top=None):
        """一致性评分（关键词 ∩ 向量高分）；向量不可用时交集为空。"""
        if not self.enable or not self.bot.memory:
            return []
        return self.bot.memory.recall_consistent(identity, query, top=top or self.consistent_top)

    def search_logs(self, identity, query, window="default", top=8, include_today=False,
                    time_prefixes=None, time_windows=None, exclude_text=None):
        """聊天记录检索：二元组重叠×10 + 近度×3 计分；跳过已纠正条目。

        time_windows 非空（"今天/昨天/上周X/第N周周X时段"解析结果）→ 按条目剧情时间戳
        前缀 + 可选时段过滤（内容正文不含"今天"字样，按词检索必零命中），近度用全局条目序号；
        time_prefixes 为旧参数兼容（等价于 [(p, None) for p in time_prefixes]）。
        exclude_text 用于跳过当前用户消息本身（防自命中）。
        剧情时间线无真实日期窗，其余场景一律检索全部条目。
        """
        entries = self.bot.chatlog_store.read_entries(identity)
        if time_windows is None and time_prefixes:
            time_windows = [(p, None) for p in time_prefixes]
        if time_windows:
            hits = []
            for pos, e in enumerate(entries):
                if self._corrected(identity, e):
                    continue
                if exclude_text and e[3].strip() == exclude_text.strip():
                    continue
                if not relative_time.match_stamp(e[1], time_windows):
                    continue
                recency = min(10, pos + 1)   # entries 为时间升序：pos 越大越新
                hits.append((3 * recency, e[1], e[2], e[3], e[0]))
            hits.sort(key=lambda x: (-x[0], x[4]))
            return hits[:top]
        q_grams = textutil.ngrams(query)
        hits = []
        for pos, e in enumerate(entries):
            if self._corrected(identity, e):
                continue
            content = e[3]
            overlap = len(q_grams & textutil.ngrams(content)) if q_grams else 1
            if overlap <= 0:
                continue
            recency = min(10, pos + 1)  # entries 为时间升序：pos 越大越新
            hits.append((10 * overlap + 3 * recency, e[1], e[2], e[3], e[0]))
        hits.sort(key=lambda x: (-x[0], x[4]))
        return hits[:top]

    # ---------- 统一召回重排（四路候选 → 同一套打分 → 去重 → 可解释） ----------
    # 权重集中在这里：调整召回偏好只改这几个数，度量见 tests/test_memory_quality.py
    W_SOURCE = {"important": 0.5, "longterm": 0.3, "vector": 0.1, "chatlog": 0.0}
    W_KEYWORD = 3.0      # 每个精确命中的关键词
    W_KEYWORD_MAX = 3    # 最多计 3 个，避免关键词多的条目碾压
    W_VECTOR = 1.0       # 向量余弦（0~1）
    W_TIME = 0.8         # 剧情时间接近度（同周=1，越久越小）
    W_IMPORTANCE = 0.5   # importance/10
    W_DELTA = 0.5        # |affect_delta|/10
    W_OVERLAP = 0.3      # 2-gram 命中比例（按记忆长度做归一，见 _length_norm）
    W_RECALL = 0.2       # 召回强化：被想起过的记忆上浮
    W_CHATLOG = 1.0      # 聊天记录自带分（近度+重叠，归一化后）
    TIME_NEUTRAL = 0.4   # 无时间戳=中性；时间项只给"更近"加分，不惩罚旧记忆
    LEN_FREE = 120       # 该长度内不做长度归一（记忆正常长度约 40~120 字）

    @staticmethod
    def _length_norm(n):
        """BM25 风格的文档长度归一：长转录块的字面重叠不该按字数线性放大。

        120 字以内不折扣；更长时按 120/len 衰减，下限 0.35。
        """
        n = max(1, int(n))
        if n <= RecallAgent.LEN_FREE:
            return 1.0
        return max(0.35, RecallAgent.LEN_FREE / float(n))

    def _current_week(self):
        try:
            return int(self.bot.timeline.clock.get("week") or 1)
        except Exception:
            return 1

    def _time_proximity(self, story_time):
        """剧情时间接近度：同周 1.0，差 1 周 0.5，差 2 周 0.33…；无时间戳取中性 0.4。"""
        week = textutil.parse_story_week(story_time)
        if week is None:
            return 0.4
        return 1.0 / (1.0 + max(0, self._current_week() - week))

    def _kw_hits(self, keywords, query):
        try:
            return [k for k in (keywords or []) if self.bot.memory._kw_in_text(k, query)]
        except Exception:
            return []

    def gather_candidates(self, identity, query, text="", window="default",
                          include_today=False, time_windows=None):
        """四路召回原始候选（不做跨源比较；重排交给 rerank）。

        - 重要记忆：字面命中（关键词或词重叠）的全部候选（带生命周期字段）
        - 长期记忆：【记住X】的事实
        - 向量：语义命中（词面不重叠也能召回）
        - 聊天记录：原文兜底
        """
        hits = []
        mem = self.bot.memory
        if mem is None:
            return hits
        q_grams = textutil.ngrams(query)
        try:
            important = mem.important_candidates(identity)
        except Exception:
            important = []
        for _id, content, delta, imp, kws, story, _track, rc, _last in important:
            exact = self._kw_hits(kws, query)
            ratio = textutil.overlap_ratio(q_grams, content)
            if not exact and ratio <= 0:
                continue
            hits.append(RecallHit(
                text=content, source="important", delta=int(delta or 0),
                importance=int(imp or 0), story_time=story or "", keywords=exact,
                overlap=ratio, recalled=int(rc or 0), surface=True,
                detail=("关键词%s" % "、".join(exact)) if exact else "词重叠%.2f" % ratio))
        try:
            longterm = mem.longterm_candidates(identity)
        except Exception:
            longterm = []
        for _id, content, kws in longterm:
            exact = self._kw_hits(kws, query)
            ratio = textutil.overlap_ratio(q_grams, content)
            if not exact and ratio <= 0:
                continue
            hits.append(RecallHit(
                text=content, source="longterm", keywords=exact, overlap=ratio,
                surface=True,
                detail=("关键词%s" % "、".join(exact)) if exact else "词重叠%.2f" % ratio))
        for kind, content, delta, imp, score in self.vector_recall(identity, query):
            hits.append(RecallHit(
                text=content, source="vector", delta=int(delta or 0),
                importance=int(imp or 0), vector=float(score or 0.0), kind=kind,
                detail="语义 %.2f" % float(score or 0.0)))
        top = self.explicit_top_k if include_today else self.top_k
        for sc, stamp, speaker, content, _idx in self.search_logs(
                identity, query, window, top, include_today=include_today,
                time_windows=time_windows, exclude_text=text):
            hits.append(RecallHit(
                text=content, source="chatlog", stamp=stamp, speaker=speaker,
                overlap=textutil.overlap_ratio(q_grams, content),
                base=min(1.0, float(sc or 0) / 60.0), surface=True,
                detail="记录 %s" % stamp))
        return hits

    def _score_hit(self, h):
        proximity = self._time_proximity(h.story_time)
        return (
            self.W_SOURCE.get(h.source, 0.0)
            + self.W_KEYWORD * min(self.W_KEYWORD_MAX, len(h.keywords))
            + self.W_VECTOR * max(0.0, min(1.0, h.vector))
            # 时间项只奖励"比中性更近"的记忆：老记忆不会因为"有明确时间戳"
            # 反而输给没有时间戳的条目
            + self.W_TIME * max(0.0, proximity - self.TIME_NEUTRAL)
            + self.W_IMPORTANCE * min(10, max(0, h.importance)) / 10.0
            + self.W_DELTA * min(10, abs(h.delta)) / 10.0
            + self.W_OVERLAP * h.overlap * self._length_norm(len(h.text))
            + self.W_RECALL * min(5, max(0, h.recalled)) / 5.0
            + self.W_CHATLOG * h.base
        )

    @staticmethod
    def _near_dup(a, b):
        """近似重复：去标点后相等/包含，或 2-gram Jaccard ≥ 0.85（同一件事的不同措辞）。"""
        ka, kb = textutil.dedup_key(a), textutil.dedup_key(b)
        if not ka or not kb:
            return False
        if ka == kb or ka in kb or kb in ka:
            return True
        ga, gb = textutil.ngrams(ka), textutil.ngrams(kb)
        if not ga or not gb:
            return False
        return len(ga & gb) / float(len(ga | gb)) >= 0.85

    _SOURCE_RANK = {"important": 0, "longterm": 1, "vector": 2, "chatlog": 3}

    def _merge_hits(self, hits):
        """同一件事被多路召回时合并证据（关键词并集 + 向量/情绪/重要度取最大）。

        这正是统一重排的价值：关键词命中的那条如果同时被向量命中，
        就应该以"两路一致"的形态参与竞争，而不是当成两条互相挤占名额。
        """
        out = []
        for h in hits:
            target = next((k for k in out if self._near_dup(h.text, k.text)), None)
            if target is None:
                h.keywords = list(h.keywords or [])
                out.append(h)
                continue
            for kw in (h.keywords or []):
                if kw not in target.keywords:
                    target.keywords.append(kw)
            target.vector = max(target.vector, h.vector)
            target.importance = max(target.importance, h.importance)
            if abs(h.delta) > abs(target.delta):
                target.delta = h.delta
            target.recalled = max(target.recalled, h.recalled)
            target.overlap = max(target.overlap, h.overlap)
            target.base = max(target.base, h.base)
            target.surface = target.surface or h.surface
            if self._SOURCE_RANK.get(h.source, 9) < self._SOURCE_RANK.get(target.source, 9):
                target.source = h.source
            if h.detail and h.detail not in target.detail:
                target.detail = "%s + %s" % (target.detail, h.detail) if target.detail else h.detail
            if not target.story_time:
                target.story_time = h.story_time
        return out

    def rerank(self, hits, limit=None):
        """合并证据 → 统一打分 → 语义置信时的精度守门 → 截断。"""
        merged = self._merge_hits(hits)
        for h in merged:
            h.score = round(self._score_hit(h), 4)
        merged.sort(key=lambda x: -x.score)
        if self.vector_guard:
            # 向量引擎给出了有信心的命中时，其余记忆必须有真实字面重叠才算数：
            # 光靠（可能标注错的）关键词命中不足以进入注入块
            confident = any(h.vector >= self.semantic_min_score for h in merged)
            if confident:
                merged = [h for h in merged
                          if h.vector >= self.semantic_min_score
                          or h.overlap >= self.vector_guard_overlap]
        return merged[:int(limit)] if limit else merged

    def format_recall_hits(self, hits):
        return "\n".join(h.line(self.max_hit_chars) for h in hits)

    def recall_block(self, query, hits):
        """重排结果 → 注入块 (记忆块, 聊天记录块)。

        标题与旧实现语义一致：字面命中的重要记忆 →【重要记忆】；
        只有长期记忆 →【长期记忆】；纯语义/记录命中 →【回忆结果】。
        """
        if not hits:
            return None, None
        mem_hits = [h for h in hits if h.source != "chatlog"]
        log_hits = [h for h in hits if h.source == "chatlog"]
        mem_block = None
        if mem_hits:
            if any(h.source == "important" and h.surface for h in mem_hits):
                title = "【重要记忆】"
            elif any(h.source == "longterm" and h.surface for h in mem_hits):
                title = "【长期记忆】"
            else:
                title = "【回忆结果】"
            mem_block = "%s\n%s" % (title, self.format_recall_hits(mem_hits))
        log_block = None
        if log_hits:
            # 短命中直引原文，长命中走一次 LLM 归纳（沿用旧逻辑）
            raw = [(h.base * 60.0, h.stamp, h.speaker, h.text, 0) for h in log_hits]
            log_block = self._block(query, raw, "回忆结果")
        return mem_block, log_block

    def reranked_recall(self, identity, query, text="", window="default",
                        include_today=False, time_windows=None, limit=None):
        """四路召回 → 重排 → 注入块；命中即强化（生命周期）。"""
        hits = self.gather_candidates(identity, query, text=text, window=window,
                                      include_today=include_today,
                                      time_windows=time_windows)
        ranked = self.rerank(hits, limit or self.top_k)
        if not ranked:
            return None, None
        self._reinforce(identity, ranked)
        return self.recall_block(query, ranked)

    def _reinforce(self, identity, hits):
        """召回强化：把真正被想起来的记忆 importance+1（不删除、不衰减任何条目）。"""
        if not getattr(self, "reinforce", True):
            return
        texts = [h.text for h in hits if h.source in ("important", "longterm")]
        if not texts:
            return
        try:
            self.bot.memory.mark_recalled(identity, texts)
        except Exception:
            pass

    def _corrected(self, identity, entry):
        """条目是否已被【纠正】作废：序号或稳定 id 命中即算（重排后仍准确）。"""
        try:
            return self.bot.correct.is_corrected(identity, entry[0], entry)
        except Exception:
            return False

    # ---------- 归纳 ----------
    def synthesize(self, query, hits):
        if not self.synthesize_enabled:
            return None
        ctx = "\n".join(
            "[%s %s] %s: %s" % (h[1], h[2], h[2], h[3].replace("\n", " ")[:self.max_hit_chars])
            for h in hits[:12]
        )
        try:
            return self.bot.llm.chat(
                [
                    {"role": "system", "content": RECALL_SYNTHESIZE_PROMPT.replace("{context}", ctx)},
                    {"role": "user", "content": "帮我回忆一下：%s" % query},
                ],
                temperature=0.6,
                call_type="recall",
            ).strip()
        except Exception:
            return None

    def format_hits(self, hits):
        # 不带真实时间戳（HH:MM 是系统时间，与剧情时间线冲突，注入上下文会混淆）
        lines = []
        for h in hits[:8]:
            lines.append("%s: %s" % (h[2], h[3].replace("\n", " ")[:self.max_hit_chars]))
        return "\n".join(lines)

    # ---------- 主入口 ----------
    def _block(self, query, hits, label):
        # 命中条数少且每条都短 → 直引原文，省一次 LLM 归纳调用
        if hits and len(hits) <= 2 and all(len(h[3]) <= 60 for h in hits):
            return "【%s】\n%s" % (label, self.format_hits(hits))
        summary = self.synthesize(query, hits)
        body = summary or self.format_hits(hits)
        return "【%s】\n%s" % (label, body)

    def _named_identity(self, query):
        """query 提到身份名（≥2字）→ 返回最长匹配的身份；否则 None。

        回忆"梦中的哥哥"这类按身份名的检索：目标身份的聊天记录正文不含身份名
        （内容是剧情对话），按词重叠必零命中——须按身份定位，直接检索该身份的
        全部记录（近度排序）。最长匹配："梦中的哥哥"含"哥哥"，须整体先命中。"""
        candidates = set(self.bot.identity.all_identities())
        for name in self.bot.chatlog_store.identity_dirs():
            if len(name) >= 2:
                candidates.add(name)
        best = None
        for name in candidates:
            if name in query and (best is None or len(name) > len(best)):
                best = name
        return best

    def _recall_by_identity(self, target, query, top):
        """按身份名回忆：该身份的重要记忆（时间新在前）+ 聊天记录（近度排序，跳过已纠正）。
        返回 (重要记忆块或None, 日志块或None)。"""
        time_windows = self._time_windows(query)
        if time_windows:
            imp = self.bot.memory.list_important_by_time(target, time_windows)
        else:
            imp = [(c, d) for c, d, _i in self.bot.memory.list_important(target, 100)]
        if imp:
            lines = ["【重要记忆】"]
            for content, delta in imp[:6]:
                lines.append("- %s（好感度%s%d）" % (content, "+" if delta > 0 else "", delta))
            return "\n".join(lines), None
        entries = self.bot.chatlog_store.read_entries(target)
        n = len(entries)
        hits = []
        for pos, e in enumerate(entries):
            if self._corrected(target, e):
                continue
            if time_windows and not relative_time.match_stamp(e[1], time_windows):
                continue
            hits.append((3 * min(10, n - pos), e[1], e[2], e[3], e[0]))
        hits.sort(key=lambda x: (-x[0], x[4]))
        hits = hits[:top]
        if not hits:
            return None, None
        return None, self._block(query, hits, "回忆结果")

    def dream_memory_block(self, day_prefix, top=8):
        """【青柠好好睡了一觉】换天时：前一深夜梦境窗口（梦中的哥哥身份）的记忆块。

        只灌注"前一天晚上"（梦境窗口=深夜时段）和梦中的哥哥的记忆：
        - 梦中的哥哥不是每天都有——该时段无记录/无重要记忆 → 返回 None（不灌注）
        - 重要记忆按 story_time 前缀过滤；聊天记录按时间戳前缀过滤（跳过已纠正）
        返回注入块（【昨晚的梦】）或 None。
        """
        target = getattr(self.bot, "dream_name", None)
        if not target or not day_prefix:
            return None
        prefix = day_prefix + "深夜"
        imp = self.bot.memory.list_important_story(target, prefix)
        entries = self.bot.chatlog_store.read_entries(target)
        n = len(entries)
        hits = []
        for pos, e in enumerate(entries):
            if self._corrected(target, e):
                continue
            if not e[1].startswith(prefix):
                continue
            hits.append((3 * min(10, n - pos), e[1], e[2], e[3], e[0]))
        hits.sort(key=lambda x: (-x[0], x[4]))
        hits = hits[:top]
        if not imp and not hits:
            return None
        lines = ["【昨晚的梦】"]
        for content, delta in imp[:4]:
            lines.append("- %s（好感度%s%d）" % (content, "+" if delta > 0 else "", delta))
        for h in hits[:8]:
            lines.append("%s: %s" % (h[2], h[3].replace("\n", " ")[:self.max_hit_chars]))
        return "\n".join(lines)

    def maybe_recall(self, user_id, identity, text):
        """返回 (重要记忆块或None, 日志块或None)；不触发返回 (None, None)。"""
        if not self.enable:
            return None, None
        jr = self.judge(text, identity)
        if not jr.needs_recall:
            return None, None
        query = " ".join(jr.keywords) if jr.keywords else text
        # 按身份名定位（"梦中的哥哥"）：主人可回忆任意身份，客人只能回忆自己
        named = self._named_identity(query)
        if named:
            if identity == self.bot.identity.owner_name or named == identity:
                return self._recall_by_identity(named, query, self.explicit_top_k)
            named = None
        time_windows = self._time_windows(query)
        if time_windows:
            # 流动性时间关键词：按日期前缀 + 时段过滤重要记忆，聊天记录兜底
            imp = self.bot.memory.list_important_by_time(identity, time_windows)
            if imp:
                lines = ["【重要记忆】"]
                for content, delta in imp[:6]:
                    lines.append("- %s（好感度%s%d）" % (content, "+" if delta > 0 else "", delta))
                return "\n".join(lines), None
            hits = self.search_logs(identity, query, "default", self.top_k,
                                    include_today=True, time_windows=time_windows,
                                    exclude_text=text)
            if not hits:
                return None, None
            return None, self._block(query, hits, "回忆结果")
        # ① 四路统一召回 + 重排：关键词/长期/向量/聊天记录同场竞争，
        #    而不是"哪一路先命中就返回哪一路"（旧实现会让弱关键词压过强语义）
        mem_block, log_block = self.reranked_recall(
            identity, query, text=text, window=jr.window,
            include_today=False, limit=self.top_k)
        if mem_block or log_block:
            return mem_block, log_block
        return None, None

    def _time_windows(self, query):
        """query 含流动性时间关键词 → [(剧情日前缀, 时段集合|None)]；否则 []。

        时间戳记在条目/记忆的时间戳字段（"第1周周日…"），内容正文不含
        "今天/昨天"字样——纯时间词查询按词检索必零命中，须按当前时钟解析成具体剧情日。
        """
        return relative_time.resolve_time_windows(query, self.bot.timeline.clock)

    def force_recall(self, user_id, identity, query):
        """【回忆xxx】：跳过 judge，含今天；主人合并全部身份。"""
        if not self.enable:
            return None, None
        query = (query or "").strip() or "最近的事"
        owner = self.bot.identity.is_owner(user_id)
        # 按身份名定位（"梦中的哥哥"）：主人可回忆任意身份，客人只能回忆自己
        named = self._named_identity(query)
        if named and (owner or named == identity):
            return self._recall_by_identity(named, query, self.explicit_top_k)
        # ① 重要记忆（主人搜全部身份；身份集 = 聊过天的 + 身份映射里的，记忆可能先于聊天存在）
        if owner:
            identities = sorted(set(self.bot.chatlog_store.identity_dirs()) | set(self.bot.identity.all_identities()))
            # 身份数上限：跨身份召回是 O(身份 × 记忆) 的热点（向量索引还会按身份重建）
            if len(identities) > self.force_identity_max:
                active = self.bot.chatlog_store.identity_dirs()
                rest = [i for i in identities if i not in active][:1]
                identities = (list(active)[:self.force_identity_max - len(rest)] + rest)[:self.force_identity_max]
        else:
            identities = [identity]
        # 流动性时间窗："今天/昨天/上周X/第N周周X时段" → 按解析出的日期前缀+时段匹配
        time_windows = self._time_windows(query)
        if time_windows:
            imp = []
            for ident in identities:
                imp += self.bot.memory.list_important_by_time(ident, time_windows)
            if imp:
                lines = ["【重要记忆】"]
                for content, delta in imp[:6]:
                    lines.append("- %s（好感度%s%d）" % (content, "+" if delta > 0 else "", delta))
                return "\n".join(lines), None
            hits = []
            for ident in identities:
                hits += self.search_logs(ident, query, "default", self.explicit_top_k,
                                         include_today=True, time_windows=time_windows)
            hits.sort(key=lambda x: (-x[0], x[4]))
            hits = hits[:self.explicit_top_k]
            if not hits:
                return None, None
            return None, self._block(query, hits, "回忆结果")
        # ② 四路统一召回 + 重排（跨身份合并后一起竞争，而不是"重要记忆先返回"）
        hits = []
        for ident in identities:
            hits += self.gather_candidates(
                ident, query, text="", window="default", include_today=True)
        ranked = self.rerank(hits, self.explicit_top_k)
        if not ranked:
            return None, None
        # 强化只对当前身份生效（跨身份回忆不应改动别人的记忆权重）
        self._reinforce(identity, [h for h in ranked if h.source != "chatlog"])
        return self.recall_block(query, ranked)

    # ---------- 跨身份记忆 / 内心活动 ----------
    def cross_memory(self, identity, text):
        """消息中提到其他角色 → 注入该角色的重要记忆。

        扫描身份映射中所有身份名（排除当前身份），名字（≥2 字）出现在消息里即检索其重要记忆。
        返回注入块或 None。
        """
        if not self.enable or not self.cross_identity or not text:
            return None
        blocks = []
        for name in self.bot.identity.all_identities():
            if name == identity or len(name) < 2 or name not in text:
                continue
            imp = self.search_important(name, text, self.mention_top_k)
            if not imp:
                continue
            lines = ["【提到 %s 的记忆】" % name]
            for content, delta in imp:
                lines.append("- %s（好感度%s%d）" % (content, "+" if delta > 0 else "", delta))
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks) if blocks else None

    def rumination(self, identity):
        """非主人身份聊天时，小概率把主人的重要记忆作为内心活动注入。"""
        if not self.enable or self.rumination_prob <= 0:
            return None
        owner = self.bot.identity.owner_name
        if identity == owner or random.random() > self.rumination_prob:
            return None
        pool = self.bot.memory.list_important(owner, limit=20)
        if not pool:
            return None
        return "【%s的内心活动】（突然想起和%s的事：%s）" % (self.bot.bot_name, owner, random.choice(pool)[0])
