# -*- coding: utf-8 -*-
"""模拟聊天端到端演练：用脚本化 LLM 把整条管线跑一遍，并输出可读的对话记录。

    python scripts/simulate_chat.py                    # 演练 + 打印/写入记录
    python scripts/simulate_chat.py --out docs/xxx.md  # 指定输出文件

覆盖：身份声明 → 角色扮演 → 长期记忆 → 世界书触发 → 回忆注入 → 睡前提炼/摘要/封存
→ 好感度 → 记忆状态 → 纠错重答 → 总结压缩 → 内置指令 → 忘记 → 清空会话归档
→ 结束这一天（世界轨道）→ 异步 worker 路径，并在最后校验数据一致性与封存 id 稳定性。

脚本化 LLM 按提示词特征分派（提炼/合并/世界/压缩），不联网、不花钱、结果可复现。
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

import numpy as np  # noqa: E402

from qqbot.bot_core import BotCore  # noqa: E402
from qqbot.config import load_config  # noqa: E402
from qqbot import textutil, worldlog  # noqa: E402

OWNER = 10000001
GUEST = 10001
IDENT = "主人"

STATUS_BAR = ("<状态栏>\n时间: 第1周周六晚上\n地点: 家里客厅\n<青柠>\n"
              "表情: 眉眼弯弯、嘴角上扬\n姿态: 靠在沙发上、手撑着脸\n"
              "服装: 白色居家衬衫、浅灰长裤、拖鞋\n"
              "身体反应: 肩膀放松、呼吸平稳、指尖微暖\n"
              "内心: 很开心\n</状态栏>")


class ScriptedLLM:
    """按 call_type / 提示词特征返回脚本化结果（模拟一个"正常发挥"的模型）。"""

    def __init__(self, log=None):
        self.log = log or (lambda m: None)
        self.chat_calls = []
        self.json_calls = []
        self.reply_queue = []

    # ---- 主对话 ----
    def chat(self, messages, temperature=None, max_tokens=None, call_type="chat"):
        self.chat_calls.append({"call_type": call_type, "messages": messages})
        if self.reply_queue:
            return self.reply_queue.pop(0)
        if call_type == "compress":
            return "第1周周六晚上 两人在家里聊了开心的事，气氛很好。" + STATUS_BAR
        return "（嗯嗯，我在听～）" + STATUS_BAR

    # ---- 结构化调用 ----
    def chat_json(self, messages, temperature=0.2, call_type="chat", max_tokens=None):
        self.json_calls.append({"call_type": call_type, "messages": messages})
        prompt = " ".join(str(m.get("content") or "") for m in messages)
        if "affect_delta" in prompt and "keywords" in prompt:
            # 睡前提炼：给一条带剧情时间戳的重要记忆
            return [{
                "content": "第1周周六晚上 主人和%s一起在欢乐谷坐了过山车，约定下次再去" % "青柠",
                "affect_delta": 20, "importance": 8,
                "keywords": ["欢乐谷", "过山车", "约定"],
            }]
        if "merge" in prompt:
            return []                      # 未超上限，不需要合并
        if "世界书剧情记录" in prompt or "剧情档案员" in prompt:
            return [{"content": "第1周周六晚上 在欢乐谷的过山车体验成为两人的共同回忆",
                     "importance": 6}]
        return None


class FeatureEmbedder:
    """按特征词构造向量（离线、确定性）。"""

    FEATURES = ["过山车", "欢乐谷", "香菜", "生日", "围巾", "电影"]

    def available(self):
        return True

    def loaded(self):
        return True

    def model_cached(self):
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


class CaptureClient:
    def __init__(self):
        self.sent = []
        self.fail_next = False

    def send_private(self, user_id, text):
        if self.fail_next:
            self.fail_next = False
            return False
        self.sent.append((int(user_id), str(text)))
        return True


def build_bot(tmp):
    cfg = load_config(use_env=False)
    cfg.update({
        "allow_all": True,
        "owner_user_ids": [OWNER],
        "allowed_user_ids": [OWNER, GUEST],
        "typing_indicator": False,
        "summary_dir": os.path.join(tmp, "summary"),
        "archive_dir": os.path.join(tmp, "archive"),
        "bot_name": "青柠",
        "system_prompt": "你是温柔的角色扮演助手青柠。",
        "character_card": "",
        "message_chunk_chars": 200,
    })
    cfg["identity"].update({"map_path": os.path.join(tmp, "identity_map.json"),
                            "owner_name": IDENT})
    cfg["time"]["path"] = os.path.join(tmp, "clock.json")
    with open(cfg["time"]["path"], "w", encoding="utf-8") as f:
        json.dump({"weekday": "周六", "time_of_day": "晚上", "week": 1, "set": True}, f)
    cfg["rules"]["dir"] = os.path.join(tmp, "rules")
    cfg["correct"]["dir"] = os.path.join(tmp, "corrected")
    cfg["seal"]["dir"] = os.path.join(tmp, "seal")
    cfg["memory"]["db_path"] = os.path.join(tmp, "memory.db")
    cfg["chatlog"]["dir"] = os.path.join(tmp, "chatlog")
    cfg["vault"] = {"enable": False, "dir": os.path.join(tmp, "vault")}
    cfg["shared"] = {"dir": os.path.join(tmp, "shared")}
    cfg["worldbook"] = {"dir": os.path.join(tmp, "book"), "files": [],
                        "base_mode": "constant", "base_max_chars": 500,
                        "emphasis_max_chars": 300}
    cfg["worldlog_dir"] = os.path.join(tmp, "worldlog")
    cfg["plugins"] = {"memory": True, "chatlog": True, "vault_sync": False,
                      "proactive": False, "worldbook": True}
    # 一本最小世界书（内容为演练用虚构内容，不含任何用户数据）
    os.makedirs(cfg["worldbook"]["dir"], exist_ok=True)
    with open(os.path.join(cfg["worldbook"]["dir"], "sim.json"), "w",
              encoding="utf-8") as f:
        json.dump({"name": "演练世界书", "entries": [
            {"keys": ["欢乐谷", "过山车"], "content": "欢乐谷的过山车是两人第一次一起坐的。",
             "constant": True, "enabled": True},
        ]}, f, ensure_ascii=False)
    bot = BotCore(cfg, cfg["system_prompt"], None)
    bot.llm = ScriptedLLM(log=bot.log)
    bot.client = CaptureClient()
    bot.memory._embedder = FeatureEmbedder()
    return bot


def snapshot(bot):
    """每轮结束后的数据快照（用于校验"某一步之后应当出现/消失"）。"""
    import sqlite3
    conn = sqlite3.connect(bot.memory.db_path)
    try:
        longterm = [r[0] for r in conn.execute("SELECT content FROM memories")]
        important = conn.execute(
            "SELECT content, track FROM important_memories").fetchall()
        affection = dict(conn.execute("SELECT identity, score FROM affection"))
    finally:
        conn.close()
    return {
        "longterm": longterm,
        "important": [c for c, _t in important],
        "tracks": [t for _c, t in important],
        "affection": affection,
        "entries": len(bot.chatlog_store.read_entries(IDENT)),
        "worldlog": len(worldlog.read_events(
            IDENT, base=bot.config.get("worldlog_dir"))),
        "history": len(bot.history.get(IDENT) or []),
        "prompts": [c["messages"] for c in getattr(bot.llm, "chat_calls", [])][-4:],
        "json_calls": [c["messages"] for c in getattr(bot.llm, "json_calls", [])][-2:],
    }


def run_scenario(bot, verbose=False):
    """按剧本推进；返回记录行列表（每轮：用户说了什么 / 机器人回了什么 / 数据快照）。"""
    lines = []

    def turn(text, note="", user=OWNER):
        before = len(bot.client.sent)
        bot._handle(user, text)
        replies = [t for _u, t in bot.client.sent[before:]]
        row = {"input": text, "note": note, "user": user, "replies": replies,
               "state": snapshot(bot)}
        lines.append(row)
        if verbose:
            print("用户(%s) > %s" % (note or "RP", text))
            for r in replies:
                print("青柠     < %s" % r.replace("\n", " / ")[:160])
        return replies

    def by_note(note):
        for row in lines:
            if row["note"] == note:
                return row
        return {"state": {"longterm": [], "important": [], "tracks": [],
                          "affection": {}, "entries": 0, "worldlog": 0,
                          "history": 0, "prompts": [], "json_calls": []},
                "replies": [], "input": ""}

    # 1) 未声明身份的客人：应被拦住（不进入角色扮演）
    bot.client.sent.clear()
    turn("在吗", note="未声明身份", user=GUEST)
    # 2) 客人声明身份（第二个身份，用于验证记忆隔离）
    turn("【我是梦梦】", note="声明第二身份", user=GUEST)
    bot.llm.reply_queue = ["（歪头）嗯～我在。" + STATUS_BAR]
    turn("今天一起看电影吧", note="客人 RP", user=GUEST)
    # 3) 主人登场
    turn("【我是主人】", note="声明身份")
    # 4) 角色扮演（含会话命令关键词，用于验证不会误触发清空）
    bot.llm.reply_queue = ["（轻轻抱住）不许说失忆这种话，我们重新开始也要一起。\n" + STATUS_BAR]
    turn("你怎么会失忆呢？我们重新开始好不好", note="RP（含误触发关键词）")
    # 4) 记住了某件事
    turn("【记住我不吃香菜，点菜要注意】", note="长期记忆")
    # 5) 触发世界书条目
    turn("我们上次在欢乐谷坐过山车好开心", note="世界书触发+回忆")
    # 6) 强制回忆
    turn("【回忆香菜】", note="强制回忆长期记忆")
    # 7) 睡前仪式：提炼 + 摘要 + 封存 + 换天
    turn("好好睡觉吧", note="睡前提炼")
    # 8) 醒来后回忆
    turn("还记得欢乐谷的过山车吗", note="回忆重要记忆")
    # 9) 好感度 / 记忆状态
    turn("【好感度】", note="好感度查询")
    turn("【记忆状态】", note="记忆状态查询")
    # 10) 纠错
    bot.llm.reply_queue = ["（低头）嗯…我记住了，下次不会再说错。\n" + STATUS_BAR]
    turn("【纠正：青柠其实不喜欢过山车】", note="纠错重答")
    # 11) 总结压缩
    turn("【总结一下】", note="总结压缩")
    # 12) 内置指令
    turn("【随机触发词】", note="内置指令")
    turn("【触发词解释：过山车】", note="内置指令")
    # 13) 忘记
    turn("【忘记香菜】", note="忘记长期记忆")
    # 14) 清空会话（摘要应归档而不是删除）
    turn("【清空会话】", note="清空会话（归档）")
    # 15) 结束这一天：世界轨道提炼
    turn("【结束这一天】", note="结束这一天")

    # 16) 异步路径：worker 线程 + inbox（真实运行时的入口）
    bot.stop_event.clear()
    worker = threading.Thread(target=bot._worker, daemon=True)
    worker.start()
    time.sleep(0.3)
    before = len(bot.client.sent)
    bot.handle_user_message(OWNER, "我今天很开心，谢谢你")
    deadline = time.time() + 10
    while time.time() < deadline:
        if len(bot.client.sent) > before:
            break
        time.sleep(0.2)
    lines.append({"input": "我今天很开心，谢谢你", "note": "异步 worker",
                  "replies": [t for _u, t in bot.client.sent[before:]],
                  "state": snapshot(bot)})
    bot.stop_event.set()
    worker.join(timeout=3)
    return lines, by_note


def audit(bot, lines, by_note):
    """一致性校验：返回 (检查项, 结论, 说明) 列表。"""
    checks = []

    def add(name, ok, detail=""):
        checks.append((name, bool(ok), str(detail)[:100]))

    replies = [r for row in lines for r in row["replies"]]
    add("每轮都有回复", all(row["replies"] for row in lines),
        "缺回复：%s" % [r["note"] for r in lines if not r["replies"]])
    undeclared = by_note("未声明身份")["replies"]
    add("未声明身份被拦住",
        bool(undeclared) and any("告诉机器人你是谁" in r for r in undeclared), undeclared[:1])
    guest_rp = by_note("客人 RP")["replies"]
    add("客人可正常角色扮演", bool(guest_rp) and "歪头" in guest_rp[0], guest_rp[:1])
    add("无内部错误回复", not any("出错" in r or "Traceback" in r for r in replies),
        [r[:60] for r in replies if "出错" in r][:2])

    # —— 数据生命周期（看每一步之后的快照，而不是只看终态）——
    remembered = by_note("长期记忆")["state"]
    add("长期记忆已落库", any("香菜" in c for c in remembered["longterm"]),
        remembered["longterm"])
    forgotten = by_note("忘记长期记忆")["state"]
    add("忘记指令已生效", not any("香菜" in c for c in forgotten["longterm"]),
        forgotten["longterm"])
    after_sleep = by_note("睡前提炼")["state"]
    add("睡前提炼出重要记忆", any("过山车" in c for c in after_sleep["important"]),
        after_sleep["important"])
    add("好感度已累计", after_sleep["affection"].get(IDENT, 0) > 300,
        after_sleep["affection"])
    triggered = by_note("世界书触发+回忆")["state"]
    add("世界书触发已记录", triggered["worldlog"] >= 1, triggered["worldlog"])
    ended = by_note("结束这一天")["state"]
    add("世界日志已提炼并清理", ended["worldlog"] == 0, ended["worldlog"])
    add("world 轨道已写入", "world" in ended["tracks"], ended["tracks"])
    add("重要记忆无重复",
        len({textutil.dedup_key(c) for c in ended["important"]}) == len(ended["important"]),
        len(ended["important"]))
    add("聊天记录已写入", ended["entries"] >= 4, "%d 条" % ended["entries"])
    import sqlite3
    conn = sqlite3.connect(bot.memory.db_path)
    try:
        guest_mem = [r[0] for r in conn.execute(
            "SELECT content FROM memories WHERE identity=?", ("梦梦",))]
        guest_imp = [r[0] for r in conn.execute(
            "SELECT content FROM important_memories WHERE identity=?", ("梦梦",))]
    finally:
        conn.close()
    add("记忆按身份隔离", not any("香菜" in c for c in guest_mem + guest_imp),
        "客人记忆 %d 条" % (len(guest_mem) + len(guest_imp)))

    # —— 会话命令误触发回归：RP 里出现"失忆/重新开始"不能清空会话 ——
    rp = by_note("RP（含误触发关键词）")
    add("RP 未误触发清空会话", rp["state"]["history"] >= 3, rp["state"]["history"])
    add("RP 未收到清空回复", not any("重新开始" in r and "我们重新开始～" in r
                                     for r in rp["replies"]))
    cleared = by_note("清空会话（归档）")["state"]
    add("清空会话已清历史", cleared["history"] <= 1, cleared["history"])
    archive = os.path.join(bot.summary_dir, "archive")
    add("清空会话已归档摘要", os.path.isdir(archive), archive)

    # —— 注入链路：回忆轮次的 prompt 里必须真的带上记忆正文 ——
    recall_state = by_note("回忆重要记忆")["state"]
    joined = " ".join(str(m.get("content") or "")
                      for msgs in recall_state["prompts"] for m in msgs)
    add("回忆内容已注入 prompt", "过山车" in joined, joined[:60])
    lt_state = by_note("强制回忆长期记忆")["state"]
    joined_lt = " ".join(str(m.get("content") or "")
                         for msgs in lt_state["prompts"] for m in msgs)
    add("长期记忆已注入 prompt", "香菜" in joined_lt, joined_lt[:60])
    rp_state = by_note("声明身份")["state"]
    joined_rp = " ".join(str(m.get("content") or "")
                         for msgs in rp_state["prompts"] for m in msgs)
    add("system 提示含人设", "角色扮演助手" in joined_rp)
    add("世界书已注入 prompt", "过山车" in joined_rp)

    # —— 封存/纠正的稳定 id ——
    ids = bot.seal.sealed_ids(IDENT)
    add("封存已记录稳定 id", bool(ids), "%d 个" % len(ids))
    corr = bot.correct.corrected_ids(IDENT)
    add("作废登记带稳定 id", bool(corr), list(corr)[:1])

    # —— 状态栏强制：角色扮演回复必须带合格状态栏（缺字段 = 不合格）——
    rp_notes = {"客人 RP", "RP（含误触发关键词）", "世界书触发+回忆", "回忆重要记忆",
                "异步 worker"}
    bad = []
    for row in lines:
        if row["note"] not in rp_notes:
            continue
        for r in row["replies"]:
            gaps = bot._status_bar_gaps(r)
            if gaps:
                bad.append((row["note"], gaps))
    add("每条 RP 回复都带合格状态栏", not bad, bad[:2])

    # —— 异步 worker 路径 ——
    add("异步 worker 有回复", bool(lines[-1]["replies"]), lines[-1]["replies"][:1])
    return checks


def main():
    ap = argparse.ArgumentParser(description="模拟聊天端到端演练")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "simulated-chat-report.md"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="luotavern-sim-")
    try:
        bot = build_bot(tmp)
        try:
            lines, by_note = run_scenario(bot, verbose=not args.quiet)
            checks = audit(bot, lines, by_note)
        finally:
            try:
                bot.plugins.shutdown()
            except Exception:
                pass

        passed = sum(1 for _n, ok, _d in checks if ok)
        report = ["# 模拟聊天演练报告（自动生成）", "",
                  "> 生成命令：`python scripts/simulate_chat.py`　脚本化 LLM，离线可复现",
                  "", "## 演练剧本与对话", ""]
        for i, row in enumerate(lines, 1):
            report.append("### %d. %s" % (i, row["note"]))
            report.append("")
            report.append("- 用户：`%s`" % row["input"])
            for r in row["replies"]:
                report.append("- 青柠：%s" % r.replace("\n", " ⏎ ")[:300])
            if not row["replies"]:
                report.append("- 青柠：（无回复）")
            report.append("")
        report += ["## 一致性校验", "",
                   "| 检查项 | 结果 | 说明 |", "|---|---|---|"]
        for name, ok, detail in checks:
            report.append("| %s | %s | %s |" % (name, "✅" if ok else "❌",
                                                str(detail).replace("|", "/")[:80]))
        report += ["", "**合计：%d/%d 项通过**" % (passed, len(checks)), ""]
        text = "\n".join(report)
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(text)
        print("报告已写入：%s" % args.out)
        return 0 if passed == len(checks) else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
