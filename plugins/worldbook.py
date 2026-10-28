# -*- coding: utf-8 -*-
"""酒馆融合插件：角色卡 + 世界书。

- provide_context：角色卡 system prompt + 世界书常驻设定（稳定块，system 前缀缓存友好）
- provide_user_prefix：当前消息命中世界书触发词 → 二次强调块（紧贴用户消息重新投喂）
"""
import os

from qqbot import sillytavern
from qqbot.bot_core import parse_user_names
from qqbot import worldlog


class Plugin:
    name = "worldbook"

    def __init__(self, bot):
        self.bot = bot
        self.cfg = bot.config.get("worldbook", {}) or {}
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根
        card_rel = bot.config.get("character_card", "")
        self.card_path = self._resolve(base, card_rel)
        self.card = sillytavern.load_character_card(self.card_path)
        book_dir = self._resolve(base, self.cfg.get("dir", ""))
        files = [self._resolve(base, f) for f in (self.cfg.get("files") or [])]
        self.entries = sillytavern.load_world_books(book_dir, files)
        if self.card and self.card.get("character_book"):
            self.entries += sillytavern.entries_from_worldbook(self.card["character_book"])
        self.entries.sort(key=lambda e: e["order"])
        self.base_mode = self.cfg.get("base_mode", "constant")
        self.base_max = max(200, int(self.cfg.get("base_max_chars", 2000)))
        self.emphasis_max = max(100, int(self.cfg.get("emphasis_max_chars", 1500)))
        self.case_sensitive = bool(self.cfg.get("case_sensitive", False))
        # 配置为空时跟随环境变量（测试隔离）/默认目录；配置了则用人格独立目录
        self.worldlog_dir = (self._resolve(base, bot.config["worldlog_dir"])
                             if bot.config.get("worldlog_dir")
                             else worldlog._dir())
        self.synonyms = self.cfg.get("synonyms") or {}
        self.match_context = bool(self.cfg.get("match_context", False))
        self.context_turns = max(1, int(self.cfg.get("match_context_turns", 4)))
        deny_keys = set(self.cfg.get("deny_keys") or [])
        if deny_keys:
            before = len(self.entries)
            self.entries = [
                e for e in self.entries
                if not any(deny in k for k in e["keys"] for deny in deny_keys)
            ]
            if len(self.entries) != before:
                bot.log("[worldbook] 已屏蔽 %d 条 deny_keys 条目: %s" % (
                    before - len(self.entries), "、".join(sorted(deny_keys))))
        self.user_display = bot.config.get("identity", {}).get("owner_name", "用户")
        self._base_block = None
        card_ext = (self.card or {}).get("extensions") or {}
        if not bot.config.get("bot_name"):
            # 优先级：config.bot_name > 卡片 extensions.bot_name > 卡片 name
            bot.bot_name = (card_ext.get("bot_name") or
                            (self.card.get("name") if self.card else None) or "机器人")
        if not bot.config.get("user_names") and card_ext.get("user_names"):
            bot.user_names = parse_user_names(card_ext.get("user_names"))
        bot.log("[worldbook] 角色卡: %s | 世界书条目: %d" % (
            (self.card.get("name") or "无") if self.card else "无", len(self.entries)))

    def _resolve(self, base, rel):
        if not rel:
            return ""
        return rel if os.path.isabs(rel) else os.path.normpath(os.path.join(base, rel))

    def _base(self):
        if self._base_block is None:
            parts = []
            card_p = sillytavern.card_system_prompt(self.card, self.user_display)
            if card_p:
                parts.append(card_p)
            # 世界书常驻：只保留 constant 条目（本地提炼的世界剧情记忆负责承载其余内容）
            wb = sillytavern.build_world_base(self.entries, "constant", self.base_max)
            if wb:
                parts.append(wb)
            self._base_block = "\n\n".join(parts)
        return self._base_block

    def provide_context(self, user_id, text):
        """角色卡 + 世界书常驻设定 + 世界剧情记忆（本地提炼的精简块）。"""
        parts = [self._base()]
        identity = self.bot.identity.get_identity(user_id) if self.bot.identity else None
        if identity:
            world_mem = self.bot.memory.list_important_by_track(identity, "world", limit=6)
            if world_mem:
                lines = ["【世界书剧情·记忆】（本地提炼的已发生世界事件）"]
                for content, _delta, importance in world_mem:
                    lines.append("- %s（重要度%d）" % (content, importance))
                parts.append("\n".join(lines))
        return "\n\n".join(p for p in parts if p)

    def provide_user_prefix(self, user_id, text, context=None):
        """二次强调：消息命中触发词时，把对应条目重新投喂给模型。

        context：近文（可选，由 bot_core 传入最近若干轮消息文本）。
        match_context=True 时主触发词同时扫描近文（模拟酒馆递归扫描）；
        synonyms 配置的别名命中时等价于命中对应主触发词。
        """
        if not text:
            return ""
        context = context or ""
        if len(context) > 3000:
            context = context[-3000:]
        matched = sillytavern.match_world_entries(
            self.entries, text, self.case_sensitive,
            extra_context=context if self.match_context else None,
            synonyms=self.synonyms)
        if matched:
            identity = self.bot.identity.get_identity(user_id) if self.bot.identity else None
            if identity:
                worldlog.append_events(identity, matched, base=self.worldlog_dir)  # 记录本次使用的世界书剧情
            try:
                self.bot.log("[worldbook] 触发 %d 条: %s" % (
                    len(matched),
                    " | ".join(
                        "[%s]%s" % (e.get("_via", "key"),
                                    e.get("name") or (e["keys"][0] if e["keys"] else "?"))
                        for e in matched[:8])))
            except Exception:
                pass
        return sillytavern.build_world_emphasis(matched, self.emphasis_max)
