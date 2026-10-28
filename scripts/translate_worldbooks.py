# -*- coding: utf-8 -*-
"""把指定世界书 JSON 的触发词和内容替换为中文（内容取自对应中文 md）。

用法：
    python scripts/translate_worldbooks.py

只处理三对文件，其他文件不动；处理前自动生成 .bak 备份。
"""
import json
import os
import re
import shutil

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOK = os.path.join(BASE, "book")

PAIRS = [
    ("main_Sexual_position_lore_book_world_info.json", "性爱体位_世界书.md", "positions"),
    ("main_Ultimate_Sex_Toy_List_world_info.json", "性爱玩具大全_世界书.md", "toys"),
    ("main_Sex_Acts_world_info.json", "性爱行为_世界书.md", "acts"),
]

# 体位：中文主名 → 中文触发词
POS_KEYS = {
    "女上位": ["女上位", "骑乘位", "骑乘", "女上男下", "骑在上面"],
    "传教士体位": ["传教士", "传教士体位", "男上女下", "传统体位"],
    "站立后入": ["站立后入", "站着后入", "站立背后位"],
    "后入式": ["后入", "后入式", "背后位", "狗爬式", "从后面进入"],
    "站立传教士": ["站立传教士", "站着传教士", "站立男上女下"],
    "69 式": ["69", "69式", "六九", "互舔"],
    "芭蕾舞者式": ["芭蕾舞者", "芭蕾舞者式"],
    "口交": ["口交", "吹箫", "含住", "用嘴"],
    "舔阴": ["舔阴", "舔下面", "舔舐私处", "口交舔阴"],
}

# 行为：中文主名 → 中文触发词
ACT_KEYS = {
    "乳交": ["乳交", "胸交", "奶子夹", "用胸夹", "乳沟"],
    "口交": ["口交", "吹箫", "含住", "吞吐", "用嘴"],
    "手交": ["手交", "打手枪", "撸管", "用手撸"],
    "骚话": ["骚话", "脏话", "淫语", "下流话"],
    "付费指令": ["付费", "付钱", "打赏"],
    "射精": ["射精", "高潮", "射了", "内射"],
    "颜面口爆": ["颜面口爆", "口爆", "射在脸上", "脸射"],
    "倒计时": ["倒计时", "倒数"],
    "调试指令": ["调试", "调试指令"],
    "画面渲染指令": ["画面渲染", "渲染", "出图", "生成画面"],
    "打量女生": ["打量", "打量女生", "观察"],
    "含物口齿不清": ["含物说话", "含着说话", "口含", "口齿不清"],
}


def parse_md(path):
    """md → [{num, title, content}]；content 为 内容行 + 后续行（含 感受 等）。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    sections = []
    cur = None
    for line in text.splitlines():
        m = re.match(r"^## (\d+)\.\s*(.+)$", line)
        if m:
            if cur:
                sections.append(cur)
            cur = {"num": int(m.group(1)), "title": m.group(2).strip(), "content": []}
            continue
        if cur is None:
            continue
        cm = re.match(r"^- 内容：\s*(.*)$", line)
        if cm and not cur["content"]:
            cur["content"].append(cm.group(1).strip())
            continue
        if cur["content"] and line.strip():
            cur["content"].append(line.strip())
    if cur:
        sections.append(cur)
    for s in sections:
        s["content"] = "\n".join(s["content"]).strip()
    return sections


def main_title(title):
    return re.sub(r"[（(].*$", "", title).strip()


def chinese_keys(kind, title):
    main = main_title(title)
    if kind == "positions":
        return POS_KEYS.get(main, [main])
    if kind == "acts":
        return ACT_KEYS.get(main, [main])
    # toys：中文名（含 "/" 别名拆分）
    keys = []
    for part in re.split(r"[/／]", main):
        part = part.strip()
        if part and part not in keys:
            keys.append(part)
    return keys[:8]


def translate(json_name, md_name, kind):
    jpath = os.path.join(BOOK, json_name)
    mpath = os.path.join(BOOK, md_name)
    if not os.path.isfile(jpath) or not os.path.isfile(mpath):
        print("SKIP", json_name, "缺文件")
        return
    shutil.copyfile(jpath, jpath + ".bak")
    with open(jpath, "r", encoding="utf-8") as f:
        data = json.load(f)
    sections = parse_md(mpath)
    raw = data.get("entries", {})
    if isinstance(raw, dict):
        items = sorted(raw.values(), key=lambda e: int(e.get("id", 0)))
    else:
        items = sorted(raw, key=lambda e: int(e.get("id", 0)))
    if len(items) != len(sections):
        print("WARN", json_name, "条目数不一致: json=%d md=%d" % (len(items), len(sections)))
    n = 0
    for i, entry in enumerate(items):
        if i >= len(sections):
            break
        sec = sections[i]
        keys = chinese_keys(kind, sec["title"])
        entry["keys"] = list(keys)
        if "key" in entry:
            entry["key"] = list(keys)
        entry["content"] = sec["content"]
        entry["name"] = sec["title"]
        n += 1
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if items:
        first = items[0]
        print("OK", json_name, "更新 %d 条 | 示例: %s -> keys=%s | content=%s..." % (
            n, first.get("name"), first.get("keys"), first.get("content", "")[:40]))
    else:
        print("OK", json_name, "更新 %d 条（entries 为空）" % n)


def main():
    for j, m, k in PAIRS:
        translate(j, m, k)


if __name__ == "__main__":
    main()
