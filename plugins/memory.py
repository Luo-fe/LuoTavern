# -*- coding: utf-8 -*-
"""长期记忆插件（统一存储层入口）。

三层记忆语义不变：
- memories          【记住xxx】显式记忆（按身份）          → store.longterm
- important_memories 睡前仪式提炼的重要记忆（带好感度变动） → store.important
- affection         每个身份的当前好感度                   → store.affection

本插件是薄适配器：全部读写委托 qqbot.memstore.MemoryStore（本地 SQLite /
TencentDB-Agent-Memory / 混合），见 qqbot/memstore/__init__.py build_store()。
调用方（recall / sleep_ritual / bot_core / worldbook）继续用同样的方法名。
"""
import re

from qqbot.memstore import build_store


class Plugin:
    name = "memory"

    def __init__(self, bot):
        self.bot = bot
        self.cfg = bot.config.get("memory", {})
        self.store = build_store(self.cfg, bot=bot, log=bot.log)
        bot.log("[memory] 记忆后端: %s" % self.store.backend)
        self._maybe_prewarm()

    def _maybe_prewarm(self):
        """后台预热向量模型（仅当模型已缓存；避免第一条消息卡在加载/下载上）。"""
        vec = self.cfg.get("vector") or {}
        if not vec.get("enable", True) or not vec.get("prewarm", True):
            return
        embedder = getattr(self.store, "embedder", None)
        if embedder is None or not hasattr(embedder, "prewarm"):
            return
        try:
            if embedder.prewarm():
                self.bot.log("[memory] 向量模型已在后台预热")
        except Exception:
            pass

    # ---------- 兼容属性（旧代码 / 测试直连） ----------
    @property
    def db_path(self):
        return getattr(self.store, "db_path", None)

    @property
    def conn(self):
        """本地后端暴露 SQLite 连接（测试/工具直查）；远端后端无此属性。"""
        conn = getattr(self.store, "conn", None)
        if conn is None:
            raise AttributeError("conn（后端 %s 不提供 SQLite 连接）" % self.store.backend)
        return conn

    @conn.setter
    def conn(self, value):
        if not hasattr(self.store, "conn"):
            raise AttributeError("conn（后端 %s 不提供 SQLite 连接）" % self.store.backend)
        self.store.conn = value

    @property
    def _embedder(self):
        return getattr(self.store, "embedder", None)

    @_embedder.setter
    def _embedder(self, value):
        self.store.embedder = value

    @property
    def _persistent_imp(self):
        return getattr(self.store, "_persistent_imp", 9)

    @_persistent_imp.setter
    def _persistent_imp(self, value):
        self.store._persistent_imp = int(value)

    @property
    def _persistent_max(self):
        return getattr(self.store, "_persistent_max", 3)

    @_persistent_max.setter
    def _persistent_max(self, value):
        self.store._persistent_max = int(value)

    @property
    def _vec_cfg(self):
        return getattr(self.store, "_vec_cfg", {})

    @property
    def lock(self):
        return getattr(self.store, "lock", None)

    # ---------- 关键词工具（沿用旧静态方法名） ----------
    @staticmethod
    def _dump_keywords(keywords):
        from qqbot.memstore.base import KeywordMixin
        return KeywordMixin._dump_keywords(keywords)

    @staticmethod
    def _load_keywords(raw):
        from qqbot.memstore.base import KeywordMixin
        return KeywordMixin._load_keywords(raw)

    # ---------- 旧方法名别名（统一到新接口名） ----------
    def save_memory(self, identity, content, keywords=None):
        return self.store.save_longterm(identity, content, keywords=keywords)

    def delete_memory(self, identity, keyword):
        return self.store.delete_longterm(identity, keyword)

    def list_memories(self, identity, limit=500):
        return self.store.list_longterm(identity, limit=limit)

    def recall_memories(self, identity, query, top=None):
        top = top or int(self.cfg.get("recall_top_k", 3))
        return self.store.recall_longterm(identity, query, top=top)

    # ---------- 其余全部方法委托存储层 ----------
    def __getattr__(self, name):
        if name == "store":
            # 构造失败/序列化探测时避免对 "store" 无限递归
            raise AttributeError("store 未初始化")
        return getattr(self.store, name)

    # ---------- 钩子 ----------
    def on_instruction(self, user_id, inst_raw):
        """处理【记住xxx】/【忘记xxx】，返回 ack 或 None。"""
        identity = self.bot.identity.get_identity(user_id)
        m = re.match(r"^\s*(?:记住|记得|记下)[：:，,、\s]*(.+)$", inst_raw)
        if m and m.group(1).strip():
            if not identity:
                return self.bot.undeclared_reply
            self.store.save_longterm(identity, m.group(1).strip())
            return "【记住啦：%s】" % m.group(1).strip()[:40]
        m2 = re.match(r"^\s*忘记[：:，,、\s]*(.+)$", inst_raw)
        if m2 and m2.group(1).strip():
            if not identity:
                return self.bot.undeclared_reply
            if self.store.delete_longterm(identity, m2.group(1).strip()):
                return "【已经忘掉啦～】"
            return "【没有找到这条记忆哦】"
        return None

    def provide_context(self, user_id, text):
        """长期记忆注入：按新近度取 top-K（默认 5），每条截断 100 字，控注入量。"""
        identity = self.bot.identity.get_identity(user_id)
        if not identity:
            return ""
        top = int(self.cfg.get("max_context_memories", 5))
        rows = self.store.list_longterm(identity, limit=top)
        if not rows:
            return ""
        mems = []
        for content in rows:
            c = content.strip()
            if len(c) > 100:
                c = c[:100] + "…"
            mems.append(c)
        return "【%s的长期记忆（这些是真的发生过/你告诉过我的）】\n" % self.bot.bot_name + "\n".join("- " + m for m in mems)

    def on_turn(self, user_id, user_text, bot_text):
        """一轮对话结束 → 同步到远端记忆引擎（本地后端为 no-op）。"""
        identity = self.bot.identity.get_identity(user_id)
        if not identity:
            return
        sync = getattr(self.store, "sync_conversation", None)
        if sync is not None:
            sync(identity, user_text, bot_text)

    def on_shutdown(self):
        self.store.close()
