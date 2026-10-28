# -*- coding: utf-8 -*-
"""记忆库可视化工具（schema v2：按身份）。

网页版（本地管理台，简约暗色风格）:
    python memory_ui.py        -> http://127.0.0.1:8008
命令行:
    python memory_ui.py list [--identity 身份] [--q 关键词]
    python memory_ui.py add <内容> [--identity 身份]
    python memory_ui.py del <记忆ID>
    python memory_ui.py edit <记忆ID> <新内容> [--keywords 关键词]
    python memory_ui.py important [--identity 身份]
    python memory_ui.py edit-important <重要记忆ID> <新内容> [--keywords 关键词]
    python memory_ui.py del-important <重要记忆ID>
    python memory_ui.py affection / affection-set <身份> <分数>
    python memory_ui.py corrected / usage / timeline / vault / keywordize
    python memory_ui.py worldbook / worldbook-edit <文件> <ID> <内容> --keys 触发词
"""
import argparse
import datetime
import json
import os
import re
import sqlite3

from qqbot import textutil
from qqbot import persona
from qqbot import sillytavern

PORT = 8008


def set_root(root=None):
    """重算项目根目录与数据路径（EXE 桌面版切换项目后调用）。"""
    global BASE, DATA_ROOT, DB, IDENTITY_MAP, USAGE_LOG, TIME_HISTORY
    BASE = os.path.normpath(
        root
        or os.environ.get("LUOTAVERN_ROOT")
        or os.path.dirname(os.path.abspath(__file__)))
    DATA_ROOT = os.path.join(BASE, "data")
    cur_persona = persona.current()
    if cur_persona:
        DATA_ROOT = persona.data_dir(cur_persona.get("name"), root=BASE)
    DB = os.path.join(DATA_ROOT, "memory.db")
    if not cur_persona:
        # 与 bot 的 db_path 解析一致：未切换人格时读 config.json 的 memory.db_path
        # （自定义/绝对路径时管理台不再编辑错库）
        try:
            with open(os.path.join(BASE, "config.json"), "r", encoding="utf-8") as f:
                _cfg = json.load(f)
            db_rel = (_cfg.get("memory") or {}).get("db_path", "data/memory.db")
            DB = (db_rel if os.path.isabs(db_rel)
                  else os.path.normpath(os.path.join(BASE, db_rel)))
        except Exception:
            pass
    IDENTITY_MAP = os.path.join(DATA_ROOT, "identity_map.json")
    USAGE_LOG = os.path.join(BASE, "data", "usage.json")
    TIME_HISTORY = os.path.join(DATA_ROOT, "time", "history.json")
    return BASE


set_root()

# schema / 迁移统一来自 qqbot.memstore.local（此前本文件与 plugins/memory.py、
# vault_export.py 各维护一份，已合并为单源）
from qqbot.memstore.local import migrate_conn
from qqbot.memstore.base import KeywordMixin

# 每个进程每个库只迁移一次（其余 connect 只开连接；避免每请求全量 DDL 的性能热点）
_MIGRATED = set()


def connect():
    """打开本地记忆库连接（每请求一个新连接，迁移仅首建时执行一次）。"""
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    if DB not in _MIGRATED:
        migrate_conn(conn)
        _MIGRATED.add(DB)
    return conn


