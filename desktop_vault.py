# -*- coding: utf-8 -*-
"""EXE 密钥库：读取项目现有密钥 → 统一存放在 EXE 旁的密钥库文件中。

- 密钥库路径：EXE 同目录 luotavern_vault.json（便携）；目录不可写时回退 %APPDATA%\\LuoTavern
- 值做 base64 混淆（防止随手打开就看到明文；不是强加密，请勿分享该文件）
- 首次运行自动从项目 .env / config*.json 导入现有密钥
- 旧版 deepdiscord_vault.json 首次读取时自动迁移到新文件名
"""
import base64
import json
import os
import sys

VAULT_FILENAME = "luotavern_vault.json"
LEGACY_VAULT_FILENAME = "deepdiscord_vault.json"

try:
    import ctypes
    from ctypes import wintypes
    _DPAPI_OK = os.name == "nt"
except Exception:
    ctypes = None
    wintypes = None
    _DPAPI_OK = False

if _DPAPI_OK:
    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    # 64 位句柄/指针：未声明 restype 会被截断成 32 位，DPAPI 缓冲区无法正确释放
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DATA_BLOB)]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wintypes.LPCWSTR),
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DATA_BLOB)]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL


def _dpapi_protect(data):
    if not _DPAPI_OK:
        raise RuntimeError("DPAPI is not available on this platform")
    inbuf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(inbuf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not _crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        if blob_out.pbData:
            _kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data):
    if not _DPAPI_OK:
        raise RuntimeError("DPAPI is not available on this platform")
    inbuf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(inbuf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    if not _crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        if blob_out.pbData:
            _kernel32.LocalFree(blob_out.pbData)


# (EXE 密钥库键名, 项目配置中的点路径)
KNOWN_KEYS = [
    ("DEEPSEEK_API_KEY", "deepseek_api_key"),
    ("DEEPSEEK_BASE_URL", "deepseek_base_url"),
    ("OPENAI_API_KEY", "llm_providers.openai.api_key"),
    ("ANTHROPIC_API_KEY", "llm_providers.anthropic.api_key"),
    ("GLM_API_KEY", "llm_providers.glm.api_key"),
    ("CUSTOM_API_KEY", "llm_providers.custom.api_key"),
    ("DISCORD_TOKEN", "discord.token"),
    ("DISCORD_PROXY", "discord.proxy"),
    ("ONEBOT_WS_URL", "onebot_ws_url"),
    ("ONEBOT_TOKEN", "onebot_token"),
    ("TENCENTDB_MEMORY_API_KEY", "memory.tencentdb.api_key"),
]

KNOWN_KEY_META = [
    {"envar": "DEEPSEEK_API_KEY", "label": "DeepSeek API Key",
     "desc": "调用 DeepSeek 模型的主要凭据", "required": True},
    {"envar": "DEEPSEEK_BASE_URL", "label": "DeepSeek API 地址",
     "desc": "默认 https://api.deepseek.com", "required": False},
    {"envar": "OPENAI_API_KEY", "label": "OpenAI / Codex API Key",
     "desc": "OpenAI 系接口（含 Codex）的主要凭据", "required": False},
    {"envar": "ANTHROPIC_API_KEY", "label": "Claude API Key",
     "desc": "Anthropic Claude 原生接口凭据", "required": False},
    {"envar": "GLM_API_KEY", "label": "GLM 智谱 API Key",
     "desc": "智谱 GLM 接口凭据", "required": False},
    {"envar": "CUSTOM_API_KEY", "label": "自定义接口 API Key",
     "desc": "OpenAI 兼容的自定义接口凭据", "required": False},
    {"envar": "DISCORD_TOKEN", "label": "Discord Bot Token",
     "desc": "启动 Discord 平台时需要", "required": False},
    {"envar": "DISCORD_PROXY", "label": "Discord 代理",
     "desc": "可选，如 http://127.0.0.1:7890", "required": False},
    {"envar": "ONEBOT_WS_URL", "label": "NapCat WebSocket 地址",
     "desc": "默认 ws://127.0.0.1:3001", "required": False},
    {"envar": "ONEBOT_TOKEN", "label": "NapCat 访问令牌",
     "desc": "与 NapCat 配置一致", "required": False},
    {"envar": "TENCENTDB_MEMORY_API_KEY", "label": "TencentDB-Agent-Memory 网关密钥",
     "desc": "记忆后端连接 MemoryCore 网关/腾讯云 Agent Memory 实例时填写",
     "required": False},
]


def exe_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _writable(path):
    """目录可写探测（os.access 在 Windows 上对只读目录常误判为可写）。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".luotavern_write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("")
        os.remove(probe)
        return True
    except OSError:
        return False


_WRITABLE_CACHE = {}


def _is_writable(path):
    if path not in _WRITABLE_CACHE:
        _WRITABLE_CACHE[path] = _writable(path)
    return _WRITABLE_CACHE[path]


def _vault_candidates():
    """密钥库候选目录：项目根（与机器人共用同一把库）→ EXE 目录 → %APPDATA%\\LuoTavern。

    旧实现固定用 EXE 同目录：EXE 放在 dist/ 时会和项目根的 vault 形成两份互不
    同步的密钥库，start_bot 又无条件回写 .env → 旧值覆盖新值。
    """
    out = []
    root = os.environ.get("LUOTAVERN_ROOT") or os.environ.get("LUOTAVERN_PROJECT")
    if root:
        out.append(os.path.abspath(root))
    out.append(exe_dir())
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(os.path.join(appdata, "LuoTavern"))
    seen, uniq = set(), []
    for c in out:
        key = os.path.normcase(c)
        if c and key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


def _vault_dir():
    """主密钥库目录（第一个可写候选）。"""
    for c in _vault_candidates():
        if _is_writable(c):
            return c
    return exe_dir()


def vault_path():
    return os.path.join(_vault_dir(), VAULT_FILENAME)


def legacy_vault_path():
    return os.path.join(_vault_dir(), LEGACY_VAULT_FILENAME)


def _obf(value):
    """加密单条密钥：Windows 上用 DPAPI（当前用户绑定），否则 base64 混淆。"""
    data = str(value).encode("utf-8")
    if _DPAPI_OK:
        try:
            blob = _dpapi_protect(data)
            return {"t": "dpapi", "v": base64.b64encode(blob).decode("ascii")}
        except Exception:
            pass
    return {"t": "b64", "v": base64.b64encode(data).decode("ascii")}


def _deobf(entry):
    if isinstance(entry, dict):
        kind = entry.get("t", "b64")
        raw = entry.get("v", "")
        if not raw:
            return ""
        try:
            data = base64.b64decode(str(raw).encode("ascii"))
        except Exception:
            return ""
        if kind == "dpapi":
            try:
                data = _dpapi_unprotect(data)
            except Exception:
                return ""
        try:
            return data.decode("utf-8")
        except Exception:
            return ""
    # 兼容旧版：纯 base64 字符串
    try:
        return base64.b64decode(str(entry).encode("ascii")).decode("utf-8")
    except Exception:
        return str(entry)


def _read_vault_file(path):
    """读一个密钥库文件 → {键: 明文}（损坏返回 {}）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, dict):
        return {}
    return {str(k): _deobf(v) for k, v in keys.items() if v}


