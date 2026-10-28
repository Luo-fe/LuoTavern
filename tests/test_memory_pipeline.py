# -*- coding: utf-8 -*-
"""记忆管线（第二轮改进）：统一重排 / 生命周期强化 / 注入预算 / 增量向量索引。

对应 docs/memory-capability.md 第 4 节的第 2~6 步，每步都有对应断言：
- 步骤2 统一 rerank：多路证据合并、语义精度守门、可解释来源
- 步骤3 生命周期：召回强化（importance+1 / recall_count）、剧情时间接近度
- 步骤4 提炼门槛：story_time / keywords / importance 缺失时规则兜底
- 步骤5 注入预算：按块/按行裁剪，不腰斩记忆，优先保留高价值块
- 步骤6 索引：增量嵌入（只嵌入新增内容）、零副作用探测
"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

import numpy as np  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot.bot_core import BotCore  # noqa: E402
from qqbot.recall import RecallHit  # noqa: E402
from tests.test_tavern import make_bot  # noqa: E402

IDENT = "主人"


class CountingEmbedder:
    """按特征词构造向量的假嵌入器，并记录每次嵌入的文本（验证增量）。"""

    FEATURES = ["视频", "录像", "拍摄", "欢乐谷", "过山车", "草莓", "蛋糕", "小狗"]

    def __init__(self):
        self.calls = []          # 每次 embed() 的文本列表
        self.embedded = []       # 被嵌入过的文本总数

    def available(self):
        return True

    def loaded(self):
        return True

    def model_cached(self):
        return True

    def embed(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        self.embedded.extend(texts)
        out = []
        for t in texts:
            v = np.array([1.0 if f in t else 0.0 for f in self.FEATURES], dtype="float32")
            n = float(np.linalg.norm(v))
            out.append(v if n == 0 else v / n)
        return out

    @staticmethod
    def cosine(a, b):
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        self.bot.llm.json_script = []

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rerank(self, query, **kw):
        hits = self.bot.recall.gather_candidates(IDENT, query, **kw)
        return self.bot.recall.rerank(hits)


class UnifiedRerankTest(Base):
    def test_keyword_and_vector_evidence_are_merged(self):
        """同一件事被关键词和向量同时命中 → 合并为一条（两路一致），而不是两条挤名额。"""
        mem = self.bot.memory
        mem._embedder = CountingEmbedder()
        mem.save_important(IDENT, "第1周周六晚上 哥哥给青柠拍了一段视频", 10, 8,
                           keywords=["视频", "录像"])
        ranked = self.rerank("那段视频")
        self.assertEqual(len(ranked), 1)
        hit = ranked[0]
        self.assertIn("视频", hit.keywords)
        self.assertGreater(hit.vector, 0.0)          # 向量证据被合并进来
        self.assertIn("+", hit.detail)               # 可解释：来源叠加

    def test_semantic_guard_drops_keyword_only_noise(self):
        """语义引擎有信心的命中在场时，只有（可能标错的）关键词命中的记忆不进入注入块。"""
        mem = self.bot.memory
        mem._embedder = CountingEmbedder()
        mem.save_important(IDENT, "第1周周六晚上 哥哥给青柠拍了一段视频", 10, 8,
                           keywords=["视频", "录像"])
        mem.save_important(IDENT, "第1周周六晚上 一起吃了草莓蛋糕", 5, 6,
                           keywords=["视频"])       # 关键词错标
        texts = [h.text for h in self.rerank("那段视频")]
        self.assertTrue(any("拍了一段视频" in t for t in texts))
        self.assertFalse(any("草莓蛋糕" in t for t in texts))

    def test_guard_can_be_disabled(self):
        mem = self.bot.memory
        mem._embedder = CountingEmbedder()
        self.bot.recall.vector_guard = False
        mem.save_important(IDENT, "第1周周六晚上 哥哥给青柠拍了一段视频", 10, 8, keywords=["视频"])
        mem.save_important(IDENT, "第1周周六晚上 一起吃了草莓蛋糕", 5, 6, keywords=["视频"])
        texts = [h.text for h in self.rerank("那段视频")]
        self.assertTrue(any("草莓蛋糕" in t for t in texts))

    def test_open_topic_query_keeps_multiple_topics(self):
        """没有任何语义置信命中时，多主题关键词查询不应被守门误删。"""
        mem = self.bot.memory
        mem.save_important(IDENT, "第1周 一起去了欢乐谷坐过山车", 5, 6, keywords=["欢乐谷"])
        mem.save_important(IDENT, "第1周 一起吃了草莓蛋糕", 5, 6, keywords=["蛋糕"])
        texts = [h.text for h in self.rerank("欢乐谷和蛋糕")]
        self.assertTrue(any("欢乐谷" in t for t in texts))
        self.assertTrue(any("蛋糕" in t for t in texts))

    def test_every_hit_is_explainable(self):
        mem = self.bot.memory
        mem.save_important(IDENT, "第1周 一起去了欢乐谷坐过山车", 5, 6, keywords=["欢乐谷"])
        ranked = self.rerank("还记得欢乐谷吗")
        self.assertTrue(ranked)
        self.assertTrue(ranked[0].detail)
        self.assertGreater(ranked[0].score, 0)


class LifecycleTest(Base):
    def test_recall_reinforces_importance(self):
        mem = self.bot.memory
        mem.save_important(IDENT, "第1周 一起去了欢乐谷坐过山车", 5, 6, keywords=["欢乐谷"])
        before = mem.important_candidates(IDENT)[0]
        self.assertEqual(before[3], 6)
        self.bot.recall.reranked_recall(IDENT, "还记得欢乐谷吗")
        after = mem.important_candidates(IDENT)[0]
        self.assertEqual(after[3], 7)        # importance +1
        self.assertEqual(after[7], 1)        # recall_count +1
        self.assertIsNotNone(after[8])       # last_recalled_at

    def test_reinforcement_raises_ranking(self):
        mem = self.bot.memory
        mem.save_important(IDENT, "第1周 一起去了欢乐谷坐过山车", 5, 6, keywords=["欢乐谷"])
        mem.mark_recalled(IDENT, ["第1周 一起去了欢乐谷坐过山车"])  # 连记 4 次
        mem.mark_recalled(IDENT, ["第1周 一起去了欢乐谷坐过山车"])
        mem.mark_recalled(IDENT, ["第1周 一起去了欢乐谷坐过山车"])
        mem.mark_recalled(IDENT, ["第1周 一起去了欢乐谷坐过山车"])
        hit = self.rerank("欢乐谷")[0]
        self.assertGreater(hit.score, 0.5 + 3.0)   # 基础分 + 关键词 + 强化分

    def test_time_proximity_prefers_recent_story_week(self):
        """剧情时间更接近当前的记忆，在其它条件相同时排前面。"""
        mem = self.bot.memory
        self.bot.timeline.clock["week"] = 5
        mem.save_important(IDENT, "第1周周一上午 一起去了欢乐谷玩", 5, 6, keywords=["欢乐谷"])
        mem.save_important(IDENT, "第5周周一上午 一起去了欢乐谷玩", 5, 6, keywords=["欢乐谷"])
        ranked = self.rerank("欢乐谷")
        self.assertIn("第5周", ranked[0].text)
        self.assertGreater(ranked[0].score, ranked[1].score)

    def test_no_story_time_is_neutral(self):
        hit = RecallHit(text="没有任何时间戳的记忆", source="important")
        self.assertAlmostEqual(self.bot.recall._time_proximity(hit.story_time), 0.4, places=6)


class ExtractGateTest(Base):
    def test_keywords_and_story_time_are_filled_when_missing(self):
        mem = self.bot.memory
        mem.save_important(IDENT, "第2周周三晚上 一起学了做戚风蛋糕", 5, 7)
        row = mem.important_candidates(IDENT)[0]
        self.assertEqual(row[5], "第2周周三晚上")          # story_time 兜底
        self.assertTrue(row[4])                            # keywords 兜底非空

    def test_importance_derived_from_delta_when_missing(self):
        mem = self.bot.memory
        mem.save_important(IDENT, "第1周 一件很激动的事", 80, None)
        self.assertGreaterEqual(mem.important_candidates(IDENT)[0][3], 8)

    def test_explicit_keywords_are_kept(self):
        mem = self.bot.memory
        mem.save_important(IDENT, "第1周 一起去了欢乐谷", 5, 7, keywords=["欢乐谷", "过山车"])
        self.assertEqual(mem.important_candidates(IDENT)[0][4], ["欢乐谷", "过山车"])


class InjectBudgetTest(unittest.TestCase):
    def setUp(self):
        self.blocks = [
            "【常驻记忆】\n- 第一条很短的记忆",
            "【重要记忆】\n- " + "很长的记忆内容" * 60,
            "【回忆结果】\n- 低优先级内容",
        ]

    def test_keeps_whole_blocks_within_budget(self):
        out = BotCore._fit_blocks(self.blocks, 400)
        self.assertIn("【常驻记忆】", out)
        self.assertIn("【重要记忆】", out)
        self.assertNotIn("【回忆结果】", out)      # 低优先级块整体丢弃

    def test_cuts_on_line_boundary(self):
        out = BotCore._fit_blocks(self.blocks, 120)
        self.assertTrue(out.endswith("…（其余低优先级记忆已省略）"))
        self.assertNotIn("很长的记忆内容…", out)   # 不是腰斩式截断
        for line in out.splitlines():
            self.assertFalse(line.endswith("很长的记"))

    def test_zero_budget_returns_empty(self):
        self.assertEqual(BotCore._fit_blocks(self.blocks, 0), "")

    def test_persistent_block_drops_whole_lines(self):
        lines = ["【常驻记忆】", "- 第一条", "- 第二条", "- 第三条"]
        out = BotCore._fit_block(lines, 16)
        self.assertIn("第一条", out)
        self.assertTrue(out.endswith("…（其余常驻记忆已省略）"))


class IncrementalVectorIndexTest(Base):
    def test_second_query_only_embeds_new_texts(self):
        mem = self.bot.memory
        emb = CountingEmbedder()
        mem._embedder = emb

        def memory_texts():
            return [t for t in emb.embedded
                    if t.startswith("重要记忆：") or t.startswith("长期记忆：")]

        mem.save_important(IDENT, "第1周 一起去了欢乐谷", 5, 6, keywords=["欢乐谷"])
        self.bot.recall.gather_candidates(IDENT, "欢乐谷")
        first = len(memory_texts())
        self.assertEqual(first, 1)
        # 再查一次：内容没变 → 不该重新嵌入（旧实现每次缓存失效都全量重嵌入）
        self.bot.recall.gather_candidates(IDENT, "欢乐谷")
        self.assertEqual(len(memory_texts()), first)
        # 新增一条：只嵌入新增的那条（不是全部重来）
        mem.save_important(IDENT, "第1周 一起吃了草莓蛋糕", 5, 6, keywords=["蛋糕"])
        self.bot.recall.gather_candidates(IDENT, "蛋糕")
        self.assertEqual(len(memory_texts()), first + 1)

    def test_stats_do_not_load_model(self):
        mem = self.bot.memory

        class Boom:
            def loaded(self):
                return False

            def available(self):
                raise AssertionError("stats() 不应触发模型加载")

            def model_cached(self):
                return False

            def error(self):
                return None

        mem._embedder = Boom()
        st = mem.stats()
        self.assertFalse(st["vector"]["available"])


if __name__ == "__main__":
    unittest.main()
