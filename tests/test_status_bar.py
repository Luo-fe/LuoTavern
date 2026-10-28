# -*- coding: utf-8 -*-
"""状态栏代码级强制测试：系统提示注入 + 回复不合格时兜底修复 + 位置归一化。

（软件内建的"每轮都要状态栏 + 必填字段校验"见 tests/test_status_bar_builtin.py）
"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.test_tavern import CaptureClient, declare, make_bot

GUEST = 10001

def good_bar(time="晚上", place="家"):
    """含全部必填字段的合格状态栏（用"衣着"写服装，保留卡片自己的字段顺序）。"""
    return ("<状态栏>时间: %s\n地点: %s\n表情: 微笑\n姿态: 坐在沙发上\n"
            "衣着: 居家服\n身体反应: 放松\n内心: 开心</状态栏>" % (time, place))


GOOD_BAR = good_bar()


def status_bar_card():
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "测试角色",
            "description": "测试用角色卡",
            "personality": "温柔",
            "scenario": "在房间里聊天",
            "system_prompt": (
                "请用中文回复。每次回复末尾输出状态栏：\n"
                "<状态栏>\n时间: {{当下时间}}\n地点: {{当前场景}}\n"
                "表情: ...\n姿态: ...\n衣着: ...\n身体反应: ...\n内心: ...\n</状态栏>"
            ),
            "mes_example": "",
            "first_mes": "你好",
            "character_book": None,
            "extensions": {},
        },
    }


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.chat_calls = []
        self.json_calls = []

    def chat(self, messages, temperature=None, max_tokens=None, call_type="chat"):
        self.chat_calls.append(messages)
        return self.replies.pop(0) if self.replies else "（回复）"

    def chat_json(self, messages, temperature=0.2, call_type="chat"):
        self.json_calls.append(messages)
        return None


class StatusBarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp, card=status_bar_card())
        self.bot.client = CaptureClient()
        declare(self.bot, GUEST, "测试员")

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sent(self):
        return [t for _, t in self.bot.client.sent if t != "……（正在思考）"]

    def test_system_prompt_contains_status_bar_rule(self):
        self.bot.llm = ScriptedLLM(["回复 " + GOOD_BAR])
        self.bot._handle(GUEST, "你好")
        sp = self.bot.history["测试员"][0]["content"]
        self.assertIn("输出要求·状态栏", sp)
        self.assertIn("不可省略", sp)

    def test_reply_missing_status_bar_is_repaired(self):
        self.bot.llm = ScriptedLLM([
            "忘了状态栏的回复",
            "补上后的回复 " + GOOD_BAR,
        ])
        self.bot._handle(GUEST, "你好")
        self.assertEqual(len(self.bot.llm.chat_calls), 2)  # 原回复 + 一次修复
        sent = self._sent()
        self.assertTrue(sent[-1].startswith("忘了状态栏的回复"))  # 正文不变
        self.assertIn("<状态栏>", sent[-1])
        self.assertIn("时间: 晚上", sent[-1])

    def test_reply_with_status_bar_no_repair(self):
        self.bot.llm = ScriptedLLM(["正常回复 " + GOOD_BAR])
        self.bot._handle(GUEST, "你好")
        self.assertEqual(len(self.bot.llm.chat_calls), 1)
        self.assertIn("<状态栏>", self._sent()[-1])

    def test_repair_failure_keeps_original(self):
        self.bot.llm = ScriptedLLM(["无状态栏回复", "还是没补上"])
        self.bot._handle(GUEST, "你好")
        self.assertEqual(len(self.bot.llm.chat_calls), 3)  # 原回复 + 廉价修复 + 全量兜底
        self.assertEqual(self._sent()[-1], "无状态栏回复")

    def test_status_bar_position_normalized(self):
        self.bot.llm = ScriptedLLM([
            "正文 " + GOOD_BAR + " 状态栏后面不该有的内容",
        ])
        self.bot._handle(GUEST, "你好")
        self.assertEqual(len(self.bot.llm.chat_calls), 1)  # 不触发修复，本地截断
        sent = self._sent()[-1]
        self.assertEqual(sent, "正文 " + GOOD_BAR)
        self.assertNotIn("不该有", sent)

    def test_multiple_status_bars_keep_last(self):
        self.bot.llm = ScriptedLLM([
            "前半 <状态栏>旧的</状态栏> 中间话 " + GOOD_BAR + " 尾巴",
        ])
        self.bot._handle(GUEST, "你好")
        sent = self._sent()[-1]
        self.assertEqual(sent, "前半 <状态栏>旧的</状态栏> 中间话 " + GOOD_BAR)

    def test_status_bar_can_be_disabled(self):
        """status_bar.enable=false：卡片没写状态栏时完全不介入（旧行为）。"""
        tmp2 = tempfile.mkdtemp()
        try:
            bot2 = make_bot(tmp2)  # 默认卡没有状态栏
            bot2.config["status_bar"] = {"enable": False}
            bot2.llm = ScriptedLLM(["普通回复"])
            bot2.client = CaptureClient()
            declare(bot2, GUEST, "测试员")
            bot2._handle(GUEST, "你好")
            self.assertEqual(len(bot2.llm.chat_calls), 1)  # 不触发修复
            sp = bot2.history["测试员"][0]["content"]
            self.assertNotIn("输出要求·状态栏", sp)
            sent = [t for _, t in bot2.client.sent if t != "……（正在思考）"]
            self.assertEqual(sent[-1], "普通回复")
            bot2.plugins.shutdown()
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
