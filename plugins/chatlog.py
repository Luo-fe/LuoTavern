# -*- coding: utf-8 -*-
"""聊天记录插件 v2：按身份写入 data/chatlog/{身份}/YYYY-MM-DD.md。

- 用户消息：解析后写入（管理指令剔除；未声明身份/纯管理消息不写）
- 机器人回复：非管理消息写入（“正在思考”提示跳过）
- 文件按 (身份, 日期) 缓存句柄，全局锁保护
"""
import os
import threading

from qqbot import parser


class Plugin:
    name = "chatlog"

    def __init__(self, bot):
        self.bot = bot
        self.lock = threading.Lock()
        base = os.path.dirname(os.path.abspath(__file__))
        rel = bot.config.get("chatlog", {}).get("dir", "data/chatlog")
        self.dir = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(base, "..", rel))
        self.store = bot.chatlog_store
        self._fhs = {}          # (identity, date) -> fh
        self._fh_order = []     # LRU 顺序（控制打开句柄数量）
        self._max_fhs = 32
        self._counters = {}     # identity -> 已写条目数
        # 启动预热：一次性把各身份的条目计数读入（避免首次写入时锁内全量解析阻塞）
        try:
            for ident in self.store.identity_dirs():
                self._counters[ident] = len(self.store.read_entries(ident))
        except OSError:
            pass
        bot.log("[chatlog] 聊天记录目录: %s" % self.dir)

    # ---------- 写入 ----------
    def _counter(self, identity):
        n = self._counters.get(identity)
        if n is None:
            # 预热漏网的身份（外部脚本直接写入文件后调用）：锁外快速计数一次
            n = len(self.store.read_entries(identity))
            self._counters[identity] = n
        return n

    def _touch_fh(self, key):
        """LRU 更新 + 超限时关闭最久未用的句柄（长期运行不累积文件句柄）。"""
        if key in self._fh_order:
            self._fh_order.remove(key)
        self._fh_order.append(key)
        while len(self._fh_order) > self._max_fhs:
            old = self._fh_order.pop(0)
            fh = self._fhs.pop(old, None)
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass

    def _append(self, identity, who, text):
        text = str(text).strip()
        if not text or not identity:
            return
        with self.lock:
            # 剧情日期分类：第N周周X（同一剧情日内跨时段同文件；跨周自动换文件）
            clock = self.bot.timeline.current()
            date = "第%d周%s" % (clock.get("week", 1), clock.get("weekday", "周一"))
            key = (identity, date)
            fh = self._fhs.get(key)
            if fh is None:
                d = os.path.join(self.dir, identity)
                os.makedirs(d, exist_ok=True)
                path = os.path.join(d, date + ".md")
                exists = os.path.exists(path)
                fh = open(path, "a", encoding="utf-8")
                if not exists:
                    fh.write("# 聊天记录 %s %s\n\n" % (identity, date))
                self._fhs[key] = fh
            self._touch_fh(key)
            idx = self._counter(identity)
            self._counters[identity] = idx + 1
            stamp = self.bot.timeline.stamp()
            lines = text.split("\n")
            fh.write("- %s **%s**：%s\n" % (stamp, who, lines[0]))
            for line in lines[1:]:
                fh.write("  %s\n" % line)
            fh.flush()

    # ---------- 钩子 ----------
    def on_message(self, user_id, text):
        """记录用户消息；纯管理消息/未声明身份/时间门控阶段不写，不拦截。"""
        pm = parser.parse_message(text)
        if pm.is_management_only:
            return None
        identity = self.bot.identity.get_identity(user_id)
        if not identity:
            return None
        if not self.bot.timeline.is_set():
            return None  # 门控阶段：时钟未设置，消息无剧情时间可记（避免假"周一早上"污染日志）
        if (self.bot.shared_ctx.is_shared_for(identity)
                or (identity == self.bot.identity.owner_name
                    and self.bot.shared_ctx.has_pending())):
            return None  # 共享期/等待期主人消息进共享记录，分家时归位（见 shared_ctx.settle）
        self._append(identity, identity, pm.rebuilt)
        return None

    def on_message_out(self, user_id, text, management=False):
        """记录机器人发出的消息；跳过“正在思考”提示与管理层 ack。"""
        if management or text == "……（正在思考）":
            return
        identity = self.bot.identity.get_identity(user_id)
        if not identity:
            return
        if self.bot.shared_ctx.is_shared_for(identity):
            return  # 共享期同上
        self._append(identity, self.bot.bot_name, text)

    def refresh_counters(self):
        """外部（共享分家）直接写 chatlog 文件后调用：丢弃序号缓存，下次重新计数。"""
        with self.lock:
            self._counters.clear()

    def on_shutdown(self):
        with self.lock:
            for fh in self._fhs.values():
                try:
                    fh.close()
                except Exception:
                    pass
            self._fhs.clear()
