# -*- coding: utf-8 -*-
"""Obsidian vault 导出：把 SQLite 记忆库同步成 md 文件（frontmatter + 双链）。

纯函数、DB 只读、幂等全量重建；供 plugins/vault_sync.py（自动同步）
与 memory_ui.py vault 命令（手动导出）调用。

vault 结构：
  {vault}/青柠记忆/
    梦梦.md                       ← 身份索引（好感度 + 双链 + 长期记忆列表）
    梦梦/
      2026-08-08-001-被老板批评.md  ← 每条重要记忆一个文件
"""
import datetime
import json
import os
import re
import tempfile

from qqbot import textutil
from qqbot.memstore.base import KeywordMixin
from qqbot.memstore.local import SQLiteMemoryStore

ROOT_DIR = "青柠记忆"

_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n]+')


def _open_db(db_path):
    """打开记忆库：schema/补列迁移统一交给 SQLiteMemoryStore（单源，幂等）。"""
    store = SQLiteMemoryStore({"db_path": db_path})
    return store


def _load_keywords(raw):
    """关键词解析统一走存储层（旧实现与 memory_ui/plugins 各有一份）。"""
    return KeywordMixin._load_keywords(raw)


def _safe_title(text, max_len=24):
    """从记忆内容生成标题：去剧情时间戳前缀（文件名已含），截断放宽，去尾标点。"""
    raw = str(text or "").strip()
    ts = textutil.parse_story_time(raw)
    if ts:
        raw = raw[len(ts):].lstrip("：:，,、 ")
    t = _ILLEGAL.sub("", raw).strip()
    t = t[:max_len].rstrip("，。；、：,.;:！？!?…")
    return t or "记忆"


def _fmt_time(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _sign(delta):
    return "+" if delta > 0 else ""


def _atomic_write(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _cleanup_managed(base):
    """只删除上次导出生成的文件（manifest 白名单），保留人工添加的笔记。

    无 manifest（旧版本导出）时不删任何文件，避免误删；残留旧文件可手动清理。
    """
    manifest_path = os.path.join(base, ".manifest.json")
    if not os.path.isfile(manifest_path):
        return
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            files = json.load(f)
    except (OSError, ValueError):
        return
    for rel in files:
        p = os.path.join(base, rel)
        if os.path.isfile(p):
            os.remove(p)
    # 清理空目录（身份目录 + root 自身，不动人工新建目录）
    for name in os.listdir(base):
        d = os.path.join(base, name)
        if os.path.isdir(d):
            try:
                os.rmdir(d)
            except OSError:
                pass  # 非空（有人工文件）则保留


def export_vault(db_path, vault_dir, memories_root=None):
    """全量导出记忆库到 vault。返回 (写入文件数, 身份名列表)。

    幂等：按 manifest 白名单清掉上次生成的文件再重建；人工添加的笔记不受影响。
    """
    root = memories_root or ROOT_DIR
    base = os.path.join(vault_dir, root)

    store = _open_db(db_path)  # schema/补列迁移由 SQLiteMemoryStore 完成（幂等）
    try:
        conn = store.conn
        important = conn.execute(
            "SELECT id, identity, content, affect_delta, importance, story_time, keywords, created_at "
            "FROM important_memories ORDER BY created_at").fetchall()
        longterm = conn.execute(
            "SELECT id, identity, content, created_at "
            "FROM memories ORDER BY created_at").fetchall()
        affection = conn.execute("SELECT identity, score FROM affection").fetchall()
    finally:
        store.close()

    _cleanup_managed(base)

    affection_map = dict(affection)
    by_identity = {}
    for row in important:
        by_identity.setdefault(row[1], {"important": [], "longterm": []})["important"].append(row)
    for row in longterm:
        by_identity.setdefault(row[1], {"important": [], "longterm": []})["longterm"].append(row)

    written = 0
    manifest_files = []
    for identity, data in sorted(by_identity.items()):
        ident_dir = os.path.join(base, identity)
        os.makedirs(ident_dir, exist_ok=True)
        links = []
        for (mid, _idn, content, delta, imp, st, kw_raw, created) in data["important"]:
            title = _safe_title(content)
            # 文件名/时间字段用剧情时间（如"周六下午"），无剧情时间时退回真实日期
            story_time = st or textutil.parse_story_time(content)
            keywords = _load_keywords(kw_raw)
            fname = "%s-%03d-%s.md" % (story_time or datetime.datetime.fromtimestamp(created).strftime("%Y-%m-%d"),
                                       mid, title)
            lines = [
                "---",
                "identity: %s" % identity,
                "kind: important",
                "affect_delta: %s%d" % (_sign(delta), delta),
                "importance: %d" % imp,
                "story_time: %s" % (story_time or "（未知）"),
                "created: %s" % _fmt_time(created),
                "id: %d" % mid,
                "---",
                "",
                "# %s" % title,
                "",
                content,
                "",
                "> 剧情时间 %s ｜ 好感度变动 %s%d ｜ 重要度 %d" % (
                    story_time or "（未知）", _sign(delta), delta, imp),
                "> 相关：[[%s]]" % identity,
                "",
            ]
            if keywords:
                lines.insert(lines.index("---", 1), "tags: [%s]" % ", ".join(keywords))
            _atomic_write(os.path.join(ident_dir, fname), "\n".join(lines))
            written += 1
            manifest_files.append(os.path.join(identity, fname))
            links.append(fname[:-3])

        score = affection_map.get(identity)
        idx_lines = [
            "---",
            "identity: %s" % identity,
            "kind: identity",
            "affection: %s" % (score if score is not None else "（未建立）"),
            "important_count: %d" % len(data["important"]),
            "---",
            "",
            "# %s" % identity,
            "",
            "好感度：%s/400" % (score if score is not None else "（未建立）"),
            "",
        ]
        if links:
            idx_lines += ["## 重要记忆", ""]
            idx_lines += ["- [[%s]]" % l for l in links]
            idx_lines += [""]
        if data["longterm"]:
            idx_lines += ["## 长期记忆", ""]
            for (_mid, _idn, content, created) in data["longterm"]:
                idx_lines.append("- %s （%s）" % (content, _fmt_time(created)))
            idx_lines.append("")
        _atomic_write(os.path.join(base, identity + ".md"), "\n".join(idx_lines))
        written += 1
        manifest_files.append(identity + ".md")

    # 更新 manifest：下次重建只删这些文件，人工笔记保留
    _atomic_write(os.path.join(base, ".manifest.json"),
                  json.dumps(sorted(manifest_files), ensure_ascii=False, indent=1))
    return written, sorted(by_identity.keys())
