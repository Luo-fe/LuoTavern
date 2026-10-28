# -*- coding: utf-8 -*-
"""人格切换：角色卡列表 / 查找 / 当前状态 / 按人格隔离数据目录。

- 【角色卡查看】：列出 character 目录下所有角色卡
- 【切换为xxx】：写入 data/persona_current.json 并重启进程
- 每个角色卡拥有独立 data/personas/{名字}/ 下的聊天记录、记忆库、摘要、
  时钟、身份映射、世界书剧情日志、Obsidian vault 等
"""
import hashlib
import json
import os
import re
import shutil
import threading

from qqbot import sillytavern

STATE_FILE = "data/persona_current.json"
_ILLEGAL = re.compile(r"[\\/:*?\"<>|\r\n]+")
_DATA_LOCK = threading.RLock()
_DATA_OWNER = ".persona-name.json"


def _root():
    """项目根目录：优先取管理台注入的 LUOTAVERN_ROOT（EXE 便携模式）。"""
    return os.path.normpath(
        os.environ.get("LUOTAVERN_ROOT")
        or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def state_path():
    return os.path.join(_root(), STATE_FILE)


# Windows 保留设备名：作为文件名会 open 失败（CON/AUX/COM1/LPT1 等）
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} \
    | {"LPT%d" % i for i in range(1, 10)}


def safe_name(name):
    """目录安全名：去非法字符、限长、避开 Windows 保留名。"""
    n = _ILLEGAL.sub("_", str(name or "")).strip(" ._") or "默认"
    n = n[:40]
    if n.upper() in _WIN_RESERVED:
        n = "_" + n
    return n


def character_dir(cfg):
    rel = cfg.get("character_dir", "character")
    base = _root()
    return rel if os.path.isabs(rel) else os.path.normpath(os.path.join(base, rel))


# 角色卡列表缓存：管理台每几秒轮询状态都会 list_cards，PNG 卡要解压/解 base64。
# 用 (文件名, mtime, size) 做签名，管理台改卡后自动失效。
_CARDS_CACHE = {}


def list_cards(cfg=None):
    """列出 character 目录下的角色卡：[{file, name}]（按目录签名缓存）。"""
    d = character_dir(cfg or {})
    try:
        names = sorted(fn for fn in os.listdir(d)
                       if fn.lower().endswith((".json", ".png")))
        sig = tuple(
            (fn, os.path.getmtime(os.path.join(d, fn)),
             os.path.getsize(os.path.join(d, fn)))
            for fn in names
        )
    except OSError:
        return []
    cached = _CARDS_CACHE.get(d)
    if cached and cached[0] == sig:
        return [dict(c) for c in cached[1]]
    out = []
    for fn in names:
        card = sillytavern.load_character_card(os.path.join(d, fn))
        if card and card.get("name"):
            out.append({"file": fn, "name": card["name"]})
    _CARDS_CACHE[d] = (sig, out)
    return [dict(c) for c in out]


def find_card(name, cfg=None):
    """按角色名查找角色卡（忽略大小写）；返回 (路径, 名字) 或 None。"""
    name = (name or "").strip()
    if not name:
        return None
    for c in list_cards(cfg or {}):
        if c["name"].lower() == name.lower():
            return os.path.join(character_dir(cfg or {}), c["file"]), c["name"]
    return None


def current():
    """当前人格：{"name": ..., "card": ...} 或 None。"""
    path = state_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("card"):
            return {"name": str(data.get("name") or "默认"), "card": str(data["card"])}
    except Exception:
        pass
    return None


def set_current(name, card_path):
    data = {"name": str(name or "默认"), "card": str(card_path)}
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def data_dir(name, root=None):
    """角色目录：新目录用原名指纹消除清洗/截断/大小写碰撞，旧目录保留。

    safe_name 仍用于展示和导出；它本身不是唯一标识。
    """
    name = str(name or "默认")
    parent = os.path.join(root or _root(), "data", "personas")
    safe = safe_name(name)
    legacy = os.path.join(parent, safe)
    if os.path.isdir(legacy):
        try:
            with open(os.path.join(legacy, _DATA_OWNER), encoding="utf-8") as f:
                if json.load(f).get("name") == name:
                    return legacy
        except FileNotFoundError:
            # 精确检查磁盘大小写：Windows 下 isdir('alice') 会命中 Alice。
            exact = safe == name and name in os.listdir(parent)
            cur = current()
            if exact or (safe != name and cur and cur.get("name") == name):
                return legacy
    # 中文原名可继续沿用旧路径；有大小写或被清洗的名字加完整原名的指纹。
    if safe != name or any(ch.lower() != ch.upper() for ch in name):
        fingerprint = hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]
        return os.path.join(parent, "%s-%s" % (safe, fingerprint))
    return legacy


def ensure_persona_data(name):
    """首次切换前，把现有 data/ 复制到 data/personas/{名字}/（只复制一次，不删原数据）。"""
    src = os.path.join(_root(), "data")
    with _DATA_LOCK:
        target = data_dir(name)
        if not os.path.isdir(target):
            if os.path.isdir(src):
                shutil.copytree(
                    src, target,
                    ignore=shutil.ignore_patterns("personas", "persona_current.json", _DATA_OWNER))
            else:
                os.makedirs(target, exist_ok=True)
        owner = os.path.join(target, _DATA_OWNER)
        # 原子独占创建：不覆盖已有目录的归属声明。
        try:
            with open(owner, "x", encoding="utf-8") as f:
                json.dump({"name": str(name or "默认")}, f, ensure_ascii=False)
        except FileExistsError:
            with open(owner, encoding="utf-8") as f:
                if json.load(f).get("name") != str(name or "默认"):
                    raise ValueError("角色数据目录归属不一致：%s" % target)
        return target


def data_overrides(cfg, persona_name):
    """当前人格的数据路径覆盖：data/personas/{名字}/..."""
    base = data_dir(persona_name)
    return {
        "chatlog": {"dir": os.path.join(base, "chatlog")},
        "memory": {"db_path": os.path.join(base, "memory.db")},
        "summary_dir": os.path.join(base, "summary"),
        "archive_dir": os.path.join(base, "archive"),
        "rules": {"dir": os.path.join(base, "rules")},
        "correct": {"dir": os.path.join(base, "corrected")},
        "seal": {"dir": os.path.join(base, "seal")},
        "shared": {"dir": os.path.join(base, "shared")},
        "time": {"path": os.path.join(base, "time", "clock.json")},
        "identity": {"map_path": os.path.join(base, "identity_map.json")},
        "vault": {"dir": os.path.join(base, "obsidian_vault")},
        "worldlog_dir": os.path.join(base, "worldlog"),
        "proactive": {"score_file": os.path.join(base, "proactive_score.json")},
    }
