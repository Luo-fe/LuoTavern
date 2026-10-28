# -*- coding: utf-8 -*-
"""本地 SQLite 记忆适配器（完整移植自原 plugins/memory.py 的行为）。

- 单连接 + threading.RLock + WAL：写不阻塞读、多线程安全
- 向量索引按身份缓存，键 = (db mtime, 写入计数)；外部进程（memory_ui 管理台）
  写入靠 mtime 变化自动失效，本进程写入靠计数立即失效
- 嵌入模型不可用/失败 → 向量路径返回 []，关键词路径不受影响
"""

import os
import sqlite3
import threading
import time

from qqbot import relative_time, textutil
from qqbot.embeddings import LocalEmbedder
from qqbot.memstore.base import MemoryStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity TEXT NOT NULL,
    content TEXT NOT NULL,
    keywords TEXT,
    created_at REAL NOT NULL,
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_recalled_at REAL
);
CREATE TABLE IF NOT EXISTS important_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity TEXT NOT NULL,
    content TEXT NOT NULL,
    affect_delta INTEGER NOT NULL DEFAULT 0,
    importance INTEGER NOT NULL DEFAULT 5,
    story_time TEXT,
    keywords TEXT,
    track TEXT NOT NULL DEFAULT 'story',
    created_at REAL NOT NULL,
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_recalled_at REAL
);
CREATE TABLE IF NOT EXISTS affection (
    identity TEXT PRIMARY KEY,
    score INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_important_identity ON important_memories(identity, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_identity ON memories(identity, created_at DESC);
"""


def _to_int(value, default):
    """宽松取整：None/NaN/坏字符串一律回退默认值。

    写路径契约是"失败返回 False，绝不抛异常"；此前 int(None)/int('abc')/int(nan)
    会直接把异常抛进消息管线（表现为"出错了"，且封存点不推进 → 重复提炼）。
    """
    try:
        if value is None:
            return default
        f = float(value)
        if f != f or f in (float("inf"), float("-inf")):  # NaN / Inf
            return default
        return int(f)
    except (TypeError, ValueError, OverflowError):
        return default


def _clamp_delta(value):
    return max(-100, min(100, _to_int(value, 0)))


def _clamp_importance(value):
    return max(0, min(10, _to_int(value, 5)))


def _dedup_key(text):
    """去重键（统一实现见 textutil.dedup_key）。"""
    return textutil.dedup_key(text)


def _overlap_ratio(query_grams, text):
    """查询 2-gram 命中比例（统一实现见 textutil.overlap_ratio）。"""
    return textutil.overlap_ratio(query_grams, text)


def migrate_conn(conn, log=None):
    """建表 + 旧库迁移（幂等，可对任意 SQLite 连接调用）。

    - 旧版 user_id 建表的 memories → 更名保留（memories_legacy_userid），不删数据
    - 旧库缺列 → ALTER 补列（importance/story_time/keywords/track）
    - 回填历史 story_time：content 自带剧情时间戳但字段为空的 → 解析回填
    """
    log = log or (lambda msg: None)
    conn.execute("PRAGMA busy_timeout=5000")
    # 先识别两张旧表，再建表/补列，最后建索引。索引不能引用尚不存在的 identity。
    for table, backup in (("memories", "memories_legacy_userid"),
                          ("important_memories", "important_memories_legacy")):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
        if cols and "identity" not in cols:
            target, suffix = backup, 1
            while conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (target,)).fetchone():
                target = "%s_%d" % (backup, suffix)
                suffix += 1
            conn.execute("ALTER TABLE %s RENAME TO %s" % (table, target))
            # 已命名索引会随旧表更名，释放名字供新表建索引。
            for index in ("idx_memories_identity", "idx_important_identity"):
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=? AND tbl_name=?",
                                (index, target)).fetchone():
                    conn.execute("DROP INDEX %s" % index)
            conn.commit()
    conn.executescript(SCHEMA)
    imp_cols = [r[1] for r in conn.execute("PRAGMA table_info(important_memories)").fetchall()]
    mem_cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
    for cols, ddl in (
        (mem_cols, "ALTER TABLE memories ADD COLUMN keywords TEXT"),
        (mem_cols, "ALTER TABLE memories ADD COLUMN recall_count INTEGER NOT NULL DEFAULT 0"),
        (mem_cols, "ALTER TABLE memories ADD COLUMN last_recalled_at REAL"),
        (imp_cols,
         "ALTER TABLE important_memories ADD COLUMN importance INTEGER NOT NULL DEFAULT 5"),
        (imp_cols, "ALTER TABLE important_memories ADD COLUMN story_time TEXT"),
        (imp_cols, "ALTER TABLE important_memories ADD COLUMN keywords TEXT"),
        (imp_cols,
         "ALTER TABLE important_memories ADD COLUMN track TEXT NOT NULL DEFAULT 'story'"),
        (imp_cols,
         "ALTER TABLE important_memories ADD COLUMN recall_count INTEGER NOT NULL DEFAULT 0"),
        (imp_cols, "ALTER TABLE important_memories ADD COLUMN last_recalled_at REAL"),
    ):
        col = ddl.split("ADD COLUMN ")[1].split(" ")[0]
        if col not in cols:
            try:
                conn.execute(ddl)
                conn.commit()
            except sqlite3.OperationalError:
                pass
    conn.executescript(INDEXES)
    # 管理台等外部连接也受触发器约束。编辑/删除后隔离整份旧远端画像，
    # 避免远端对旧正文的改写/蒸馏绕过简单的文本黑名单。
    conn.execute("CREATE TABLE IF NOT EXISTS remote_memory_versions "
                 "(identity TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0)")
    for table in ("memories", "important_memories"):
        for event in ("DELETE", "UPDATE"):
            when = (" WHEN OLD.identity IS NOT NEW.identity OR OLD.content IS NOT NEW.content "
                    "OR OLD.keywords IS NOT NEW.keywords" if event == "UPDATE" else "")
            bump = ("INSERT INTO remote_memory_versions(identity, version) VALUES (OLD.identity, 1) "
                    "ON CONFLICT(identity) DO UPDATE SET version=version+1;")
            if event == "UPDATE":
                bump += (" INSERT INTO remote_memory_versions(identity, version) "
                         "SELECT NEW.identity, 1 WHERE NEW.identity IS NOT OLD.identity "
                         "ON CONFLICT(identity) DO UPDATE SET version=version+1;")
            conn.execute("CREATE TRIGGER IF NOT EXISTS remote_version_%s_%s AFTER %s ON %s%s "
                         "BEGIN %s END" % (table, event.lower(), event, table, when, bump))
    conn.commit()
    # 回填历史 story_time
    try:
        rows = conn.execute(
            "SELECT id, content FROM important_memories WHERE story_time IS NULL OR story_time=''"
        ).fetchall()
        fixed = 0
        for row_id, content in rows:
            ts = textutil.parse_story_time(content)
            if ts:
                conn.execute(
                    "UPDATE important_memories SET story_time=? WHERE id=?",
                    (ts, row_id),
                )
                fixed += 1
        if fixed:
            conn.commit()
            log("[memory] 回填重要记忆剧情时间 %d 条" % fixed)
    except Exception:
        pass


class SQLiteMemoryStore(MemoryStore):
    backend = "local"

    def __init__(self, config, db_path=None, embedder=None, log=None):
        cfg = config or {}
        self.cfg = cfg
        self.log = log or (lambda msg: None)
        self._vec_cfg = cfg.get("vector", {}) or {}
        self._persistent_imp = int(cfg.get("persistent_importance", 9))
        self._persistent_max = max(1, int(cfg.get("persistent_max", 3)))
        self._max_memories = max(1, int(cfg.get("max_memories_per_user", 200)))
        self.lock = threading.RLock()
        # 项目根与机器人其余模块同源：优先 LUOTAVERN_ROOT（桌面 EXE 切换项目），
        # 否则取 qqbot/memstore/local.py 的上级三目录。
        # 旧实现只看 __file__：桌面版切换项目后记忆库会开到安装目录，
        # 与管理台/机器人读写的库静默分裂成两个。
        from qqbot.config import root_path
        base = root_path()
        rel = db_path or cfg.get("db_path", "data/memory.db")
        self.db_path = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(base, rel))
        d = os.path.dirname(self.db_path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout=5000")
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        self._migrate()
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._vec_index = {}       # identity -> (key, [(kind, content, delta, importance, vec)])
        # 内容哈希 → 向量：跨身份复用、写入后只重嵌入新增/改写的文本
        self._vec_cache = {}
        self._write_count = 0
        self.embedder = embedder or LocalEmbedder(
            model_name=self._vec_cfg.get("model"),
            cache_dir=self._vec_cfg.get("cache_dir"),
        )

    # ---------- 迁移 ----------
    def _migrate(self):
        """旧版按 user_id 建表 → 更名保留；旧库缺列 → ALTER 补列；回填 story_time。"""
        migrate_conn(self.conn, self.log)

    def _touch(self):
        """本进程写入后使向量缓存失效（mtime 之外的双保险）。"""
        self._write_count += 1

    def _db_version(self):
        """SQLite data_version：任何连接（含外部进程管理台）提交都会自增。

        WAL 模式下 mtime 只在 checkpoint 变化，不能可靠探测外部写入；
        data_version 是可靠的外部变更探测手段。"""
        try:
            with self.lock:      # 共享连接不能跨线程并发使用
                return self.conn.execute("PRAGMA data_version").fetchone()[0]
        except Exception:
            return 0

    def remote_revision(self, identity):
        """远端画像版本：查询失败必须向上传递，不能回退旧版本召回已删除内容。"""
        with self.lock:
            row = self.conn.execute("SELECT version FROM remote_memory_versions WHERE identity=?",
                                    (identity,)).fetchone()
        return int(row[0]) if row else 0

    # ---------- 长期记忆 ----------
    def save_longterm(self, identity, content, keywords=None):
        content = str(content).strip()[:300]
        if not content or not identity:
            return False
        norm = _dedup_key(content)
        with self.lock:
            # 同一条内容重复记住：不重复入库，只把新关键词并进去（幂等）
            row = self.conn.execute(
                "SELECT id, keywords FROM memories WHERE identity=? ORDER BY created_at DESC LIMIT 200",
                (identity,),
            ).fetchall()
            for mid, raw in row:
                existing = self.conn.execute(
                    "SELECT content FROM memories WHERE id=?", (mid,)).fetchone()
                if existing and _dedup_key(existing[0]) == norm:
                    merged = self._load_keywords(raw)
                    for k in (keywords or []):
                        k = str(k).strip()
                        if k and k not in merged:
                            merged.append(k)
                    self.conn.execute("UPDATE memories SET keywords=?, created_at=? WHERE id=?",
                                      (self._dump_keywords(merged), time.time(), mid))
                    self.conn.commit()
                    return True
            self.conn.execute(
                "INSERT INTO memories (identity, content, keywords, created_at) VALUES (?, ?, ?, ?)",
                (identity, content, self._dump_keywords(keywords), time.time()),
            )
            self.conn.execute(
                "DELETE FROM memories WHERE identity=? AND id NOT IN "
                "(SELECT id FROM memories WHERE identity=? ORDER BY created_at DESC LIMIT ?)",
                (identity, identity, self._max_memories),
            )
            self.conn.commit()
            self._touch()
        return True

    def delete_longterm(self, identity, keyword):
        if not identity or not keyword:
            return False
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, content FROM memories WHERE identity=? ORDER BY created_at DESC LIMIT 200",
                (identity,),
            ).fetchall()
            kg = textutil.ngrams(keyword)
            hit = None
            for mid, content in rows:
                if keyword in content or (kg and (kg & textutil.ngrams(content))):
                    hit = mid
                    break
            if hit is None:
                return False
            self.conn.execute("DELETE FROM memories WHERE id=?", (hit,))
            self.conn.commit()
            self._touch()
        return True

    def list_longterm(self, identity, limit=500):
        if not identity:
            return []
        with self.lock:
            rows = self.conn.execute(
                "SELECT content FROM memories WHERE identity=? ORDER BY created_at DESC LIMIT ?",
                (identity, int(limit)),
            ).fetchall()
        return [r[0] for r in rows]

    def recall_longterm(self, identity, query, top=3):
        top = int(top) if top else 3
        mems = self.list_longterm(identity)
        if not mems:
            return []
        q_grams = textutil.ngrams(query)
        scored = []
        for mem in mems:
            ratio = _overlap_ratio(q_grams, mem)
            if ratio > 0:
                scored.append((ratio, mem))
        scored.sort(key=lambda x: -x[0])
        return [m for _, m in scored[:top]]

    # ---------- 重要记忆 ----------
    def save_important(self, identity, content, affect_delta=0, importance=5,
                       story_time=None, keywords=None, track="story"):
        content = str(content).strip()[:300]
        if not content or not identity:
            return False
        delta = _clamp_delta(affect_delta)
        track = track or "story"
        # 字段硬门槛：提炼模型偶尔漏字段，缺失时用规则兜底
        # （story_time / keywords 的覆盖率直接决定时间窗检索与关键词召回能不能用）
        story = story_time or textutil.parse_story_time(content)
        kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
        if not kws:
            kws = textutil.derive_keywords(content)
        if importance is None:
            # 未给重要度 → 由情绪强度推导（|delta| 越大越重要），默认 5
            imp = max(1, min(10, 5 + int(abs(delta) / 10)))
        else:
            imp = _clamp_importance(importance)
        norm = _dedup_key(content)
        with self.lock:
            # 同内容不重复入库（睡前提炼常把同一件事反复提炼出来，
            # 旧行为会让重复条目挤占 max_important 并触发破坏性的合并）
            rows = self.conn.execute(
                "SELECT id, content FROM important_memories WHERE identity=? AND track=? "
                "ORDER BY created_at DESC LIMIT 200",
                (identity, track),
            ).fetchall()
            for mid, old in rows:
                if _dedup_key(old) == norm:
                    return True
            self.conn.execute(
                "INSERT INTO important_memories "
                "(identity, content, affect_delta, importance, story_time, keywords, track, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (identity, content, delta, imp, story,
                 self._dump_keywords(kws), track, time.time()),
            )
            self.conn.commit()
            self._touch()
        return True

    def _fetch_important_full(self, identity, limit=500):
        """重要记忆完整字段（含关键词）：
        (content, affect_delta, importance, keywords)。"""
        if not identity:
            return []
        with self.lock:
            rows = self.conn.execute(
                "SELECT content, affect_delta, importance, keywords FROM important_memories "
                "WHERE identity=? ORDER BY created_at DESC LIMIT ?",
                (identity, int(limit)),
            ).fetchall()
        return [(r[0], r[1], r[2], self._load_keywords(r[3])) for r in rows]

    def important_candidates(self, identity, limit=500):
        """召回重排用的重要记忆候选（带生命周期字段与 track）：

        [(id, content, affect_delta, importance, keywords, story_time, track,
          recall_count, last_recalled_at)]
        """
        if not identity:
            return []
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, content, affect_delta, importance, keywords, story_time, "
                "track, recall_count, last_recalled_at FROM important_memories "
                "WHERE identity=? ORDER BY created_at DESC LIMIT ?",
                (identity, int(limit)),
            ).fetchall()
        return [(r[0], r[1], r[2], r[3], self._load_keywords(r[4]), r[5] or "",
                 r[6] or "story", int(r[7] or 0), r[8]) for r in rows]

    def longterm_candidates(self, identity, limit=200):
        """召回重排用的长期记忆候选：[(id, content, keywords)]。"""
        if not identity:
            return []
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, content, keywords FROM memories WHERE identity=? "
                "ORDER BY created_at DESC LIMIT ?",
                (identity, int(limit)),
            ).fetchall()
        return [(r[0], r[1], self._load_keywords(r[2])) for r in rows]

    def mark_recalled(self, identity, contents, boost=1):
        """召回强化：命中的记忆 importance+boost、recall_count+1、记录召回时间。

        让"经常被想起"的记忆自然上浮（不删除任何条目，只调整排序权重）。
        返回实际更新的条数。
        """
        if not identity or not contents:
            return 0
        keys = {_dedup_key(c) for c in contents if str(c).strip()}
        if not keys:
            return 0
        updated = 0
        now = time.time()
        # 注意：这里故意不调 _touch()——importance 变化不影响文本嵌入，
        # 若让向量索引失效，每次"想起来"都会触发全量重嵌入
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, content, importance FROM important_memories "
                "WHERE identity=? ORDER BY created_at DESC LIMIT 500",
                (identity,),
            ).fetchall()
            for mid, content, importance in rows:
                if _dedup_key(content) not in keys:
                    continue
                new_imp = max(0, min(10, int(importance or 0) + int(boost)))
                self.conn.execute(
                    "UPDATE important_memories SET importance=?, "
                    "recall_count=recall_count+1, last_recalled_at=? WHERE id=?",
                    (new_imp, now, mid))
                updated += 1
            rows = self.conn.execute(
                "SELECT id, content FROM memories WHERE identity=? "
                "ORDER BY created_at DESC LIMIT 200",
                (identity,),
            ).fetchall()
            for mid, content in rows:
                if _dedup_key(content) not in keys:
                    continue
                self.conn.execute(
                    "UPDATE memories SET recall_count=recall_count+1, "
                    "last_recalled_at=? WHERE id=?", (now, mid))
                updated += 1
            if updated:
                self.conn.commit()
        return updated

    def list_important(self, identity, limit=500):
        if not identity:
            return []
        with self.lock:
            rows = self.conn.execute(
                "SELECT content, affect_delta, importance FROM important_memories WHERE identity=? "
                "ORDER BY created_at DESC LIMIT ?",
                (identity, int(limit)),
            ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def list_important_ids(self, identity, limit=500, track=None):
        """带 id 版本（记忆合并对账用）：
        (id, content, affect_delta, importance, keywords)。

        - track：只取指定轨道（合并只应处理 story，world 轨道不能被改写/复制）
        - limit=None：取全部（合并对账必须看到全量，截断会导致旧条目被误删）
        """
        if not identity:
            return []
        sql = ("SELECT id, content, affect_delta, importance, keywords "
               "FROM important_memories WHERE identity=?")
        args = [identity]
        if track:
            sql += " AND track=?"
            args.append(str(track))
        sql += " ORDER BY created_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self.lock:
            rows = self.conn.execute(sql, tuple(args)).fetchall()
        return [(r[0], r[1], r[2], r[3], self._load_keywords(r[4])) for r in rows]

    def list_important_by_track(self, identity, track, limit=10):
        """按轨道取重要记忆（world=世界书剧情 / story=角色故事）；
        [(content, affect_delta, importance)]。"""
        if not identity:
            return []
        with self.lock:
            rows = self.conn.execute(
                "SELECT content, affect_delta, importance FROM important_memories "
                "WHERE identity=? AND track=? ORDER BY created_at DESC LIMIT ?",
                (identity, track or "story", int(limit)),
            ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def track_counts(self, identity):
        """各轨道条数：{"story": n, "world": n}。"""
        if not identity:
            return {"story": 0, "world": 0}
        with self.lock:
            rows = self.conn.execute(
                "SELECT track, COUNT(*) FROM important_memories WHERE identity=? GROUP BY track",
                (identity,),
            ).fetchall()
        out = {"story": 0, "world": 0}
        for track, n in rows:
            out[track or "story"] = n
        return out

    def list_important_story(self, identity, prefix, limit=100):
        """重要记忆按剧情时间前缀过滤（如"第1周周日深夜"）：
        [(content, affect_delta)]。"""
        if not identity or not prefix:
            return []
        with self.lock:
            rows = self.conn.execute(
                "SELECT content, affect_delta FROM important_memories "
                "WHERE identity=? AND story_time LIKE ? ORDER BY created_at DESC LIMIT ?",
                (identity, prefix + "%", int(limit)),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def list_important_by_time(self, identity, time_windows, limit=500):
        """按流动性时间窗过滤重要记忆：
        [(剧情日前缀, 时段集合)] → [(content, affect_delta)]。"""
        if not identity or not time_windows:
            return []
        with self.lock:
            rows = self.conn.execute(
                "SELECT content, affect_delta, story_time FROM important_memories "
                "WHERE identity=? ORDER BY created_at DESC LIMIT ?",
                (identity, int(limit)),
            ).fetchall()
        out = []
        for content, delta, st in rows:
            stamp = relative_time.memory_stamp(content, st)
            if stamp and relative_time.match_stamp(stamp, time_windows):
                out.append((content, delta))
        return out

    def replace_important(self, identity, items, scope_ids=None):
        """替换重要记忆（合并去重用）；
        items=[(content, delta, importance[, keywords[, track]])]。

        - scope_ids=None（兼容旧调用）：删除该身份全部 `track='story'` 行后整体写回；
        - scope_ids=[id, ...]（推荐，合并场景）：**只删除这些 id**，其余条目（含 world
          轨道与未参与合并的条目）原样保留。传全量 id 时两者等价，但截断读取造成的
          "删了却没写回"在 scope_ids 模式下不可能发生。
        返回新条数。"""
        if not identity:
            return 0
        norm = []
        for item in items:
            c = str(item[0]).strip()[:300]
            d = _clamp_delta(item[1])
            i = _clamp_importance(item[2])
            kw = item[3] if len(item) > 3 else None
            track = item[4] if len(item) > 4 else "story"
            norm.append((identity, c, d, i, self._dump_keywords(kw),
                         textutil.parse_story_time(c), track or "story", time.time()))
        ids = []
        for raw in (scope_ids or []):
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        with self.lock:
            if scope_ids is None:
                self.conn.execute(
                    "DELETE FROM important_memories WHERE identity=? AND track='story'",
                    (identity,))
            elif ids:
                self.conn.executemany(
                    "DELETE FROM important_memories WHERE identity=? AND id=?",
                    [(identity, i) for i in ids])
            self.conn.executemany(
                "INSERT INTO important_memories "
                "(identity, content, affect_delta, importance, keywords, story_time, track, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                norm,
            )
            self.conn.commit()
            self._touch()
        return len(norm)

    # ---------- 好感度 ----------
    def get_affection(self, identity):
        if not identity:
            return None
        with self.lock:
            row = self.conn.execute(
                "SELECT score FROM affection WHERE identity=?", (identity,)
            ).fetchone()
        if row is not None:
            return int(row[0])
        return None

    def set_affection(self, identity, score):
        if not identity:
            return
        with self.lock:
            self.conn.execute(
                "INSERT INTO affection (identity, score, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(identity) DO UPDATE SET score=excluded.score, updated_at=excluded.updated_at",
                (identity, int(score), time.time()),
            )
            self.conn.commit()
            # 好感度不参与向量索引（记忆文本没变），不 _touch()：
            # 否则每次好感度变动都会让全部身份的向量索引失效并全量重嵌入

    # ---------- 检索 ----------
    def recall_important(self, identity, query, top=3):
        """重要记忆检索：关键词精确命中优先（零 LLM），宽松匹配与词重叠兜底，
        importance/|delta| 加权。返回 [(content, affect_delta)]。"""
        top = int(top) if top else 3
        mems = self._fetch_important_full(identity)
        if not mems:
            return []
        q_grams = textutil.ngrams(query)
        scored = []
        for content, delta, importance, keywords in mems:
            kw_hit = [k for k in keywords if self._kw_in_text(k, query)]
            kw_score = sum(self._kw_partial_score(k, query) for k in keywords)
            ratio = _overlap_ratio(q_grams, content)
            if ratio <= 0 and not kw_hit and kw_score <= 0:
                continue
            # 关键词精确命中 +300/个，宽松匹配 +20/分，词重叠按**命中比例**×10
            # （旧实现用原始命中个数，长条目有系统性优势），
            # 情绪强度/重要度 0.5 权重平衡
            score = (300 * len(kw_hit) + 20 * kw_score + 10 * ratio
                     + 0.5 * min(10, abs(delta)) + 0.5 * importance)
            scored.append((score, content, delta))
        scored.sort(key=lambda x: -x[0])
        return [(c, d) for _, c, d in scored[:top]]

    def content_overlap(self, identity, text):
        """消息与重要记忆正文的 2-gram 最大重叠数（零 LLM 的语义近似）。"""
        if not identity or not text:
            return 0
        tg = textutil.ngrams(text)
        if not tg:
            return 0
        best = 0
        with self.lock:
            rows = self.conn.execute(
                "SELECT content FROM important_memories "
                "WHERE identity=? ORDER BY created_at DESC LIMIT 500",
                (identity,),
            ).fetchall()
        for (content,) in rows:
            cg = textutil.ngrams(content)
            if cg:
                best = max(best, len(tg & cg))
        return best

    def keyword_hits(self, identity, text):
        """消息命中记忆关键词的条目（重要记忆优先，长期记忆其次）。
        返回 [(kind, content, affect_delta, importance, hit_keywords)]，
        kind=important/memory。"""
        if not identity or not text:
            return []
        hits = []
        with self.lock:
            rows = self.conn.execute(
                "SELECT content, affect_delta, importance, keywords FROM important_memories "
                "WHERE identity=? ORDER BY created_at DESC LIMIT 500",
                (identity,),
            ).fetchall()
            for content, delta, importance, raw in rows:
                ks = self._load_keywords(raw)
                hit = [k for k in ks if self._kw_in_text(k, text)]
                if hit:
                    hits.append(("important", content, delta, importance, hit))
            rows = self.conn.execute(
                "SELECT content, keywords FROM memories WHERE identity=? ORDER BY created_at DESC LIMIT 200",
                (identity,),
            ).fetchall()
            for content, raw in rows:
                ks = self._load_keywords(raw)
                hit = [k for k in ks if self._kw_in_text(k, text)]
                if hit:
                    hits.append(("memory", content, 0, 5, hit))
        return hits

    def has_keyword_hit(self, identity, text):
        """本地判断：消息是否命中任何记忆关键词（零 LLM 调用）。"""
        return bool(self.keyword_hits(identity, text))

    def has_memory_trigger(self, identity, text):
        """本地记忆触发：关键词命中，或消息与重要记忆正文有 ≥2 个 2-gram 重叠
        （同义改写/部分提及也能激活，零 LLM 调用）。"""
        if not identity or not text:
            return False
        if self.has_keyword_hit(identity, text):
            return True
        return self.content_overlap(identity, text) >= 2

    # ---------- 常驻记忆（importance 达标即常驻 system prompt） ----------
    def list_persistent(self, identity, min_importance=None, limit=None):
        """返回 importance ≥ 阈值的重要记忆（重要度优先、新近次之）。
        [(content, affect_delta, importance)]。"""
        if not identity:
            return []
        min_importance = int(min_importance if min_importance is not None else self._persistent_imp)
        limit = int(limit if limit is not None else self._persistent_max)
        rows = self._fetch_important_full(identity)
        picked = [r for r in rows if r[2] >= min_importance]
        picked.sort(key=lambda r: -r[2])  # 稳定排序：同重要度保持新近优先（DB 已倒序）
        return [(r[0], r[1], r[2]) for r in picked[:limit]]

    # ---------- 本地向量检索（fastembed + bge-small-zh） ----------
    def vector_recall(self, identity, query, top=None, min_score=None):
        """语义检索重要记忆 + 长期记忆；
        返回 [(kind, content, delta, importance, score)]。
        依赖本地嵌入模型；不可用/失败时返回 []（回忆回退关键词与词重叠）。"""
        if not identity or not query or not self._vec_cfg.get("enable", True):
            return []
        try:
            entries = self._vector_entries(identity)
        except Exception:
            return []
        if not entries:
            return []
        try:
            qv = self.embedder.embed([query])
        except Exception:
            return []
        if qv is None:
            return []
        qv = qv[0]
        min_score = float(min_score if min_score is not None else self._vec_cfg.get("min_score", 0.3))
        top = int(top if top is not None else self._vec_cfg.get("top_k", 3))
        scored = []
        try:
            for kind, content, delta, importance, vec in entries:
                s = self.embedder.cosine(qv, vec)
                if s >= min_score:
                    scored.append((s, kind, content, delta, importance))
        except Exception:
            return []
        scored.sort(key=lambda x: -x[0])
        return [(k, c, d, i, s) for s, k, c, d, i in scored[:top]]

    def vector_top_score(self, identity, text):
        """消息与记忆的最高语义相似度（通用触发用）；
        模型不可用/无索引返回 0.0。"""
        if not identity or not text or not self._vec_cfg.get("enable", True):
            return 0.0
        hits = self.vector_recall(identity, text, top=1, min_score=0.0)
        return hits[0][4] if hits else 0.0

    def recall_consistent(self, identity, query, top=2):
        """一致性评分：同时命中关键词与向量高分的记忆 = 高价值回忆。
        返回 [(kind, content, affect_delta, importance, kw_count, vec_score)]。"""
        if not identity or not query:
            return []
        kw_map = {}
        for kind, content, delta, importance, hit in self.keyword_hits(identity, query):
            kw_map[(kind, content)] = (delta, importance, len(hit))
        vec_map = {}
        for kind, content, delta, importance, score in self.vector_recall(
                identity, query, top=5, min_score=float(self._vec_cfg.get("min_score", 0.3))):
            vec_map[(kind, content)] = (delta, importance, score)
        out = []
        for key, (kd, ki, kc) in kw_map.items():
            if key in vec_map:
                _vd, _vi, vs = vec_map[key]
                out.append((key[0], key[1], kd, ki, kc, vs))
        out.sort(key=lambda r: -(r[4] + r[5]))
        return out[:top]

    def _vector_entries(self, identity):
        """该身份的记忆向量索引（data_version/写入计数缓存；失败不缓存，下次重试）。

        增量嵌入：向量按**内容哈希**缓存（`_vec_cache`），只有新增/改写的记忆
        才需要重新推理——旧实现每次缓存失效都全量重嵌入（写一条记忆 → 下次检索
        把所有身份的全部文本重新过一遍模型）。
        """
        version = self._db_version()
        key = (version, self._write_count)
        cached = self._vec_index.get(identity)
        if cached is not None and cached[0] == key:
            return cached[1]
        imp = self._fetch_important_full(identity)
        with self.lock:
            mem_rows = self.conn.execute(
                "SELECT content FROM memories WHERE identity=? ORDER BY created_at DESC LIMIT 200",
                (identity,),
            ).fetchall()
        rows = ([("important", c, d, i, "重要记忆：" + c) for c, d, i, _kw in imp]
                + [("memory", c, 0, 5, "长期记忆：" + c) for (c,) in mem_rows])
        if not rows:
            with self.lock:
                self._vec_index[identity] = (key, [])
            return []
        pending = [r for r in rows if r[4] not in self._vec_cache]
        if pending:
            vecs = self.embedder.embed([r[4] for r in pending])
            if vecs is None:
                return []          # 嵌入不可用：不缓存，下次再试
            for r, v in zip(pending, vecs):
                self._vec_cache[r[4]] = v
            # 防止缓存无限增长（只保留本进程最近用到的文本）
            if len(self._vec_cache) > 4000:
                for stale in list(self._vec_cache)[:2000]:
                    self._vec_cache.pop(stale, None)
        entries = [(kind, content, delta, importance, self._vec_cache[text])
                   for kind, content, delta, importance, text in rows
                   if text in self._vec_cache]
        with self.lock:
            self._vec_index[identity] = (key, entries)
        return entries

    # ---------- 生命周期 ----------
    def close(self):
        try:
            with self.lock:
                self.conn.commit()
                self.conn.close()
        except Exception:
            pass

    def stats(self):
        try:
            with self.lock:
                longterm = self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                important = self.conn.execute(
                    "SELECT COUNT(*) FROM important_memories").fetchone()[0]
                identities = self.conn.execute(
                    "SELECT COUNT(DISTINCT identity) FROM "
                    "(SELECT identity FROM memories UNION SELECT identity FROM important_memories)"
                ).fetchone()[0]
                max_ident = int(identities or 0)
                tracks = {"story": 0, "world": 0}
                for track, n in self.conn.execute(
                        "SELECT track, COUNT(*) FROM important_memories GROUP BY track").fetchall():
                    tracks[track or "story"] = n
        except Exception:
            longterm = important = max_ident = 0
            tracks = {"story": 0, "world": 0}
        # 零副作用探测：stats 会被管理台 5 秒轮询，不能用 available()（会加载模型）
        loaded = self.embedder.loaded() if hasattr(self.embedder, "loaded") else None
        cached = self.embedder.model_cached() if hasattr(self.embedder, "model_cached") else None
        return {
            "backend": self.backend,
            "db_path": self.db_path,
            "longterm": longterm,
            "important": important,
            "identities": max_ident,
            "track_counts": tracks,
            "vector": {"enabled": bool(self._vec_cfg.get("enable", True)),
                       "loaded": loaded,
                       "model_cached": cached,
                       "available": loaded if loaded is not None else self.embedder.available(),
                       "reason": str(self.embedder.error() or "")[:120]},
        }
