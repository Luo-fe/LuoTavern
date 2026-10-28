# -*- coding: utf-8 -*-
"""LuoTavern 本地管理台（简约暗色风格，仿 Codex 界面）。

两种启动方式：
- 随机器人启动：python bot.py 自动监听 http://127.0.0.1:8008
- 独立启动：python memory_ui.py（记忆管理.bat / 管理台.bat）

功能：状态总览、聊天控制台、设置（模型/平台/人格/世界书/回复档位/称呼）、
密钥管理、记忆/好感/纠错/时间线/世界书编辑、Token 用量统计。
"""
import ipaddress
import json
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from qqbot import config as qconfig
from qqbot import persona
from qqbot import card_manager
from qqbot import ai_card_writer
from qqbot.mobile_ui import LOGIN_HTML as _LOGIN_HTML
from qqbot.mobile_ui import MOBILE_PAGE_HTML as _MOBILE_HTML
from qqbot.llm import build_llm, PROVIDER_LABELS

BASE = (os.environ.get("LUOTAVERN_ROOT")
        or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _base():
    """项目根目录：优先跟随 LUOTAVERN_ROOT（桌面版切换项目后实时更新）。"""
    return os.path.normpath(
        os.environ.get("LUOTAVERN_ROOT") or BASE)


def _config_path():
    return os.path.join(_base(), "config.json")


# 配置读-改-写串行化：管理台是每请求一线程，两个请求（或与 bot 进程）同时
# "读快照 → 改 → 整份覆盖" 会静默丢掉其中一次的修改
_CFG_LOCK = threading.RLock()
# 远程鉴权失败计数（IP → {first,n,until}），限流用
_AUTH_FAILS = {}
_AUTH_LOCK = threading.RLock()


def _read_cfg():
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_cfg(cfg):
    with _CFG_LOCK:
        qconfig.atomic_write_json(_config_path(), cfg)


def _update_cfg(mutate):
    """在锁内完成"读 → 改 → 写"，避免并发覆盖（mutate 返回 None 表示放弃写入）。"""
    with _CFG_LOCK:
        cfg = _read_cfg()
        result = mutate(cfg)
        if result is False:
            return cfg
        qconfig.atomic_write_json(_config_path(), cfg)
        return cfg


def _apply_vault_keys(cfg):
    """把 EXE 密钥库中的厂家 Key 合并进配置（桌面模式下密钥以密钥库为准）。"""
    try:
        import desktop_vault
        keys = desktop_vault.load_vault()
    except Exception:
        return cfg
    provider_keys = {
        "DEEPSEEK_API_KEY": "deepseek",
        "OPENAI_API_KEY": "openai",
        "ANTHROPIC_API_KEY": "anthropic",
        "GLM_API_KEY": "glm",
        "CUSTOM_API_KEY": "custom",
    }
    provs = cfg.setdefault("llm_providers", {})
    for envar, name in provider_keys.items():
        val = keys.get(envar)
        if val:
            provs.setdefault(name, {})["api_key"] = str(val)
    return cfg


def _ai_provider(cfg, aw=None, explicit=""):
    """AI 写卡默认厂家：显式设置优先，否则自动识别已填密钥的厂家。"""
    aw = aw or {}
    provs = cfg.get("llm_providers") or {}
    explicit = explicit or aw.get("provider") or ""
    if explicit in provs:
        return explicit
    for k in ("deepseek", "openai", "anthropic", "glm", "custom"):
        if k in provs and provs[k].get("api_key"):
            return k
    return "deepseek"


def _mask(secret):
    if not secret:
        return ""
    if len(secret) <= 8:
        return "****"
    return secret[:4] + "****" + secret[-4:]


# ---------- 安全：token / Origin / 请求体限制 / 导出路径 ----------
MAX_BODY_BYTES = 20 * 1024 * 1024
# 状态接口短 TTL（秒）：前端 5 秒轮询，这里缓存 3 秒既够实时又省掉重复重活
STATUS_TTL_SEC = 3.0

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# Tailscale（以及同类 CGNAT 组网）的网段：远程模式自动放行，无需手工配置
_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")


def _cfg_admin():
    """admin 配置节（含远程绑定相关项）。"""
    cfg = (_read_cfg().get("admin") or {})
    return cfg if isinstance(cfg, dict) else {}


def _bind_host():
    """管理台监听地址：默认仅本机；配置 admin.bind 才能远程访问。"""
    return str(_cfg_admin().get("bind") or "127.0.0.1").strip() or "127.0.0.1"


def _is_loopback_addr(addr):
    try:
        return ipaddress.ip_address(str(addr).split("%")[0]).is_loopback
    except ValueError:
        return False


def _tailnet_ip():
    """本机的 Tailscale/CGNAT 地址（有的话），用于告诉用户手机该访问哪个 URL。"""
    try:
        for name in socket.gethostbyname_ex(socket.gethostname())[2]:
            try:
                if ipaddress.ip_address(name) in _TAILNET_V4:
                    return name
            except ValueError:
                continue
    except OSError:
        pass
    return ""


def _best_local_ip():
    """本机对外可用地址：优先 Tailscale，其次默认出口网卡的 IP（不实际发包）。"""
    tail = _tailnet_ip()
    if tail:
        return tail
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))          # UDP connect 不发送数据
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return ""


def _remote_enabled():
    """非回环绑定 = 允许远程访问。"""
    host = _bind_host()
    if host in ("", "127.0.0.1", "localhost", "::1"):
        return False
    return True


def _allowed_host_names():
    """远程访问允许的 Host 名（小写，去端口）。"""
    raw = _cfg_admin().get("allowed_hosts") or []
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    out = set()
    for item in raw:
        name = str(item).strip().lower()
        if name:
            out.add(name)
    return out


def _host_name_ok(name):
    """Host 是否可信：本机名 / 配置白名单 / CGNAT（Tailscale）网段。"""
    name = (name or "").strip().strip("[]").lower()
    if not name:
        return True                      # 无 Host 的本地工具/HTTP1.0
    if name in _LOCAL_HOSTS:
        return True
    if name in _allowed_host_names():
        return True
    if not _remote_enabled():
        return False                     # 只监听本机时，外来 Host 一律拒绝
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False                     # 域名必须显式写进 allowed_hosts
    # Tailscale（CGNAT）+ 私网/链路本地：这些地址只有同一网络内能到达，
    # 且远程访问仍要过 token，所以直接放行（同 WiFi 直连可用于排查）
    return (ip in _TAILNET_V4 or ip.is_private
            or ip.is_link_local or ip.is_loopback)


def _token_path():
    return os.path.join(_base(), "data", "admin_token")


def _admin_token():
    """管理台访问 token：优先 config admin.token，否则项目内持久随机 token。

    token 持久到 data/admin_token（bot 进程与桌面 EXE 共用同一把），
    桌面版转发聊天请求时凭此通过鉴权；注入进 HTML 页面
    （window.__ADMIN_TOKEN__），前端所有请求带 X-Admin-Token 头——
    跨源网页无法伪造该头（浏览器 CORS 预检拦截），未鉴权的 GET 仅用于状态轮询。
    """
    cfg = _read_cfg()
    token = str((cfg.get("admin") or {}).get("token") or "").strip()
    if token:
        return token
    import secrets
    path = _token_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_hex(16)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(token)
        os.replace(tmp, path)
    except OSError:
        pass
    return token


def _origin_allowed(origin, port):
    """Origin 为空（同源/工具请求）或指向本机本端口时放行；其余 403。"""
    if not origin:
        return True
    try:
        from urllib.parse import urlparse as _up
        host = (_up(origin).hostname or "").lower()
        return _host_name_ok(host)
    except Exception:
        return False


def _redact(text):
    """日志脱敏：与桌面服务共用 textutil.redact（单一实现）。"""
    from qqbot.textutil import redact
    return redact(text)


