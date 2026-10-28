# -*- coding: utf-8 -*-
"""重要记忆关键词触发 / 宽容剧情时间解析 测试。"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot import textutil
from tests.test_tavern import make_bot


class StoryTimeParseTest(unittest.TestCase):
    def test_tolerant_periods(self):
        self.assertEqual(textutil.parse_story_time("第1周周二清晨，爸爸夸她"),
                         "第1周周二早上")
        self.assertEqual(textutil.parse_story_time("第1周周三夜晚，艾莉…"),
                         "第1周周三晚上")
        self.assertEqual(textutil.parse_story_time("第1周周一下午，一起…"),
                         "第1周周一下午")
        self.assertEqual(textutil.parse_story_time("第1周周四睡前，凛…"),
                         "第1周周四晚上")
        self.assertIsNone(textutil.parse_story_time("爸爸说周末去欢乐谷"))


class MemoryTriggerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        self.mem = self.bot.plugins.get("memory")

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_keyword_limit_ten(self):
        kw = ["词%d" % i for i in range(15)]
        stored = self.mem._dump_keywords(kw)
        self.assertEqual(len(self.mem._load_keywords(stored)), 10)

    def test_has_memory_trigger_keyword_and_overlap(self):
        self.mem.save_important(
            "主人", "第1周周二晚上 一起去欢乐谷玩过山车，约定周末再去",
            affect_delta=10, importance=7,
            keywords=["欢乐谷", "过山车", "游乐园"])
        self.assertTrue(self.mem.has_memory_trigger("主人", "过山车门票还有吗"))
        # 同义改写：没有关键词，但正文 2-gram 重叠（山车/过山）也能触发
        self.assertTrue(self.mem.has_memory_trigger("主人", "坐那个过山车好玩吗"))
        self.assertFalse(self.mem.has_memory_trigger("主人", "今天天气不错"))

    def test_recall_judge_activates_on_overlap(self):
        self.mem.save_important(
            "主人", "第1周周二晚上 约好周末去欢乐谷坐过山车和摩天轮",
            affect_delta=15, importance=8,
            keywords=["欢乐谷", "摩天轮"])
        jr = self.bot.recall._local_judge("摩天轮还开不开", identity="主人")
        self.assertTrue(jr.needs_recall)
        jr2 = self.bot.recall._local_judge("晚饭想吃什么", identity="主人")
        self.assertFalse(jr2.needs_recall)


if __name__ == "__main__":
    unittest.main()
