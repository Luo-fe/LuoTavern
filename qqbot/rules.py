# -*- coding: utf-8 -*-
"""按 QQ 号持久化的角色扮演规则（【纠正规则：xxx】）。

规则跟随账号（身份切换不带走），纠正语义 = 整体替换该账号规则列表。
"""
import json
import os
import threading


class RuleManager:
    def __init__(self, bot, rules_dir=None):
        self.bot = bot
        cfg = bot.config.get("rules", {})
        base = os.path.dirname(os.path.abspath(__file__))
        rel = rules_dir or cfg.get("dir", "data/rules")
        self.dir = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(base, "..", rel))
        self.lock = threading.Lock()
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, user_id):
        return os.path.join(self.dir, "%d.json" % int(user_id))

    def load(self, user_id):
        path = self._path(user_id)
        if not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return list(data.get("rules", []))
        except Exception:
            return []

    def set_rule(self, user_id, rule):
        """整体替换该账号规则；返回 ack。"""
        rule = (rule or "").strip()[:200]
        if not rule:
            return "【规则内容不能为空哦～】"
        with self.lock:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self._path(user_id) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"rules": [rule]}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path(user_id))
        return "【收到～%s会记住这个规则的：%s】" % (self.bot.bot_name, rule)

    def to_prompt_block(self, user_id):
        rules = self.load(user_id)
        if not rules:
            return ""
        return "【本账号专属规则】\n" + "\n".join("- " + r for r in rules)
