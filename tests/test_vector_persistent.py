# -*- coding: utf-8 -*-
"""P1 常驻记忆 + P2 本地向量检索 + keywordize 回填命令测试。

向量部分用可注入的假嵌入器（按特征词命中构造向量），真实 fastembed 模型
在数据验证阶段单独跑，不依赖网络。
"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.support import GUEST, declare, last_user_content, make_bot


class FakeEmbedder:
    """按特征词构造归一化向量：含该词=1，否则 0。"""
    FEATURES = ["视频", "录像", "拍摄", "拍", "公园", "生日", "电影", "小狗"]

    def available(self):
        return True

    def embed(self, texts):
        out = []
        for t in texts:
            v = np.array([1.0 if f in t else 0.0 for f in self.FEATURES], dtype="float32")
            n = float(np.linalg.norm(v))
            out.append(v if n == 0 else v / n)
        return out

    @staticmethod
    def cosine(a, b):
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


class UnavailableEmbedder:
    def available(self):
        return False

    def embed(self, texts):
        return None

    @staticmethod
    def cosine(a, b):
        return 0.0


class VectorPersistentTest(unittest.TestCase):
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

    # ---------- P1 常驻记忆 ----------
    def test_persistent_memories_in_system_prompt(self):
        self.bot.memory.save_important("梦梦", "第1周周六晚上 青柠和哥哥约定永远在一起", 20, 10)
        self.bot.memory.save_important("梦梦", "第1周周六晚上 一起吃了草莓蛋糕", 5, 8)
        self.bot._handle(GUEST, "你好呀")
        sys_prompt = self.bot.history["梦梦"][0]["content"]
        self.assertIn("【常驻记忆", sys_prompt)
        self.assertIn("约定永远在一起", sys_prompt)
        self.assertNotIn("草莓蛋糕", sys_prompt)  # importance 8 不常驻

    def test_persistent_cap(self):
        self.bot.config["memory"]["persistent_max"] = 3
        self.bot.memory._persistent_max = 3
        for i in range(5):
            self.bot.memory.save_important("梦梦", "第1周周六晚上 重要事件%d号" % i, 10, 10)
        rows = self.bot.memory.list_persistent("梦梦")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r[2] >= 9 for r in rows))

    # ---------- P2 向量检索 ----------
    def test_vector_recall_direct(self):
        self.bot.memory._embedder = FakeEmbedder()
        self.bot.memory.save_important("梦梦", "第1周周六晚上 哥哥给青柠拍了一段视频", 10, 8)
        hits = self.bot.memory.vector_recall("梦梦", "我们上次拍的东西")
        self.assertTrue(hits)
        self.assertEqual(hits[0][0], "important")
        self.assertIn("拍了一段视频", hits[0][1])

    def test_vector_recall_through_pipeline(self):
        """词面不重叠（"上次拍的东西" vs "视频"）时向量兜底命中。"""
        self.bot.memory._embedder = FakeEmbedder()
        self.bot.memory.save_important("梦梦", "第1周周六晚上 哥哥给青柠拍了一段视频", 10, 8)
        self.bot._handle(GUEST, "我们上次拍的东西还在吗")
        user = last_user_content(self.bot)
        self.assertIn("【回忆结果】", user)
        self.assertIn("拍了一段视频", user)

    def test_vector_disabled(self):
        self.bot.memory._embedder = FakeEmbedder()
        self.bot.config["memory"]["vector"]["enable"] = False
        self.bot.memory.save_important("梦梦", "第1周周六晚上 哥哥给青柠拍了一段视频", 10, 8)
        self.assertEqual(self.bot.memory.vector_recall("梦梦", "视频"), [])

    def test_vector_unavailable_no_crash(self):
        self.bot.memory._embedder = UnavailableEmbedder()
        self.bot.memory.save_important("梦梦", "第1周周六晚上 哥哥给青柠拍了一段视频", 10, 8)
        self.assertEqual(self.bot.memory.vector_recall("梦梦", "视频"), [])
        self.bot._handle(GUEST, "【回忆拍的东西】")
        user = last_user_content(self.bot)
        self.assertNotIn("【回忆结果】", user)  # 无命中也不报错
        self.assertNotIn("【重要记忆】", user)

    def test_semantic_trigger_without_keyword(self):
        """通用语义触发：记忆没有关键词，"小狗"也能靠向量相似度触发回忆（零 LLM）。"""
        self.bot.memory._embedder = FakeEmbedder()
        self.bot.memory.save_important("梦梦", "第1周周六晚上 青柠是哥哥永远的小狗", 10, 8)
        self.bot.llm.json_script = []
        self.bot._handle(GUEST, "小狗")
        self.assertEqual(len(self.bot.llm.json_calls), 0)
        user = last_user_content(self.bot)
        self.assertIn("【重要记忆】", user)
        self.assertIn("永远的小狗", user)

    def test_semantic_trigger_below_threshold(self):
        """语义相似度低于阈值 → 不误触发回忆。"""
        self.bot.memory._embedder = FakeEmbedder()
        self.bot.memory.save_important("梦梦", "第1周周六晚上 一起吃了草莓蛋糕", 5, 6)
        self.bot._handle(GUEST, "小狗")
        user = last_user_content(self.bot)
        self.assertNotIn("【重要记忆】", user)
        self.assertNotIn("【回忆结果】", user)

    def test_consistency_prefers_agreed_memory(self):
        """一致性评分：关键词+向量两路一致的记忆优先注入，仅关键词命中的不混入。"""
        self.bot.memory._embedder = FakeEmbedder()
        self.bot.memory.save_important("梦梦", "第1周周六晚上 哥哥给青柠拍了一段视频", 10, 8,
                                       keywords=["视频", "录像"])
        self.bot.memory.save_important("梦梦", "第1周周六晚上 一起吃了草莓蛋糕", 5, 6,
                                       keywords=["视频"])  # 关键词命中但语义不相关
        cons = self.bot.memory.recall_consistent("梦梦", "那段视频", top=2)
        self.assertEqual(len(cons), 1)
        self.assertIn("拍了一段视频", cons[0][1])
        self.bot._handle(GUEST, "那段视频")
        user = last_user_content(self.bot)
        self.assertIn("【重要记忆】", user)
        self.assertIn("拍了一段视频", user)
        self.assertNotIn("草莓蛋糕", user)

    def test_consistency_falls_back_when_vector_unavailable(self):
        """向量不可用时一致性交集为空，关键词路径仍能正常触发。"""
        self.bot.memory._embedder = UnavailableEmbedder()
        self.bot.memory.save_important("梦梦", "第1周周六晚上 哥哥给青柠拍了一段视频", 10, 8,
                                       keywords=["视频", "录像"])
        self.bot._handle(GUEST, "视频")
        user = last_user_content(self.bot)
        self.assertIn("【重要记忆】", user)
        self.assertIn("拍了一段视频", user)

    # ---------- keywordize 回填 ----------
    def test_keywordize_backfill_with_fake_llm(self):
        import memory_ui

        db = os.path.join(self.tmp, "ui_memory.db")
        old_db = memory_ui.DB
        memory_ui.DB = db
        try:
            conn = memory_ui.connect()
            conn.execute(
                "INSERT INTO important_memories (identity, content, affect_delta, importance, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("梦梦", "第1周周六晚上 哥哥给青柠拍了一段视频", 10, 8, 1750000000.0))
            conn.execute("INSERT INTO memories (identity, content, created_at) VALUES (?, ?, ?)",
                         ("梦梦", "哥哥生日是6月1日", 1750000000.0))
            conn.commit()
            conn.close()

            class KeywordizeLLM:
                def __init__(self):
                    self.calls = []
                    self.script = [
                        [{"id": 1, "keywords": ["视频", "录像", "拍摄"]}],
                        [{"id": 1, "keywords": ["生日", "6月1日"]}],
                    ]

                def chat_json(self, messages, temperature=0.2, call_type="chat", max_tokens=None):
                    self.calls.append(messages)
                    return self.script.pop(0) if self.script else []

            llm = KeywordizeLLM()
            imp, mem, fail = memory_ui.keywordize(identity="梦梦", batch=20, force=False,
                                                  dry_run=False, llm=llm)
            self.assertEqual((imp, mem, fail), (1, 1, 0))
            conn = memory_ui.connect()
            row = conn.execute(
                "SELECT keywords FROM important_memories WHERE id=1").fetchone()
            self.assertEqual(memory_ui._load_keywords(row[0]), ["视频", "录像", "拍摄"])
            row = conn.execute("SELECT keywords FROM memories WHERE id=1").fetchone()
            self.assertEqual(memory_ui._load_keywords(row[0]), ["生日", "6月1日"])
            conn.close()
        finally:
            memory_ui.DB = old_db

    def test_keywordize_dry_run_counts(self):
        import memory_ui

        db = os.path.join(self.tmp, "ui_memory2.db")
        old_db = memory_ui.DB
        memory_ui.DB = db
        try:
            conn = memory_ui.connect()
            conn.execute(
                "INSERT INTO important_memories (identity, content, created_at) VALUES (?, ?, ?)",
                ("梦梦", "没有关键词的记忆", 1750000000.0))
            conn.execute(
                "INSERT INTO important_memories (identity, content, keywords, created_at) VALUES (?, ?, ?, ?)",
                ("梦梦", "已有关键词", "[\"视频\"]", 1750000000.0))
            conn.commit()
            conn.close()
            imp, mem, fail = memory_ui.keywordize(identity="梦梦", dry_run=True)
            self.assertEqual(imp, 1)
            self.assertEqual(mem, 0)
            self.assertEqual(fail, 0)
        finally:
            memory_ui.DB = old_db


if __name__ == "__main__":
    unittest.main()
