# -*- coding: utf-8 -*-
"""P0 记忆关键词索引测试：零 token 关键词触发回忆、睡前提炼关键词、合并保留、旧数据兼容。"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.support import GUEST, declare, last_user_content, make_bot

TIME_NOOP = {
    "weekday": None, "time_of_day": None, "hhmm": None,
    "advance_day": False, "query_time": False,
}
JUDGE_NO = {"needs_recall": False, "window": None, "keywords": []}


class RecallKeywordsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        declare(self.bot, GUEST, "梦梦")

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cols(self, table):
        return [r[1] for r in self.bot.memory.conn.execute(
            "PRAGMA table_info(%s)" % table).fetchall()]

    def test_migration_adds_keywords_column(self):
        self.assertIn("keywords", self._cols("memories"))
        self.assertIn("keywords", self._cols("important_memories"))

    def test_save_important_keywords_stored_and_hit(self):
        self.bot.memory.save_important(
            "梦梦", "第1周周六晚上 哥哥给青柠拍了一段视频", 10, 8,
            keywords=["视频", "录像", "拍摄"])
        rows = self.bot.memory.list_important_ids("梦梦")
        self.assertEqual(rows[0][4], ["视频", "录像", "拍摄"])
        self.assertTrue(self.bot.memory.has_keyword_hit("梦梦", "那段视频还在吗"))
        self.assertFalse(self.bot.memory.has_keyword_hit("梦梦", "今天天气不错"))

    def test_mention_video_triggers_recall_zero_llm_judge(self):
        """不带"回忆/记得"等硬编码词，仅凭关键词"视频"就触发回忆；判断层零 LLM。"""
        self.bot.memory.save_important(
            "梦梦", "第1周周六晚上 哥哥用手机给青柠拍了一段视频", 10, 8,
            keywords=["视频", "录像", "拍摄", "手机"])
        self.bot.llm.json_script = []
        self.bot._handle(GUEST, "那段视频还在吗")
        self.assertEqual(len(self.bot.llm.json_calls), 0)  # 关键词命中，不调 LLM 判断
        user = last_user_content(self.bot)
        self.assertIn("【重要记忆】", user)
        self.assertIn("拍了一段视频", user)

    def test_forced_recall_video_hits_keyword(self):
        self.bot.memory.save_important(
            "梦梦", "第1周周六晚上 哥哥给青柠拍了一段视频", 10, 8,
            keywords=["视频", "录像", "拍摄"])
        self.bot._handle(GUEST, "【回忆视频】")
        user = last_user_content(self.bot)
        self.assertIn("【重要记忆】", user)
        self.assertIn("拍了一段视频", user)

    def test_remember_memory_keywords_hit(self):
        """【记住】层也可手动带关键词（memory_ui/CLI 编辑），命中触发回忆。"""
        self.bot.memory.save_memory("梦梦", "哥哥生日是 6 月 1 日", keywords=["生日", "6月1日"])
        self.assertTrue(self.bot.memory.has_keyword_hit("梦梦", "生日快到了"))
        self.bot._handle(GUEST, "生日快到了呢")
        user = last_user_content(self.bot)
        self.assertIn("【长期记忆】", user)
        self.assertIn("6 月 1 日", user)

    def test_sleep_ritual_extracts_keywords(self):
        self.bot._handle(GUEST, "今天哥哥给我拍了一段视频")
        self.bot.llm.json_script = [
            [{"content": "第1周周六晚上 哥哥给青柠拍了一段视频", "affect_delta": 10,
              "importance": 8, "keywords": ["视频", "录像", "拍摄"]}],
            JUDGE_NO,
        ]
        self.bot._handle(GUEST, "【结束这一天】")
        rows = self.bot.memory.list_important_ids("梦梦")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], ["视频", "录像", "拍摄"])

    def test_consolidate_merges_keywords_union(self):
        self.bot.memory.save_important(
            "梦梦", "第1周周六晚上 哥哥拍了一段视频", 5, 6, keywords=["视频", "拍摄"])
        self.bot.memory.save_important(
            "梦梦", "第1周周六晚上 哥哥用手机录像", 5, 6, keywords=["录像", "手机"])
        self.bot.config["sleep"]["max_important"] = 1
        ids = [r[0] for r in self.bot.memory.list_important_ids("梦梦")]
        self.bot.llm.json_script = [[
            {"merge": ids, "content": "第1周周六晚上 哥哥给青柠拍视频并录像"}
        ]]
        self.bot.sleep.consolidate("梦梦")
        rows = self.bot.memory.list_important_ids("梦梦")
        self.assertEqual(len(rows), 1)
        self.assertEqual(sorted(rows[0][4]), sorted(["拍摄", "手机", "录像", "视频"]))

    def test_legacy_memory_without_keywords_still_recalls(self):
        """旧数据无关键词：强制回忆仍按正文二元组命中，不退化。"""
        self.bot.memory.save_important("梦梦", "第1周周六晚上 一起看了那部电影", 6, 7)
        self.bot._handle(GUEST, "【回忆电影】")
        user = last_user_content(self.bot)
        self.assertIn("【重要记忆】", user)
        self.assertIn("电影", user)

    def test_keyword_partial_trigger_long_keyword(self):
        """长关键词反查：消息"小狗"能命中关键词"永远的小狗"；单字"狗"不触发。"""
        self.bot.memory.save_important(
            "梦梦", "第1周周六晚上 青柠是哥哥永远的小狗", 10, 8,
            keywords=["永远的小狗"])
        self.assertTrue(self.bot.memory.has_keyword_hit("梦梦", "小狗"))
        self.assertTrue(self.bot.memory.has_keyword_hit("梦梦", "永远的小狗"))
        self.assertFalse(self.bot.memory.has_keyword_hit("梦梦", "狗"))
        self.bot._handle(GUEST, "小狗")
        user = last_user_content(self.bot)
        self.assertIn("【重要记忆】", user)
        self.assertIn("永远的小狗", user)


if __name__ == "__main__":
    unittest.main()