def resolve_identity(user_id):
    """QQ号 → 身份名（旧 CLI 兼容）。"""
    try:
        with open(IDENTITY_MAP, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get(str(int(user_id)))
    except Exception:
        return None


def fmt_ts(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _load_keywords(raw):
    """关键词解析统一走存储层（JSON 数组 / 逗号文本、去空、上限 10）。"""
    return KeywordMixin._load_keywords(raw)


def _dump_keywords(keywords):
    """关键词序列化统一走存储层（去重/限长/最多 10 个，兼容逗号字符串）。"""
    return KeywordMixin._dump_keywords(keywords)


def list_memories(identity=None, keyword=None):
    conn = connect()
    sql = "SELECT id, identity, content, keywords, created_at FROM memories"
    args, conds = [], []
    if identity:
        conds.append("identity = ?")
        args.append(identity)
    if keyword:
        # 转义 LIKE 通配符（%/_），否则搜索 "%" 会命中全部
        kw = str(keyword).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conds.append("content LIKE ? ESCAPE '\\'")
        args.append("%" + kw + "%")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [{"id": r[0], "identity": r[1], "content": r[2],
             "keywords": _load_keywords(r[3]),
             "created_at": fmt_ts(r[4])} for r in rows]


def add_memory(identity, content):
    content = (content or "").strip()
    if not content:
        raise ValueError("记忆内容不能为空")
    conn = connect()
    cur = conn.execute(
        "INSERT INTO memories (identity, content, created_at) VALUES (?, ?, ?)",
        (identity or "主人", content, datetime.datetime.now().timestamp()),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def add_important(identity, content, affect_delta=0, importance=5, keywords=None):
    """添加一条重要记忆（管理台「重要记忆」页）。返回新 id。"""
    content = (content or "").strip()
    if not content:
        raise ValueError("记忆内容不能为空")
    try:
        delta = max(-100, min(100, int(affect_delta)))
        imp = max(0, min(10, int(importance)))
    except (TypeError, ValueError):
        raise ValueError("好感度变动/重要度必须是整数")
    conn = connect()
    cur = conn.execute(
        "INSERT INTO important_memories "
        "(identity, content, affect_delta, importance, story_time, keywords, track, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'story', ?)",
        (identity or "主人", content, delta, imp,
         textutil.parse_story_time(content),
         _dump_keywords(keywords), datetime.datetime.now().timestamp()),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def update_memory(mid, content, keywords=None):
    if not isinstance(content, str) or not content.strip():
        # 空内容会把这条记忆写成空串（且无版本历史）——直接拒绝
        raise ValueError("记忆内容不能为空")
    conn = connect()
    try:
        if keywords is None:
            cur = conn.execute("UPDATE memories SET content=? WHERE id=?", (content.strip(), int(mid)))
        else:
            cur = conn.execute(
                "UPDATE memories SET content=?, keywords=? WHERE id=?",
                (content.strip(), _dump_keywords(keywords), int(mid)),
            )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_memory(mid):
    conn = connect()
    cur = conn.execute("DELETE FROM memories WHERE id=?", (int(mid),))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def list_important(identity=None):
    conn = connect()
    sql = ("SELECT id, identity, content, affect_delta, importance, story_time, keywords, track, created_at "
           "FROM important_memories")
    args = []
    if identity:
        sql += " WHERE identity = ?"
        args.append(identity)
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [{"id": r[0], "identity": r[1], "content": r[2], "affect_delta": r[3],
             "importance": r[4],
             "story_time": r[5] or textutil.parse_story_time(r[2]),
             "keywords": _load_keywords(r[6]),
             "track": r[7] or "story",
             "created_at": fmt_ts(r[8])} for r in rows]


def update_important(mid, content, keywords=None):
    if not isinstance(content, str) or not content.strip():
        raise ValueError("重要记忆内容不能为空")
    conn = connect()
    # 编辑正文后同步 story_time：按时间窗召回（list_important_by_time 等）依赖该列
    story_time = textutil.parse_story_time(content) if content else None
    if keywords is None:
        cur = conn.execute(
            "UPDATE important_memories SET content=?, story_time=? WHERE id=?",
            (content.strip(), story_time, int(mid)),
        )
    else:
        cur = conn.execute(
            "UPDATE important_memories SET content=?, keywords=?, story_time=? WHERE id=?",
            (content.strip(), _dump_keywords(keywords), story_time, int(mid)),
        )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def delete_important(mid):
    conn = connect()
    cur = conn.execute("DELETE FROM important_memories WHERE id=?", (int(mid),))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def list_affection():
    conn = connect()
    rows = conn.execute("SELECT identity, score, updated_at FROM affection ORDER BY score DESC").fetchall()
    conn.close()
    return [{"identity": r[0], "score": r[1], "updated_at": fmt_ts(r[2])} for r in rows]


def set_affection(identity, score):
    try:
        score = int(score)
    except (TypeError, ValueError):
        raise ValueError("好感度必须是整数")
    # 设计上无硬上限，但限制在 ±10 万防止异常值污染注入文案
    score = max(-100000, min(100000, score))
    now = datetime.datetime.now().timestamp()
    conn = connect()
    conn.execute(
        "INSERT INTO affection (identity, score, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(identity) DO UPDATE SET score=?, updated_at=?",
        (identity, score, now, score, now),
    )
    conn.commit()
    conn.close()
    return True


def list_corrected(identity=None):
    out = []
    base = os.path.join(DATA_ROOT, "corrected")
    if not os.path.isdir(base):
        return out
    for fn in sorted(os.listdir(base)):
        if not fn.endswith(".json"):
            continue
        ident = fn[:-5]
        if identity and ident != identity:
            continue
        try:
            with open(os.path.join(base, fn), "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                out.append({"identity": ident, "entry": int(k), "correction": v})
        except Exception:
            continue
    return out


def list_timeline(limit=100):
    try:
        with open(TIME_HISTORY, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        rows = list(data[-limit:])
        for r in rows:
            ts = r.get("ts")
            if isinstance(ts, (int, float)):
                r["ts"] = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            else:
                r["ts"] = str(ts or "")
        return list(reversed(rows))
    except Exception:
        return []


def _worldbook_dir():
    try:
        with open(os.path.join(BASE, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        rel = (cfg.get("worldbook") or {}).get("dir", "book")
    except Exception:
        rel = "book"
    return os.path.join(BASE, rel) if not os.path.isabs(rel) else rel


def list_worldbook():
    d = _worldbook_dir()
    out = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith((".json", ".png")):
            continue
        try:
            path = os.path.join(d, fn)
            if fn.lower().endswith(".png"):
                with open(path, "rb") as f:
                    data = sillytavern.load_worldbook_from_png_bytes(f.read())
            else:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            if data is None:
                continue
            raw = data.get("entries", []) if isinstance(data, dict) else []
            if isinstance(raw, dict):
                items = [dict(e, _id=k) for k, e in raw.items() if isinstance(e, dict)]
            else:
                items = [dict(e, _id=e.get("id")) for e in raw if isinstance(e, dict)]
            entries = []
            for idx, e in enumerate(items):
                keys = e.get("keys") or e.get("key") or []
                if isinstance(keys, str):
                    keys = [keys]
                entries.append({
                    # 无 id 的条目（很多 ST 世界书是"数组 + 无 id"）用下标兜底，
                    # 否则管理台点"编辑"必然 404
                    "id": e.get("_id", e.get("id")),
                    "idx": idx,
                    "name": e.get("name") or "",
                    "keys": [str(k) for k in keys],
                    "content": str(e.get("content") or ""),
                })
            out.append({"file": fn, "name": data.get("name", fn), "entries": entries})
        except Exception:
            continue
    return out


def update_worldbook_entry(file_name, entry_id, keys_text, content):
    # 防路径穿越：file_name 必须解析后仍落在 worldbook 目录内
    base_dir = os.path.abspath(_worldbook_dir())
    path = os.path.abspath(os.path.join(base_dir, str(file_name or "")))
    if not (path == base_dir or path.startswith(base_dir + os.sep)):
        return False
    if not os.path.isfile(path):
        return False
    is_png = path.lower().endswith(".png")
    if is_png:
        with open(path, "rb") as f:
            data = sillytavern.load_worldbook_from_png_bytes(f.read())
        if data is None:
            return False
    else:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    raw = data.get("entries", {})
    keys = [k.strip() for k in
            re.split(r"[,，、;；]", str(keys_text or "")) if k.strip()]
    target = None
    if isinstance(raw, dict):
        for k, e in raw.items():
            if str(k) == str(entry_id) or str(e.get("id")) == str(entry_id):
                target = e
                break
    elif isinstance(raw, list):
        for e in raw:
            if str(e.get("id")) == str(entry_id):
                target = e
                break
    if target is None and isinstance(raw, list):
        for idx, e in enumerate(raw):
            if isinstance(e, dict) and str(idx) == str(entry_id):
                target = e
                break
    if target is None:
        return False
    target["keys"] = list(keys)
    if "key" in target:
        target["key"] = list(keys)
    target["content"] = str(content or "")
    if is_png:
        with open(path, "rb") as f:
            base = f.read()
        with open(path, "wb") as f:
            f.write(sillytavern.build_png_with_text(
                base, "worldbook",
                json.dumps(data, ensure_ascii=False), cover_bytes=base))
    else:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    return True


KEYWORDIZE_PROMPT = """你是记忆关键词助手。为下面的每条记忆生成 3-5 个中文触发词，
以后只要听到这些词就该想起这条记忆。写具体名词/动作（物品、地点、事件名、称呼），
包含同义词（如"视频/录像/拍摄"）；不要写情绪词或空泛词。只输出 JSON 数组，不要其他内容：
[{"id": 1, "keywords": ["视频", "录像", "拍摄"]}]

记忆列表：
{items}"""


def keywordize(identity=None, batch=20, force=False, dry_run=False, llm=None):
    """为缺少触发词的记忆批量生成关键词（LLM 回填）。返回 (重要条数, 长期条数, 失败条数)。"""
    if dry_run:
        conn = connect()
        try:
            if identity:
                imp = conn.execute(
                    "SELECT COUNT(*) FROM important_memories WHERE identity=? AND (keywords IS NULL OR keywords='')",
                    (identity,)).fetchone()[0]
                mem = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE identity=? AND (keywords IS NULL OR keywords='')",
                    (identity,)).fetchone()[0]
            else:
                imp = conn.execute(
                    "SELECT COUNT(*) FROM important_memories WHERE keywords IS NULL OR keywords=''").fetchone()[0]
                mem = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE keywords IS NULL OR keywords=''").fetchone()[0]
            return imp, mem, 0
        finally:
            conn.close()

    if llm is None:
        from qqbot.config import load_config
        from qqbot.llm import build_llm
        llm = build_llm(load_config(os.path.join(BASE, "config.json")))

    def _backfill(table):
        if identity:
            rows = conn.execute(
                "SELECT id, identity, content, keywords FROM %s WHERE identity=? ORDER BY id" % table,
                (identity,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, identity, content, keywords FROM %s ORDER BY id" % table).fetchall()
        targets = [(r[0], r[1], r[2]) for r in rows
                   if force or not (r[3] or "").strip()]
        done = fail = 0
        for start in range(0, len(targets), batch):
            chunk = targets[start:start + batch]
            items = "\n".join("[%d] %s：%s" % (mid, ident, content[:200])
                              for mid, ident, content in chunk)
            data = llm.chat_json(KEYWORDIZE_PROMPT.replace("{items}", items),
                                 temperature=0.2, call_type="keywordize")
            if not isinstance(data, list):
                fail += len(chunk)
                continue
            mapping = {}
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    mid = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                kw = item.get("keywords")
                if isinstance(kw, str):
                    kw = [k.strip() for k in kw.replace("，", ",").split(",") if k.strip()]
                if isinstance(kw, list):
                    kw = [str(k).strip() for k in kw if str(k).strip()]
                if kw:
                    mapping[mid] = _dump_keywords(kw)
            for mid, _ident, _content in chunk:
                if mid in mapping:
                    conn.execute("UPDATE %s SET keywords=? WHERE id=?" % table,
                                 (mapping[mid], mid))
                    done += 1
                else:
                    fail += 1
        conn.commit()
        return done, fail

    conn = connect()
    try:
        imp_done, imp_fail = _backfill("important_memories")
        mem_done, mem_fail = _backfill("memories")
        return imp_done, mem_done, imp_fail + mem_fail
    finally:
        conn.close()


def identities():
    try:
        with open(IDENTITY_MAP, "r", encoding="utf-8") as f:
            data = json.load(f)
        return sorted(set(data.values()))
    except Exception:
        return []


def memory_stats():
    """记忆库统计（管理台「记忆」页与 memory_ui.py backend 命令）。

    只读 SQLite 计数 + 配置意图；不触发本地嵌入模型加载（首访页不卡顿）。
    返回 {backend, backend_config, db_path, longterm, important, identities,
          track_counts, tencentdb:{enabled, endpoint}}。
    """
    out = {"backend": "local", "backend_config": "auto", "db_path": DB,
           "longterm": 0, "important": 0, "identities": 0,
           "track_counts": {"story": 0, "world": 0},
           "tencentdb": {"enabled": False, "endpoint": ""}}
    try:
        with open(os.path.join(BASE, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    m = cfg.get("memory") or {}
    out["backend_config"] = str(m.get("backend", "auto"))
    t = m.get("tencentdb") or {}
    out["tencentdb"] = {"enabled": bool(t.get("enabled")),
                        "endpoint": str(t.get("endpoint") or "")}
    if out["backend_config"] in ("hybrid", "tencentdb") and out["tencentdb"]["enabled"]:
        out["backend"] = out["backend_config"]
    elif out["backend_config"] == "auto" and out["tencentdb"]["enabled"]:
        out["backend"] = "auto"  # 运行时按网关连通性在 hybrid/local 间自动选择
    try:
        conn = connect()
        try:
            out["longterm"] = conn.execute(
                "SELECT COUNT(*) FROM memories").fetchone()[0]
            out["important"] = conn.execute(
                "SELECT COUNT(*) FROM important_memories").fetchone()[0]
            out["identities"] = conn.execute(
                "SELECT COUNT(DISTINCT identity) FROM "
                "(SELECT identity FROM memories UNION SELECT identity FROM important_memories)"
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT track, COUNT(*) FROM important_memories GROUP BY track").fetchall()
            for track, n in rows:
                out["track_counts"][track or "story"] = n
        finally:
            conn.close()
    except Exception:
        pass
    return out


def usage_stats():
    """读 data/usage.json：调用次数 / 输入输出 token / 缓存命中 / 按天 / 最近调用。"""
    try:
        with open(USAGE_LOG, "r", encoding="utf-8") as f:
            data = json.load(f)
        t = data.get("total", {})
        cost = (
            int(t.get("prompt_tokens", 0)) * 2.0 / 1e6
            - int(t.get("cache_hit_tokens", 0)) * 1.5 / 1e6
            + int(t.get("completion_tokens", 0)) * 8.0 / 1e6
        )
        per = []
        for model, s in data.items():
            if model in ("total", "by_day", "recent"):
                continue
            per.append({
                "model": model,
                "calls": s.get("calls", 0),
                "prompt_tokens": s.get("prompt_tokens", 0),
                "completion_tokens": s.get("completion_tokens", 0),
                "cache_hit_tokens": s.get("cache_hit_tokens", 0),
                "cache_rate": round(100.0 * s.get("cache_hit_tokens", 0) / max(1, s.get("prompt_tokens", 0)), 1),
            })
        by_type = []
        for ctype, s in t.get("by_type", {}).items():
            by_type.append({
                "type": ctype,
                "calls": s.get("calls", 0),
                "prompt_tokens": s.get("prompt_tokens", 0),
                "completion_tokens": s.get("completion_tokens", 0),
                "cache_hit_tokens": s.get("cache_hit_tokens", 0),
                "cache_rate": round(100.0 * s.get("cache_hit_tokens", 0) / max(1, s.get("prompt_tokens", 0)), 1),
            })
        by_type.sort(key=lambda r: r["calls"], reverse=True)
        by_day = []
        for day, s in (data.get("by_day") or {}).items():
            by_day.append({"day": day, **s})
        by_day.sort(key=lambda r: r["day"], reverse=True)
        return {"total": {
            "calls": t.get("calls", 0),
            "prompt_tokens": t.get("prompt_tokens", 0),
            "completion_tokens": t.get("completion_tokens", 0),
            "cache_hit_tokens": t.get("cache_hit_tokens", 0),
            "cache_rate": round(100.0 * t.get("cache_hit_tokens", 0) / max(1, t.get("prompt_tokens", 0)), 1),
            "est_cost_yuan": round(cost, 4),
        }, "per_model": per, "by_type": by_type,
            "by_day": by_day, "recent": list(reversed(data.get("recent") or []))}
    except Exception:
        return {"total": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                          "cache_hit_tokens": 0, "cache_rate": 0, "est_cost_yuan": 0},
                "per_model": [], "by_type": [], "by_day": [], "recent": []}


def run_web(port=PORT):
    """启动本地管理台（委托 qqbot.admin，独立模式）。"""
    from qqbot.admin import start_admin
    start_admin(bot=None, port=port, block=True)


def main():
    parser = argparse.ArgumentParser(description="记忆库管理")
    sub = parser.add_subparsers(dest="cmd")
    p_list = sub.add_parser("list", help="列出记忆")
    p_list.add_argument("--identity", default=None)
    p_list.add_argument("--user", type=int, default=None)
    p_list.add_argument("--q", default=None)
    p_add = sub.add_parser("add", help="添加记忆")
    p_add.add_argument("content")
    p_add.add_argument("--identity", default=None)
    p_add.add_argument("--user", type=int, default=None)
    p_edit = sub.add_parser("edit", help="修改记忆")
    p_edit.add_argument("id", type=int)
    p_edit.add_argument("content")
    p_edit.add_argument("--keywords", default=None)
    p_del = sub.add_parser("del", help="删除记忆")
    p_del.add_argument("id", type=int)
    p_imp = sub.add_parser("important", help="列出重要记忆")
    p_imp.add_argument("--identity", default=None)
    p_edit_imp = sub.add_parser("edit-important", help="修改重要记忆")
    p_edit_imp.add_argument("id", type=int)
    p_edit_imp.add_argument("content")
    p_edit_imp.add_argument("--keywords", default=None)
    p_del_imp = sub.add_parser("del-important", help="删除重要记忆")
    p_del_imp.add_argument("id", type=int)
    p_aff = sub.add_parser("affection", help="列出好感度")
    p_aff_set = sub.add_parser("affection-set", help="设定好感度")
    p_aff_set.add_argument("identity")
    p_aff_set.add_argument("score", type=int)
    p_cor = sub.add_parser("corrected", help="列出纠错记录")
    p_cor.add_argument("--identity", default=None)
    sub.add_parser("usage", help="Token 用量统计")
    sub.add_parser("timeline", help="列出时间线变更")
    sub.add_parser("vault", help="导出 Obsidian 记忆库")
    sub.add_parser("backend", help="记忆后端与库统计")
    p_kw = sub.add_parser("keywordize", help="批量生成关键词（LLM 回填）")
    p_kw.add_argument("--identity", default=None)
    p_kw.add_argument("--batch", type=int, default=20)
    p_kw.add_argument("--force", action="store_true")
    p_kw.add_argument("--dry-run", action="store_true")
    p_wb = sub.add_parser("worldbook", help="列出世界书条目")
    p_wb.add_argument("--file", default=None)
    p_wbe = sub.add_parser("worldbook-edit", help="编辑世界书条目")
    p_wbe.add_argument("file")
    p_wbe.add_argument("id")
    p_wbe.add_argument("content")
    p_wbe.add_argument("--keys", default="")
    args = parser.parse_args()

    if args.cmd == "list":
        ident = args.identity or (resolve_identity(args.user) if args.user else None)
        rows = list_memories(ident, args.q)
        if not rows:
            print("（空）")
            return
        for m in rows:
            print("[%d] %s | %s | %s | 关键词:%s" % (
                m["id"], m["created_at"], m["identity"], m["content"],
                "、".join(m["keywords"]) if m.get("keywords") else "-"))
    elif args.cmd == "add":
        ident = args.identity or (resolve_identity(args.user) if args.user else None) or "主人"
        print("已添加，ID =", add_memory(ident, args.content))
    elif args.cmd == "edit":
        print("已修改" if update_memory(args.id, args.content, keywords=args.keywords)
              else "未找到该记忆")
    elif args.cmd == "del":
        print("已删除" if delete_memory(args.id) else "未找到该记忆")
    elif args.cmd == "important":
        rows = list_important(args.identity)
        if not rows:
            print("（空）")
            return
        for m in rows:
            sign = "+" if m["affect_delta"] > 0 else ""
            print("[%d] %s | %s | %s%d | %s" % (m["id"], m["identity"], m["created_at"],
                                                sign, m["affect_delta"], m["content"]))
            if m.get("keywords"):
                print("      关键词: %s" % "、".join(m["keywords"]))
    elif args.cmd == "edit-important":
        print("已修改" if update_important(args.id, args.content, keywords=args.keywords)
              else "未找到该记忆")
    elif args.cmd == "del-important":
        print("已删除" if delete_important(args.id) else "未找到该记忆")
    elif args.cmd == "affection":
        rows = list_affection()
        if not rows:
            print("（空）")
            return
        for a in rows:
            print("%s: %d%s | %s" % (a["identity"], a["score"],
                                     "（已满）" if a["score"] > 400 else "/400", a["updated_at"]))
    elif args.cmd == "affection-set":
        set_affection(args.identity, args.score)
        print("已设定 %s 好感度 = %d" % (args.identity, args.score))
    elif args.cmd == "corrected":
        rows = list_corrected(args.identity)
        if not rows:
            print("（空）")
            return
        for c in rows:
            print("%s #%d: %s" % (c["identity"], c["entry"], c["correction"]))
    elif args.cmd == "backend":
        s = memory_stats()
        tc = s.get("tencentdb") or {}
        print("记忆库: %s" % s.get("db_path"))
        print("后端配置: %s | 实际生效: %s" % (s.get("backend_config"), s.get("backend")))
        print("长期记忆 %d 条 | 重要记忆 %d 条 | 身份 %d 个"
              % (s.get("longterm", 0), s.get("important", 0), s.get("identities", 0)))
        print("轨道: story %d / world %d"
              % (s.get("track_counts", {}).get("story", 0),
                 s.get("track_counts", {}).get("world", 0)))
        print("TencentDB 远端: %s（endpoint: %s）"
              % ("已启用" if tc.get("enabled") else "未启用", tc.get("endpoint") or "-"))
    elif args.cmd == "usage":
        u = usage_stats()
        t = u["total"]
        print("调用 %d 次 | 输入 %d | 输出 %d | 缓存命中 %d (%.1f%%) | 估算 ￥%.4f"
              % (t["calls"], t["prompt_tokens"], t["completion_tokens"],
                 t["cache_hit_tokens"], t["cache_rate"], t["est_cost_yuan"]))
        for m in u["per_model"]:
            print("  %s: %d 次 | 输入 %d, 输出 %d, 缓存 %.1f%%"
                  % (m["model"], m["calls"], m["prompt_tokens"],
                     m["completion_tokens"], m["cache_rate"]))
    elif args.cmd == "timeline":
        rows = list_timeline()
        if not rows:
            print("（空）")
            return
        for e in rows:
            print("%s | %s | %s | %s | %s → %s" % (
                e.get("ts") or "-", e.get("identity") or "-",
                e.get("source") or "-", e.get("action") or "-",
                e.get("before") or "-", e.get("after") or "-"))
    elif args.cmd == "keywordize":
        imp, mem, fail = keywordize(identity=args.identity, batch=args.batch,
                                    force=args.force, dry_run=args.dry_run)
        print("重要记忆 %d 条 / 长期记忆 %d 条 / 失败 %d 条" % (imp, mem, fail))
    elif args.cmd == "worldbook":
        rows = list_worldbook()
        if not rows:
            print("（空）")
            return
        for f in rows:
            if args.file and f["file"] != args.file:
                continue
            print("== %s ==" % f["file"])
            for e in f["entries"]:
                print("  [%s] %s | 触发词: %s" % (e["id"], e["content"],
                                                  "、".join(e["keys"])))
    elif args.cmd == "worldbook-edit":
        ok = update_worldbook_entry(args.file, args.id, args.keys, args.content)
        print("已保存" if ok else "未找到该世界书条目")
    elif args.cmd == "vault":
        # 修复 F1：vault 子命令此前无分支，实际落入了 run_web() 启动 Web 服务
        from qqbot.config import load_config
        from qqbot.vault_export import export_vault
        cfg = load_config(os.path.join(BASE, "config.json"))
        vault_dir = (cfg.get("vault") or {}).get("dir", "data/obsidian_vault")
        # 当前人格的数据目录覆盖：插件写的是 data/personas/{角色}/obsidian_vault，
        # 这里若只读 config 的默认目录，手动同步会生成另一个"空档案库"
        cur = persona.current()
        if cur and cur.get("name"):
            over = persona.data_overrides(cfg, cur["name"])
            vault_dir = (over.get("vault") or {}).get("dir", vault_dir)
        if not os.path.isabs(vault_dir):
            vault_dir = os.path.normpath(os.path.join(BASE, vault_dir))
        n, ids = export_vault(DB, vault_dir)
        print("已导出 %d 个文件（身份：%s）\n目标目录：%s"
              % (n, "、".join(ids) if ids else "无", vault_dir))
    else:
        run_web()


if __name__ == "__main__":
    main()