def _qint(qs, key, default=0):
    """查询参数安全取整：非法输入回退默认值（不让坏参数打挂接口）。"""
    try:
        return int(qs.get(key, [str(default)])[0] or default)
    except (TypeError, ValueError):
        return default


_EXPORT_DENY_NAMES = {
    ".env", "config.json", "config.qq.json", "config.discord.json",
    "admin_token", "luotavern_vault.json", "deepdiscord_vault.json",
    "requirements.txt",
}
_EXPORT_DENY_EXTS = (".py", ".pyc", ".pyd", ".bat", ".cmd", ".ps1", ".psm1",
                     ".pem", ".key", ".crt", ".db", ".sqlite", ".sqlite3",
                     ".spec", ".toml", ".cfg", ".ini", ".dll", ".exe")


def _check_export_path(save_path):
    """导出落盘目录白名单：项目目录内，或用户桌面/文档/下载。

    拒绝绝对路径写到系统目录/项目外任意位置；另外拒绝敏感文件名与可执行/配置
    类扩展名——否则"导出"就等价于"受 JSON 内容限制的任意文件写入"。
    """
    if not save_path or not str(save_path).strip():
        raise ValueError("缺少导出路径")
    p = os.path.abspath(str(save_path))
    name = os.path.basename(p).lower()
    if name in _EXPORT_DENY_NAMES or name.endswith(_EXPORT_DENY_EXTS):
        raise ValueError("不允许导出覆盖 %s" % os.path.basename(p))
    root = os.path.normpath(_base())
    if p == root or p.startswith(root + os.sep):
        return p
    home = os.path.expanduser("~")
    allowed = ("Desktop", "Documents", "Downloads", "桌面", "文档", "下载")
    for n in allowed:
        d = os.path.join(home, n)
        if p == d or p.startswith(d + os.sep):
            return p
    raise ValueError("导出路径只允许项目目录或桌面/文档/下载目录")


def _worldbook_files():
    """列出 book/ 下的世界书文件名（json/png，不读内容）。"""
    cfg = _read_cfg()
    rel = (cfg.get("worldbook") or {}).get("dir", "book")
    d = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(_base(), rel))
    if not os.path.isdir(d):
        return []
    return sorted(fn for fn in os.listdir(d)
                  if fn.lower().endswith((".json", ".png")))


def _selected_worldbooks(cfg=None):
    cfg = cfg or _read_cfg()
    files = list((cfg.get("worldbook") or {}).get("files") or [])
    if files:
        return files
    return _worldbook_files()


def _personas(cfg=None):
    cfg = cfg or _read_cfg()
    try:
        return persona.list_cards(cfg)
    except Exception:
        return []


def _napcat_login_payload(svc):
    """NapCat 登录状态 + 二维码（含"这张码是不是新鲜的"信息）。

    - qrcode_png：优先用 WebUI 实时二维码内容渲染；只有在 NapCat 没给出
      qrcodeurl 时才回退到缓存图片，且**过期缓存不再显示**
    - qr_source：live（实时）/ cache（新鲜缓存）/ none（没有可用二维码）
    - cache_age：缓存图片年龄（秒），便于前端提示"二维码已过期"
    """
    st = svc.napcat_login_status(svc.project_root)
    st["webui_url"] = svc._napcat_webui_url(svc.project_root)
    if not st.get("ok"):
        return st
    st["cache_age"] = svc.qrcode_cache_age(svc.project_root)
    kicked_ts = getattr(svc, "_napcat_kicked_ts", None)
    if kicked_ts:
        st["kicked"] = True
        st["kicked_ago"] = int(max(0, time.time() - float(kicked_ts)))
    if st.get("logged_in"):
        st["qr_source"] = "none"
        return st
    if st.get("kicked"):
        st.setdefault("note", "QQ 被踢下线（登录态已失效）：请重新扫码；"
                              "配置“账号+密码快速登录”后可自动重登，无需每次扫码")
    png = svc.napcat_qrcode_png(st.get("qrcode_url"))
    st["qr_source"] = "live" if png else "none"
    if not png:
        png = svc.napcat_qrcode_png_from_cache(svc.project_root)
        if png:
            st["qr_source"] = "cache"
    st["qrcode_png"] = png
    if not png:
        st["qr_expired"] = True
        st.setdefault("note", "NapCat 没有可用的二维码（二维码已过期或未生成），"
                             "可点“重启 NapCat”重新获取")
    return st


def _reply_info(cfg=None, bot=None):
    if bot is not None:
        return bot.reply_snapshot()
    cfg = cfg or _read_cfg()
    from qqbot.textutil import chars_to_tokens
    r = cfg.get("reply") or {}
    presets = {}
    for name, p in (r.get("presets") or {}).items():
        chars = max(1, int(p.get("max_chars") or 0))
        presets[name] = {
            "max_chars": chars,
            "max_tokens": chars_to_tokens(chars),
            "desc": p.get("desc", ""),
        }
    mode = str(r.get("mode", "medium"))
    return {"mode": mode, "presets": presets,
            "active_max_tokens": chars_to_tokens(
                (presets.get(mode) or {}).get("max_chars", 900))}


def _status(bot=None):
    cfg = _read_cfg()
    cur = persona.current()
    secrets = qconfig.load_env()
    reply = _reply_info(cfg, bot)
    wb = {
        "all_files": _worldbook_files(),
        "selected": _selected_worldbooks(cfg),
        "dir": (cfg.get("worldbook") or {}).get("dir", "book"),
    }
    meta = dict(bot._meta) if bot is not None else {}
    out_seq = 0
    if bot is not None:
        with bot._out_lock:
            out_seq = bot.outbox[-1]["seq"] if bot.outbox else 0
    return {
        "running": bot is not None,
        "platform": cfg.get("platform", "qq"),
        "model": cfg.get("model", ""),
        "base_url": cfg.get("deepseek_base_url", ""),
        "llm_provider": cfg.get("llm_provider", "deepseek"),
        "persona": cur or {},
        "personas": _personas(cfg),
        "bot_name": cfg.get("bot_name") or "",
        "user_names": cfg.get("user_names") or "",
        "owner_name": (cfg.get("identity") or {}).get("owner_name", "主人"),
        "worldbook": wb,
        "reply": reply,
        "usage": _usage(),
        "meta": meta,
        "outbox_seq": out_seq,
        "memory": _memory_stats(bot),
        "secrets": {
            "deepseek_api_key": _mask(secrets.get("DEEPSEEK_API_KEY", "")),
            "discord_token": _mask(secrets.get("DISCORD_TOKEN", "")),
            "onebot_token": _mask(secrets.get("ONEBOT_TOKEN", "")),
        },
        **_qq_login_mode_info(),
    }



def _memory_stats(bot):
    """记忆后端统计（管理台状态页展示 backend/local/hybrid 与远端健康）。"""
    if bot is None or getattr(bot, "memory", None) is None:
        return {}
    try:
        return bot.memory.stats() or {}
    except Exception:
        return {}


def _desktop_memory_stats():
    """桌面独立模式（bot=None）下的本地记忆库统计（不加载嵌入模型）。"""
    try:
        import memory_ui as ui
        return ui.memory_stats()
    except Exception:
        return {}

def _usage():
    try:
        import memory_ui as ui
        return ui.usage_stats()
    except Exception:
        return {"total": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                          "cache_hit_tokens": 0, "cache_rate": 0, "est_cost_yuan": 0},
                "per_model": [], "by_type": []}


