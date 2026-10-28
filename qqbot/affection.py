# -*- coding: utf-8 -*-
"""好感度管理：每个身份一个分数（存 memory.db 的 affection 表）。

- 哥哥初始 300，其他身份初始 100；无上限，400 只是"最喜欢的程度"阈值（超过仍累计）
- 由睡前仪式提炼的重要记忆 affect_delta 累计而来
- 注入系统提示词影响青柠言行；【好感度】指令查询
"""


class AffectionManager:
    def __init__(self, bot):
        self.bot = bot
        cfg = bot.config.get("affection", {})
        self.max_score = int(cfg.get("max_score", 400))
        self.owner_initial = int(cfg.get("owner_initial", 300))
        self.guest_initial = int(cfg.get("guest_initial", 100))

    def _db(self):
        return self.bot.memory

    def get(self, identity):
        """当前好感度；无记录时按身份类型给初始值（不落库）。"""
        if not identity:
            return self.guest_initial
        db = self._db()
        if db is None:
            # memory 插件未加载（被禁用/加载失败）：返回初始值，不让 RP 崩溃
            return self.owner_initial if identity == self.bot.identity.owner_name else self.guest_initial
        score = db.get_affection(identity)
        if score is not None:
            return score
        owner_name = self.bot.identity.owner_name
        return self.owner_initial if identity == owner_name else self.guest_initial

    def apply_delta(self, identity, delta):
        """累计好感度变动，无上限（400 只是最喜欢的程度阈值）；返回新分数。"""
        cur = self.get(identity)
        db = self._db()
        if db is None:
            # memory 插件未加载：只返回推算值，不落库（此前会 AttributeError 打断整条管线）
            return cur + int(delta)
        new = cur + int(delta)
        db.set_affection(identity, new)
        return new

    def adjust(self, identity, diff):
        """直接增减 diff（记忆合并对账用，把分数与重要记忆 Σdelta 重新对齐）。"""
        if not diff:
            return
        db = self._db()
        if db is None:
            return
        cur = self.get(identity)
        db.set_affection(identity, cur + int(diff))

    def to_prompt_line(self, identity):
        score = self.get(identity)
        if score > self.max_score:
            return "【当前好感度：%s %d（已超过 400，达到最喜欢的程度）】" % (
                identity, score,
            )
        return "【当前好感度：%s %d/%d】" % (identity, score, self.max_score)

    def query_answer(self, identity):
        score = self.get(identity)
        if score >= self.max_score:
            note = "%s最喜欢%s了！" % (self.bot.bot_name, identity)
        elif score >= 300:
            note = "%s超级喜欢%s！" % (self.bot.bot_name, identity)
        elif score >= 200:
            note = "%s很喜欢%s～" % (self.bot.bot_name, identity)
        elif score >= 100:
            note = "%s觉得%s还不错～" % (self.bot.bot_name, identity)
        elif score >= 0:
            note = "%s对%s有点距离感呢…" % (self.bot.bot_name, identity)
        else:
            note = "%s不太想理%s……" % (self.bot.bot_name, identity)
        if score > self.max_score:
            return "【%s的好感度：%d（已超过上限 400）】%s" % (identity, score, note)
        return "【%s的好感度：%d/%d】%s" % (identity, score, self.max_score, note)
