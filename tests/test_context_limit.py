# -*- coding: utf-8 -*-
"""单次回复上下文长度限制测试。"""
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


class ScriptedLLM:
    def __init__(self, reply="回复 <状态栏>x</状态栏>"):
        self.reply = reply
        self.chat_calls = []

    def chat(self, messages, temperature=None, max_tokens=None, call_type="chat"):
        self.chat_calls.append(messages)
        return self.reply

    def chat_json(self, messages, temperature=0.2, call_type="chat"):
        return None


class ContextLimitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        # 本文件与状态栏无关：关掉软件内建的状态栏强制（否则缺状态栏会触发修复调用）
        self.bot.config["status_bar"] = {"enable": False}
        self.bot.client = CaptureClient()
        declare(self.bot, GUEST, "测试员")

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _history(self, turns=5, content_len=3000):
        h = [{"role": "system", "content": "你是一个角色扮演助手。"}]
        for i in range(turns):
            h.append({"role": "user", "content": "用户消息" + "长" * content_len + str(i)})
            h.append({"role": "assistant", "content": "助手回复" + "长" * content_len + str(i)})
        h.append({"role": "user", "content": "当前问题"})
        return h

    @staticmethod
    def _size(msgs):
        return sum(len(str(m.get("content") or "")) for m in msgs)

    def test_trim_when_over_cap(self):
        msgs = self._history(turns=5, content_len=3000)
        self.bot.config["max_context_chars"] = 12000
        out = self.bot._limit_context(msgs, "测试员")
        self.assertLessEqual(self._size(out), 12000)
        self.assertEqual(out[0]["role"], "system")          # 系统提示保留
        self.assertEqual(out[-1]["content"], "当前问题")     # 最新消息保留
        self.assertLess(len(out), len(msgs))                # 确实裁剪了

    def test_no_trim_under_cap(self):
        msgs = self._history(turns=2, content_len=100)
        self.bot.config["max_context_chars"] = 50000
        self.assertEqual(self.bot._limit_context(msgs, "测试员"), msgs)

    def test_disabled_when_zero(self):
        msgs = self._history(turns=5, content_len=3000)
        self.bot.config["max_context_chars"] = 0
        self.assertEqual(self.bot._limit_context(msgs, "测试员"), msgs)

    def test_pipeline_applies_limit(self):
        self.bot.llm = ScriptedLLM()
        self.bot.config["max_context_chars"] = 6000
        h = [{"role": "system", "content": "系统"}]
        for i in range(4):
            h.append({"role": "user", "content": "很长" * 2000 + str(i)})
            h.append({"role": "assistant", "content": "回复" * 2000 + str(i)})
        h.append({"role": "user", "content": "你好"})
        self.bot.history["测试员"] = h
        self.bot._handle(GUEST, "你好")
        sent = self.bot.llm.chat_calls[-1]
        self.assertLessEqual(self._size(sent), 6000)
        self.assertEqual(sent[-1]["content"], "你好")

    def _history_with_status(self, turns=5):
        h = [{"role": "system", "content": "系统"}]
        for i in range(turns):
            h.append({"role": "user", "content": "用户第%d句" % i})
            h.append({"role": "assistant", "content": (
                "助手第%d句\n<状态栏>\n时间: 晚上\n姿态: x\n</状态栏>" % i)})
        h.append({"role": "user", "content": "当前问题"})
        return h

    def test_trim_history_strips_status_bar_only(self):
        msgs = self._history_with_status(turns=5)
        self.bot.config["max_history_chars"] = 0
        self.bot.config["keep_recent_full"] = 6
        out = self.bot._trim_history(msgs)
        self.assertEqual(len(out), len(msgs))
        self.assertEqual(out[0], msgs[0])                 # 系统提示完整
        # 最早的 2 个来回：状态栏被裁掉，对话正文保留
        self.assertIn("助手第0句", out[2]["content"])
        self.assertNotIn("<状态栏>", out[2]["content"])
        self.assertIn("助手第1句", out[4]["content"])
        self.assertNotIn("<状态栏>", out[4]["content"])
        # 最近 6 条 + 当前用户消息保持完整（状态栏仍在）
        for i in range(5, len(msgs)):
            self.assertEqual(out[i], msgs[i])
        self.assertIn("<状态栏>", out[6]["content"])

    def test_trim_history_disabled_when_zero(self):
        # max_history_chars=0：不做硬截断，但状态栏裁剪仍生效
        msgs = self._history_with_status(turns=5)
        self.bot.config["max_history_chars"] = 0
        out = self.bot._trim_history(msgs)
        self.assertNotIn("<状态栏>", out[2]["content"])

    def test_summary_limit_config(self):
        self.bot.config["summary_max_chars"] = 800
        self.assertEqual(self.bot._summary_limit(), 800)
        self.bot.config.pop("summary_max_chars", None)
        self.assertEqual(self.bot._summary_limit(), 2000)


if __name__ == "__main__":
    unittest.main()
