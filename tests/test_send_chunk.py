# -*- coding: utf-8 -*-
"""超长回复分段发送 + 回复长度上限测试。"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.test_tavern import CaptureClient, make_bot

GUEST = 10001


class SendChunkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        self.bot.client = CaptureClient()
        self.bot.config["message_chunk_chars"] = 100
        self.out_log = []
        self.bot.plugins.on_message_out = (
            lambda user, text, management=False: self.out_log.append(text))

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _long_text():
        lines = []
        for i in range(5):
            lines.append("第%d行" % i + "字" * 60)
        return "\n".join(lines)

    def test_long_text_sent_in_chunks(self):
        text = self._long_text()
        self.assertGreater(len(text), 100)
        ok = self.bot.send_text(GUEST, text)
        self.assertTrue(ok)
        parts = [t for _, t in self.bot.client.sent]
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(len(p), 100)
        # 边界换行会被吞掉，但正文内容应完整保留
        self.assertEqual("".join(parts), text.replace("\n", ""))

    def test_short_text_single_send(self):
        self.bot.send_text(GUEST, "短回复")
        self.assertEqual([t for _, t in self.bot.client.sent], ["短回复"])

    def test_on_message_out_gets_full_text(self):
        text = self._long_text()
        self.bot.send_text(GUEST, text)
        self.assertEqual(self.out_log, [text])  # 日志记录完整原文，而非最后一段

    def test_max_reply_tokens_default_fallback(self):
        # 配置里没有 max_reply_tokens 时，代码默认应为 2000（提高后的上限）
        from qqbot.bot_core import BotCore
        cfg = dict(self.bot.config)
        cfg.pop("max_reply_tokens", None)
        cfg.pop("reply", None)
        bot2 = BotCore(cfg, "t", None)
        try:
            self.assertEqual(bot2.max_reply_tokens, 2000)
        finally:
            bot2.plugins.shutdown()

    def test_reply_preset_controls_max_tokens(self):
        # 回复档位（字符 → token）优先于旧 max_reply_tokens
        from qqbot.bot_core import BotCore
        from qqbot.textutil import chars_to_tokens
        cfg = dict(self.bot.config)
        cfg["reply"] = {
            "mode": "medium",
            "presets": {
                "short": {"max_chars": 300},
                "medium": {"max_chars": 900},
                "long": {"max_chars": 2000},
            },
        }
        bot2 = BotCore(cfg, "t", None)
        try:
            self.assertEqual(bot2.max_reply_tokens, chars_to_tokens(900))
            bot2.apply_reply_mode("short")
            self.assertEqual(bot2.max_reply_tokens, chars_to_tokens(300))
            bot2.apply_reply_mode("long")
            self.assertEqual(bot2.max_reply_tokens, chars_to_tokens(2000))
        finally:
            bot2.plugins.shutdown()

    def test_default_config_has_raised_limit(self):
        from qqbot.config import load_config
        cfg = load_config(os.path.join(self.tmp, "missing-config.json"), use_env=False)
        self.assertGreaterEqual(int(cfg.get("max_reply_tokens", 0)), 2000)


if __name__ == "__main__":
    unittest.main()
