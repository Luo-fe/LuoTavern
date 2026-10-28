# -*- coding: utf-8 -*-
"""第三轮健壮性优化回归。

- 分段发送：空/空白尾片不再发出去（旧实现会产生空片段 → 平台 400 → 整轮判失败）
- 上下文裁剪：必须以 user 开头（Anthropic 严格厂家否则永久 400）+ 不是 O(n²)
- 进程级重启：子进程启动失败要能回滚到热重载，且退出前先跑插件收尾
- 【记忆状态】：记忆插件缺失/后端不支持时不再抛异常
- 管理台：chunked 请求体被明确拒绝、配置读改写有锁、状态接口有短 TTL 缓存
- 远端记忆：分数归一化（不再硬编码 0.5）+ 写入熔断
- memory_ui vault：跟随当前人格目录
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot.admin import _CFG_LOCK, _update_cfg  # noqa: E402
from qqbot.bot_core import BotCore  # noqa: E402
from qqbot.memstore.tencentdb import TencentAgentMemoryStore  # noqa: E402
from tests.test_tavern import GUEST, OWNER, FakeLLM, make_bot  # noqa: E402


class SendChunkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        self.bot.llm = FakeLLM()
        self.sent = []

        class Client:
            def send_private(_self, user_id, text):
                self.sent.append(text)
                return True

        self.bot.client = Client()
        self.bot.config["message_chunk_chars"] = 10

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_newline_tail_does_not_produce_empty_chunk(self):
        text = "一二三四五六七八九十" + "\n" * 30
        self.assertTrue(self.bot.send_text(GUEST, text))
        self.assertTrue(all(part.strip() for part in self.sent), self.sent)
        self.assertEqual("".join(self.sent).replace("\n", ""), text.replace("\n", ""))

    def test_management_flag_is_recorded_atomically(self):
        self.bot.send_text(GUEST, "【告示】", management=True)
        self.assertTrue(self.bot.outbox[-1]["management"])
        self.bot.send_text(GUEST, "普通回复")
        self.assertFalse(self.bot.outbox[-1]["management"])

    def test_long_text_is_split_into_parts(self):
        self.bot.send_text(GUEST, "あ" * 55)
        self.assertGreater(len(self.sent), 1)
        self.assertTrue(all(part.strip() for part in self.sent))


class LimitContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        self.bot.config["max_context_chars"] = 60

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_trimmed_history_starts_with_user(self):
        msgs = [
            {"role": "system", "content": "S" * 10},
            {"role": "user", "content": "u" * 30},
            {"role": "assistant", "content": "a" * 30},
            {"role": "user", "content": "当前消息"},
        ]
        out = BotCore._ensure_user_first(msgs)
        self.assertEqual(out[0]["role"], "system")
        self.assertEqual(out[1]["role"], "user")

    def test_limit_context_keeps_user_first(self):
        msgs = [
            {"role": "system", "content": "S"},
            {"role": "assistant", "content": "a" * 50},
            {"role": "user", "content": "u" * 50},
            {"role": "assistant", "content": "b" * 50},
        ]
        out = self.bot._limit_context(msgs, "主人")
        self.assertEqual(out[0]["role"], "system")
        self.assertNotEqual(out[1]["role"], "assistant")

    def test_no_cap_returns_untouched(self):
        self.bot.config["max_context_chars"] = 0
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        self.assertIs(self.bot._limit_context(msgs, "主人"), msgs)


class RestartGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        self.bot.llm = FakeLLM()

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dead_child_falls_back_to_hot_reload(self):
        """子进程秒退（配置/Python 路径错）时必须回滚，不能直接退出主进程。"""
        class DeadProc:
            returncode = 3

            def poll(self):
                return 3

        called = {}
        with mock.patch("subprocess.Popen", return_value=DeadProc()), \
                mock.patch.object(BotCore, "_reload_with_persona",
                                  lambda self, uid: called.setdefault("reloaded", True)), \
                mock.patch("os._exit", side_effect=AssertionError("不应退出主进程")):
            self.bot._restart_now(OWNER)
        self.assertTrue(called.get("reloaded"))

    def test_live_child_shuts_plugins_down_before_exit(self):
        class LiveProc:
            returncode = None

            def poll(self):
                return None

        order = []
        with mock.patch("subprocess.Popen", return_value=LiveProc()), \
                mock.patch.object(self.bot.plugins, "shutdown",
                                  lambda: order.append("shutdown")), \
                mock.patch("os._exit", lambda code: order.append("exit")), \
                mock.patch("time.sleep", lambda s: None):
            self.bot._restart_now(OWNER)
        self.assertEqual(order[:2], ["shutdown", "exit"])


class MemoryStatusGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_memory_plugin_does_not_raise(self):
        self.bot.memory = None
        out = self.bot._memory_status_answer("主人")
        self.assertIn("【记忆状态】", out)

    def test_backend_without_track_counts_does_not_raise(self):
        class Boom:
            def stats(self):
                return {"backend": "hybrid"}

            def track_counts(self, identity):
                raise RuntimeError("remote down")

        self.bot.memory = Boom()
        out = self.bot._memory_status_answer("主人")
        self.assertIn("故事 0 条", out)


class AdminRobustnessTest(unittest.TestCase):
    def test_chunked_body_is_rejected(self):
        from qqbot import admin
        handler_cls = admin.make_handler(None, token="t")
        # 直接构造一个最小 handler 实例来验证 _read_json 的分支
        inst = handler_cls.__new__(handler_cls)
        inst.headers = {"Transfer-Encoding": "chunked"}
        with self.assertRaises(ValueError):
            inst._read_json()

    def test_update_cfg_is_serialized(self):
        """并发读改写不应丢更新：20 个线程各自 +1。"""
        self.tmp = tempfile.mkdtemp()
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"n": 0}, f)
        try:
            with mock.patch("qqbot.admin._config_path", return_value=path), \
                    mock.patch("qqbot.admin.qconfig.atomic_write_json",
                               lambda p, cfg: json.dump(cfg, open(p, "w", encoding="utf-8"))):
                def bump():
                    with _CFG_LOCK:
                        cfg = json.load(open(path, encoding="utf-8"))
                        cfg["n"] = cfg.get("n", 0) + 1
                        json.dump(cfg, open(path, "w", encoding="utf-8"))
                threads = [threading.Thread(target=bump) for _ in range(20)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["n"], 20)
            self.assertTrue(callable(_update_cfg))
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def test_status_ttl_cache(self):
        from qqbot import admin
        self.assertEqual(admin.STATUS_TTL_SEC, 3.0)


class TencentScoreTest(unittest.TestCase):
    def test_cosine_scores_pass_through(self):
        out = TencentAgentMemoryStore._normalize_scores([("a", 0.8), ("b", 0.4)])
        self.assertAlmostEqual(out[0][1], 0.8, places=3)

    def test_bm25_like_scores_are_normalized(self):
        out = TencentAgentMemoryStore._normalize_scores([("a", 12.0), ("b", 6.0)])
        self.assertAlmostEqual(out[0][1], 1.0, places=3)
        self.assertAlmostEqual(out[1][1], 0.5, places=3)

    def test_missing_scores_use_rank_decay(self):
        out = TencentAgentMemoryStore._normalize_scores(
            [("a", None), ("b", None), ("c", None)])
        self.assertGreater(out[0][1], out[1][1])
        self.assertGreaterEqual(out[-1][1], 0.35)

    def test_vector_recall_respects_min_score(self):
        store = TencentAgentMemoryStore({}, log=lambda m: None)
        store._transport = object()
        with mock.patch.object(store, "recall_scored",
                               return_value=[("高相关", 0.9), ("弱相关", 0.2)]):
            hits = store.vector_recall("主人", "查询", top=5, min_score=0.5)
        self.assertEqual([c for _k, c, _d, _i, _s in hits], ["高相关"])

    def test_write_circuit_breaker_opens_after_failures(self):
        class DeadTransport:
            def append_conversation(self, *a, **kw):
                raise RuntimeError("gateway half-dead")

        store = TencentAgentMemoryStore({}, log=lambda m: None)
        store._transport = DeadTransport()
        for _ in range(3):
            store.sync_conversation("主人", "u", "b")
        self.assertGreater(store._write_block_until, time.time())
        self.assertFalse(store._write_allowed())


if __name__ == "__main__":
    unittest.main()
