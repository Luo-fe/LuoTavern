# -*- coding: utf-8 -*-
"""LLM 回复调用重试（异常/空输出）与推理 token 余量 测试。"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.test_tavern import make_bot


class FlakyLLM:
    def __init__(self, fail_raises=0, fail_empty=0, reply="最终回复"):
        self.fail_raises = fail_raises
        self.fail_empty = fail_empty
        self.reply = reply
        self.calls = 0
        self.max_tokens_seen = []

    def chat(self, messages, temperature=None, max_tokens=None, call_type="chat"):
        self.calls += 1
        self.max_tokens_seen.append(max_tokens)
        if self.calls <= self.fail_raises:
            raise RuntimeError("网络抖动")
        if self.calls <= self.fail_raises + self.fail_empty:
            return ""
        return self.reply

    def chat_json(self, messages, **kw):
        return None


class LlmRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        self.bot.config["llm_retries"] = 2
        self.bot.config["llm_reasoning_headroom"] = 2000
        self.bot.max_reply_tokens = 900

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_retries_on_exception(self):
        llm = FlakyLLM(fail_raises=2)
        self.bot.llm = llm
        out = self.bot._chat_with_retry([{"role": "user", "content": "hi"}])
        self.assertEqual(out, "最终回复")
        self.assertEqual(llm.calls, 3)

    def test_retries_on_empty(self):
        llm = FlakyLLM(fail_empty=1)
        self.bot.llm = llm
        out = self.bot._chat_with_retry([{"role": "user", "content": "hi"}])
        self.assertEqual(out, "最终回复")
        self.assertEqual(llm.calls, 2)

    def test_headroom_added(self):
        llm = FlakyLLM()
        self.bot.llm = llm
        self.bot._chat_with_retry([{"role": "user", "content": "hi"}])
        self.assertEqual(llm.max_tokens_seen[0], 900 + 2000)

    def test_no_retry_when_disabled_and_raises(self):
        llm = FlakyLLM(fail_raises=3)
        self.bot.llm = llm
        self.bot.config["llm_retries"] = 0
        with self.assertRaises(RuntimeError):
            self.bot._chat_with_retry([{"role": "user", "content": "hi"}])
        self.assertEqual(llm.calls, 1)


if __name__ == "__main__":
    unittest.main()