def _settings_payload():
    cfg = qconfig.load_config(_config_path())
    return {
        "model": cfg.get("model", ""),
        "deepseek_base_url": cfg.get("deepseek_base_url", ""),
        "llm_provider": cfg.get("llm_provider", "deepseek"),
        "llm_providers": {
            name: {
                "base_url": p.get("base_url", ""),
                "model": p.get("model", ""),
                "api_style": p.get("api_style", "openai"),
                "api_key_set": bool(p.get("api_key")),
            }
            for name, p in (cfg.get("llm_providers") or {}).items()
        },
        "provider_labels": PROVIDER_LABELS,
        "platform": cfg.get("platform", "qq"),
        "bot_name": cfg.get("bot_name", ""),
        "user_names": cfg.get("user_names", ""),
        "owner_name": (cfg.get("identity") or {}).get("owner_name", "主人"),
        "worldbook_dir": (cfg.get("worldbook") or {}).get("dir", "book"),
        "worldbook_files": (cfg.get("worldbook") or {}).get("files") or [],
        "reply": cfg.get("reply") or {},
        "status_bar": cfg.get("status_bar") or {},
        "plugins": cfg.get("plugins") or {},
        "memory": cfg.get("memory") or {},
        "recall": cfg.get("recall") or {},
        "max_context_chars": cfg.get("max_context_chars", 20000),
        "max_history_turns": cfg.get("max_history_turns", 20),
        "temperature": cfg.get("temperature", 0.8),
        **_qq_login_mode_info(),
    }


def _save_settings(bot, body):
    # 整个"读配置 → 逐字段校验 → 落盘"放进锁里：两个标签页同时保存时
    # 旧实现会各自基于旧快照整份覆盖，导致其中一次的修改静默丢失
    mode = None
    if "qq_login_mode" in body:
        mode = str(body.get("qq_login_mode") or "").strip().lower()
        if mode not in ("framework", "shell", ""):
            raise ValueError("qq_login_mode 只能是 framework / shell")
    with _CFG_LOCK:
        cfg = _read_cfg()
        _merge_settings(cfg, body)
        qconfig.atomic_write_json(_config_path(), cfg)
        if mode is not None:
            qcfg = _read_platform_cfg("qq")
            if mode:
                qcfg["login_mode"] = mode
            else:
                qcfg.pop("login_mode", None)
            _write_platform_cfg("qq", qcfg)
    return _apply_settings_hot(bot, cfg)


def _merge_settings(cfg, body):
    for key in ("model", "deepseek_base_url", "bot_name", "user_names"):
        if key in body:
            cfg[key] = str(body[key]).strip()
    if "llm_provider" in body:
        provider = str(body["llm_provider"]).strip()
        # use_env=False：只为拿 DEFAULTS 里的厂家名单，不需要注入真实密钥
        known = (qconfig.load_config(_config_path(), use_env=False).get("llm_providers") or {})
        if provider in known:
            cfg["llm_provider"] = provider
    if "llm_providers" in body and isinstance(body["llm_providers"], dict):
        provs = cfg.setdefault("llm_providers", {})
        defaults = qconfig.DEFAULTS.get("llm_providers") or {}
        for name, vals in body["llm_providers"].items():
            if name not in provs and name not in defaults:
                raise ValueError("未知模型厂家：%s" % name)
            if not isinstance(vals, dict):
                continue
            p = provs.setdefault(name, dict(defaults.get(name) or {}))
            for k in ("base_url", "model", "api_style"):
                if k in vals and str(vals[k]).strip():
                    p[k] = str(vals[k]).strip()
    if "platform" in body:
        platform = str(body["platform"]).strip().lower()
        if platform in ("qq", "discord"):
            cfg["platform"] = platform
    if "temperature" in body:
        try:
            cfg["temperature"] = min(2.0, max(0.0, float(body["temperature"])))
        except (TypeError, ValueError):
            pass
    for int_key in ("max_context_chars", "max_history_turns"):
        if int_key in body:
            try:
                cfg[int_key] = max(1, int(body[int_key]))
            except (TypeError, ValueError):
                pass
    if "owner_name" in body:
        cfg.setdefault("identity", {})["owner_name"] = str(body["owner_name"])
    if "worldbook_files" in body:
        selected = [str(f) for f in (body["worldbook_files"] or [])]
        all_files = _worldbook_files()
        sel_set = set(selected)
        if not selected or sel_set >= set(all_files):
            # 空列表/全选 → files=[]（全部加载）；否则按目录顺序保留勾选（去重）
            cfg.setdefault("worldbook", {})["files"] = []
        else:
            cfg.setdefault("worldbook", {})["files"] = [f for f in all_files if f in sel_set]
    if "reply" in body and isinstance(body["reply"], dict):
        # 深合并：只更新 mode 与 presets[].max_chars，保留 desc 等既有字段
        cur = dict(cfg.get("reply") or {})
        new_presets = (body["reply"].get("presets") or {})
        if isinstance(new_presets, dict):
            old_presets = dict(cur.get("presets") or {})
            for name, p in new_presets.items():
                if not isinstance(p, dict):
                    continue
                merged = dict(old_presets.get(name) or {})
                if "max_chars" in p:
                    try:
                        merged["max_chars"] = max(1, int(p["max_chars"]))
                    except (TypeError, ValueError):
                        continue
                if "desc" in p and p["desc"]:
                    merged["desc"] = str(p["desc"])
                old_presets[name] = merged
            cur["presets"] = old_presets
        mode = str(body["reply"].get("mode") or "")
        if mode in (cur.get("presets") or {}):
            cur["mode"] = mode
        cfg["reply"] = cur
    if "plugins" in body and isinstance(body["plugins"], dict):
        cfg.setdefault("plugins", {}).update(body["plugins"])
    if "memory" in body and isinstance(body["memory"], dict):
        cfg.setdefault("memory", {}).update(body["memory"])
    if "recall" in body and isinstance(body["recall"], dict):
        cfg.setdefault("recall", {}).update(body["recall"])
    if "status_bar" in body and isinstance(body["status_bar"], dict):
        # 深合并：字段清单/必填/模板都可由管理台改写，缺项保留原值
        src = body["status_bar"]
        cur = dict(cfg.get("status_bar") or {})
        for k in ("enable", "repair", "inherit_card"):
            if k in src:
                cur[k] = bool(src[k])
        for k in ("fields", "required"):
            if k in src and isinstance(src[k], list):
                seen, uniq = set(), []
                for f in src[k]:
                    f = str(f).strip()
                    if f and f not in seen:
                        seen.add(f)
                        uniq.append(f)
                cur[k] = uniq
        if "template" in src:
            cur["template"] = str(src["template"] or "")
        if "cheap_max_tokens" in src:
            try:
                cur["cheap_max_tokens"] = max(64, int(src["cheap_max_tokens"]))
            except (TypeError, ValueError):
                pass
        cfg["status_bar"] = cur


def _apply_settings_hot(bot, cfg):
    """设置热生效（重建 LLM 客户端等）。cfg 为刚落盘的最新配置。"""
    from qqbot.bot_core import parse_user_names
    # 热生效部分
    if bot is not None:
        for key in ("model", "deepseek_base_url", "temperature",
                    "max_context_chars", "max_history_turns", "llm_provider"):
            if key in cfg:
                bot.config[key] = cfg[key]
        # llm_providers 深拷贝同步：否则切换厂家后，下一次"保存密钥"会用
        # bot.config（仍是启动时的旧厂家）重建客户端，静默切回旧厂家
        if "llm_providers" in cfg:
            bot.config["llm_providers"] = json.loads(json.dumps(cfg["llm_providers"]))
        # 模型/厂家/接口地址变更后重建 LLM 客户端（多厂家支持），
        # 需合并 .env 中的厂家密钥
        cfg2 = json.loads(json.dumps(cfg))
        qconfig.apply_env(cfg2, qconfig.load_env())
        bot.llm = build_llm(cfg2)
        bot.bot_name = cfg.get("bot_name") or bot.bot_name
        bot.user_names = parse_user_names(cfg.get("user_names"))
        # 状态栏设置热生效：下一轮回复立刻按新字段/必填/模板执行（无需重启）
        if isinstance(cfg.get("status_bar"), dict):
            bot.config["status_bar"] = json.loads(json.dumps(cfg["status_bar"]))
        if "reply" in cfg:
            bot.reply_presets = dict(cfg["reply"].get("presets") or {})
            bot._apply_reply_mode(cfg["reply"].get("mode"))
    return {"ok": True, "note": "已保存；人格/世界书/平台相关改动需重启生效"}


