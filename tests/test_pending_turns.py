# -*- coding: utf-8 -*-
"""自动提炼轮数持久化测试：重启/切人格后不丢进度。"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.test_tavern import make_bot


class PendingTurnsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_persist_across_restart(self):
        bot1 = make_bot(self.tmp)
        bot1._pending_turns["测试员"] = 15
        bot1._save_pending_turns()
        bot1.plugins.shutdown()
        bot2 = make_bot(self.tmp)
        try:
            self.assertEqual(bot2._pending_turns.get("测试员"), 15)
        finally:
            bot2.plugins.shutdown()

    def test_increment_and_reset_persist(self):
        bot1 = make_bot(self.tmp)
        bot1._pending_turns["测试员"] = bot1._pending_turns.get("测试员", 0) + 1
        bot1._save_pending_turns()
        bot1.plugins.shutdown()
        bot2 = make_bot(self.tmp)
        try:
            self.assertEqual(bot2._pending_turns.get("测试员", 0), 1)
            bot2._pending_turns["测试员"] = 0
            bot2._save_pending_turns()
        finally:
            bot2.plugins.shutdown()
        bot3 = make_bot(self.tmp)
        try:
            self.assertEqual(bot3._pending_turns.get("测试员", 0), 0)
        finally:
            bot3.plugins.shutdown()

    def test_file_location_under_data_root(self):
        bot = make_bot(self.tmp)
        try:
            path = bot._pending_turns_path()
            self.assertTrue(path.startswith(self.tmp))
            self.assertTrue(path.endswith("pending_turns.json"))
        finally:
            bot.plugins.shutdown()


if __name__ == "__main__":
    unittest.main()
