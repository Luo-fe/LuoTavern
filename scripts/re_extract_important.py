# -*- coding: utf-8 -*-
"""全量重提炼重要记忆（默认预览，--apply 执行）。

从已按剧情日期修正的聊天记录出发，用配置的 LLM 重新提炼该身份的全部
story 轨道重要记忆：事件时间取自条目自身剧情时间戳，关键词 5-10 个含同义词。
执行前自动备份 memory.db；world 轨道不受影响。

用法：
    python scripts/re_extract_important.py --persona "Eri and Rin"
    python scripts/re_extract_important.py --persona "Eri and Rin" --apply
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot import config as qconfig
from qqbot import textutil
from qqbot.llm import build_llm
from qqbot.memstore.local import migrate_conn
from qqbot.sleep_ritual import IMPORTANT_EXTRACT_PROMPT, SleepRitual

ENTRY_RE = re.compile(r"^- ([^\s*]+) \*\*([^*\n]+?)\*\*：(.*)$")
STORY_FILE_RE = re.compile(
    r"^第(\d+)周(周[一二三四五六日天])(?: \((\d+)\))?\.md$")
_WEEKDAY = {"周一": 0, "周二": 1, "周三": 2, "周四": 3,
            "周五": 4, "周六": 5, "周日": 6}
STAMP_RE = re.compile(r"^(第\d+周(?:周[一二三四五六日天]))")
_WD_IDX = {"周一": 0, "周二": 1, "周三": 2, "周四": 3,
           "周五": 4, "周六": 5, "周日": 6}


def _date_key(stamp):
    m = re.match(r"^第(\d+)周(周[一二三四五六日天])", stamp or "")
    if not m:
        return None
    return (int(m.group(1)), _WD_IDX.get(m.group(2), 0))


def read_entries(d):
    """按剧情日期顺序读取身份目录全部条目。"""
    files = sorted(
        (fn for fn in os.listdir(d) if fn.endswith(".md")),
        key=lambda fn: (
            (lambda m: (int(m.group(1)), _WEEKDAY.get(m.group(2), 0),
                        int(m.group(3) or 1)))(STORY_FILE_RE.match(fn))
            if STORY_FILE_RE.match(fn) else (10 ** 9, 0, 0)))
    out = []
    for fn in files:
        cur = None
        with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                m = ENTRY_RE.match(line)
                if m:
                    cur = {"stamp": m.group(1), "speaker": m.group(2).strip(),
                           "content": m.group(3)}
                    out.append(cur)
                elif cur is not None and line.startswith("  "):
                    cur["content"] += "\n" + line[2:]
                else:
                    cur = None
    return out


def chunk_entries(entries, size):
    return [entries[i:i + size] for i in range(0, len(entries), size)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", default="Eri and Rin")
    ap.add_argument("--identity", default="主人")
    ap.add_argument("--chunk", type=int, default=20)
    ap.add_argument("--apply", action="store_true",
                    help="执行（默认仅预览，执行前备份数据库）")
    ap.add_argument("--append", action="store_true",
                    help="追加提炼（不删除已有 story 轨道记忆）")
    ap.add_argument("--since", default=None,
                    help="只处理该剧情日期及之后，如：第1周周四")
    ap.add_argument("--mark-sealed", action="store_true",
                    help="完成后把封存点推进到处理到的最后一条")
    args = ap.parse_args()

    chat_dir = os.path.join(ROOT, "data", "personas", args.persona,
                            "chatlog", args.identity)
    db = os.path.join(ROOT, "data", "personas", args.persona, "memory.db")
    if not os.path.isdir(chat_dir):
        print("聊天记录目录不存在:", chat_dir)
        sys.exit(1)
    if not os.path.isfile(db):
        print("记忆库不存在:", db)
        sys.exit(1)

    all_entries = read_entries(chat_dir)
    # 跳过已纠正作废的条目（与 sleep_ritual 一致，防止被纠正的内容重新进入 story 记忆）
    corrected = {}
    corrected_path = os.path.join(ROOT, "data", "personas", args.persona,
                                  "corrected", args.identity + ".json")
    try:
        with open(corrected_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            corrected = {int(k): True for k in data.keys()}
    except Exception:
        pass
    all_entries = [
        dict(e, _idx=i) for i, e in enumerate(read_entries(chat_dir))
        if i not in corrected]
    entries = all_entries
    if args.since:
        since_key = _date_key(args.since + "早上")
        if since_key is None:
            print("--since 格式错误，应为如：第1周周四")
            sys.exit(1)
        entries = [e for e in entries
                   if _date_key(e["stamp"]) and _date_key(e["stamp"]) >= since_key]
    if not entries:
        print("无可提炼的聊天记录")
        sys.exit(1)
    chunks = chunk_entries(entries, max(5, args.chunk))
    print("条目总数:", len(all_entries),
          "| 本次处理:", len(entries), "| 分块:", len(chunks))

    if not args.apply:
        print("（预览：执行将调用 LLM 提炼%s story 轨道重要记忆）"
              % ("并追加" if args.append else "并整体替换"))
        print("示例条目：%s %s" % (entries[0]["stamp"], entries[0]["content"][:30]))
        return

    cfg = qconfig.load_config(os.path.join(ROOT, "config.json"))
    cfg["timeout_seconds"] = int(cfg.get("timeout_seconds", 180) or 180) // 2
    client = build_llm(cfg)
    items = []
    for ci, chunk in enumerate(chunks, 1):
        lines = "\n".join(
            "[%s] %s: %s" % (e["stamp"], e["speaker"],
                             e["content"].replace("\n", " ")[:300])
            for e in chunk)
        data = None
        for attempt in range(2):
            data = client.chat_json(
                [
                    {"role": "system", "content": IMPORTANT_EXTRACT_PROMPT},
                    {"role": "user", "content": lines},
                ],
                temperature=0.2, max_tokens=12000, call_type="extract")
            if isinstance(data, list):
                break
            print("[块%d] 第%d次返回空/不可解析，重试…" % (ci, attempt + 1))
        if not isinstance(data, list):
            print("[块%d] LLM 未返回 JSON 列表，跳过" % ci)
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            if not STAMP_RE.match(content):
                print("[块%d] 跳过无剧情时间戳的条目：%s"
                      % (ci, content[:40]))
                continue
            try:
                delta = max(-100, min(100, int(item.get("affect_delta", 0))))
            except (TypeError, ValueError):
                delta = 0
            try:
                importance = max(1, min(10, int(item.get("importance", 5))))
            except (TypeError, ValueError):
                importance = 5
            keywords = SleepRitual._clean_keywords(item.get("keywords"))
            items.append((content[:300], delta, importance, keywords))
        print("[块%d/%d] 提炼 %d 条" % (ci, len(chunks), len(items)))

    if not items:
        print("未提炼到任何重要记忆，未改动数据库")
        return
    if len(items) < max(10, len(chunks)):
        print("提炼数量过少（%d 条），拒绝替换现有记忆，未改动数据库"
              % len(items))
        return

    bak = db + ".bak_%s" % time.strftime("%Y%m%d_%H%M%S")
    # WAL 模式下最新提交只在 -wal 里：直接 copy2 主库可能备份不到最新记忆。
    # 用 SQLite 的在线备份 API（自动包含 WAL 内容）。
    try:
        src = sqlite3.connect(db)
        dst = sqlite3.connect(bak)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    except Exception as exc:
        print("在线备份失败（回退文件复制）:", exc)
        shutil.copy2(db, bak)
    print("数据库备份:", bak)
    conn = sqlite3.connect(db)
    migrate_conn(conn)   # 旧库（缺 track/story_time 列）先迁移，否则 INSERT 会失败
    if not args.append:
        conn.execute(
            "DELETE FROM important_memories WHERE identity=? AND track='story'",
            (args.identity,))
    conn.executemany(
        "INSERT INTO important_memories "
        "(identity, content, affect_delta, importance, keywords, story_time, track, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'story', ?)",
        [(args.identity, c, d, i,
          json.dumps(kw, ensure_ascii=False) if kw else "",
          textutil.parse_story_time(c), time.time())
         for c, d, i, kw in items])
    conn.commit()
    conn.close()
    if args.mark_sealed and entries:
        # 封存点推进到本次实际处理到的最后一条（--since 时不得封住中间未处理条目）
        last_idx = entries[-1]["_idx"]
        seal_path = os.path.join(ROOT, "data", "personas", args.persona,
                                 "seal", args.identity + ".json")
        os.makedirs(os.path.dirname(seal_path), exist_ok=True)
        with open(seal_path, "w", encoding="utf-8") as f:
            json.dump({"sealed_through": last_idx}, f,
                      ensure_ascii=False, indent=2)
        print("封存点已推进到 %d" % last_idx)
    print("完成：提炼 %d 条重要记忆（%s）"
          % (len(items), "追加" if args.append else "整体替换"))
    for c, d, i, kw in items[:5]:
        print("  [%s] %s... 关键词:%s" % (i, c[:30], "、".join(kw)))


if __name__ == "__main__":
    main()
