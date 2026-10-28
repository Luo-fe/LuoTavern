# -*- coding: utf-8 -*-
"""混合记忆后端：本地 SQLite（source of truth + 元数据）+ TencentDB-Agent-Memory（语义引擎）。

- 写：本地必写（好感度/关键词/剧情时间等元数据只有本地有）；远端 best-effort 蒸馏
- 读：list_*/affection/关键词触发 → 本地（零延迟、离线可用）
- 语义召回（recall_important/vector_recall/recall_longterm）：本地 + 远端合并去重，
  远端不可达时自动只剩本地（聊天管线不受影响）
- 对话同步（sync_conversation）：把每轮真实对话写入远端 L0，由 MemoryCore 自动蒸馏
"""
import threading

from qqbot.memstore.base import MemoryStore
from qqbot.memstore.local import SQLiteMemoryStore
from qqbot.memstore.tencentdb import TencentAgentMemoryStore


class HybridMemoryStore(MemoryStore):
    backend = "hybrid"

    def __init__(self, config, log=None):
        cfg = dict(config or {})
        self.cfg = cfg
        self.log = log or (lambda msg: None)
        self.local = SQLiteMemoryStore(cfg, log=log)
        # 使用与本地库一致的绝对路径派生远端命名空间。
        cfg["db_path"] = self.local.db_path
        self.remote = TencentAgentMemoryStore(cfg, log=log)
        self.remote.revision_for = self.local.remote_revision
        self._sync_lock = threading.Lock()
        if self.remote.available():
            self.log("[memory] 混合后端：本地 SQLite + TencentDB-Agent-Memory(%s)"
                     % self.remote.tcfg.get("endpoint", ""))
        else:
            self.log("[memory] 混合后端：TencentDB 传输不可用，仅本地生效")

    # ---------- 兼容直通（本地连接/嵌入器供插件与测试使用） ----------
    @property
    def conn(self):
        return self.local.conn

    @conn.setter
    def conn(self, value):
        self.local.conn = value

    @property
    def embedder(self):
        return self.local.embedder

    @embedder.setter
    def embedder(self, value):
        self.local.embedder = value

    @property
    def _persistent_imp(self):
        return self.local._persistent_imp

    @_persistent_imp.setter
    def _persistent_imp(self, value):
        self.local._persistent_imp = int(value)

    @property
    def _persistent_max(self):
        return self.local._persistent_max

    @_persistent_max.setter
    def _persistent_max(self, value):
        self.local._persistent_max = int(value)

    @property
    def _vec_cfg(self):
        return self.local._vec_cfg

    @property
    def db_path(self):
        return self.local.db_path

    @property
    def lock(self):
        return self.local.lock

    # ---------- 写：本地必写，远端 best-effort ----------
    def save_longterm(self, identity, content, keywords=None):
        ok = self.local.save_longterm(identity, content, keywords=keywords)
        if ok:
            self.remote.save_longterm(identity, content, keywords=keywords)
        return ok

    def delete_longterm(self, identity, keyword):
        # SQLite 触发器递增画像版本；远端旧版本从全部召回入口退出。
        return self.local.delete_longterm(identity, keyword)

    def save_important(self, identity, content, affect_delta=0, importance=5,
                       story_time=None, keywords=None, track="story"):
        ok = self.local.save_important(identity, content, affect_delta, importance,
                                       story_time, keywords, track)
        if ok:
            self.remote.save_important(identity, content, affect_delta, importance,
                                       story_time, keywords, track)
        return ok

    def replace_important(self, identity, items, scope_ids=None):
        n = self.local.replace_important(identity, items, scope_ids=scope_ids)
        for item in items[:200]:
            self.remote.save_important(identity, str(item[0])[:300], item[1],
                                       item[2], None, item[3] if len(item) > 3 else None,
                                       item[4] if len(item) > 4 else "story")
        return n

    def set_affection(self, identity, score):
        self.local.set_affection(identity, score)
        # 好感度只存本地：远端无可靠 KV；避免远端不一致成为 source of truth 的负担

    # ---------- 读：本地 ----------
    def list_longterm(self, identity, limit=500):
        return self.local.list_longterm(identity, limit)

    def important_candidates(self, identity, limit=500):
        return self.local.important_candidates(identity, limit)

    def longterm_candidates(self, identity, limit=200):
        return self.local.longterm_candidates(identity, limit)

    def mark_recalled(self, identity, contents, boost=1):
        return self.local.mark_recalled(identity, contents, boost)

    def list_important(self, identity, limit=500):
        return self.local.list_important(identity, limit)

    def list_important_ids(self, identity, limit=500, track=None):
        return self.local.list_important_ids(identity, limit, track)

    def list_important_by_track(self, identity, track, limit=10):
        return self.local.list_important_by_track(identity, track, limit)

    def list_important_story(self, identity, prefix, limit=100):
        return self.local.list_important_story(identity, prefix, limit)

    def list_important_by_time(self, identity, time_windows, limit=500):
        return self.local.list_important_by_time(identity, time_windows, limit)

    def track_counts(self, identity):
        return self.local.track_counts(identity)

    def get_affection(self, identity):
        return self.local.get_affection(identity)

    def keyword_hits(self, identity, text):
        return self.local.keyword_hits(identity, text)

    def has_keyword_hit(self, identity, text):
        return self.local.has_keyword_hit(identity, text)

    def has_memory_trigger(self, identity, text):
        return self.local.has_memory_trigger(identity, text)

    def content_overlap(self, identity, text):
        return self.local.content_overlap(identity, text)

    def list_persistent(self, identity, min_importance=9, limit=3):
        return self.local.list_persistent(identity, min_importance, limit)

    # ---------- 语义召回：本地 + 远端合并 ----------
    # 远端语义引擎要真正参与，就必须给远端留配额：此前"本地先填满 top 再追加远端
    # 再 out[:top]"，等于本地有命中时远端永远被截掉（只有本地为空才轮得到远端）。
    _REMOTE_QUOTA = 1

    def _remote_slots(self, top):
        """远端预留条数：top<=1 时不留（否则本地就没有位置了）。"""
        top = max(0, int(top))
        return min(self._REMOTE_QUOTA, max(0, top - 1))

    def recall_longterm(self, identity, query, top=3):
        out = list(self.local.recall_longterm(identity, query, top))
        remote = []
        for c in self.remote.recall_texts(identity, query, top):
            if c not in out and c not in remote:
                remote.append(c)
        slots = min(self._remote_slots(top), len(remote))
        keep = max(0, top - slots)
        out = out[:keep]
        return (out + remote[:max(0, top - len(out))])[:top]

    def recall_important(self, identity, query, top=3):
        out = list(self.local.recall_important(identity, query, top))
        have = {c for c, _d in out}
        remote = []
        for c in self.remote.recall_texts(identity, query, top):
            if c not in have:
                remote.append((c, 0))
                have.add(c)
        slots = min(self._remote_slots(top), len(remote))
        out = out[:max(0, top - slots)]
        return (out + remote[:max(0, top - len(out))])[:top]

    def vector_recall(self, identity, query, top=3, min_score=0.3):
        local = self.local.vector_recall(identity, query, top, min_score)
        if local:
            return local
        # 本地嵌入不可用/未命中 → 远端语义兜底（BM25/向量），命中记 0.5 分
        return self.remote.vector_recall(identity, query, top, min_score)

    def vector_top_score(self, identity, text):
        score = self.local.vector_top_score(identity, text)
        if score > 0:
            return score
        return self.remote.vector_top_score(identity, text)

    def recall_consistent(self, identity, query, top=2):
        # 一致性评分依赖「关键词 ∩ 向量」本地精确语义，远端参与会破坏语义 → 本地
        return self.local.recall_consistent(identity, query, top)

    # ---------- 对话同步（远端 L0） ----------
    def sync_conversation(self, identity, user_text, bot_text):
        return self.remote.sync_conversation(identity, user_text, bot_text)

    # ---------- 生命周期 / 观测 ----------
    def close(self):
        self.local.close()
        self.remote.close()

    def stats(self):
        st = self.local.stats()
        st.update({"backend": self.backend, "remote": self.remote.stats()})
        return st

    def migrate_local_to_remote(self, log=None):
        """一次性回填：把本地全部长期/重要记忆蒸馏进远端（幂等 best-effort）。"""
        log = log or self.log
        sent = 0
        for identity in _local_identities(self.local):
            for c in self.local.list_longterm(identity, 500):
                if self.remote.save_longterm(identity, c):
                    sent += 1
            for _mid, c, d, i, kw in self.local.list_important_ids(identity, 500):
                if self.remote.save_important(identity, c, d, i, keywords=kw):
                    sent += 1
        log("[memory] 本地 → TencentDB-Agent-Memory 回填 %d 条" % sent)
        return sent


def _local_identities(store):
    try:
        with store.lock:
            rows = store.conn.execute(
                "SELECT DISTINCT identity FROM memories "
                "UNION SELECT DISTINCT identity FROM important_memories").fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []
