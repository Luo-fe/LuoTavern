# -*- coding: utf-8 -*-
"""流动性时间关键词测试：解析 + 回忆集成（今天/昨天/前天/上周X/昨晚/显式时间戳）。"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests import support
from tests.support import GUEST, declare, last_user_content
from qqbot.relative_time import resolve_time_windows

CLOCK = {"week": 1, "weekday": "周一", "time_of_day": "中午", "hhmm": None, "set": True}


def make_bot(tmp):
    return support.make_bot(tmp, clock=CLOCK)


class ResolveTimeWindowsTest(unittest.TestCase):
    CLOCK_MON = {"week": 1, "weekday": "周一"}
    CLOCK_WED_W2 = {"week": 2, "weekday": "周三"}

    def test_today(self):
        self.assertEqual(resolve_time_windows("今天上午", self.CLOCK_MON),
                         [("第1周周一", {"上午"})])
        self.assertEqual(resolve_time_windows("今天", self.CLOCK_MON),
                         [("第1周周一", None)])

    def test_yesterday_and_last_week(self):
        self.assertEqual(resolve_time_windows("昨天", self.CLOCK_MON),
                         [("第1周周日", None)])
        self.assertEqual(resolve_time_windows("昨天上午", self.CLOCK_MON),
                         [("第1周周日", {"上午"})])
        self.assertEqual(resolve_time_windows("上周日上午", self.CLOCK_MON),
                         [("第1周周日", {"上午"})])

    def test_week2_relative(self):
        self.assertEqual(resolve_time_windows("昨天上午", self.CLOCK_WED_W2),
                         [("第2周周二", {"上午"})])
        self.assertEqual(resolve_time_windows("上周一", self.CLOCK_WED_W2),
                         [("第1周周一", None)])
        self.assertEqual(resolve_time_windows("这周五", self.CLOCK_WED_W2),
                         [("第2周周五", None)])
        self.assertEqual(resolve_time_windows("大前天", self.CLOCK_WED_W2),
                         [("第1周周日", None)])

    def test_night_variants(self):
        self.assertEqual(resolve_time_windows("昨晚", self.CLOCK_MON),
                         [("第1周周日", {"晚上", "深夜"})])
        self.assertEqual(resolve_time_windows("今晚", self.CLOCK_MON),
                         [("第1周周一", {"晚上", "深夜"})])
        self.assertEqual(resolve_time_windows("今早", self.CLOCK_MON),
                         [("第1周周一", {"早上"})])

    def test_explicit_stamp(self):
        self.assertEqual(resolve_time_windows("第一周周一上午", self.CLOCK_MON),
                         [("第1周周一", {"上午"})])
        self.assertEqual(resolve_time_windows("第2周周日深夜", self.CLOCK_MON),
                         [("第2周周日", {"深夜"})])
        self.assertEqual(resolve_time_windows("第1周周六", self.CLOCK_MON),
                         [("第1周周六", None)])
        self.assertEqual(resolve_time_windows("第十周周三下午", self.CLOCK_MON),
                         [("第10周周三", {"下午"})])

    def test_whole_week_and_no_time(self):
        self.assertEqual(len(resolve_time_windows("上周", self.CLOCK_WED_W2)), 7)
        self.assertEqual(len(resolve_time_windows("这周", self.CLOCK_WED_W2)), 7)
        self.assertEqual(resolve_time_windows("你好呀", self.CLOCK_MON), [])
        self.assertEqual(resolve_time_windows("最近怎么样", self.CLOCK_MON), [])


class RelativeTimeRecallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        declare(self.bot, GUEST, "梦梦")
        self.bot.memory.save_important(
            "梦梦", "第1周周一上午 青柠和哥哥一起吃了早餐", 8, 7,
            story_time="第1周周一上午")
        self.bot.memory.save_important(
            "梦梦", "第1周周日上午 青柠和哥哥去了公园", 8, 7,
            story_time="第1周周日上午")

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_today_morning_recall(self):
        self.bot.llm.json_script = []
        self.bot._handle(GUEST, "今天上午做了什么")
        self.assertEqual(len(self.bot.llm.json_calls), 0)  # 全程本地，零 LLM
        user = last_user_content(self.bot)
        self.assertIn("【重要记忆】", user)
        self.assertIn("早餐", user)
        self.assertNotIn("公园", user)

    def test_yesterday_morning_recall(self):
        self.bot._handle(GUEST, "昨天上午做了什么")
        user = last_user_content(self.bot)
        self.assertIn("【重要记忆】", user)
        self.assertIn("公园", user)
        self.assertNotIn("早餐", user)

    def test_explicit_stamp_recall(self):
        self.bot._handle(GUEST, "第一周周一上午的事")
        user = last_user_content(self.bot)
        self.assertIn("【重要记忆】", user)
        self.assertIn("早餐", user)

    def test_today_recall_no_self_hit(self):
        """"今天天气不错"不会把自己回忆出来（exclude_text 跳过当前消息）。"""
        # 清空重要记忆，模拟"今天只有聊天记录、没有重要记忆"
        self.bot.memory.replace_important("梦梦", [])
        self.bot._handle(GUEST, "今天早上吃了粥")
        self.bot._handle(GUEST, "今天天气不错")
        user = last_user_content(self.bot)
        self.assertIn("【回忆结果】", user)
        self.assertIn("梦梦: 今天早上吃了粥", user)
        self.assertNotIn("梦梦: 今天天气不错", user)  # 当前消息未被自己回忆出来

    def test_force_recall_relative_time(self):
        self.bot._handle(GUEST, "【回忆昨天上午】")
        user = last_user_content(self.bot)
        self.assertIn("【重要记忆】", user)
        self.assertIn("公园", user)


if __name__ == "__main__":
    unittest.main()
