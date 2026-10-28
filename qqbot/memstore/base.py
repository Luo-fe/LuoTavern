# -*- coding: utf-8 -*-
"""记忆存储抽象接口（deep module 的 seam）。

所有记忆读写方（qqbot.recall / qqbot.sleep_ritual / qqbot.bot_core /
plugins.worldbook / memory_ui / qqbot.admin）都只穿过这一个接口；
后端（本地 SQLite / TencentDB-Agent-Memory / 混合）藏在适配器实现里。

接口 = 调用方必须知道的全部：方法签名 + 返回形态 + 失败语义（查询失败返回
空列表/None，写入失败返回 False，绝不抛异常中断聊天管线）。
"""
import abc
import json

from qqbot import textutil


class KeywordMixin:
    """关键词存取与命中的公共实现（本地/远端适配器共用）。"""

    @staticmethod
    def _dump_keywords(keywords):
        """关键词列表 → 存储用 JSON 字符串（去重、限长、最多 10 个）。

        兼容调用方直接传逗号分隔字符串（CLI/管理台表单），不再按字符拆分。
        """
        if isinstance(keywords, str):
            keywords = keywords.replace("，", ",").split(",")
        out = []
        for k in keywords or []:
            k = str(k).strip().strip("【】[]\"'“”")
            if not k or len(k) > 12 or k in out:
                continue
            out.append(k)
            if len(out) >= 10:
                break
        return json.dumps(out, ensure_ascii=False) if out else ""

    @staticmethod
    def _load_keywords(raw):
        """存储值 → 关键词列表；兼容 JSON 数组与逗号分隔文本。"""
        if not raw:
            return []
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(k).strip() for k in data if str(k).strip()][:10]
        except Exception:
            pass
        return [k.strip() for k in str(raw).replace("，", ",").split(",") if k.strip()][:10]

    @staticmethod
    def _kw_partial_score(keyword, text):
        """关键词与消息的宽松匹配分：精确包含 3 分；2-gram 交集 1 分/个（上限 2 分）。"""
        if not keyword or not text:
            return 0
        keyword = keyword.strip()
        text = text.strip()
        if keyword in text or (len(text) >= 2 and text in keyword):
            return 3
        kg = textutil.ngrams(keyword)
        tg = textutil.ngrams(text)
        if not kg or not tg:
            return 0
        return min(2, len(kg & tg))

    @staticmethod
    def _kw_in_text(keyword, text):
        """关键词命中判断（双向包含）。

        - 消息含关键词（"永远的小狗" → 消息里有"永远的小狗"）
        - 长关键词被消息中的短词反查命中（消息"小狗" → 关键词"永远的小狗"）
        短词须 ≥2 字，避免"狗/的"这类单字造成大量误触发。
        """
        if not keyword or not text:
            return False
        keyword = keyword.strip()
        text = text.strip()
        if keyword in text:
            return True
        if len(text) >= 2 and len(keyword) >= len(text) and text in keyword:
            return True
        return False