def _save_secrets(bot, body):
    values = {}
    mapping = {
        "deepseek_api_key": "DEEPSEEK_API_KEY",
        "deepseek_base_url": "DEEPSEEK_BASE_URL",
        "openai_api_key": "OPENAI_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "glm_api_key": "GLM_API_KEY",
        "custom_api_key": "CUSTOM_API_KEY",
        "discord_token": "DISCORD_TOKEN",
        "discord_proxy": "DISCORD_PROXY",
        "onebot_ws_url": "ONEBOT_WS_URL",
        "onebot_token": "ONEBOT_TOKEN",
    }
    for key, envar in mapping.items():
        val = body.get(key)
        if val is None:
            continue
        val = str(val).strip()
        # 空值 → None（save_env 会从 .env 删除该键：支持清空密钥）
        values[envar] = val if val else None
    qconfig.save_env(values)
    # 非密钥的连接信息同时写回 config.json，保证 .env 只存真正的密钥
    cfg = _read_cfg()
    changed = False
    for body_key, target in (
            ("deepseek_base_url", "deepseek_base_url"),
            ("discord_proxy", ("discord", "proxy")),
            ("onebot_ws_url", "onebot_ws_url")):
        val = body.get(body_key)
        if val is None or not str(val).strip():
            continue
        if isinstance(target, tuple):
            cfg.setdefault(target[0], {})[target[1]] = str(val).strip()
        else:
            cfg[target] = str(val).strip()
        changed = True
    if changed:
        _write_cfg(cfg)
    if bot is not None:
        env = qconfig.load_env()
        qconfig.apply_env(bot.config, env)
        bot.llm = build_llm(bot.config)
    return {"ok": True, "note": "已写入 .env；平台连接类密钥需重启机器人后生效"}


def _schedule_restart(bot, delay=1.2):
    def _do():
        time.sleep(delay)
        try:
            owners = sorted(bot.owner_users)
            bot._restart_now(owners[0] if owners else 0)
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()


def _save_persona(bot, body):
    name = str(body.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "未指定人格名"}
    found = persona.find_card(name, _read_cfg())
    if not found:
        names = "、".join(c["name"] for c in _personas()) or "（无）"
        return {"ok": False, "error": "找不到「%s」，可用：%s" % (name, names)}
    path, card_name = found
    persona.set_current(card_name, path)
    if bot is not None:
        _schedule_restart(bot)
        return {"ok": True, "note": "已切换到「%s」，机器人即将重启" % card_name}
    return {"ok": True, "note": "已保存「%s」，下次启动生效" % card_name}


def _platform_cfg_path(platform):
    fname = {"qq": "config.qq.json", "discord": "config.discord.json"}.get(platform)
    return os.path.join(_base(), fname) if fname else None


def _read_platform_cfg(platform):
    path = _platform_cfg_path(platform)
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_platform_cfg(platform, data):
    path = _platform_cfg_path(platform)
    if not path:
        raise ValueError("未知平台：%s" % platform)
    qconfig.atomic_write_json(path, data)


def _platform_allow(platform):
    """读取平台配置文件里的 allowed_user_ids（QQ/Discord 各有一个平台文件）。"""
    return list(_read_platform_cfg(platform).get("allowed_user_ids") or [])


def _qq_login_mode_info():
    """QQ 接入方式：显式配置值 + 生效值 + Framework 是否可用。"""
    configured = str(_read_platform_cfg("qq").get("login_mode") or "").strip().lower()
    if configured not in ("framework", "shell"):
        configured = ""
    try:
        from desktop_service import DesktopService
        fw = DesktopService._framework_dir(_base())
    except Exception:
        fw = None
    return {
        "qq_login_mode": configured,
        "qq_login_mode_effective": configured or "shell",
        "qq_framework_available": bool(fw),
    }


def _readiness(svc, vault, keys, cfg=None):
    """启动就绪检查清单，供桌面首页展示。"""
    cfg = cfg or _read_cfg()
    platform = cfg.get("platform", "qq")
    items = []
    items.append({
        "ok": bool(svc.python),
        "key": "python",
        "label": "Python 环境",
        "detail": svc.python if svc.python else "未找到系统 Python（py -3 / python）",
    })
    req = os.path.join(svc.project_root, "requirements.txt")
    items.append({
        "ok": os.path.isfile(req),
        "key": "requirements",
        "label": "依赖清单",
        "detail": req if os.path.isfile(req) else "项目目录缺少 requirements.txt",
    })
    items.append({
        "ok": bool(keys.get("DEEPSEEK_API_KEY")),
        "key": "deepseek",
        "label": "DeepSeek API Key",
        "detail": "已配置" if keys.get("DEEPSEEK_API_KEY") else "未配置，请到「密钥管理」填写",
    })
    if platform == "discord":
        items.append({
            "ok": bool(keys.get("DISCORD_TOKEN")),
            "key": "discord",
            "label": "Discord Token",
            "detail": "已配置" if keys.get("DISCORD_TOKEN") else "未配置，无法启动 Discord 机器人",
        })
    else:
        mode = _qq_login_mode_info()
        framework = mode["qq_login_mode_effective"] == "framework"
        items.append({
            "ok": True,
            "key": "onebot",
            "label": "OneBot 连接",
            "detail": keys.get("ONEBOT_WS_URL") or "ws://127.0.0.1:3001（默认）",
        })
        items.append({
            "ok": True,
            "key": "qq_mode",
            "label": "QQ 接入方式",
            "detail": ("本机 QQ 客户端（官方 QQ + NapCat Framework）"
                       if framework else "NapCat 无头模式（Shell）"),
        })
        try:
            nap = (svc.status() or {}).get("napcat") or {}
        except Exception:
            nap = {}
        if framework:
            items.append({
                "ok": bool(nap.get("qq_alive") or nap.get("running")),
                "key": "qq_client",
                "label": "本机 QQ 客户端",
                "detail": ("已运行（PID %s）" % "、".join(str(p) for p in (nap.get("qq_pids") or []))
                           if nap.get("qq_alive")
                           else ("Framework 未安装：请把 NapCat.Framework 放到项目根"
                                 if not mode["qq_framework_available"]
                                 else "未运行（启动机器人时自动拉起官方 QQ）")),
            })
        else:
            items.append({
                "ok": bool(nap.get("running")),
                "key": "napcat",
                "label": "NapCat 进程",
                "detail": ("端口 %s 已就绪" % nap.get("port")
                           if nap.get("running")
                           else "未运行（启动机器人时自动拉起）"),
            })
    allow = _platform_allow(platform)
    items.append({
        "ok": bool(allow),
        "key": "allowed",
        "label": "允许用户白名单",
        "detail": ("已配置 %d 个用户" % len(allow)) if allow
                  else "未配置，机器人会拒绝所有人，请检查平台配置文件",
    })
    return items


from qqbot.admin_ui import HTML


