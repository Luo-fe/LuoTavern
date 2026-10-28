# -*- coding: utf-8 -*-
"""睡前沉淀模块：提炼失败不封存、无时间戳条目跳过 测试。"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.test_tavern import make_bot


class FakeSleepLLM:
    def __init__(self, extraction_result):
        self.calls = 0
        self.extraction_result = extraction_result

    def chat(self, messages, temperature=None, max_tokens=None, call_type="chat"):
        return ""

    def chat_json(self, messages, temperature=0.2, call_type="chat",
                  max_tokens=None):
        self.calls += 1
        if self.calls == 1:
            return self.extraction_result
        return None


def write_entries(tmp, lines):
    d = os.path.join(tmp, "chatlog", "主人")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "第1周周四.md"), "w", encoding="utf-8") as f:
        f.write("# 聊天记录 主人 第1周周四\n\n")
        for ln in lines:
            f.write(ln + "\n")


class SleepRitualTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        write_entries(self.tmp, [
            "- 第1周周四晚上 **主人**：你好",
            "- 第1周周四晚上 **Eri and Rin**：回复",
        ])

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_failed_extraction_does_not_seal(self):
        self.bot.llm = FakeSleepLLM(None)
        extracted, _summary = self.bot.sleep._extract_and_summarize("主人")
        self.assertEqual(extracted, 0)
        self.assertEqual(self.bot.seal.get("主人"), -1)
        self.assertEqual(self.bot.memory.list_important("主人"), [])

    def test_invalid_entry_skipped_and_seal_advances(self):
        self.bot.llm = FakeSleepLLM([
            {"content": "第1周周四晚上 一起做了晚饭",
             "affect_delta": 5, "importance": 6,
             "keywords": ["晚饭", "厨房"]},
            {"content": "用户要求露骨描写，已拒绝",
             "affect_delta": 0, "importance": 5, "keywords": []},
        ])
        extracted, _summary = self.bot.sleep._extract_and_summarize("主人")
        self.assertEqual(extracted, 1)
        mems = self.bot.memory.list_important("主人")
        self.assertEqual(len(mems), 1)
        self.assertIn("第1周周四晚上", mems[0][0])
        self.assertEqual(self.bot.seal.get("主人"), 1)


if __name__ == "__main__":
    unittest.main()