class MemoryStore(KeywordMixin, abc.ABC):
    """记忆存储接口。

    语义约定（所有适配器一致）：
    - identity 为身份名（非 QQ 号），None/空 → 各方法安全返回空值
    - content 统一截断 300 字；affect_delta ∈ [-100, 100]；importance ∈ [0, 10]
    - 检索方法在任何后端失败时返回 []（调用方自动回退下一优先级路径）
    """

    backend = "abstract"

    # ---------- 长期记忆（【记住X】显式灌输） ----------
    @abc.abstractmethod
    def save_longterm(self, identity, content, keywords=None):
        """写入一条长期记忆；返回是否成功。"""

    @abc.abstractmethod
    def delete_longterm(self, identity, keyword):
        """按关键词命中删除一条长期记忆；返回是否删到。"""

    @abc.abstractmethod
    def list_longterm(self, identity, limit=500):
        """长期记忆列表（新→旧），返回 [content]。"""

    @abc.abstractmethod
    def recall_longterm(self, identity, query, top=3):
        """长期记忆检索（词重叠）；返回 [content]。"""

    # ---------- 重要记忆（睡前仪式 LLM 提炼） ----------
    @abc.abstractmethod
    def save_important(self, identity, content, affect_delta=0, importance=5,
                       story_time=None, keywords=None, track="story"):
        """写入一条重要记忆；返回是否成功。"""

    @abc.abstractmethod
    def list_important(self, identity, limit=500):
        """[(content, affect_delta, importance)]（新→旧）。"""

    @abc.abstractmethod
    def list_important_ids(self, identity, limit=500, track=None):
        """带 id 版本（记忆合并对账用）：
        [(id, content, affect_delta, importance, keywords)]。
        track=None 取全部轨道；limit=None 表示不限条数（合并对账必须看到全量，
        截断读取 + 整轨删除 = 静默丢数据）。"""

    @abc.abstractmethod
    def list_important_by_track(self, identity, track, limit=10):
        """按轨道取重要记忆（world=世界书剧情 / story=角色故事）：
        [(content, affect_delta, importance)]。"""

    @abc.abstractmethod
    def list_important_story(self, identity, prefix, limit=100):
        """按剧情时间前缀过滤（如"第1周周日深夜"）：[(content, affect_delta)]。"""

    @abc.abstractmethod
    def list_important_by_time(self, identity, time_windows, limit=500):
        """按流动性时间窗过滤：[(content, affect_delta)]。"""

    @abc.abstractmethod
    def replace_important(self, identity, items, scope_ids=None):
        """替换重要记忆（合并去重用）；
        items=[(content, delta, importance[, keywords[, track]])]。
        scope_ids=None：删除该身份全部 story 轨道后写回（旧行为，兼容）；
        scope_ids=[id...]：只删除这些 id，其余条目原样保留。返回新条数。"""

    @abc.abstractmethod
    def track_counts(self, identity):
        """各轨道条数：{"story": n, "world": n}。"""

    # ---------- 好感度 ----------
    @abc.abstractmethod
    def get_affection(self, identity):
        """当前好感度；无记录返回 None。"""

    @abc.abstractmethod
    def set_affection(self, identity, score):
        pass

    # ---------- 检索（关键词 + 语义 + 一致性，深度藏在实现里） ----------
    @abc.abstractmethod
    def recall_important(self, identity, query, top=3):
        """重要记忆检索：[(content, affect_delta)]。"""

    @abc.abstractmethod
    def keyword_hits(self, identity, text):
        """消息命中记忆关键词的条目：
        [(kind, content, affect_delta, importance, hit_keywords)]，
        kind=important/memory。"""

    @abc.abstractmethod
    def has_keyword_hit(self, identity, text):
        pass

    @abc.abstractmethod
    def has_memory_trigger(self, identity, text):
        """本地记忆触发：关键词命中或正文 2-gram 重叠 ≥2。"""

    @abc.abstractmethod
    def content_overlap(self, identity, text):
        """消息与重要记忆正文的最大 2-gram 重叠数。"""

    @abc.abstractmethod
    def vector_recall(self, identity, query, top=3, min_score=0.3):
        """语义检索：[(kind, content, delta, importance, score)]；
        向量不可用/未启用返回 []。"""

    @abc.abstractmethod
    def vector_top_score(self, identity, text):
        """消息与记忆的最高语义相似度（通用触发用）；不可用返回 0.0。"""

    @abc.abstractmethod
    def recall_consistent(self, identity, query, top=2):
        """一致性评分（关键词 ∩ 向量高分）：
        [(kind, content, delta, importance, kw_count, vec_score)]。"""

    @abc.abstractmethod
    def list_persistent(self, identity, min_importance=9, limit=3):
        """importance ≥ 阈值的重要记忆（重要度优先、新近次之）：
        [(content, affect_delta, importance)]。"""

    # ---------- 对话同步（可选能力，默认 no-op） ----------
    def important_candidates(self, identity, limit=500):
        """重排候选：带 id、关键词、剧情时间、轨道及召回次数。"""
        return []

    def longterm_candidates(self, identity, limit=200):
        """长期记忆重排候选：[(id, content, keywords)]。"""
        return []

    def mark_recalled(self, identity, contents, boost=1):
        """强化实际召回的记忆；不支持元数据的后端返回 0。"""
        return 0

    def sync_conversation(self, identity, user_text, bot_text):
        """把一轮真实对话写入远端 L0（TencentDB-Agent-Memory 后端实现，
        MemoryCore 自动蒸馏 L1→L2→L3）；本地后端 no-op。返回是否成功。"""
        return False

    # ---------- 生命周期 / 观测 ----------
    @abc.abstractmethod
    def close(self):
        pass

    def stats(self):
        """管理台展示用：{"backend": ..., ...}。"""
        return {"backend": self.backend}
