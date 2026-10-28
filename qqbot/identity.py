# -*- coding: utf-8 -*-
"""身份管理：QQ 号 → 身份名的映射（同名共享模型）。

- 主人（config.owner_user_ids）恒为固定身份（默认 哥哥），不可切换
- 客人必须用【我是xxx】声明/切换身份；多个 QQ 号可声明同一身份名 = 同一身份世界
"""
import json
import os
import re
import threading

_NAME_RE = re.compile(r"^[一-鿿A-Za-z0-9_·]{1,12}$")


class IdentityManager:
    def __init__(self, bot, map_path=None):
        self.bot = bot
        cfg = bot.config.get("identity", {})
        base = os.path.dirname(os.path.abspath(__file__))
        rel = map_path or cfg.get("map_path", "data/identity_map.json")
        self.path = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(base, "..", rel))
        self.owner_name = cfg.get("owner_name", "主人")
        self.reserved = {s.lower() for s in cfg.get("reserved_names", ["主人"])}
        self.lock = threading.Lock()
        self.owner_users = {int(u) for u in bot.config.get("owner_user_ids", [])}
        self.map = self._load()
        # 保证主人条目存在
        dirty = False
        for u in self.owner_users:
            key = str(int(u))
            if self.map.get(key) != self.owner_name:
                self.map[key] = self.owner_name
                dirty = True
        if dirty:
            self._save()

    # ---------- 读写 ----------
    def _load(self):
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except Exception:
                pass
        return {}

    def _save(self):
        with self.lock:
            d = os.path.dirname(self.path)
            os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.map, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    # ---------- 查询 ----------
    def get_identity(self, user_id):
        """返回身份名；主人恒为固定身份；未声明客人返回 None。"""
        if int(user_id) in self.owner_users:
            return self.owner_name
        return self.map.get(str(int(user_id)))

    def is_owner(self, user_id):
        return int(user_id) in self.owner_users

    def identity_users(self, identity):
        return [int(k) for k, v in self.map.items() if v == identity]

    def all_identities(self):
        """所有已声明的身份名（去重，含主人）。"""
        return sorted(set(self.map.values()))

    # ---------- 声明/切换 ----------
    def resolve_name(self, name):
        name = (name or "").strip()
        if not name:
            return False, "【说个名字呀，比如【我是小美】～】"
        if name.lower() in self.reserved:
            return False, "【这个名字%s不能用啦，换一个吧～】" % self.owner_name
        if not _NAME_RE.match(name):
            return False, "【名字太长或含特殊字符啦，1-12个字，中文/字母/数字都可以～】"
        return True, name

    def declare(self, user_id, name):
        """返回 (ok, ack)。ok=False 时 ack 为拒绝理由。"""
        if int(user_id) in self.owner_users:
            return False, "【%s永远是%s啦，不用重新介绍自己～】" % (self.owner_name, self.owner_name)
        ok, ack = self.resolve_name(name)
        if not ok:
            return False, ack
        with self.lock:
            switched = self.map.get(str(int(user_id))) != ack
            self.map[str(int(user_id))] = ack
        self._save()
        if switched:
            return True, "【你好呀，%s～%s记住你啦】" % (ack, self.owner_name)
        return True, "【已经记住啦，%s～欢迎回来】" % ack
