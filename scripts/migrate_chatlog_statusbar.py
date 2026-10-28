# -*- coding: utf-8 -*-
"""一次性迁移：按状态栏剧情时间（第N周 周几）重新分类历史聊天记录。

背景：早期版本聊天记录条目时间戳全部写死为写入时的时钟（如"第1周周一"），
而回复状态栏里模型的剧情时间已推进（如"第1周 周二 早上"）。本脚本按状态栏
时间把历史条目归入 第N周周X.md，并重写条目行首日期；用户消息归入其后的
机器人回复所在剧情日。

用法：
    python scripts/migrate_chatlog_statusbar.py            # 预览（不写文件）
    python scripts/migrate_chatlog_statusbar.py --apply    # 执行（原件入 archive 备份）

安全：先完整解析并校验条目数一致，才移动原件到 archive 并写新文件；
每个身份目录成功后写入标记文件 _statusbar_migrated，避免重复迁移。
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ENTRY_RE = re.compile(r"^- ([^\s*]+) \*\*([^*\n]+?)\*\*：(.*)$")
STAMP_DATE_RE = re.compile(r"^(第\d+周(?:周[一二三四五六日天]))")
STAMP_PERIOD_RE = re.compile(
    r"^(第\d+周(?:周[一二三四五六日天]))(早上|上午|中午|下午|傍晚|晚上|深夜|凌晨)?")
BAR_TIME_RE = re.compile(r"时间\s*[:：]\s*([^\n]+)")
BAR_DATE_RE = re.compile(r"第\s*(\d+)\s*周\s*周?\s*([一二三四五六日天1-7])")
BAR_PERIOD_RE = re.compile(r"(早上|上午|中午|下午|傍晚|晚上|深夜|凌晨|清晨)")

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_WD = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
_PERIOD_FIX = {"清晨": "早上"}
MARKER = "_statusbar_migrated"


def parse_entries(path):
    """解析文件 → [{stamp, speaker, content, raw}]，续行并入 content。"""
    out = []
    cur = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("# "):
                    continue
                m = ENTRY_RE.match(line)
                if m:
                    cur = {"stamp": m.group(1), "speaker": m.group(2).strip(),
                           "content": m.group(3), "raw": line}
                    out.append(cur)
                elif cur is not None and line.startswith("  "):
                    cur["content"] += "\n" + line[2:]
                    cur["raw"] += "\n" + line
                else:
                    cur = None
    except OSError as exc:
        raise RuntimeError("读取失败 %s: %s" % (path, exc))
    return out


def bar_date(content):
    """从条目内容提取 (week, weekday, period) 或 None。

    直接扫描内容中的"时间:"行（历史数据的状态栏标签很多没有闭合，
    不能依赖 <状态栏>...</状态栏> 匹配）。
    """
    for line in content.split("\n"):
        s = line.strip()
        if not s.startswith("时间"):
            continue
        tm = re.match(r"时间\s*[:：]\s*(.+)", s)
        if not tm:
            continue
        dm = BAR_DATE_RE.search(tm.group(1))
        if not dm:
            continue
        week = int(dm.group(1))
        tok = dm.group(2)
        wd = int(tok) if tok.isdigit() else _WD.get(tok)
        if not wd or not (1 <= wd <= 7):
            continue
        pm = BAR_PERIOD_RE.search(tm.group(1))
        period = _PERIOD_FIX.get(pm.group(1), pm.group(1)) if pm else None
        return week, wd, period
    return None


def bucket_name(week, wd):
    return "第%d周%s" % (week, WEEKDAYS[wd - 1])


def rewrite_stamp(stamp, week, wd, period=None):
    m = STAMP_PERIOD_RE.match(stamp)
    old_period = m.group(2) if m else None
    return bucket_name(week, wd) + (period or old_period or "")


def migrate_identity(root, identity, apply=False, dry=False):
    """迁移一个身份目录；返回 (计划描述, 是否成功)。"""
    d = os.path.join(root, identity)
    marker = os.path.join(d, MARKER)
    if os.path.exists(marker):
        return "跳过（已迁移）", True
    files = sorted(f for f in os.listdir(d) if f.endswith(".md"))
    if not files:
        return "无文件", True
    entries = []
    for fn in files:
        entries.extend(parse_entries(os.path.join(d, fn)))
    if not entries:
        return "无可解析条目", True

    # 每个条目：状态栏日期优先；无状态栏的条目归到最近的日期（同距取前）
    n = len(entries)
    labels = []
    for e in entries:
        bar = bar_date(e["content"])
        if bar and 1 <= bar[0] <= 52 and 1 <= bar[1] <= 7:
            labels.append(bar)
        else:
            labels.append(None)
    prev_idx = [None] * n
    last = None
    for i in range(n):
        prev_idx[i] = last
        if labels[i] is not None:
            last = i
    next_idx = [None] * n
    nxt = None
    for i in range(n - 1, -1, -1):
        next_idx[i] = nxt
        if labels[i] is not None:
            nxt = i
    assigned = []
    for i in range(n):
        if labels[i] is not None:
            assigned.append(labels[i])
            continue
        pi, ni = prev_idx[i], next_idx[i]
        if pi is None and ni is None:
            assigned.append((1, 1, None))
        elif pi is None:
            assigned.append(labels[ni])
        elif ni is None:
            assigned.append(labels[pi])
        else:
            assigned.append(labels[pi] if (i - pi) <= (ni - i) else labels[ni])

    buckets = {}
    order = []
    for e, lab in zip(entries, assigned):
        key = bucket_name(lab[0], lab[1])
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        e["_period"] = lab[2]
        e["_key"] = key
        buckets[key].append(e)

    total = sum(len(v) for v in buckets.values())
    if total != len(entries):
        return "条目数校验失败：%d != %d" % (total, len(entries)), False
    plan = "; ".join("%s=%d条" % (k, len(buckets[k])) for k in order)
    if not apply:
        return "预览：%d 条 → %s" % (total, plan), True

    # 执行：先移原件到 archive 备份，再写新文件
    archive = os.path.join(os.path.dirname(root), "archive", identity)
    os.makedirs(archive, exist_ok=True)
    for fn in files:
        src = os.path.join(d, fn)
        dst = os.path.join(archive, fn)
        try:
            if os.path.exists(dst):
                os.remove(dst)
            os.replace(src, dst)
        except OSError as exc:
            return "归档失败 %s: %s" % (fn, exc), False
    for key in order:
        path = os.path.join(d, key + ".md")
        km = re.match(r"^第(\d+)周(?:周([一二三四五六日天]))", key)
        week = int(km.group(1))
        wd = _WD[km.group(2)]
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 聊天记录 %s %s\n\n" % (identity, key))
            for e in buckets[key]:
                new_stamp = rewrite_stamp(
                    e["stamp"], week, wd, period=e["_period"])
                first = re.sub(
                    r"^-\s*[^\s*]+(?=\s+\*\*)",
                    "- " + new_stamp, e["raw"].split("\n")[0])
                f.write(first + "\n")
                for cont in e["raw"].split("\n")[1:]:
                    f.write("  %s\n" % cont.lstrip())
    with open(marker, "w", encoding="utf-8") as f:
        f.write("migrated by status bar dates\n")
    return "完成：%d 条 → %s" % (total, plan), True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="执行迁移（默认仅预览）")
    ap.add_argument("--root", default=None,
                    help="聊天记录根目录；默认扫描 data/chatlog 与 data/personas/*/chatlog")
    args = ap.parse_args()
    roots = []
    if args.root:
        roots.append(args.root)
    else:
        roots.append(os.path.join(ROOT, "data", "chatlog"))
        personas_dir = os.path.join(ROOT, "data", "personas")
        if os.path.isdir(personas_dir):
            for persona in sorted(os.listdir(personas_dir)):
                p = os.path.join(personas_dir, persona, "chatlog")
                if os.path.isdir(p):
                    roots.append(p)
    any_fail = False
    for root in roots:
        if not os.path.isdir(root):
            continue
        for identity in sorted(os.listdir(root)):
            if not os.path.isdir(os.path.join(root, identity)):
                continue
            msg, ok = migrate_identity(root, identity, apply=args.apply)
            try:
                label = os.path.relpath(root, ROOT)
            except ValueError:
                label = root
            print("[%s] %s/%s：%s" % ("OK" if ok else "FAIL",
                                      label, identity, msg))
            any_fail = any_fail or not ok
    sys.exit(1 if any_fail else 0)


if __name__ == "__main__":
    main()
