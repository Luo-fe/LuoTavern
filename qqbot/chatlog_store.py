# -*- coding: utf-8 -*-
"""聊天记录存储：路径方案 + 通用读取器 + 全局条目序号。

格式：data/chatlog/{身份}/{第N周周X}.md（剧情日期分类，如"第1周周六.md"；
同一剧情日内跨时段（下午→晚上）同文件；周日睡到周一跨周 → 第2周周一.md）
条目行：`- {剧情时间戳} **{身份名}**：内容`（时间戳为完整剧情时间，如"第1周周六晚上"；
身份名不含有空格和 *，可安全正则）
续行：两个空格前缀
条目序号：按 (剧情日期升序, 行序) 全局递增，供封存/纠正标记引用。
条目稳定 id：内容指纹（时间戳+说话人+正文），用于抵抗重排/迁移导致的序号错位
（`entry_id()`；封存/纠正同时记录序号与 id，见 seal.py / correct.py）。
启动时把旧真实日期文件（YYYY-MM-DD.md）按行内时间戳拆分迁移为剧情日期文件。
"""
import hashlib
import os
import re

ENTRY_RE = re.compile(r"^- ([^\s*]+) \*\*([^*\n]+?)\*\*：(.*)$")


def entry_id(entry_or_parts):
    """条目稳定 id：sha1(时间戳|说话人|正文)[:16]。

    entry 可以是 (index, stamp, speaker, content) 或 (story_date, stamp, speaker, content)，
    取后三项；内容重排/迁移后 id 不变（正文被改动才会变，此时按"未处理"重来，安全）。
    """
    parts = list(entry_or_parts)
    stamp, speaker, content = parts[-3], parts[-2], parts[-1]
    raw = "%s|%s|%s" % (stamp, speaker, content)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
# 剧情日期文件名：第N周周X.md
_STORY_FILE_RE = re.compile(r"^第(\d+)周(周[一二三四五六日天])\.md$")
_REAL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_WEEKDAY_ORDER = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6}


def _story_sort_key(fn):
    """剧情日期文件按 (周, 星期序) 排序；无法识别的文件名排最后（真实日期文件兼容）。"""
    m = _STORY_FILE_RE.match(fn)
    if m:
        return (int(m.group(1)), _WEEKDAY_ORDER.get(m.group(2), 0))
    return (10 ** 9, 0)


