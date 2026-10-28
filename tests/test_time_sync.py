# -*- coding: utf-8 -*-
"""状态栏时间同步：剧情时钟跟随回复状态栏推进 测试。"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.test_tavern import CaptureClient, OWNER, declare, make_bot


class StatusBarLLM:
    """固定返回带状态栏的回复；其余 JSON 调用返回 None。"""

    def __init__(self, reply):
        self.reply = reply

    def chat(self, messages, temperature=None, max_tokens=None, call_type="chat"):
        return self.reply

    def chat_json(self, messages, temperature=0.2, call_type="chat",
                  max_tokens=None):
        return None


def status_bar(time_line):
    return "（角色回复）\n<状态栏>\n%s\n地点: 家\n</状态栏>" % time_line


class TimelineSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_advance_from_status_bar(self):
        tl = self.bot.timeline
        self.assertEqual((tl.clock["week"], tl.clock["weekday"]), (1, "周六"))
        ok = tl.sync_from_status_bar(
            status_bar("时间: 第1周 周日 晚上"), identity="主人")
        self.assertTrue(ok)
        self.assertEqual((tl.clock["week"], tl.clock["weekday"],
                          tl.clock["time_of_day"]), (1, "周日", "晚上"))
        self.assertTrue(tl._history and
                        tl._history[-1].get("source") == "status_bar")

    def test_ignore_backward(self):
        tl = self.bot.timeline
        self.assertFalse(tl.sync_from_status_bar(
            status_bar("时间: 第1周 周六 上午"), identity="主人"))
        self.assertEqual(tl.clock["time_of_day"], "下午")

    def test_week_jump_cap(self):
        tl = self.bot.timeline
        self.assertTrue(tl.sync_from_status_bar(
            status_bar("时间: 第3周 周一 早上"), identity="主人"))
        self.assertEqual(tl.clock["week"], 3)
        # 超过 2 周视为幻觉
        self.assertFalse(tl.sync_from_status_bar(
            status_bar("时间: 第6周 周一 早上"), identity="主人"))
        self.assertEqual(tl.clock["week"], 3)

    def test_same_time_no_change(self):
        tl = self.bot.timeline
        self.assertFalse(tl.sync_from_status_bar(
            status_bar("时间: 第1周 周六 下午"), identity="主人"))

    def test_tolerant_formats(self):
        tl = self.bot.timeline
        # "第2周 周2 清晨"：周2→周二；清晨归一化为早上并推进时段
        ok = tl.sync_from_status_bar(
            status_bar("时间：第2周 周2 清晨"), identity="主人")
        self.assertTrue(ok)
        self.assertEqual((tl.clock["week"], tl.clock["weekday"]), (2, "周二"))
        self.assertEqual(tl.clock["time_of_day"], "早上")

    def test_unclosed_status_bar(self):
        """状态栏标签没有闭合时，也要能从“时间:”行同步。"""
        tl = self.bot.timeline
        text = "（角色回复）\n<状态栏>\n时间: 第1周 周日 晚上\n地点: 家"
        self.assertTrue(tl.sync_from_status_bar(text, identity="主人"))
        self.assertEqual((tl.clock["week"], tl.clock["weekday"],
                          tl.clock["time_of_day"]), (1, "周日", "晚上"))

    def test_advance_from_凌晨_to_清晨(self):
        """凌晨是每天的开始：同一周几内 清晨/傍晚/夜晚 应能继续推进时钟。"""
        tl = self.bot.timeline
        tl.clock.update({"week": 1, "weekday": "周四", "time_of_day": "凌晨"})
        self.assertTrue(tl.sync_from_status_bar(
            status_bar("时间: 第1周 周四 清晨"), identity="主人"))
        self.assertEqual(tl.clock["time_of_day"], "早上")
        self.assertTrue(tl.sync_from_status_bar(
            status_bar("时间: 第1周 周四 傍晚"), identity="主人"))
        self.assertEqual(tl.clock["time_of_day"], "傍晚")
        self.assertTrue(tl.sync_from_status_bar(
            status_bar("时间: 第1周 周四 夜晚"), identity="主人"))
        self.assertEqual(tl.clock["time_of_day"], "晚上")

    def test_非标准时段词归一化(self):
        tl = self.bot.timeline
        self.assertEqual(tl._norm_period("清晨"), "早上")
        self.assertEqual(tl._norm_period("夜晚"), "晚上")
        self.assertEqual(tl._norm_period("睡前"), "晚上")
        self.assertEqual(tl._norm_period("午后"), "下午")
        self.assertEqual(tl._norm_period("傍晚"), "傍晚")


class BotHandleSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        self.bot.client = CaptureClient()
        declare(self.bot, OWNER, "主人")
        self.bot.llm = StatusBarLLM(
            status_bar("时间: 第2周 周日 晚上"))

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_handle_syncs_clock_and_chatlog(self):
        self.bot._handle(OWNER, "你好")
        tl = self.bot.timeline
        self.assertEqual((tl.clock["week"], tl.clock["weekday"],
                          tl.clock["time_of_day"]), (2, "周日", "晚上"))
        # 机器人回复按同步后的剧情时间写入聊天记录（用户消息仍按旧时钟）
        d = os.path.join(self.tmp, "chatlog", "主人")
        files = sorted(os.listdir(d))
        self.assertIn("第2周周日.md", files)
        with open(os.path.join(d, "第2周周日.md"), encoding="utf-8") as f:
            content = f.read()
        self.assertIn("第2周周日晚上", content)


if __name__ == "__main__":
    unittest.main()
