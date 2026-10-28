# -*- coding: utf-8 -*-
"""记忆库体检（只读）：给"记忆能力该从哪里改"提供可量化的起点。

用法：
    python scripts/memory_eval.py                 # 当前人格的记忆库
    python scripts/memory_eval.py --db path.db    # 指定库
    python scripts/memory_eval.py --query "过山车"  # 顺带跑一次检索冒烟
    python scripts/memory_eval.py --show-samples  # 打印重复条目样例（默认不打印正文）

报告内容：
- 规模：长期记忆 / 重要记忆（story、world 轨道）/ 好感度身份数
- 质量：重复条目比例、关键词覆盖率、剧情时间覆盖率、超长条目比例
- 压力：距离合并阈值（sleep.max_important）还有多少、预估索引重建成本
- 检索：给定查询的 Top5（关键词路径，零 LLM）

只读打开（mode=ro），不会迁移或修改数据库。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot import config as qconfig          # noqa: E402
from qqbot import persona                    # noqa: E402
from qqbot import textutil                   # noqa: E402


def _dedup_key(text):
    return re.sub(r"[\s，。！？、,.!?;；:：\"'“”‘’()（）【】\[\]-]+", "", str(text or ""))


def current_db_path():
    cfg = qconfig.load_config(use_env=False)
    cur = persona.current()
    if cur and cur.get("name"):
        ov = persona.data_overrides(cfg, cur["name"])
        return (ov.get("memory") or {}).get("db_path")
    rel = (cfg.get("memory") or {}).get("db_path", "data/memory.db")
    return rel if os.path.isabs(rel) else os.path.normpath(os.path.join(ROOT, rel))


def open_ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/").replace("?", "%3f"),
                           uri=True)


def width(text):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text))


def _make_ranker(db_path, cfg, vector=False):
    """用一个最小 shim 复用机器人真正的召回重排（不启动插件、不写记忆）。

    返回 (agent, close)。评测只做读取+重排，不触发召回强化（不改变 importance）。
    """
    from qqbot.memstore.local import SQLiteMemoryStore
    from qqbot.recall import RecallAgent

    class _Timeline:
        def __init__(self, clock):
            self.clock = clock

    class _Correct:
        def corrected_map(self, identity):
            return {}

    class _Chatlog:
        def read_entries(self, identity, dates=None):
            return []

        def recent_entries(self, identity, limit=20):
            return []

        def identity_dirs(self):
            return []

    class _Identity:
        def all_identities(self):
            return []

        def is_owner(self, user_id):
            return True

    class _Bot:
        pass

    mem_cfg = dict(cfg.get("memory") or {})
    mem_cfg.setdefault("vector", {})
    mem_cfg["vector"] = dict(mem_cfg.get("vector") or {})
    if not vector:
        mem_cfg["vector"]["enable"] = False

    bot = _Bot()
    bot.config = {"memory": mem_cfg, "recall": cfg.get("recall") or {}}
    bot.memory = SQLiteMemoryStore(mem_cfg, db_path=db_path, log=lambda m: None)
    bot.timeline = _Timeline(_read_clock(db_path))
    bot.correct = _Correct()
    bot.chatlog_store = _Chatlog()
    bot.identity = _Identity()
    bot.log = lambda m: None
    return RecallAgent(bot), bot.memory.close


def _read_clock(db_path):
    """读同人格目录下的 clock.json（拿当前剧情周，用于时间接近度）。"""
    p = os.path.join(os.path.dirname(db_path), "time", "clock.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def run_query_set(agent, queries, top=3):
    """跑一份真实查询集：queries=[{query, expect}]，返回 (指标, 未命中列表)。"""
    hit1 = hit3 = 0
    rr = 0.0
    misses = []
    for item in queries:
        query = str(item.get("query") or "").strip()
        expect = str(item.get("expect") or "").strip()
        identity = str(item.get("identity") or "").strip()
        if not query:
            continue
        if not identity:
            identity = agent.bot.memory.list_identities()[0] if hasattr(
                agent.bot.memory, "list_identities") else "主人"
        hits = agent.gather_candidates(identity, query)
        ranked = agent.rerank(hits, top)
        texts = [h.text for h in ranked]
        pos = next((i for i, t in enumerate(texts) if expect and expect in t), None)
        if pos == 0:
            hit1 += 1
        if pos is not None:
            hit3 += 1
            rr += 1.0 / (pos + 1)
        else:
            misses.append("  %s（期望含「%s」）→ %s" % (
                query, expect or "?", (texts[0][:30] if texts else "（无命中）")))
    n = max(1, len(queries))
    return {
        "n": len(queries),
        "hit1": hit1 / n,
        "hit3": hit3 / n,
        "mrr": rr / n,
        "misses": misses,
    }


def main():
    ap = argparse.ArgumentParser(description="记忆库体检（只读）")
    ap.add_argument("--db", default=None, help="memory.db 路径（默认当前人格）")
    ap.add_argument("--identity", default=None, help="只体检某个身份")
    ap.add_argument("--query", default=None, help="检索冒烟查询")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--show-samples", action="store_true",
                    help="打印重复条目样例（含正文，谨慎）")
    ap.add_argument("--queries", default=None,
                    help="真实查询集 JSON：[{query, expect, identity?}] → 输出 Hit@1/Hit@3/MRR")
    ap.add_argument("--vector", action="store_true",
                    help="评测时启用向量检索（需模型已缓存，否则会联网下载）")
    args = ap.parse_args()

    path = args.db or current_db_path()
    if not path or not os.path.isfile(path):
        print("找不到记忆库：%s" % path)
        return 2
    cfg = qconfig.load_config(use_env=False)

    # ---- 真实查询集评测（走机器人真正的召回重排）----
    if args.queries:
        if not os.path.isfile(args.queries):
            print("找不到查询集文件：%s" % args.queries)
            return 2
        with open(args.queries, encoding="utf-8") as f:
            queries = json.load(f)
        if isinstance(queries, dict):
            queries = queries.get("queries") or []
        if not isinstance(queries, list) or not queries:
            print("查询集格式应为 [{query, expect, identity?}, ...]")
            return 2
        agent, close = _make_ranker(path, cfg, vector=args.vector)
        try:
            res = run_query_set(agent, queries, top=args.top)
        finally:
            close()
        print("=" * 66)
        print("真实查询集评测：%s（%d 条）" % (args.queries, res["n"]))
        print("记忆库：%s" % path)
        print("=" * 66)
        print("Hit@1=%.2f  Hit@3=%.2f  MRR=%.2f%s"
              % (res["hit1"], res["hit3"], res["mrr"],
                 "" if args.vector else "（未启用向量，仅关键词/词重叠路径）"))
        for line in res["misses"]:
            print("  未命中：%s" % line)
        print("-" * 66)
        print("把未命中的查询补进 expect 或调整记忆关键词后重跑，即可观察改进效果。")
        return 0

    max_important = int(((cfg.get("sleep") or {}).get("max_important")) or 100)

    conn = open_ro(path)
    try:
        where, params = "", []
        if args.identity:
            where, params = " WHERE identity=?", [args.identity]

        rows = conn.execute(
            "SELECT identity, content, keywords, created_at FROM memories" + where,
            params).fetchall()
        imp = conn.execute(
            "SELECT identity, content, keywords, story_time, track, importance, affect_delta "
            "FROM important_memories" + where, params).fetchall()
        aff = conn.execute("SELECT COUNT(*) FROM affection" + where, params).fetchone()[0]

        print("=" * 66)
        print("记忆库体检：%s" % path)
        print("=" * 66)
        print("长期记忆（【记住X】）: %d 条" % len(rows))
        tracks = Counter(r[4] or "story" for r in imp)
        print("重要记忆合计      : %d 条  （story %d / world %d%s）"
              % (len(imp), tracks.get("story", 0), tracks.get("world", 0),
                 "".join(" / %s %d" % (k, v) for k, v in tracks.items()
                         if k not in ("story", "world"))))
        print("好感度身份数      : %d" % aff)
        print("-" * 66)

        # ---- 质量指标 ----
        def dup_stats(items, label):
            keys = Counter(_dedup_key(it[1]) for it in items)
            dup_rows = sum(v for v in keys.values() if v > 1)
            dup_groups = sum(1 for v in keys.values() if v > 1)
            ratio = (dup_rows / len(items) * 100) if items else 0.0
            print("%s：重复组 %d，涉及条目 %d（%.1f%%）"
                  % (label, dup_groups, dup_rows, ratio))
            if dup_groups and args.show_samples:
                for k, v in keys.most_common(5):
                    if v > 1:
                        sample = next(it[1] for it in items if _dedup_key(it[1]) == k)
                        print("    ×%d %s" % (v, sample[:60]))
            return ratio

        dup_stats(rows, "长期记忆重复度")
        dup_stats(imp, "重要记忆重复度")

        kw_long = sum(1 for r in rows if (r[2] or "").strip() not in ("", "[]"))
        kw_imp = sum(1 for r in imp if (r[2] or "").strip() not in ("", "[]"))
        print("关键词覆盖率：长期 %d/%d，重要 %d/%d"
              % (kw_long, len(rows), kw_imp, len(imp)))
        ts_ok = sum(1 for r in imp if (r[3] or "").strip())
        print("剧情时间覆盖率（重要记忆）：%d/%d" % (ts_ok, len(imp)))

        lens = [len(r[1] or "") for r in rows + imp]
        if lens:
            long_rows = sum(1 for n in lens if n >= 250)
            print("条目长度：平均 %.0f 字，最长 %d 字，接近上限(≥250) %d 条"
                  % (sum(lens) / len(lens), max(lens), long_rows))

        print("-" * 66)
        # ---- 压力 ----
        story = tracks.get("story", 0)
        print("合并阈值（sleep.max_important）: %d，当前 story %d 条%s"
              % (max_important, story,
                 "（已超限，下次睡前会触发合并）" if story > max_important else
                 "（还差 %d 条）" % max(0, max_important - story)))
        if rows:
            print("向量索引全量重建成本 ≈ %d 条文本嵌入（每次记忆写入后首次检索触发）"
                  % len(rows))
        print("-" * 66)

        # ---- 检索冒烟 ----
        if args.query:
            print("检索冒烟：%s（关键词路径，零 LLM）" % args.query)
            target = args.identity or conn.execute(
                "SELECT identity FROM important_memories "
                "GROUP BY identity ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
            target = target[0] if isinstance(target, tuple) else target
            if not target:
                print("  （库中还没有身份数据）")
            else:
                q_grams = textutil.ngrams(args.query)
                scored = []
                for ident, content, keywords, *_rest in imp:
                    if ident != target:
                        continue
                    ks = textutil.json_extract(keywords) if isinstance(keywords, str) else keywords
                    ks = ks if isinstance(ks, list) else []
                    exact = [k for k in ks if str(k) and str(k) in args.query]
                    overlap = len(q_grams & textutil.ngrams(content))
                    score = 300 * len(exact) + 10 * overlap
                    if score > 0:
                        scored.append((score, exact, content))
                scored.sort(key=lambda x: -x[0])
                print("  身份：%s，命中 %d 条" % (target, len(scored)))
                for i, (score, exact, content) in enumerate(scored[:args.top], 1):
                    # 默认只打印分数与关键词，正文需要 --show-samples 才显示
                    line = "  %d) 分=%.1f 关键词=%s" % (i, score, exact or "-")
                    if args.show_samples:
                        line += "\n     %s" % content[:80].replace("\n", " ")
                    print(line)
                if not scored:
                    print("  未命中：这条查询在当前库里检索不到任何记忆（可考虑补关键词/向量）")
                # 时间型查询：剧情时间窗是独立的检索路径（list_important_by_time）
                if re.search(r"第\d+周|今天|昨天|前天|这周|上周|本周", args.query):
                    from qqbot import relative_time
                    clock = _read_clock(path)
                    windows = relative_time.resolve_time_windows(args.query, clock)
                    if windows:
                        n = conn.execute(
                            "SELECT COUNT(*) FROM important_memories WHERE identity=? AND ("
                            + " OR ".join(["story_time LIKE ?"] * len(windows)) + ")",
                            [target] + ["%s%%" % w[0] for w in windows]).fetchone()[0]
                        print("  剧情时间窗 %s → 命中 %d 条（另一条检索路径）"
                              % (windows, n))
        print("-" * 66)
        print("重复度、关键词覆盖率、剧情时间覆盖率是三个最值得盯的指标：")
        print("  重复度高 → 提炼/写入去重策略（save_important/save_longterm）")
        print("  关键词覆盖低 → 睡前提炼的 keywords 质量（或给旧记忆回填：scripts 里的 keywordize）")
        print("  时间覆盖低 → 提示词里要求带「第N周周X时段」前缀")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
