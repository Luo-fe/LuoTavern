# -*- coding: utf-8 -*-
"""世界书剧情日志：触发使用过的世界书条目按身份记录，供"结束这一天"提炼成 world 轨道记忆。"""
import json
import os
import threading
import time

# 读-改-写 JSON 的并发保护（worldbook 插件的 provide_user_prefix 与睡前仪式提炼
# 可能在多线程下同时访问同一身份日志）
_LOCK = threading.Lock()


def _root():
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _dir():
    """世界书触发日志目录；测试可用 LUOTAVERN_WORLDLOG_DIR 环境变量隔离。"""
    override = os.environ.get("LUOTAVERN_WORLDLOG_DIR", "")
    if override:
        return override
    return os.path.join(_root(), "data", "worldlog")


def path_for(identity, base=None):
    base = base or _dir()
    return os.path.join(base, "%s.json" % identity)


def append_events(identity, events, max_entries=500, base=None):
    """记录一批被触发使用的世界书条目。events=[{keys, content}]。"""
    if not identity or not events:
        return
    path = path_for(identity, base)
    with _LOCK:
        rows = read_events(identity, base)
        # 时间戳必须严格递增：time.time() 在 Windows 上可能两次返回同一个值，
        # 而 trim_events 用 ts > cutoff 区分"提炼期间新增"——同一时刻写入的新事件
        # 会被误删（回归用例 test_session_command_guard.WorldlogTrimTest）。
        last_ts = max((float(e.get("ts") or 0) for e in rows), default=0.0)
        now = time.time()
        for e in events:
            now = max(now, last_ts + 0.000001)
            rows.append({
                "ts": now,
                "keys": list(e.get("keys") or [])[:8],
                "content": str(e.get("content") or "")[:600],
            })
            last_ts = now
        rows = rows[-max_entries:]
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)


def read_events(identity, base=None):
    path = path_for(identity, base)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def trim_events(identity, keep_ts, base=None):
    """只删除 ts <= keep_ts 的已提炼事件，保留期间新写入的。

    提炼要调 LLM（秒级），期间世界书仍在 append_events；旧实现用 clear_events
    整个删文件，会把这段时间新触发的事件一起丢掉。
    """
    path = path_for(identity, base)
    with _LOCK:
        rows = read_events(identity, base)
        rest = [e for e in rows if float(e.get("ts") or 0) > float(keep_ts or 0)]
        try:
            if not rest:
                if os.path.isfile(path):
                    os.remove(path)
                return 0
            d = os.path.dirname(path)
            os.makedirs(d, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rest, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except OSError:
            pass
        return len(rest)