def find_existing_vault():
    """在候选目录里找一份已存在的密钥库（新名优先于旧名），返回 (路径, keys)。"""
    found = []
    for d in _vault_candidates():
        for fn in (VAULT_FILENAME, LEGACY_VAULT_FILENAME):
            p = os.path.join(d, fn)
            if os.path.isfile(p):
                found.append(p)
    # 新名优先
    found.sort(key=lambda p: 0 if p.endswith(VAULT_FILENAME) else 1)
    for p in found:
        keys = _read_vault_file(p)
        if keys:
            return p, keys
    return None, {}


def load_vault(path=None):
    explicit = path is not None
    path = path or vault_path()
    if os.path.isfile(path):
        keys = _read_vault_file(path)
        if keys:
            return keys
    # 主路径没有（或损坏）：在其它候选目录里找一份并迁移过来，
    # 避免"EXE 在 dist 里就用 dist 的库、在别处就用另一份"的分裂
    src, keys = find_existing_vault()
    if not keys:
        # 同一目录下的旧名文件 deepdiscord_vault.json
        legacy = legacy_vault_path()
        if os.path.isfile(legacy):
            keys = _read_vault_file(legacy)
            src = legacy
    if keys and not explicit:
        try:
            save_vault(keys, path)
            return keys
        except OSError:
            return keys
    return keys


