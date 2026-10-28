# -*- coding: utf-8 -*-
"""会话命令误触发防护（P0 数据丢失回归）。

背景：`_check_command` 曾用 "关键词 in 整条原文" 匹配，
角色扮演里出现"失忆 / 翻篇 / 重新开始"等词就会清空会话，
而 `reset_session` 还会 `os.remove` 掉落盘的摘要文件（不可恢复）。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot.parser import parse_message  # noqa: E402
from tests.test_tavern import FakeLLM, make_bot  # noqa: E402

IDENT = "主人"


class SessionCommandGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        self.bot.llm = FakeLLM()
        self.summary = os.path.join(self.tmp, "summary", "%s.md" % IDENT)

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 匹配语义 ----------

    def _hit(self, text):
        return self.bot._command_hit(
            self.bot.commands_cfg.get("session_reset", []),
            text, parse_message(text))

    def test_plain_roleplay_text_does_not_trigger(self):
        for text in ("你怎么会失忆呢？",
                     "这件事就让它翻篇吧",
                     "我们重新开始好不好",
                     "要不要清空会话记录呀",
                     "我刚刚在回忆我们的过去"):
            self.assertIsNone(self._hit(text), "误触发：%s" % text)

    def test_explicit_forms_trigger(self):
        self.assertEqual(self._hit("【清空会话】"), "清空会话")
        self.assertEqual(self._hit("清空会话"), "清空会话")
        self.assertEqual(self._hit("/clear"), "/clear")
        self.assertEqual(self._hit("  【失忆】  "), "失忆")

    def test_summarize_keyword_no_longer_matches_inside_sentence(self):
        hit = self.bot._command_hit(
            self.bot.commands_cfg.get("summarize", []),
            "我总结一下今天发生的事，你先别说话", parse_message("我总结一下今天发生的事，你先别说话"))
        self.assertIsNone(hit)

    # ---------- 行为 ----------

    def test_roleplay_text_keeps_summary_and_history(self):
        os.makedirs(os.path.dirname(self.summary), exist_ok=True)
        with open(self.summary, "w", encoding="utf-8") as f:
            f.write("之前的摘要")
        self.bot.summaries[IDENT] = "之前的摘要"
        self.bot.history[IDENT] = [{"role": "system", "content": "s"},
                                   {"role": "user", "content": "u"}]
        handled = self.bot._check_command(1, "你怎么会失忆呢？", IDENT,
                                          parse_message("你怎么会失忆呢？"))
        self.assertFalse(handled)
        self.assertTrue(os.path.isfile(self.summary))
        self.assertIn(IDENT, self.bot.summaries)
        self.assertIn(IDENT, self.bot.history)

    def test_reset_archives_summary_instead_of_deleting(self):
        os.makedirs(os.path.dirname(self.summary), exist_ok=True)
        with open(self.summary, "w", encoding="utf-8") as f:
            f.write("之前的摘要")
        self.bot.reset_session(IDENT)
        self.assertFalse(os.path.isfile(self.summary))
        archive = os.path.join(os.path.dirname(self.summary), "archive")
        files = os.listdir(archive)
        self.assertEqual(len(files), 1)
        with open(os.path.join(archive, files[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), "之前的摘要")
        # 归档目录不能被当成"当前摘要"读回
        self.assertNotIn(IDENT, self.bot._load_summaries())

    def test_pure_bracket_command_is_reachable(self):
        """纯【清空会话】曾落进"未知指令"分支被吞掉，文档写法反而不生效。"""
        from tests.test_tavern import OWNER
        self.bot.client = type("C", (), {"sent": [], "send_private":
                                         lambda self, uid, text: self.sent.append(text) or True})()
        self.bot.summaries[IDENT] = "旧摘要"
        self.bot._handle(OWNER, "【清空会话】")
        self.assertNotIn(IDENT, self.bot.summaries)
        self.assertTrue(any("重新开始" in t for t in self.bot.client.sent))

    def test_undeclared_user_cannot_reset_session(self):
        """未声明身份的人不能让别人掉会话（命令只对已知身份生效）。"""
        self.bot.summaries[IDENT] = "旧摘要"
        self.bot._handle(999999, "【清空会话】")
        self.assertIn(IDENT, self.bot.summaries)


class WorldlogTrimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = os.path.join(self.tmp, "worldlog")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_trim_keeps_new_events(self):
        from qqbot import worldlog
        worldlog.append_events(IDENT, [{"keys": ["a"], "content": "旧事件"}], base=self.base)
        old = worldlog.read_events(IDENT, base=self.base)
        cutoff = max(e["ts"] for e in old)
        # 提炼期间新触发的事件
        worldlog.append_events(IDENT, [{"keys": ["b"], "content": "新事件"}], base=self.base)
        left = worldlog.trim_events(IDENT, cutoff, base=self.base)
        self.assertEqual(left, 1)
        rows = worldlog.read_events(IDENT, base=self.base)
        self.assertEqual([r["content"] for r in rows], ["新事件"])

    def test_trim_keeps_new_events_even_with_same_clock_tick(self):
        """Windows 的 time.time() 可能两次返回同一值：新事件不能被 cutoff 误删。"""
        from qqbot import worldlog
        with mock.patch.object(worldlog.time, "time", return_value=1234.5):
            worldlog.append_events(IDENT, [{"keys": ["a"], "content": "旧事件"}],
                                   base=self.base)
            cutoff = max(e["ts"] for e in worldlog.read_events(IDENT, base=self.base))
            worldlog.append_events(IDENT, [{"keys": ["b"], "content": "新事件"}],
                                   base=self.base)
            left = worldlog.trim_events(IDENT, cutoff, base=self.base)
        self.assertEqual(left, 1)
        self.assertEqual([r["content"] for r in worldlog.read_events(IDENT, base=self.base)],
                         ["新事件"])

    def test_trim_removes_file_when_nothing_left(self):
        from qqbot import worldlog
        worldlog.append_events(IDENT, [{"keys": ["a"], "content": "事件"}], base=self.base)
        cutoff = max(e["ts"] for e in worldlog.read_events(IDENT, base=self.base))
        self.assertEqual(worldlog.trim_events(IDENT, cutoff, base=self.base), 0)
        self.assertFalse(os.path.isfile(worldlog.path_for(IDENT, self.base)))


if __name__ == "__main__":
    unittest.main()
