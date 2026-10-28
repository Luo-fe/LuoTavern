# -*- coding: utf-8 -*-
"""内置指令测试：【随机触发词】/【触发词解释：xxx】代码级处理。

覆盖：解析分类、词库、说明书文案，以及指令被代码拦截、不调用 LLM、
不进入角色扮演与聊天记录。
"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot import parser, trigger_words
from tests.test_tavern import CaptureClient, FakeLLM, declare, last_user_content, make_bot

GUEST = 10001


class ParserTest(unittest.TestCase):
    def test_classify_trigger_commands(self):
        self.assertEqual(parser.classify_instruction("随机触发词"), "trigger")
        self.assertEqual(parser.classify_instruction("触发词解释：偷吻"), "trigger")
        self.assertEqual(parser.classify_instruction("触发词解释"), "trigger")

    def test_other_instructions_untouched(self):
        self.assertEqual(parser.classify_instruction("时间：周六下午"), "time")
        self.assertEqual(parser.classify_instruction("说明书"), "help")
        self.assertEqual(parser.classify_instruction("随便什么"), "unknown")


class PoolTest(unittest.TestCase):
    def test_pool_size(self):
        self.assertGreaterEqual(len(trigger_words.POOL), 50)

    def test_random_words(self):
        words = trigger_words.random_words(50)
        self.assertEqual(len(words), 50)
        self.assertEqual(len(set(words)), 50)
        self.assertTrue(all(w in trigger_words.POOL for w in words))

    def test_explain_exact(self):
        contents = trigger_words.explain("偷吻")
        self.assertIsNotNone(contents)
        self.assertTrue(all(isinstance(c, str) and c for c in contents))

    def test_explain_substring_and_missing(self):
        self.assertIsNotNone(trigger_words.explain("偷吻一下"))
        self.assertIsNone(trigger_words.explain("完全不存在的词"))


class ManualTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        # 本文件与状态栏无关：关掉软件内建的状态栏强制（否则缺状态栏会触发修复调用）
        self.bot.config["status_bar"] = {"enable": False}
        self.bot.llm = FakeLLM()
        self.bot.client = CaptureClient()

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_manual_contains_trigger_commands(self):
        text = self.bot._manual_text()
        self.assertIn("【随机触发词】", text)
        self.assertIn("【触发词解释：xxx】", text)
        self.assertNotIn("青柠", text)


class TriggerCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        # 本文件与状态栏无关：关掉软件内建的状态栏强制（否则缺状态栏会触发修复调用）
        self.bot.config["status_bar"] = {"enable": False}
        self.bot.llm = FakeLLM()
        self.bot.client = CaptureClient()
        declare(self.bot, GUEST, "测试员")

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_random_trigger_words_code_handled(self):
        self.bot._handle(GUEST, "【随机触发词】")
        text = [t for _, t in self.bot.client.sent][-1]
        self.assertIn("【随机触发词】", text)
        self.assertIn("1. ", text)
        self.assertEqual(text.count("\n"), 50)  # 标题 + 50 行
        # 不调用 LLM、不入角色历史
        self.assertEqual(self.bot.llm.chat_calls, [])
        self.assertNotIn("测试员", self.bot.history)

    def test_trigger_explain_code_handled(self):
        self.bot._handle(GUEST, "【触发词解释：偷吻】")
        text = [t for _, t in self.bot.client.sent][-1]
        self.assertTrue(text.startswith("【偷吻】"))
        content = text[len("【偷吻】"):]
        self.assertIn(content, trigger_words.POOL["偷吻"])
        self.assertEqual(self.bot.llm.chat_calls, [])

    def test_trigger_explain_unknown(self):
        self.bot._handle(GUEST, "【触发词解释：不存在的词】")
        text = [t for _, t in self.bot.client.sent][-1]
        self.assertIn("未收录", text)
        self.assertEqual(self.bot.llm.chat_calls, [])

    def test_trigger_mixed_with_rp_continues(self):
        self.bot._handle(GUEST, "【随机触发词】继续聊天")
        texts = [t for _, t in self.bot.client.sent]
        self.assertTrue(any("参考触发词" in t for t in texts))
        # 管理 ack 之后仍进入 RP（LLM 被调用）
        self.assertEqual(len(self.bot.llm.chat_calls), 1)
        self.assertIn("继续聊天", last_user_content(self.bot))


if __name__ == "__main__":
    unittest.main()
