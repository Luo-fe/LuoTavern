# -*- coding: utf-8 -*-
"""封存标记：data/seal/{身份}.json 记录睡前仪式最后提炼到的位置。

- 封存只挡“下次总结重复提炼”，回忆检索不受影响（仍全文可搜）
- 同时记录**条目序号**（sealed_through，兼容旧文件）与**稳定 id 集合**（sealed_ids）：
  序号依赖文件名排序，一旦聊天记录被重排/迁移（如 scripts/migrate_chatlog_statusbar.py）
  就会指向别的条目；id 是内容指纹，重排后依然指同一条，避免"漏提炼/重复提炼"。
"""
import json
import os
import threading

from .chatlog_store import entry_id

MAX_SEALED_IDS = 2000


class SealManager:
    def __init__(self, bot, seal_dir=None):
        self.bot = bot
        cfg = bot.config.get("seal", {})
        base = os.path.dirname(os.path.abspath(__file__))
        rel = seal_dir or cfg.get("dir", "data/seal")
        self.dir = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(base, "..", rel))
        self.lock = threading.Lock()
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, identity):
        return os.path.join(self.dir, "%s.json" % identity)

    def _load(self, identity):
        path = self._path(identity)
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def get(self, identity):
        """返回最后提炼到的条目序号；无记录返回 -1。"""
        try:
            return int(self._load(identity).get("sealed_through", -1))
        except (TypeError, ValueError):
            return -1

    def sealed_ids(self, identity):
        data = self._load(identity)
        ids = data.get("sealed_ids")
        return set(str(i) for i in ids) if isinstance(ids, list) else set()

    def _migrate_ids(self, identity, data):
        """旧文件只有序号：把当前排序下前 N+1 条的 id 补进去（只做一次）。"""
        if data.get("sealed_ids") or "sealed_through" not in data:
            return data
        try:
            through = int(data.get("sealed_through", -1))
        except (TypeError, ValueError):
            return data
        if through < 0:
            return data
        try:
            entries = self.bot.chatlog_store.read_entries(identity)
        except Exception:
            return data
        data["sealed_ids"] = [entry_id(e) for e in entries[: through + 1]]
        self._write(identity, data)
        return data

    def set(self, identity, entry_index, entry=None, entry_ids=None):
        """记录提炼到 entry_index（含）。

        entry：该条目（或 entry_ids：这一批条目的列表）→ 同时写入稳定 id。
        """
        data = self._load(identity)
        self._migrate_ids(identity, data)
        data["sealed_through"] = int(entry_index)
        ids = list(data.get("sealed_ids") or [])
        if isinstance(entry_ids, (list, tuple, set)):
            ids.extend(entry_id(e) for e in entry_ids)
        elif entry is not None:
            ids.append(entry_id(entry))
        # 去重保序 + 截断，避免文件无限增长
        seen, uniq = set(), []
        for i in ids:
            if i and i not in seen:
                seen.add(i)
                uniq.append(i)
        if data.get("sealed_through") is not None and entry is None and not entry_ids:
            try:
                keep = int(entry_index) + 1
                if keep >= 0:
                    entries = self.bot.chatlog_store.read_entries(identity)
                    for e in entries[:keep]:
                        i = entry_id(e)
                        if i not in seen:
                            seen.add(i)
                            uniq.append(i)
            except Exception:
                pass
        data["sealed_ids"] = uniq[-MAX_SEALED_IDS:]
        self._write(identity, data)

    def _write(self, identity, data):
        with self.lock:
            tmp = self._path(identity) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path(identity))

    def unsealed(self, identity):
        """返回该身份尚未提炼的条目 (index, stamp, speaker, content)。

        以**稳定 id** 为主判据（重排/迁移后仍准确）；旧文件首次读取时按序号迁移出 id。
        """
        entries = self.bot.chatlog_store.read_entries(identity)
        data = self._migrate_ids(identity, self._load(identity))
        ids = set(str(i) for i in (data.get("sealed_ids") or []))
        if ids:
            return [e for e in entries if entry_id(e) not in ids]
        through = -1
        try:
            through = int(data.get("sealed_through", -1))
        except (TypeError, ValueError):
            through = -1
        return [e for e in entries if e[0] > through]

    def pending_count(self, identity):
        """未封存条目数（自动提炼阈值判断用）。"""
        return len(self.unsealed(identity))