def save_vault(keys, path=None):
    path = path or vault_path()
    data = {
        "version": 2,
        "keys": {k: _obf(v) for k, v in (keys or {}).items() if v},
    }
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def import_from_project(project_root, vault=None, path=None):
    """扫描项目 .env 与 config*.json，把已有密钥导入密钥库。返回 {键: 值} 新增项。"""
    from qqbot import config as qconfig
    vault = dict(vault if vault is not None else load_vault())
    env = {}
    env_file = os.path.join(project_root, ".env")
    if os.path.isfile(env_file):
        env = qconfig.load_env(env_file)
    cfgs = []
    for fn in ("config.json", "config.qq.json", "config.discord.json"):
        p = os.path.join(project_root, fn)
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    cfgs.append(json.load(f))
            except Exception:
                pass

    def cfg_get(dotted):
        for cfg in cfgs:
            node = cfg
            ok = True
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    ok = False
                    break
                node = node[part]
            if ok and node not in (None, ""):
                return node
        return None

    imported = {}
    for envar, dotted in KNOWN_KEYS:
        if vault.get(envar):
            continue
        val = env.get(envar) or cfg_get(dotted)
        if val:
            vault[envar] = str(val)
            imported[envar] = str(val)
    if imported:
        save_vault(vault, path)
    return imported


def apply_to_project(project_root, vault=None, path=None):
    """把密钥库中的值写回项目 .env，供机器人读取。

    - 与 .env 完全相同的键不重复写（避免无意义的文件改写）
    - 会覆盖 .env 里**不同**的旧值时，打印一行提示（此前是静默覆盖，
      旧密钥库把新 .env 覆盖掉时用户完全无从察觉）
    返回实际写入的键列表。
    """
    from qqbot import config as qconfig
    vault = dict(vault if vault is not None else load_vault(path))
    env_path = os.path.join(project_root, ".env")
    try:
        current = qconfig.load_env(env_path)
    except Exception:
        current = {}
    values = {}
    overwritten = []
    for envar, _dotted in KNOWN_KEYS:
        val = vault.get(envar)
        if not val:
            continue
        cur = current.get(envar)
        if cur == val:
            continue
        if cur:
            overwritten.append(envar)
        values[envar] = val
    if overwritten:
        print("[密钥库] 已用密钥库的值覆盖 .env 中的不同旧值：%s"
              % "、".join(overwritten), flush=True)
    if values:
        qconfig.save_env(values, env_path)
    return list(values)


def remove_from_project(project_root, envar):
    """从项目 .env 中删除某个密钥键（密钥库中删除时同步清理，避免旧值残留）。"""
    from qqbot import config as qconfig
    path = os.path.join(project_root, ".env")
    if not os.path.isfile(path):
        return False
    env = qconfig.load_env(path)
    if str(envar) not in env:
        return False
    qconfig.save_env({str(envar): None}, path)
    return True


def vault_meta():
    """返回密钥库已知键的展示元数据（键名/标签/说明/是否必需）。"""
    return [dict(item) for item in KNOWN_KEY_META]
