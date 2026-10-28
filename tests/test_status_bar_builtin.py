# -*- coding: utf-8 -*-
"""软件内建状态栏：不依赖角色卡是否写过，每轮都强制要求 + 必填字段级校验。

要覆盖的行为：
1. 卡片完全没写状态栏 → 仍然注入"输出要求·状态栏"（含字段清单 + 内建模板）
2. 卡片写了状态栏 → 沿用卡片格式（inherit_card），并要求补上缺失字段
3. 回复缺必填字段（表情/服装/身体反应）→ 触发修复，且**替换**原状态栏而不是追加
4. 同义字段（衣着=服装、神情=表情）不算缺失，避免每轮误判重写
5. 修复提示里带上"缺什么"；补不上时保留原文并计数
6. 字段/必填/模板/开关可配置
"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.test_status_bar import ScriptedLLM, good_bar
from tests.test_tavern import CaptureClient, declare, make_bot

GUEST = 10001

BARE_BAR = "<状态栏>时间: 晚上\n地点: 家</状态栏>"
FULL_BAR = good_bar()


class BuiltinStatusBarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bot(self, card=None, status_bar=None):
        bot = make_bot(self.tmp, card=card)
        if status_bar is not None:
            bot.config["status_bar"] = status_bar
        bot.client = CaptureClient()
        declare(bot, GUEST, "测试员")
        self.addCleanup(lambda: bot.plugins.shutdown())
        return bot

    @staticmethod
    def _sent(bot):
        return [t for _, t in bot.client.sent if t != "……（正在思考）"]

    # ---------- 1) 卡片没写也能强制 ----------
    def test_bare_card_still_gets_rule_and_template(self):
        bot = self._bot()                      # 默认卡没有状态栏
        bot.llm = ScriptedLLM(["普通回复 " + FULL_BAR])
        bot._handle(GUEST, "你好")
        sp = bot.history["测试员"][0]["content"]
        self.assertIn("输出要求·状态栏", sp)
        self.assertIn("表情", sp)
        self.assertIn("服装", sp)
        self.assertIn("身体反应", sp)
        self.assertIn("<状态栏>", sp)           # 内建模板一并注入
        self.assertEqual(len(bot.llm.chat_calls), 1)   # 合格的回复不触发修复

    def test_bare_card_missing_bar_is_repaired(self):
        bot = self._bot()
        bot.llm = ScriptedLLM(["没有状态栏", FULL_BAR])
        bot._handle(GUEST, "你好")
        self.assertEqual(len(bot.llm.chat_calls), 2)
        self.assertIn("<状态栏>", self._sent(bot)[-1])

    # ---------- 2) 卡片写了 → 沿用卡片格式 ----------
    def test_card_format_is_inherited(self):
        from tests.test_status_bar import status_bar_card
        bot = self._bot(card=status_bar_card())
        bot.llm = ScriptedLLM(["回复 " + FULL_BAR])
        bot._handle(GUEST, "你好")
        sp = bot.history["测试员"][0]["content"]
        self.assertIn("沿用角色卡里定义的状态栏格式", sp)

    # ---------- 3) 缺必填字段 → 修复 + 替换而非追加 ----------
    def test_missing_required_field_triggers_repair(self):
        bot = self._bot()
        bot.llm = ScriptedLLM(["正文 " + BARE_BAR, FULL_BAR])
        bot._handle(GUEST, "你好")
        self.assertEqual(len(bot.llm.chat_calls), 2)     # 触发一次修复
        sent = self._sent(bot)[-1]
        self.assertIn("表情", sent)
        self.assertEqual(sent.count("<状态栏>"), 1)      # 替换，不是两个块
        self.assertTrue(sent.startswith("正文"))
        # 修复提示里明确点出缺什么
        repair_prompt = bot.llm.chat_calls[1][0]["content"]
        self.assertIn("表情", repair_prompt)
        self.assertIn("服装", repair_prompt)
        self.assertIn("身体反应", repair_prompt)

    def test_repair_prompt_carries_existing_bar(self):
        """廉价修复要带上原文已有状态栏，避免时间/地点被重写掉。"""
        bot = self._bot()
        bot.llm = ScriptedLLM(["正文 " + BARE_BAR, FULL_BAR])
        bot._handle(GUEST, "你好")
        self.assertIn("时间", bot.llm.chat_calls[1][0]["content"])
        self.assertIn("保留", bot.llm.chat_calls[1][0]["content"])

    # ---------- 4) 同义字段不算缺 ----------
    def test_synonyms_satisfy_required_fields(self):
        bot = self._bot()
        bar = ("<状态栏>时间: 晚上\n地点: 家\n神情: 微红\n姿势: 低头\n"
               "衣着: 白衬衫、半褪的裙子\n身体: 呼吸发紧\n内心: 紧张</状态栏>")
        bot.llm = ScriptedLLM(["正文 " + bar])
        bot._handle(GUEST, "你好")
        self.assertEqual(len(bot.llm.chat_calls), 1)     # 衣着/神情/身体 视为齐备
        self.assertEqual(self._sent(bot)[-1], "正文 " + bar)

    # ---------- 5) 补不上 → 保留原文 + 计数 ----------
    def test_unfixed_reply_keeps_original_and_counts(self):
        bot = self._bot()
        bot.llm = ScriptedLLM(["正文 " + BARE_BAR, "补不上", "还是补不上"])
        bot._handle(GUEST, "你好")
        sent = self._sent(bot)[-1]
        self.assertEqual(sent, "正文 " + BARE_BAR)       # 原文保留（不静默丢弃）
        self.assertGreaterEqual(bot._meta.get("status_bar_repairs", 0), 1)
        self.assertGreaterEqual(bot._meta.get("status_bar_unfixed", 0), 1)

    # ---------- 6) 可配置 ----------
    def test_fields_and_required_are_configurable(self):
        """自定义字段表必须完全生效（注入文案/模板都由字段清单生成）。"""
        bot = self._bot(status_bar={"enable": True, "required": ["氛围"],
                                    "fields": ["时间", "氛围"]})
        bot.llm = ScriptedLLM(["正文 <状态栏>时间: 傍晚</状态栏>", "补不上", "还是补不上"])
        bot._handle(GUEST, "你好")
        sp = bot.history["测试员"][0]["content"]
        self.assertIn("氛围", sp)
        self.assertNotIn("身体反应", sp)                 # 默认字段不再出现
        self.assertNotIn("地点:", sp)                    # 模板只含配置里的字段
        self.assertEqual(len(bot.llm.chat_calls), 3)     # 缺"氛围" → 触发修复

    def test_custom_template_used(self):
        bot = self._bot(status_bar={
            "enable": True, "template": "<状态栏>\n时间: {时间}\n<{name}>\n状态: …\n</状态栏>"})
        bot.llm = ScriptedLLM(["回复 " + FULL_BAR])
        bot._handle(GUEST, "你好")
        sp = bot.history["测试员"][0]["content"]
        self.assertIn("状态: …", sp)
        self.assertIn(bot.bot_name, sp)                  # {name} 已替换

    def test_gaps_reports_block_and_fields(self):
        bot = self._bot()
        self.assertEqual(bot._status_bar_gaps("没有块"), ["<状态栏> 区块"])
        self.assertEqual(bot._status_bar_gaps(BARE_BAR), ["表情", "服装", "身体反应"])
        self.assertEqual(bot._status_bar_gaps(FULL_BAR), [])

    def test_disabled_skips_field_check(self):
        bot = self._bot(status_bar={"enable": False})
        self.assertEqual(bot._status_bar_gaps(BARE_BAR), [])   # 关掉后不查字段

    def test_apply_status_bar_replaces_last_block(self):
        from qqbot.bot_core import BotCore
        out = BotCore._apply_status_bar("正文 " + BARE_BAR + " 尾", FULL_BAR)
        self.assertEqual(out, "正文 " + FULL_BAR)
        out2 = BotCore._apply_status_bar("正文无块", FULL_BAR)
        self.assertEqual(out2, "正文无块\n" + FULL_BAR)


class SettingsIntegrationTest(unittest.TestCase):
    """管理台保存设置 → 配置合并 → 热生效（不重启即可用新字段）。"""

    def test_merge_settings_normalizes_status_bar(self):
        from qqbot.admin import _merge_settings
        cfg = {"status_bar": {"enable": True, "fields": ["时间"], "required": [],
                              "template": "", "repair": True}}
        _merge_settings(cfg, {"status_bar": {
            "enable": False, "repair": False,
            "fields": ["时间", " 表情 ", "时间", ""],          # 去空白/去重/丢空
            "required": ["表情", " 服装 "], "template": "<状态栏>x</状态栏>",
        }})
        sb = cfg["status_bar"]
        self.assertFalse(sb["enable"])
        self.assertFalse(sb["repair"])
        self.assertEqual(sb["fields"], ["时间", "表情"])
        self.assertEqual(sb["required"], ["表情", "服装"])
        self.assertEqual(sb["template"], "<状态栏>x</状态栏>")

    def test_merge_settings_keeps_untouched_keys(self):
        from qqbot.admin import _merge_settings
        cfg = {"status_bar": {"enable": True, "fields": ["时间"], "required": ["表情"],
                              "cheap_max_tokens": 300}}
        _merge_settings(cfg, {"status_bar": {"enable": False}})
        self.assertEqual(cfg["status_bar"]["fields"], ["时间"])
        self.assertEqual(cfg["status_bar"]["required"], ["表情"])
        self.assertEqual(cfg["status_bar"]["cheap_max_tokens"], 300)

    def test_merge_settings_empty_list_clears_required(self):
        from qqbot.admin import _merge_settings
        cfg = {"status_bar": {"required": ["表情"]}}
        _merge_settings(cfg, {"status_bar": {"required": []}})
        self.assertEqual(cfg["status_bar"]["required"], [])

    def test_hot_apply_syncs_status_bar_to_running_bot(self):
        import copy
        from qqbot.admin import _apply_settings_hot
        tmp = tempfile.mkdtemp()
        try:
            bot = make_bot(tmp)
            self.addCleanup(lambda: bot.plugins.shutdown())
            cfg = copy.deepcopy(bot.config)
            cfg["status_bar"] = {"enable": True, "fields": ["时间", "氛围"],
                                 "required": ["氛围"], "template": "", "repair": True}
            _apply_settings_hot(bot, cfg)
            self.assertEqual(bot.config["status_bar"]["fields"], ["时间", "氛围"])
            self.assertEqual(bot.config["status_bar"]["required"], ["氛围"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_admin_ui_blocks_save_before_settings_loaded(self):
        """设置页未加载完时不许保存（否则会把未加载项写成默认值，曾把 reply.mode 改掉）。"""
        ui = open(os.path.join(ROOT, "qqbot", "admin_ui.py"), encoding="utf-8").read()
        self.assertIn("SETTINGS_LOADED=true", ui)
        body = ui[ui.index("async function saveSettings()"):]
        body = body[:body.index("const body=")]
        self.assertIn("if(!SETTINGS_LOADED)", body)
        self.assertIn("return;", body)

    def test_admin_ui_exposes_status_bar_controls(self):
        """管理台设置页必须真的带这些控件（防止以后被误删）。"""
        ui = open(os.path.join(ROOT, "qqbot", "admin_ui.py"), encoding="utf-8").read()
        for token in ("set-sb-enable", "set-sb-repair", "set-sb-inherit",
                      "set-sb-fields", "set-sb-required", "set-sb-template",
                      "status_bar:"):
            self.assertIn(token, ui)


if __name__ == "__main__":
    unittest.main()
