# -*- coding: utf-8 -*-
"""聊天记忆质量：检索排序 / 去重 / 合并安全的度量与回归。

这个文件既是回归测试，也是**记忆能力度量表**：

    python -m unittest tests.test_memory_quality -v

改动"检索打分、写入去重、睡前提炼/合并"后跑它，就能看出召回质量有没有变差。
测试会打印 Hit@1 / Hit@3 / MRR 三个指标（当前基线见测试末尾的断言语义）。

覆盖：
- 记忆安全：replace_important 只删指定 id、world 轨道不被改写、合并保留未采用条目
- 写入质量：同一内容不重复入库（长期记忆/重要记忆）、关键词合并
- 检索质量：关键词命中优先、长条目不再靠长度压过精确短条目、近度方向正确
- 数值健壮：None/NaN/坏字符串不再抛异常
"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.test_tavern import make_bot  # noqa: E402

IDENT = "主人"


class FakeLLM:
    """只回放脚本化 JSON，不做网络调用。"""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.chat_calls = []
        self.json_calls = []

    def chat(self, messages, temperature=None, max_tokens=None, call_type="chat"):
        self.chat_calls.append(messages)
        return "（摘要）"

    def chat_json(self, messages, temperature=0.2, call_type="chat", max_tokens=None):
        self.json_calls.append(messages)
        return self.script.pop(0) if self.script else None


def write_chatlog(tmp, identity, files):
    """files = {"第1周周一": ["- 第1周周一上午 **主人**：内容", ...]}"""
    d = os.path.join(tmp, "chatlog", identity)
    os.makedirs(d, exist_ok=True)
    for day, lines in files.items():
        with open(os.path.join(d, "%s.md" % day), "w", encoding="utf-8") as f:
            f.write("# 聊天记录 %s %s\n\n" % (identity, day))
            for ln in lines:
                f.write(ln + "\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        self.bot.llm = FakeLLM()
        self.mem = self.bot.memory

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def story_rows(self, track=None):
        conn = self.mem.conn
        if track:
            return conn.execute(
                "SELECT content FROM important_memories WHERE identity=? AND track=? "
                "ORDER BY id", (IDENT, track)).fetchall()
        return conn.execute(
            "SELECT content FROM important_memories WHERE identity=? ORDER BY id",
            (IDENT,)).fetchall()

    def counts(self):
        conn = self.mem.conn
        return {
            "longterm": conn.execute(
                "SELECT COUNT(*) FROM memories WHERE identity=?", (IDENT,)).fetchone()[0],
            "story": conn.execute(
                "SELECT COUNT(*) FROM important_memories WHERE identity=? AND track='story'",
                (IDENT,)).fetchone()[0],
            "world": conn.execute(
                "SELECT COUNT(*) FROM important_memories WHERE identity=? AND track='world'",
                (IDENT,)).fetchone()[0],
        }


# ---------------------------------------------------------------- 写入安全

class MemoryWriteSafetyTest(Base):
    def test_replace_important_scope_ids_only_deletes_those(self):
        for text in ("第一条", "第二条", "第三条"):
            self.mem.save_important(IDENT, text, 1, 5)
        self.mem.save_important(IDENT, "世界书剧情", 0, 5, track="world")
        ids = [r[0] for r in self.mem.conn.execute(
            "SELECT id FROM important_memories WHERE identity=? AND track='story' "
            "ORDER BY id", (IDENT,)).fetchall()]
        # 只替换第一条：其余 story 与 world 必须原样保留
        self.mem.replace_important(IDENT, [("第一条（改写）", 1, 5)], scope_ids=[ids[0]])
        got = sorted(r[0] for r in self.story_rows("story"))
        self.assertEqual(got, sorted(["第一条（改写）", "第二条", "第三条"]))
        self.assertEqual(len(self.story_rows("world")), 1)

    def test_replace_important_legacy_default_still_clears_story_track(self):
        self.mem.save_important(IDENT, "旧的", 1, 5)
        self.mem.save_important(IDENT, "世界", 0, 5, track="world")
        self.mem.replace_important(IDENT, [("新的", 1, 5)])
        self.assertEqual([r[0] for r in self.story_rows("story")], ["新的"])
        self.assertEqual(len(self.story_rows("world")), 1)

    def test_list_important_ids_track_filter_and_no_limit(self):
        self.mem.save_important(IDENT, "故事", 1, 5)
        self.mem.save_important(IDENT, "世界", 0, 5, track="world")
        self.assertEqual(len(self.mem.list_important_ids(IDENT, track="story")), 1)
        self.assertEqual(len(self.mem.list_important_ids(IDENT, track="world")), 1)
        self.assertEqual(len(self.mem.list_important_ids(IDENT, limit=None)), 2)

    def test_consolidate_keeps_unmerged_and_world_rows(self):
        # 超过上限触发合并；LLM 只合并前两条，第三条与 world 必须留下
        self.bot.config["sleep"]["max_important"] = 2
        self.mem.save_important(IDENT, "第1周周一 一起做饭", 1, 5)
        self.mem.save_important(IDENT, "第1周周一 一起吃饭", 1, 5)
        self.mem.save_important(IDENT, "第1周周二 一起散步", 1, 5)
        self.mem.save_important(IDENT, "世界书剧情事件", 0, 5, track="world")
        ids = [r[0] for r in self.mem.conn.execute(
            "SELECT id FROM important_memories WHERE identity=? AND track='story' "
            "ORDER BY id", (IDENT,)).fetchall()]
        self.bot.llm = FakeLLM([[
            {"merge": [ids[0], ids[1]], "content": "第1周周一 一起做饭和吃饭"},
        ]])
        self.bot.sleep.consolidate(IDENT)
        contents = [r[0] for r in self.story_rows("story")]
        self.assertIn("第1周周一 一起做饭和吃饭", contents)
        self.assertIn("第1周周二 一起散步", contents)   # 未参与合并 → 保留
        self.assertEqual(len(self.story_rows("world")), 1)  # world 轨道未被复制/删除


class MemoryDedupTest(Base):
    def test_longterm_identical_content_not_duplicated(self):
        self.mem.save_longterm(IDENT, "我最喜欢的饮料是茉莉花茶", ["茉莉花茶"])
        self.mem.save_longterm(IDENT, "我最喜欢的饮料是茉莉花茶！", ["茶"])
        self.assertEqual(self.counts()["longterm"], 1)
        row = self.mem.conn.execute(
            "SELECT keywords FROM memories WHERE identity=?", (IDENT,)).fetchone()
        self.assertIn("茉莉花茶", row[0])
        self.assertIn("茶", row[0])

    def test_important_identical_content_not_duplicated(self):
        self.mem.save_important(IDENT, "第1周周一 一起做饭", 1, 5)
        self.mem.save_important(IDENT, "第1周周一  一起做饭。", 1, 5)
        self.assertEqual(self.counts()["story"], 1)

    def test_different_content_still_inserted(self):
        self.mem.save_important(IDENT, "第1周周一 一起做饭", 1, 5)
        self.mem.save_important(IDENT, "第1周周二 一起散步", 1, 5)
        self.assertEqual(self.counts()["story"], 2)


class MemoryNumericGuardTest(Base):
    def test_clamp_accepts_junk_values(self):
        from qqbot.memstore.local import _clamp_delta, _clamp_importance
        self.assertEqual(_clamp_delta(None), 0)
        self.assertEqual(_clamp_delta("abc"), 0)
        self.assertEqual(_clamp_delta(float("nan")), 0)
        self.assertEqual(_clamp_delta(float("inf")), 0)
        self.assertEqual(_clamp_delta("12"), 12)
        self.assertEqual(_clamp_importance(None), 5)
        self.assertEqual(_clamp_importance(float("nan")), 5)
        self.assertEqual(_clamp_importance(99), 10)

    def test_save_important_with_junk_delta_does_not_raise(self):
        self.assertTrue(self.mem.save_important(IDENT, "第1周周一 一起做饭", None, "x"))

    def test_affection_without_memory_plugin_does_not_raise(self):
        self.bot.affection._db = lambda: None
        self.assertEqual(self.bot.affection.apply_delta(IDENT, 5), 305)
        self.bot.affection.adjust(IDENT, 5)  # 不应抛异常


# ---------------------------------------------------------------- 检索质量

GOLDEN = [
    # (记忆内容, 关键词)
    ("第1周周一上午 一起在厨房做了番茄炒蛋和味增汤", ["番茄炒蛋", "味增汤"]),
    ("第1周周三晚上 在欢乐谷坐了过山车，约定下次再坐摩天轮", ["欢乐谷", "过山车", "摩天轮"]),
    ("第1周周六晚上 用手机拍了一段视频", ["视频", "录像", "拍摄"]),
    ("第2周周二下午 下雨天在家看了一部老电影《言叶之庭》", ["下雨", "电影", "言叶之庭"]),
    ("第2周周五深夜 因为忘记买牛奶吵了一架，后来和好了", ["牛奶", "吵架", "和好"]),
    ("第3周周一早上 收到你送的蓝色围巾，说会天天戴着", ["围巾", "礼物", "蓝色"]),
    ("第3周周三晚上 一起学了做戚风蛋糕，烤糊了两次", ["戚风蛋糕", "烘焙"]),
    ("第3周周五晚上 一起去河边放烟花，很浪漫", ["烟花", "河边", "浪漫"]),
    ("第4周周一上午 一起去医院陪打疫苗，很勇敢", ["医院", "疫苗", "勇敢"]),
    ("第4周周三下午 一起在图书馆复习考试", ["图书馆", "复习", "考试"]),
    ("第4周周六深夜 聊到很晚，说了很多心里话", ["谈心", "心里话"]),
    ("第5周周二下午 一起去了海边捡贝壳", ["海边", "贝壳"]),
    ("第5周周四晚上 学会了做咖喱饭", ["咖喱饭", "咖喱"]),
    # 长干扰项：常见 2-gram 很多，但不是上面任何查询的答案
    ("第1周周六晚上 主人今天在家做了很多事，先做饭然后看电影，后来又聊天，"
     "聊到很晚才睡，期间还说到电视剧和天气，最后一起收拾了房间互道晚安",
     ["聊天"]),
    ("第1周周日中午 一起去超市买了菜和水果", ["超市", "买水果"]),
]

# 长期记忆（【记住X】层）；与重要记忆放在同一竞争场里
LONGTERM = [
    ("哥哥生日是 6 月 1 日", ["生日", "6月1日"]),
    ("我不吃香菜，点菜要注意", ["香菜", "忌口"]),
    ("家里的猫叫团子", ["猫", "团子"]),
]

# (查询, 期望命中的正文片段, 类别)
QUERIES = [
    # 关键词直命中
    ("过山车好玩吗", "过山车", "关键词"),
    ("戚风蛋糕怎么做", "戚风蛋糕", "关键词"),
    ("我送你的蓝色围巾你还戴吗", "蓝色围巾", "关键词"),
    ("我们为了牛奶吵架那次", "牛奶", "关键词"),
    ("那部电影叫什么来着", "老电影", "关键词"),
    ("番茄炒蛋怎么做", "番茄炒蛋", "关键词"),
    ("味增汤还要再喝吗", "味增汤", "关键词"),
    ("放烟花那天真好看", "烟花", "关键词"),
    ("打疫苗的时候我哭了吗", "疫苗", "关键词"),
    ("图书馆复习到几点", "图书馆", "关键词"),
    ("在海边捡的贝壳还在吗", "贝壳", "关键词"),
    ("咖喱饭是你教我的吧", "咖喱饭", "关键词"),
    ("那段视频还在吗", "拍了一段视频", "关键词"),
    # 改写（关键词表里没有的问法，靠词重叠命中）
    ("我们那天聊到半夜才睡", "心里话", "改写"),
    ("去医院的记忆有点模糊", "医院", "改写"),
    ("忘记买什么吵起来了", "吵了一架", "改写"),
    ("在欢乐谷坐的那个很刺激", "过山车", "改写"),
    ("你做的那道味增汤", "味增汤", "改写"),
    ("下雨天看的那部片子", "老电影", "改写"),
    # 长期记忆层
    ("生日快到了呢", "6 月 1 日", "长期记忆"),
    ("我不吃什么来着", "香菜", "长期记忆"),
    ("家里的猫叫什么", "团子", "长期记忆"),
    # 干扰项抵抗（长条不能压过精确记忆）
    ("戚风蛋糕要烤多久", "戚风蛋糕", "抗干扰"),
    ("咖喱饭怎么做才好吃", "咖喱饭", "抗干扰"),
    ("那条围巾你天天戴吗", "蓝色围巾", "抗干扰"),
    # 剧情时间窗（走 maybe_recall 的时间窗分支）
    ("第2周周五发生了什么", "牛奶", "时间窗"),
    ("第4周周三下午的事", "图书馆", "时间窗"),
    ("上周三下午我们在做什么", "图书馆", "时间窗"),
    ("第1周周三晚上呢", "过山车", "时间窗"),
    # 多主题（不应只返回一个主题）
    ("欢乐谷和戚风蛋糕都记得吗", "欢乐谷", "多主题"),
    ("还记得牛奶和围巾的事吗", "牛奶", "多主题"),
]


class RetrievalQualityTest(Base):
    def setUp(self):
        super().setUp()
        # 剧情时钟设到第 5 周：让"上周X"这类相对时间窗能解析出来
        self.bot.timeline.clock["week"] = 5
        for content, keys in GOLDEN:
            self.mem.save_important(IDENT, content, 1, 6, keywords=keys)
        for content, keys in LONGTERM:
            self.mem.save_longterm(IDENT, content, keys)

    def _rank(self, query):
        """走机器人真正使用的统一召回路径（四路候选 → 合并 → 重排）。"""
        hits = self.bot.recall.gather_candidates(IDENT, query)
        return [h.text for h in self.bot.recall.rerank(hits, limit=3)]

    def _rank_pipeline(self, query):
        """时间窗等需要 judge/时间解析的查询走完整管线。"""
        block, log_block = self.bot.recall.maybe_recall(0, IDENT, query)
        return "%s\n%s" % (block or "", log_block or "")

    def test_golden_queries_quality(self):
        hit1 = hit3 = 0
        rr = 0.0
        by_cat = {}
        misses = []
        for query, expect, cat in QUERIES:
            if cat == "时间窗":
                text = self._rank_pipeline(query)
                ranked = [text] if expect in text else []
            else:
                ranked = self._rank(query)
            pos = next((i for i, c in enumerate(ranked) if expect in c), None)
            if pos == 0:
                hit1 += 1
            if pos is not None:
                hit3 += 1
                rr += 1.0 / (pos + 1)
            else:
                misses.append("%s（期望含「%s」）→ %s" % (
                    query, expect, (ranked[0][:28] if ranked else "（空）")))
            agg = by_cat.setdefault(cat, [0, 0])
            agg[0] += 1
            agg[1] += 1 if pos is not None else 0
        n = len(QUERIES)
        mrr = rr / n
        print("\n[记忆检索质量] 查询 %d 条：Hit@1=%.2f Hit@3=%.2f MRR=%.2f"
              % (n, hit1 / n, hit3 / n, mrr))
        for cat, (total, ok) in sorted(by_cat.items()):
            print("   %-6s %2d/%2d" % (cat, ok, total))
        for line in misses:
            print("   未命中：%s" % line)
        # 基线（改动检索后必须仍满足；指标只会更好不会更差）
        self.assertGreaterEqual(hit3 / n, 0.9, "Hit@3 退化：%s" % misses)
        self.assertGreaterEqual(hit1 / n, 0.65)
        self.assertGreaterEqual(mrr, 0.75)

    def test_longterm_layer_is_retrievable(self):
        ranked = self._rank("生日快到了呢")
        self.assertTrue(any("6 月 1 日" in c for c in ranked))

    def test_multi_topic_query_returns_both(self):
        ranked = self._rank("还记得牛奶和围巾的事吗")
        self.assertTrue(any("牛奶" in c for c in ranked))
        self.assertTrue(any("围巾" in c for c in ranked))

    def test_long_distractor_does_not_beat_precise_memory(self):
        """长度偏置回归：长条目靠 2-gram 数量堆分曾能压过精确短条目。"""
        ranked = self._rank("戚风蛋糕怎么做")
        self.assertIn("戚风蛋糕", ranked[0])

    def test_overlap_term_is_bounded_for_long_memories(self):
        """重叠项必须有界 + 长文档归一：旧实现用原始 2-gram 命中个数（×10），
        查询越长、记忆越长，这一项就越能堆到几百上千分，把"关键词命中(300)"
        和 importance/delta 权重完全淹没。现在 = 命中比例（≤1）×10×长度归一。
        """
        from qqbot.memstore.local import _overlap_ratio
        from qqbot import textutil
        recall = self.bot.recall
        query = "还记得我们第一次去欢乐谷坐过山车和摩天轮的那天下午吗"
        grams = textutil.ngrams(query)
        long_blob = "今天把聊天记录整理了一遍。" + query + "。" + "后来又聊了很久。" * 10
        self.assertLessEqual(_overlap_ratio(grams, long_blob), 1.0)
        # 长度归一：长转录块的字面重叠按 BM25 风格打折
        self.assertLess(recall._length_norm(300), recall._length_norm(60))
        self.assertAlmostEqual(recall._length_norm(60), 1.0, places=6)
        # 现实场景（长条只是零散共词，不是把查询抄进去）：精确记忆必须赢
        self.mem.save_important(
            IDENT, "第3周周三晚上 一起学了做戚风蛋糕，烤糊了两次", 1, 6,
            keywords=["戚风蛋糕"])
        self.mem.save_important(
            IDENT, "第1周周六晚上 主人今天在家做了很多事，先做饭然后看电影，"
                   "后来又聊天，聊到很晚才睡，期间还说到电视剧和天气和蛋糕",
            0, 5, keywords=["聊天"])
        ranked = self._rank("戚风蛋糕怎么做来着")
        self.assertIn("戚风蛋糕", ranked[0])

    def test_keyword_exact_hit_wins(self):
        ranked = self._rank("还记得过山车吗")
        self.assertIn("过山车", ranked[0])

    def test_time_window_query_returns_that_day(self):
        rows = self.mem.list_important_by_time(IDENT, [("第2周周五", None)])
        self.assertTrue(rows)
        self.assertTrue(all("第2周周五" in c for c, _d in rows))


class ChatlogRecencyTest(Base):
    def test_newer_entry_ranks_first_on_tie(self):
        write_chatlog(self.tmp, IDENT, {
            "第1周周一": ["- 第1周周一上午 **主人**：今天一起看了电影"],
            "第2周周一": ["- 第2周周一上午 **主人**：今天一起看了电影"],
        })
        hits = self.bot.recall.search_logs(IDENT, "电影", top=2)
        self.assertEqual(len(hits), 2)
        # 重叠度相同 → 必须由近度决定：更近的第2周排前面
        self.assertEqual(hits[0][1], "第2周周一上午")

    def test_time_window_ranks_newer_first(self):
        write_chatlog(self.tmp, IDENT, {
            "第1周周一": ["- 第1周周一上午 **主人**：我们去游乐园了"],
            "第2周周一": ["- 第2周周一上午 **主人**：我们去游乐园了"],
        })
        hits = self.bot.recall.search_logs(IDENT, "游乐园", top=2,
                                           time_windows=[("第1周周一", None),
                                                         ("第2周周一", None)])
        self.assertEqual(hits[0][1], "第2周周一上午")


if __name__ == "__main__":
    unittest.main()