class ChatlogStore:
    def __init__(self, bot):
        self.bot = bot
        cfg = bot.config.get("chatlog", {})
        base = os.path.dirname(os.path.abspath(__file__))
        rel = cfg.get("dir", "data/chatlog")
        self.root = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(base, "..", rel))
        os.makedirs(self.root, exist_ok=True)
        self._read_cache = {}  # identity -> (文件签名, entries)
        self._migrate_legacy()
        self._migrate_story_dates()

    # ---------- 归档目录 ----------
    def _archive_dir(self):
        archive_rel = self.bot.config.get("archive_dir", "data/archive")
        base = os.path.dirname(os.path.abspath(__file__))
        return archive_rel if os.path.isabs(archive_rel) else os.path.normpath(os.path.join(base, "..", archive_rel))

    # ---------- 旧数据迁移 ----------
    def _migrate_legacy(self):
        """旧版平铺 data/chatlog/*.md（身份化之前）原样移入 archive，幂等。"""
        archive = self._archive_dir()
        moved = 0
        try:
            names = os.listdir(self.root)
        except OSError:
            return
        for fn in names:
            if not fn.endswith(".md"):
                continue
            src = os.path.join(self.root, fn)
            if not os.path.isfile(src):
                continue
            os.makedirs(archive, exist_ok=True)
            dst = os.path.join(archive, fn)
            try:
                if os.path.exists(dst):
                    os.remove(dst)
                os.replace(src, dst)
                moved += 1
            except OSError as exc:
                # 文件被占用（旧进程仍在写）→ 跳过；平铺文件不影响新身份化读写
                self.bot.log("[chatlog] 跳过迁移 %s: %s" % (fn, exc))
        if moved:
            self.bot.log("[chatlog] 旧平铺日志 %d 个移入 %s" % (moved, archive))

    def _migrate_story_dates(self):
        """真实日期文件 → 剧情日期文件（第N周周X.md），按行内时间戳分组拆文件。

        幂等：原文件先移入 archive/{身份}/（不同身份同日期互不覆盖），再从 archive
        读回分组写入剧情日期文件——任何一步失败数据都留在 archive，下次启动重试；
        原文件离开聊天目录后不会重复迁移。含无法识别时间戳行的文件整体跳过。
        """
        archive = self._archive_dir()
        for identity in self.identity_dirs():
            d = os.path.join(self.root, identity)
            try:
                fns = [f for f in os.listdir(d) if _REAL_DATE_RE.match(f)]
            except OSError:
                continue
            for fn in fns:
                src = os.path.join(d, fn)
                ident_archive = os.path.join(archive, identity)
                os.makedirs(ident_archive, exist_ok=True)
                dst = os.path.join(ident_archive, fn)
                try:
                    if os.path.exists(dst):
                        os.remove(dst)
                    os.replace(src, dst)
                except OSError as exc:
                    self.bot.log("[chatlog] %s/%s 迁移归档失败: %s" % (identity, fn, exc))
                    continue
                groups = {}      # story_date -> [行]
                last_key = None  # 续行跟随最近的条目；不可解析行置 None
                try:
                    with open(dst, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.rstrip("\n")
                            if not line:
                                continue
                            if line.startswith("# "):
                                continue  # 文件头
                            m = ENTRY_RE.match(line)
                            if m:
                                sm = re.match(r"^(第\d+周(?:周[一二三四五六日天]))", m.group(1))
                                last_key = sm.group(1) if sm else None
                                if last_key:
                                    groups.setdefault(last_key, []).append(line)
                            elif last_key in groups and line.startswith("  "):
                                groups[last_key].append(line)
                            else:
                                last_key = None
                except OSError:
                    continue
                if not groups:
                    # 无可分组条目（如旧 HH:MM 格式）：留在 archive 不打扰聊天目录
                    self.bot.log("[chatlog] %s/%s 无剧情时间戳，保留在 archive" % (identity, fn))
                    continue
                for story_date, lines in groups.items():
                    p = os.path.join(d, story_date + ".md")
                    is_new = not os.path.exists(p)
                    with open(p, "a", encoding="utf-8") as f:
                        if is_new:
                            f.write("# 聊天记录 %s %s\n\n" % (identity, story_date))
                        f.write("\n".join(lines) + "\n")
                self.bot.log("[chatlog] %s/%s → 剧情日期 %d 个文件" % (identity, fn, len(groups)))

    # ---------- 路径 ----------
    def path_for(self, identity, story_date):
        return os.path.join(self.root, identity, story_date + ".md")

    def identity_dirs(self):
        try:
            return sorted(
                d for d in os.listdir(self.root)
                if os.path.isdir(os.path.join(self.root, d))
            )
        except OSError:
            return []

    # ---------- 读取 ----------
    def read_entries(self, identity, dates=None):
        """按时间顺序返回 [(index, stamp, speaker, content)]；index 为全局条目序号。

        stamp 为完整剧情时间戳（如"第1周周六晚上"）；dates：可选允许的剧情日期集合
        （"第N周周X"），None 表示全部。
        """
        entries = []
        idx = 0
        d = os.path.join(self.root, identity)
        if not os.path.isdir(d):
            return entries
        try:
            all_files = sorted((fn for fn in os.listdir(d) if fn.endswith(".md")), key=_story_sort_key)
        except OSError:
            return entries
        if dates is None:
            sig = tuple(
                (fn, os.path.getmtime(os.path.join(d, fn)),
                 os.path.getsize(os.path.join(d, fn)))
                for fn in all_files
            )
            cached = self._read_cache.get(identity)
            if cached and cached[0] == sig:
                return [tuple(e) for e in cached[1]]
        if dates is not None:
            wanted = set(dates)
            all_files = [fn for fn in all_files if fn[:-3] in wanted]
        for fn in all_files:
            path = os.path.join(d, fn)
            cur = None
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        m = ENTRY_RE.match(line.rstrip("\n"))
                        if m:
                            cur = (m.group(1), m.group(2), m.group(3))
                            entries.append([idx, cur[0], cur[1], cur[2]])
                            idx += 1
                        elif cur and line.startswith("  "):
                            entries[-1][3] += "\n" + line[2:].rstrip("\n")
            except OSError:
                continue
        out = [tuple(e) for e in entries]
        if dates is None:
            self._read_cache[identity] = (sig, out)
        return out

    def recent_entries(self, identity, limit=20):
        """返回最近 limit 条 [(story_date, stamp, speaker, content)]，时间正序（用于【回放聊天】）。"""
        out = []
        d = os.path.join(self.root, identity)
        if not os.path.isdir(d):
            return out
        try:
            files = sorted((fn for fn in os.listdir(d) if fn.endswith(".md")), key=_story_sort_key)
        except OSError:
            return out
        for fn in files:
            path = os.path.join(d, fn)
            cur = None
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        m = ENTRY_RE.match(line.rstrip("\n"))
                        if m:
                            cur = (m.group(1), m.group(2), m.group(3))
                            out.append([fn[:-3], cur[0], cur[1], cur[2]])
                        elif cur and line.startswith("  "):
                            out[-1][3] += "\n" + line[2:].rstrip("\n")
            except OSError:
                continue
        return [tuple(e) for e in out[-limit:]]

    def entry_count(self, identity):
        return len(self.read_entries(identity))

    def last_bot_entry(self, identity):
        """返回该身份最后一条青柠回复的 (index, hhmm, content) 或 None。"""
        for e in reversed(self.read_entries(identity)):
            if e[2] == self.bot.bot_name:
                return e
        return None