def make_handler(bot, token=None):
    token = token or _admin_token()

    class Handler(BaseHTTPRequestHandler):
        @property
        def bot(self):
            return bot

        def _auth_fail(self):
            """鉴权：Host 必须是本机 + X-Admin-Token 必须匹配 + Origin（若有）指向本机。

            Host 校验挡 DNS rebinding（攻击者域名解析到 127.0.0.1 后同源读接口）。
            """
            if self._host_fail():
                return True
            if self.headers.get("X-Admin-Token", "") != token:
                return True
            origin = self.headers.get("Origin", "")
            if origin and not _origin_allowed(origin, self.server.server_port):
                return True
            return False

        def _host_fail(self):
            host = (self.headers.get("Host") or "").strip()
            if not host:
                return False  # 无 Host 的本地工具/HTTP1.0 请求
            name = host.rsplit(":", 1)[0].strip().strip("[]").lower()
            return not _host_name_ok(name)

        @property
        def _remote_client(self):
            """请求是否来自非本机（远程模式下的手机/外网）。"""
            return not _is_loopback_addr(self.client_address[0])

        def _presented_token(self):
            """客户端提交的 token：优先请求头，其次 cookie，最后 ?token= 查询参数。"""
            got = self.headers.get("X-Admin-Token", "")
            if got:
                return got
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "lv_admin":
                    return v
            try:
                qs = parse_qs(urlparse(self.path).query)
                return (qs.get("token") or [""])[0]
            except Exception:
                return ""

        def _auth_throttled(self):
            """远程鉴权失败限流：窗口内失败过多 → 锁定一段时间。"""
            ip = self.client_address[0]
            lim = int(_cfg_admin().get("auth_fail_limit") or 8)
            win = int(_cfg_admin().get("auth_fail_window") or 300)
            lock = int(_cfg_admin().get("auth_lock_seconds") or 300)
            now = time.time()
            with _AUTH_LOCK:
                rec = _AUTH_FAILS.get(ip)
                if rec and rec.get("until", 0) > now:
                    return True
                if not rec or now - rec.get("first", 0) > win:
                    _AUTH_FAILS[ip] = {"first": now, "n": 0, "until": 0}
                    rec = _AUTH_FAILS[ip]
                if rec["n"] >= lim:
                    rec["until"] = now + lock
                    return True
            return False

        def _note_auth_fail(self):
            ip = self.client_address[0]
            with _AUTH_LOCK:
                rec = _AUTH_FAILS.setdefault(ip, {"first": time.time(), "n": 0, "until": 0})
                rec["n"] = rec.get("n", 0) + 1

        def _note_auth_ok(self):
            with _AUTH_LOCK:
                _AUTH_FAILS.pop(self.client_address[0], None)

        def _json(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _page(self, html, code=200, cookie=None):
            body = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "frame-ancestors 'none'; object-src 'none'; "
                "default-src 'self' 'unsafe-inline' data: blob:")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def _login_page(self):
            """远程访问登录页：**不含 token**，只让用户输入一次访问令牌。"""
            self._page(_LOGIN_HTML)

        def _mobile_page(self):
            """手机端页面（/m）：通过鉴权才注入 token，并记住 30 天。"""
            cookie = None
            if self._remote_client and self._presented_token() == token:
                cookie = ("lv_admin=%s; Path=/; Max-Age=%d; SameSite=Strict; HttpOnly"
                          % (token, 30 * 24 * 3600))
            self._page(_MOBILE_HTML.replace("__ADMIN_TOKEN__", token if cookie else ""),
                       cookie=cookie)

        def _html(self):
            # 只替换声明行：window.__ADMIN_TOKEN__="<token>"（token 为 hex，无转义问题）
            body = HTML.replace(
                'window.__ADMIN_TOKEN__="";',
                'window.__ADMIN_TOKEN__="%s";' % token,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # 安全头：禁止被第三方页面 iframe（点击劫持）、禁止缓存含 token 的页面
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "frame-ancestors 'none'; object-src 'none'; "
                "default-src 'self' 'unsafe-inline' data: blob:")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            # chunked 请求没有 Content-Length：旧实现当成空 body（语义错乱）。
            # 管理台的调用方都是本机 fetch/工具，明确拒绝即可。
            if self.headers.get("Transfer-Encoding", "").lower().strip() == "chunked":
                raise ValueError("不支持 chunked 请求体，请带 Content-Length")
            raw_len = self.headers.get("Content-Length")
            if not raw_len:
                return {}
            try:
                length = int(raw_len)
            except ValueError:
                raise ValueError("非法 Content-Length")
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("请求体大小非法（上限 %d 字节）" % MAX_BODY_BYTES)
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _ok(self, data=None, note=None):
            payload = {"ok": True, "data": data}
            if note:
                payload["note"] = note
            self._json(200, payload)

        def _err(self, error, code=400):
            self._json(code, {"ok": False, "error": str(error)})

        # 本机绝对路径 / UNC 路径：错误文案里必须抹掉（曾直接把异常原文回显给调用方）
        _PATH_RE = re.compile(
            r"[A-Za-z]:\\[^\s\"']*"        # C:\Users\...\card.json
            r"|\\\\[^\s\"']*"              # \\server\share\...
            r"|/(?:[^\s\"']*/)+[^\s\"']*"  # /home/user/...
        )

        def _safe_msg(self, exc, fallback="请求失败"):
            """错误文案脱敏：去掉路径等本机信息后再回显。"""
            msg = self._PATH_RE.sub("<路径>", str(exc))
            return (msg.strip()[:200] or fallback)

        def _fail(self, exc):
            """外层兜底异常分类：业务错误 → 400（回显可读信息）；
            未知错误 → 500（通用文案，细节只进服务端日志，不泄露路径）。"""
            if isinstance(exc, (ValueError, KeyError)):
                self._err(self._safe_msg(exc), 400)
            else:
                import traceback
                self.log_message("内部错误: %s", traceback.format_exc())
                self._err("服务器内部错误", 500)

        def _raw(self, code, body, content_type, filename=None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if filename:
                from urllib.parse import quote
                self.send_header(
                    "Content-Disposition",
                    "attachment; filename*=UTF-8''%s" % quote(filename))
            self.end_headers()
            self.wfile.write(body)

        def _ui(self):
            import memory_ui as ui
            return ui

        def _desktop(self):
            from desktop_service import SERVICE
            return SERVICE

        def _vault(self):
            import desktop_vault
            return desktop_vault

        def _desktop_status(self):
            svc = self._desktop()
            vault = self._vault()
            keys = vault.load_vault()
            st = svc.status()
            cfg = _read_cfg()
            st.update({
                "personas": _personas(cfg),
                "persona": persona.current() or {},
                "platform": cfg.get("platform", "qq"),
                "model": cfg.get("model", ""),
                "reply": _reply_info(cfg),
                "memory": _desktop_memory_stats(),
                "worldbook": {
                    "all_files": _worldbook_files(),
                    "selected": _selected_worldbooks(cfg),
                },
                "vault": {
                    "path": vault.vault_path(),
                    "keys": {k: {"set": bool(v), "masked": _mask(v)}
                             for k, v in keys.items()},
                    "meta": vault.vault_meta(),
                },
                "readiness": _readiness(svc, vault, keys, cfg),
            })
            return st

        _desktop_status_cache = None

        # 状态接口短 TTL 缓存：前端每 5 秒轮询一次，而 status 每次都要重扫
        # 角色卡（PNG 卡要 base64 解码）、探测端口、做几次 COUNT——
        # 多标签页时是持续的 CPU/IO 开销
        def _cached_desktop_status(self):
            now = time.time()
            cached = self.__class__._desktop_status_cache
            if cached and now - cached[0] < STATUS_TTL_SEC:
                return cached[1]
            st = self._desktop_status()
            self.__class__._desktop_status_cache = (now, st)
            return st

        # 不需要 token 的 GET：页面本身（token 由该页面注入）与两个封面图
        # （<img src=...> 无法携带自定义请求头）
        PUBLIC_GET = ("/", "/index.html", "/api/cards/cover", "/api/worldbook/cover")

        def do_GET(self):
            parsed = urlparse(self.path)
            try:
                if self._host_fail():
                    self._err("非本机请求被拒绝", 403)
                    return
                # 远程来源（手机/外网）一律先鉴权：页面本身含 token，
                # 旧实现首页公开 + 注入 token ⇒ 暴露到组网/公网等于无鉴权
                if self._remote_client:
                    if self._auth_throttled():
                        self._err("鉴权失败次数过多，请稍后再试", 429)
                        return
                    if self._presented_token() != token:
                        self._note_auth_fail()
                        if parsed.path in ("/", "/index.html", "/m", "/mobile"):
                            self._login_page()
                        else:
                            self._err("未授权", 403)
                        return
                    self._note_auth_ok()
                # 读接口同样鉴权：此前 GET 全裸，token/记忆/角色卡/QQ 登录二维码
                # 都能被本机任意进程直接读走（读侧等于零防护）
                if (not self._remote_client
                        and parsed.path not in self.PUBLIC_GET and self._auth_fail()):
                    self._err("未授权", 403)
                    return
                if parsed.path in ("/m", "/mobile"):
                    self._mobile_page()
                    return
                if parsed.path in ("/", "/index.html"):
                    self._html()
                    return
                if parsed.path == "/api/status":
                    self._ok(_status(self.bot))
                    return
                if parsed.path == "/api/desktop/status":
                    self._ok(self._cached_desktop_status())
                    return
                if parsed.path == "/api/desktop/logs":
                    qs = parse_qs(parsed.query)
                    after = _qint(qs, "after", 0)
                    self._ok(self._desktop().logs_since(after))
                    return
                if parsed.path == "/api/desktop/outbox":
                    qs = parse_qs(parsed.query)
                    after = _qint(qs, "after", 0)
                    self._ok(self._desktop().outbox(after))
                    return
                if parsed.path == "/api/desktop/napcat/login":
                    svc = self._desktop()
                    st = _napcat_login_payload(svc)
                    if st.get("ok"):
                        self._ok(st)
                    else:
                        self._err(st.get("error", "NapCat 登录状态获取失败"), 400)
                    return
                if parsed.path == "/api/desktop/vault":
                    vault = self._vault()
                    keys = vault.load_vault()
                    self._ok({
                        "path": vault.vault_path(),
                        "keys": {k: {"set": bool(v), "masked": _mask(v)}
                                 for k, v in keys.items()},
                    })
                    return
                if parsed.path == "/api/settings":
                    self._ok(_settings_payload())
                    return
                if parsed.path == "/api/memory/stats":
                    # 记忆库统计：bot 在场用运行期后端；独立模式读本地库
                    if self.bot is not None and getattr(self.bot, "memory", None):
                        self._ok(_memory_stats(self.bot))
                    else:
                        ui = self._ui()
                        self._ok(ui.memory_stats())
                    return
                if parsed.path == "/api/outbox":
                    qs = parse_qs(parsed.query)
                    after = _qint(qs, "after", 0)
                    data = self.bot.outbox_since(after) if self.bot is not None else []
                    self._ok(data)
                    return
                if parsed.path == "/api/cards":
                    self._ok(card_manager.list_cards())
                    return
                if parsed.path == "/api/ai/card":
                    cfg = _apply_vault_keys(
                        qconfig.load_config(_config_path()))
                    aw = ai_card_writer.load_settings(cfg)
                    provs = cfg.get("llm_providers") or {}
                    self._ok({
                        "prompt": aw["prompt"],
                        "provider": _ai_provider(
                            cfg, aw,
                            explicit=_read_cfg().get("llm_provider", "")),
                        "model": aw["model"],
                        "providers": [
                            {
                                "key": k,
                                "label": PROVIDER_LABELS.get(k, k),
                                "model": p.get("model", ""),
                                "api_style": p.get("api_style", "openai"),
                                "api_key_set": bool(p.get("api_key")),
                            }
                            for k, p in provs.items()
                        ],
                    })
                    return
                if parsed.path == "/api/cards/detail":
                    qs = parse_qs(parsed.query)
                    file = unquote(qs["file"][0]) if qs.get("file") else ""
                    try:
                        self._ok(card_manager.card_detail(file))
                    except Exception as exc:
                        self._err(self._safe_msg(exc), 400)
                    return
                if parsed.path in ("/api/cards/cover", "/api/worldbook/cover"):
                    qs = parse_qs(parsed.query)
                    file = unquote(qs["file"][0]) if qs.get("file") else ""
                    try:
                        fn = (card_manager.card_cover(file)
                              if parsed.path == "/api/cards/cover"
                              else card_manager.worldbook_cover(file))
                    except Exception as exc:
                        self._err(self._safe_msg(exc), 400)
                        return
                    if not fn or not os.path.isfile(fn):
                        self._err("未设置封面", 404)
                        return
                    with open(fn, "rb") as f:
                        self._raw(200, f.read(), "image/png")
                    return
                if parsed.path in ("/api/cards/export", "/api/worldbook/export"):
                    qs = parse_qs(parsed.query)
                    file = unquote(qs["file"][0]) if qs.get("file") else ""
                    fmt = (qs.get("format") or ["json"])[0] or "json"
                    try:
                        data = (card_manager.export_card(file, fmt)
                                if parsed.path == "/api/cards/export"
                                else card_manager.export_worldbook(file, fmt))
                    except Exception as exc:
                        self._err(self._safe_msg(exc), 400)
                        return
                    stem = os.path.splitext(os.path.basename(file))[0]
                    self._raw(
                        200, data,
                        "image/png" if fmt == "png" else "application/json",
                        filename="%s.%s" % (stem, fmt))
                    return
                ui = self._ui()
                qs = parse_qs(parsed.query)
                identity = unquote(qs["identity"][0]) if qs.get("identity") else None
                if parsed.path == "/api/identities":
                    self._ok(ui.identities())
                elif parsed.path == "/api/memories":
                    self._ok(ui.list_memories(identity=identity, keyword=(
                        unquote(qs["q"][0]) if qs.get("q") else None)))
                elif parsed.path == "/api/important":
                    self._ok(ui.list_important(identity))
                elif parsed.path == "/api/affection":
                    self._ok(ui.list_affection())
                elif parsed.path == "/api/corrected":
                    self._ok(ui.list_corrected(identity))
                elif parsed.path == "/api/timeline":
                    self._ok(ui.list_timeline())
                elif parsed.path == "/api/worldbook":
                    self._ok(ui.list_worldbook())
                elif parsed.path == "/api/usage":
                    self._ok(ui.usage_stats())
                else:
                    self._err("not found", 404)
            except Exception as exc:
                self._err(exc, 500)

        def _route_id(self, table):
            parts = urlparse(self.path).path.split("/")
            if len(parts) == 4 and parts[1] == "api" and parts[2] == table:
                try:
                    return int(parts[3])
                except ValueError:
                    return None
            return None

        def do_POST(self):
            try:
                # 远程来源：先限流再验 token（cookie/头/查询参数均可），
                # 避免暴露到组网后被暴力猜 token
                if self._remote_client:
                    if self._auth_throttled():
                        self._err("鉴权失败次数过多，请稍后再试", 429)
                        return
                    if self._presented_token() != token:
                        self._note_auth_fail()
                        self._err("未授权", 403)
                        return
                    # 远程写接口：Origin 必须可信（cookie 认证时的纵深防御）
                    origin = self.headers.get("Origin", "")
                    if origin and not _origin_allowed(
                            origin, self.server.server_port):
                        self._err("跨源请求被拒绝", 403)
                        return
                    self._note_auth_ok()
                elif self._auth_fail():
                    self._err("未授权", 403)
                    return
                parsed = urlparse(self.path)
                body = self._read_json()
                ui = self._ui()
                if parsed.path == "/api/settings":
                    res = _save_settings(self.bot, body)
                    self._ok(note=res.get("note"))
                elif parsed.path == "/api/desktop/project":
                    if self.bot is not None:
                        self._err("浏览器模式已绑定机器人进程，请用桌面版切换项目", 400)
                    else:
                        res = self._desktop().set_project(body.get("path", ""))
                        if res.get("ok"):
                            os.environ["LUOTAVERN_ROOT"] = res["project_root"]
                            try:
                                import memory_ui as ui
                                ui.set_root(res["project_root"])
                            except Exception:
                                pass
                            self._ok(note="项目已切换：%s" % res["project_root"])
                        else:
                            self._err(res.get("error", "切换失败"), 400)
                elif parsed.path == "/api/desktop/start":
                    if self.bot is not None:
                        self._err("浏览器模式已随机器人运行，请勿重复启动", 400)
                    else:
                        res = self._desktop().start_bot(
                            body.get("platform") or _read_cfg().get("platform", "qq"))
                        if res.get("ok"):
                            self._ok({"pid": res.get("pid")}, note="机器人已启动")
                        else:
                            self._err(res.get("error", "启动失败"), 400)
                elif parsed.path == "/api/desktop/stop":
                    if self.bot is not None:
                        self._err("浏览器模式请使用【关闭机器人】指令", 400)
                    else:
                        # 用户显式点「停止」→ 即便机器人是别处启动的也按端口结束
                        # （关闭管理台/退出程序走的是 stop_bot() 非显式路径，不会误杀）
                        res = self._desktop().stop_bot(explicit=True)
                        self._ok(note=res.get("note", "已停止"))
                elif parsed.path == "/api/desktop/restart":
                    if self.bot is not None:
                        self._err("浏览器模式请使用【关闭机器人】指令", 400)
                    else:
                        res = self._desktop().restart_bot(body.get("platform"))
                        if res.get("ok"):
                            self._ok({"pid": res.get("pid")}, note="机器人已重启")
                        else:
                            self._err(res.get("error", "重启失败"), 400)
                elif parsed.path == "/api/desktop/install":
                    if self.bot is not None:
                        self._err("浏览器模式请勿在机器人进程内安装依赖", 400)
                    else:
                        res = self._desktop().install_deps()
                        if res.get("ok"):
                            self._ok({"pid": res.get("pid")}, note="依赖安装已开始，见运行日志")
                        else:
                            self._err(res.get("error", "安装启动失败"), 400)
                elif parsed.path == "/api/desktop/chat":
                    if self.bot is not None:
                        ok = self.bot.console_send(body.get("text", ""))
                        self._ok({"sent": ok})
                    else:
                        res = self._desktop().chat(body.get("text", ""))
                        if res.get("ok"):
                            self._ok(res.get("data") or {}, note="已发送")
                        else:
                            self._err(res.get("error", "发送失败"), 400)
                elif parsed.path == "/api/desktop/vault":
                    vault = self._vault()
                    keys = vault.load_vault()
                    key = str(body.get("key") or "").strip()
                    value = body.get("value")
                    if not key:
                        self._err("缺少密钥键名", 400)
                    elif value is None or str(value).strip() == "":
                        keys.pop(key, None)
                        vault.save_vault(keys)
                        try:
                            vault.remove_from_project(
                                self._desktop().project_root, key)
                        except Exception:
                            pass
                        self._ok(note="已删除 %s" % key)
                    else:
                        keys[key] = str(value).strip()
                        vault.save_vault(keys)
                        self._ok(note="已保存 %s" % key)
                elif parsed.path == "/api/desktop/vault/import":
                    vault = self._vault()
                    imported = vault.import_from_project(
                        self._desktop().project_root, vault.load_vault())
                    if imported:
                        self._ok({"imported": list(imported)},
                                 note="已从项目导入：%s" % "、".join(imported))
                    else:
                        self._ok({"imported": []}, note="未发现可导入的新密钥")
                elif parsed.path == "/api/desktop/vault/apply":
                    vault = self._vault()
                    written = vault.apply_to_project(
                        self._desktop().project_root, vault.load_vault())
                    self._ok({"written": written},
                             note="已写入项目 .env：%s" % ("、".join(written) or "无"))
                elif parsed.path == "/api/desktop/persona":
                    name = str(body.get("name") or "").strip()
                    found = persona.find_card(name, _read_cfg())
                    if not found:
                        names = "、".join(c["name"] for c in _personas()) or "（无）"
                        self._err("找不到「%s」，可用：%s" % (name, names), 400)
                    else:
                        _path, card_name = found
                        persona.set_current(card_name, _path)
                        try:
                            import memory_ui as ui
                            ui.set_root(_base())
                        except Exception:
                            pass
                        svc = self._desktop()
                        if svc.bot_status().get("running"):
                            res = svc.restart_bot()
                            if res.get("ok"):
                                self._ok(note="已切换到「%s」，机器人正在重启…" % card_name)
                            else:
                                self._err("人格已保存，但机器人重启失败：%s"
                                          % res.get("error", ""), 400)
                        else:
                            self._ok(note="已切换到「%s」，下次启动机器人时生效" % card_name)
                elif parsed.path == "/api/cards/import":
                    res = card_manager.import_card(
                        body.get("filename"), body.get("data_b64"))
                    self._ok(res, note="已导入角色卡：%s" % res["name"])
                elif parsed.path == "/api/cards/new":
                    res = card_manager.new_card(
                        body.get("fields"), body.get("cover_b64"))
                    self._ok(res, note="已新建角色卡：%s" % res["name"])
                elif parsed.path == "/api/cards/save":
                    res = card_manager.save_card(
                        body.get("file"), body.get("fields"))
                    self._ok(res, note="已保存角色卡：%s" % res["name"])
                elif parsed.path == "/api/cards/cover":
                    card_manager.set_card_cover(
                        body.get("file"), body.get("data_b64"))
                    self._ok(note="封面已设置")
                elif parsed.path == "/api/cards/export/save":
                    save_path = _check_export_path(body.get("save_path"))
                    res = card_manager.export_card(
                        body.get("file"), body.get("format") or "json",
                        save_path)
                    self._ok(res, note="已导出到：%s" % res["path"])
                elif parsed.path == "/api/desktop/napcat/login/refresh":
                    svc = self._desktop()
                    svc.napcat_refresh_qrcode(svc.project_root)
                    st = _napcat_login_payload(svc)
                    if not st.get("ok"):
                        self._err(st.get("error", "刷新失败"), 400)
                    elif st.get("logged_in"):
                        self._ok(st, note="已登录")
                    elif st.get("refresh_ok"):
                        self._ok(st, note="二维码已刷新")
                    else:
                        # NapCat 回 success 但没生成新码：如实告知，别假装刷新成功
                        self._ok(st, note=st.get("refresh_error") or "二维码未更新")
                    return
                elif parsed.path == "/api/desktop/napcat/restart":
                    svc = self._desktop()
                    res = svc.napcat_restart(svc.project_root)
                    if res.get("ok"):
                        st = _napcat_login_payload(svc)
                        st["restarted"] = True
                        self._ok(st, note="NapCat 已重启" +
                                 ("（已登录）" if st.get("logged_in") else "，请扫码登录"))
                    else:
                        self._err(res.get("error", "重启 NapCat 失败"), 400)
                    return
                elif parsed.path == "/api/ai/card/settings":
                    cfg = _read_cfg()
                    aw = ai_card_writer.save_settings(
                        cfg,
                        prompt=body.get("prompt"),
                        provider=body.get("provider"),
                        model=body.get("model"))
                    _write_cfg(cfg)
                    self._ok(aw, note="AI 编写设置已保存")
                elif parsed.path == "/api/ai/card/generate":
                    cfg = _apply_vault_keys(
                        qconfig.load_config(_config_path()))
                    try:
                        res = ai_card_writer.generate(
                            body.get("requirements", ""), cfg,
                            prompt=body.get("prompt"),
                            provider=body.get("provider"),
                            model=body.get("model"))
                    except Exception as exc:
                        self._err(self._safe_msg(exc), 400)
                        return
                    self._ok({"card": res["card"], "name": res["name"]},
                             note="已生成角色卡：%s" % res["name"])
                elif parsed.path == "/api/ai/card/save":
                    try:
                        res = card_manager.new_card_from_data(
                            body.get("card"), body.get("cover_b64"))
                    except Exception as exc:
                        self._err(self._safe_msg(exc), 400)
                        return
                    self._ok(res, note="已保存角色卡：%s" % res["name"])
                elif parsed.path == "/api/worldbook/import":
                    res = card_manager.import_worldbook(
                        body.get("filename"), body.get("data_b64"))
                    self._ok(res, note="已导入世界书：%s" % res["file"])
                elif parsed.path == "/api/worldbook/new":
                    res = card_manager.new_worldbook(
                        body.get("name"), body.get("cover_b64"))
                    self._ok(res, note="已新建世界书：%s" % res["file"])
                elif parsed.path == "/api/worldbook/cover":
                    card_manager.set_worldbook_cover(
                        body.get("file"), body.get("data_b64"))
                    self._ok(note="封面已设置")
                elif parsed.path == "/api/worldbook/export/save":
                    save_path = _check_export_path(body.get("save_path"))
                    res = card_manager.export_worldbook(
                        body.get("file"), body.get("format") or "json",
                        save_path)
                    self._ok(res, note="已导出到：%s" % res["path"])
                elif parsed.path == "/api/secrets":
                    res = _save_secrets(self.bot, body)
                    self._ok(note=res.get("note"))
                elif parsed.path == "/api/persona":
                    res = _save_persona(self.bot, body)
                    if res.get("ok"):
                        self._ok(note=res.get("note"))
                    else:
                        self._err(res.get("error", "保存失败"), 400)
                elif parsed.path == "/api/restart":
                    if self.bot is None:
                        self._ok(note="独立模式：设置已保存，下次启动生效")
                    else:
                        _schedule_restart(self.bot)
                        self._ok(note="机器人正在重启…")
                elif parsed.path == "/api/chat":
                    if self.bot is None:
                        self._err("独立模式不可用：请从运行中的机器人打开管理台", 400)
                    else:
                        ok = self.bot.console_send(body.get("text", ""))
                        self._ok({"sent": ok})
                elif parsed.path == "/api/memories":
                    new_id = ui.add_memory(
                        body.get("identity") or "主人", body.get("content", ""))
                    self._ok({"id": new_id})
                elif parsed.path == "/api/important":
                    new_id = ui.add_important(
                        body.get("identity") or "主人", body.get("content", ""),
                        affect_delta=body.get("affect_delta", 0),
                        importance=body.get("importance", 5),
                        keywords=body.get("keywords"))
                    self._ok({"id": new_id})
                elif parsed.path == "/api/worldbook/edit":
                    ok = ui.update_worldbook_entry(
                        body.get("file", ""), body.get("id"),
                        body.get("keys", ""), body.get("content", ""))
                    if not ok:
                        self._err("未找到该世界书条目", 404)
                    else:
                        self._ok(note="已保存")
                elif parsed.path == "/api/affection":
                    ui.set_affection(body.get("identity", ""), body.get("score", 0))
                    self._ok(note="已保存")
                else:
                    self._err("not found", 404)
            except Exception as exc:
                self._fail(exc)

        def _update_or_delete(self, table, update_fn, delete_fn):
            mid = self._route_id(table)
            if mid is None:
                return False
            body = self._read_json()
            if body.get("content") is not None or "content" in body:
                if update_fn is None:
                    self._err("无效操作", 400)
                    return True
                ok = update_fn(mid, body.get("content", ""),
                               keywords=body.get("keywords")
                               if "keywords" in body else None)
            else:
                ok = delete_fn(mid)
            if ok:
                self._ok(note="已保存")
            else:
                self._err("未找到该记忆", 404)
            return True

        def do_PUT(self):
            try:
                if self._auth_fail():
                    self._err("未授权", 403)
                    return
                ui = self._ui()
                if self._update_or_delete("memories", ui.update_memory, ui.delete_memory):
                    return
                if self._update_or_delete("important", ui.update_important, ui.delete_important):
                    return
                if urlparse(self.path).path == "/api/affection":
                    body = self._read_json()
                    ui.set_affection(body.get("identity", ""), body.get("score", 0))
                    self._ok(note="已保存")
                    return
                self._err("not found", 404)
            except Exception as exc:
                self._fail(exc)

        def do_DELETE(self):
            try:
                if self._auth_fail():
                    self._err("未授权", 403)
                    return
                ui = self._ui()
                parsed = urlparse(self.path)
                if parsed.path in ("/api/cards/cover", "/api/worldbook/cover"):
                    qs = parse_qs(parsed.query)
                    file = unquote(qs["file"][0]) if qs.get("file") else ""
                    if parsed.path == "/api/cards/cover":
                        removed = card_manager.remove_card_cover(file)
                    else:
                        removed = card_manager.remove_worldbook_cover(file)
                    self._ok(note="已移除封面" if removed else "无自定义封面")
                    return
                if self._update_or_delete("memories", None, ui.delete_memory):
                    return
                if self._update_or_delete("important", None, ui.delete_important):
                    return
                parts = parsed.path.split("/")
                if len(parts) == 5 and parts[1] == "api" and parts[2] == "desktop" \
                        and parts[3] == "vault":
                    key = unquote(parts[4])
                    vault = self._vault()
                    keys = vault.load_vault()
                    keys.pop(key, None)
                    vault.save_vault(keys)
                    try:
                        vault.remove_from_project(
                            self._desktop().project_root, key)
                    except Exception:
                        pass
                    self._ok(note="已删除 %s" % key)
                    return
                self._err("not found", 404)
            except Exception as exc:
                self._fail(exc)

        def log_message(self, fmt, *args):
            try:
                print("[admin] %s" % (fmt % args), flush=True)
            except Exception:
                pass

        # 轮询接口（前端每 1.2~5 秒一次）不进日志：真实问题会被刷屏淹没
        _QUIET_PATHS = ("/api/outbox", "/api/desktop/outbox", "/api/logs",
                        "/api/status", "/api/desktop/status")

        def log_request(self, code="-", size="-"):
            try:
                path = urlparse(self.path).path
            except Exception:
                path = self.path or ""
            if path in self._QUIET_PATHS and str(code).startswith(("2", "3")):
                return          # 正常轮询静默；出错（4xx/5xx）照常打日志
            return BaseHTTPRequestHandler.log_request(self, code, size)

    return Handler


def start_admin(bot=None, port=8008, block=False, host=None):
    """启动管理台。block=True 时前台阻塞（独立模式），否则后台线程（随 bot 运行）。

    监听地址默认仅本机；把 config.admin.bind 设为 "0.0.0.0" 或 tailnet 地址即可
    远程访问——此时**非本机来源一律先验 token**（页面与接口同规则），
    并打印一条可直接在手机上打开的链接。
    """
    token = _admin_token()
    handler = make_handler(bot, token=token)
    host = (host or _bind_host()).strip() or "127.0.0.1"
    server = ThreadingHTTPServer((host, int(port)), handler)
    server.daemon_threads = True
    # 半开连接防护：声明了 Content-Length 却不发完的客户端会永久占用一个线程
    server.socket.settimeout(30)
    print("管理台已启动: http://127.0.0.1:%d （Ctrl+C 退出）" % int(port), flush=True)
    if _remote_enabled():
        tail = _tailnet_ip()
        shown = tail or _best_local_ip() or host
        print("管理台（远程已开启，监听 %s）：" % host, flush=True)
        print("  手机打开（浏览器会记住 30 天）: http://%s:%d/m?token=%s"
              % (shown, int(port), token), flush=True)
        if not tail:
            print("  提示：未检测到 Tailscale 地址（100.64.0.0/10），"
                  "请确认 tailscale 已登录；域名方式请填 admin.allowed_hosts。",
                  flush=True)
    if block:
        server.serve_forever()
        return server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    """独立模式入口（memory_ui.py 会调用）。"""
    import sys
    port = 8008
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    start_admin(bot=None, port=port, block=True)


if __name__ == "__main__":
    main()
