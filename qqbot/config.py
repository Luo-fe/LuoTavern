# -*- coding: utf-8 -*-
"""配置加载与人格预设读取。"""
import json
import os

CONFIG_PATH = "config.json"
ENV_FILE = ".env"
PLATFORM_CONFIG_FILES = {
    "qq": "config.qq.json",
    "discord": "config.discord.json",
}

# 密钥/连接信息统一走 .env，避免明文散落在配置文件里
ENV_MAP = {
    "deepseek_api_key": "DEEPSEEK_API_KEY",
    "deepseek_base_url": "DEEPSEEK_BASE_URL",
    "llm_providers.openai.api_key": "OPENAI_API_KEY",
    "llm_providers.anthropic.api_key": "ANTHROPIC_API_KEY",
    "llm_providers.glm.api_key": "GLM_API_KEY",
    "llm_providers.custom.api_key": "CUSTOM_API_KEY",
    "discord.token": "DISCORD_TOKEN",
    "discord.proxy": "DISCORD_PROXY",
    "onebot_ws_url": "ONEBOT_WS_URL",
    "onebot_token": "ONEBOT_TOKEN",
    "memory.tencentdb.api_key": "TENCENTDB_MEMORY_API_KEY",
}

DEFAULTS = {
    "onebot_ws_url": "ws://127.0.0.1:3001",
    "onebot_token": "",
    "platform": "qq",
    "discord": {
        "token": "",
        "reply_to_mention_only": True,
        "proxy": "",
    },
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.com",
    "llm_provider": "deepseek",
    "llm_providers": {
        "deepseek": {
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "api_style": "openai",
        },
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "api_style": "openai",
        },
        "anthropic": {
            "base_url": "https://api.anthropic.com",
            "model": "claude-sonnet-4-20250514",
            "api_style": "anthropic",
        },
        "glm": {
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "model": "glm-4-flash",
            "api_style": "openai",
        },
        "custom": {
            "base_url": "",
            "model": "",
            "api_style": "openai",
        },
    },
    "model": "deepseek-v4-flash",
    "temperature": 0.8,
    "timeout_seconds": 180,
    # LLM 调用可靠性：异常/空输出自动重试；推理型模型额外预留 token 给推理
    "llm_retries": 1,
    "llm_reasoning_headroom": 2000,
    "max_history_turns": 20,
    "max_context_chars": 20000,
    "max_history_chars": 0,
    "keep_recent_full": 6,
    "summary_max_chars": 2000,
    "max_reply_tokens": 2000,
    "message_chunk_chars": 3500,
    "typing_indicator": True,
    "debug_errors": False,
    "allow_all": False,
    "allowed_user_ids": [],
    "owner_user_ids": [],
    "bot_name": "",
    "user_names": "",
    "non_owner_rule_file": "",
    "system_prompt": "你是一个温柔、耐心、贴心的 AI 伙伴。",
    "persona_dir": "prompt",
    "persona_file": "",
    "admin": {
        "enable": True,
        "port": 8008,
        "open_browser": False,
        # 监听地址：默认仅本机。远程访问（如手机 + Tailscale）时改成 "0.0.0.0"
        # 或 tailnet 地址；远程来源**必须带 token**（本机来源行为不变）
        "bind": "127.0.0.1",
        # 远程访问允许的 Host 名（域名或 IP）。Tailscale 的 100.64.0.0/10
        # 地址无需列出（代码自动放行）
        "allowed_hosts": [],
        # 远程鉴权失败限流：窗口内失败 N 次 → 锁定 M 秒
        "auth_fail_limit": 8,
        "auth_fail_window": 300,
        "auth_lock_seconds": 300,
    },
    # 被动推送（方案 C）：被踢下线 / 需要扫码时把二维码推到手机
    "push": {
        "enable": False,
        "provider": "ntfy",          # ntfy | telegram | bark | webhook
        "ntfy": {"url": "https://ntfy.sh", "topic": "", "token": ""},
        "telegram": {"token": "", "chat_id": ""},
        "bark": {"url": "https://api.day.app", "key": ""},
        "webhook": {"url": ""},
        "on_kick": True,             # 被 QQ 踢下线时推
        "on_qr": True,               # 需要扫码且拿到新二维码时推
        "min_interval": 120,         # 同类推送最小间隔（秒），防刷屏
    },
    # 回复长度档位：以字符数为单位，启动时自动换算成 token
    "reply": {
        "mode": "medium",
        "presets": {
            "short": {"max_chars": 300, "desc": "简短回应"},
            "medium": {"max_chars": 900, "desc": "常规长度"},
            "long": {"max_chars": 2000, "desc": "长篇演出"},
        },
    },
    "plugins": {"memory": True, "proactive": False, "chatlog": True,
                "vault_sync": True, "worldbook": True},
    "worldbook": {
        "dir": "book",
        "files": [],
        "base_mode": "constant",
        "base_max_chars": 2000,
        "emphasis_max_chars": 1500,
        "case_sensitive": False,
        "match_context": False,
        "match_context_turns": 4,
        "synonyms": {},
        "deny_keys": [],
    },
    "vault": {
        "enable": True,
        "dir": "data/obsidian_vault",
        "check_seconds": 60,
    },
    "memory": {
        # 记忆后端：auto（TencentDB 连通→混合，否则本地）/ local / hybrid / tencentdb
        # TencentDB-Agent-Memory 说明见 docs/tencentdb-agent-memory-research.md
        "backend": "auto",
        "db_path": "data/memory.db",
        "max_memories_per_user": 200,
        "recall_top_k": 3,
        "max_context_memories": 5,
        "persistent_importance": 9,
        "persistent_max": 3,
        "vector": {
            "enable": True,
            "model": "BAAI/bge-small-zh-v1.5",
            "top_k": 3,
            "min_score": 0.3,
            "trigger_min_score": 0.46,
            # 启动时后台预热向量模型（仅当模型已缓存；不会静默联网下载）
            "prewarm": True,
        },
        "tencentdb": {
            # 启用远端记忆引擎的显式开关：auto 只在 enabled=true 时探测，
            # 避免默认配置下每次启动都去探测 127.0.0.1:8420
            "enabled": False,
            # 自托管：MemoryCore 网关 http://127.0.0.1:8420
            # 腾讯云托管：https://memory.tdai.tencentyun.com（api_key = 实例密钥）
            "endpoint": "http://127.0.0.1:8420",
            "api_key": "",           # 密钥走 .env：TENCENTDB_MEMORY_API_KEY
            "service_id": "default",
            "team_id": "deepdiscord",
            "agent_id": "luotavern",
            "session_prefix": "rp",
            "api_version": "v3",
            "timeout_seconds": 10,
            "enable_recall": True,   # 远端语义召回是否参与回忆
            "sync_chatlog": True,    # 每轮对话同步到远端 L0（自动蒸馏 L1-L3）
        },
    },
    "usage_log": "data/usage.json",
    "proactive": {
        "interval_minutes": 30,
        "idle_minutes": 30,
        "max_messages": 4,
        "quiet_start": "23:00",
        "quiet_end": "08:00",
        "trigger_windows": {
            "周一": ["早上", "中午", "晚上"],
            "周二": ["早上", "中午", "晚上"],
            "周三": ["早上", "中午", "晚上"],
            "周四": ["早上", "中午", "晚上"],
            "周五": ["早上", "下午"],
        },
        "prompt_file": "",
        "score_file": "data/proactive_score.json",
        "messages": [],
    },
    "shared": {
        "dir": "data/shared",
    },
    "chatlog": {
        "dir": "data/chatlog",
        "recall_days": 7,
        "recall_top_k": 5,
        "recall_cmd_top_k": 15,
    },
    "compress": {
        "enable": True,
        "threshold_turns": 40,
        "keep_turns": 10,
        "notify_user": True,
        "max_output_tokens": 2600,
        "prompt_file": "会话压缩提示词.md",
        "prompt": "",
    },
    "commands": {
        "session_reset": ["清空会话", "重置会话", "重置对话", "重新开始", "失忆", "/clear", "翻篇"],
        "summarize": ["总结一下", "帮我总结", "回顾一下", "概括一下", "理一理", "总结"],
        # 未识别的【…】是否短路并回"不认识这个指令"。
        # 关掉（false）后，未识别方括号内容会被当成角色扮演动作（【叹气】等）交给模型
        "unknown_instruction_blocks": True,
    },
    # 状态栏：软件内建要求——每次角色扮演回复末尾都必须带 <状态栏> 区块
    # （不依赖角色卡是否写过；卡片写过则沿用其格式，缺字段时补齐）
    "status_bar": {
        "enable": True,
        "inherit_card": True,
        # 要求出现的字段（顺序即建议顺序）
        "fields": ["时间", "地点", "表情", "姿态", "服装", "身体反应", "内心"],
        # 缺其中任一字段即判定不合格 → 触发修复
        "required": ["表情", "服装", "身体反应"],
        # 自定义整段模板（留空用内建模板；{name} 替换为角色名）
        "template": "",
        # 不合格时是否调用模型修复（廉价补块 → 失败再整段重生成）
        "repair": True,
        "cheap_max_tokens": 400,
    },
    "identity": {
        "map_path": "data/identity_map.json",
        "owner_name": "主人",
        "dream_name": "",
        "reserved_names": ["主人"],
        "protect_names": [],
        "undeclared_reply": "【请先告诉机器人你是谁～发【我是xxx】就行】",
        "unknown_instruction_reply": "【不认识这个指令哦～】",
    },
    "time": {
        "path": "data/time/clock.json",
        "default_weekday": "周一",
        "default_time_of_day": "早上",
        "enable": True,
        "undo_size": 10,
    },
    "rules": {"dir": "data/rules"},
    "recall": {
        "enable": True,
        "judge": True,
        "synthesize": True,
        "default_days": 7,
        "max_days": 30,
        "top_k": 8,
        "explicit_top_k": 15,
        "max_hit_chars": 300,
        "cross_identity": True,
        "mention_top_k": 3,
        "rumination_prob": 0.1,
        "consistent_top": 2,
        # 召回强化：被想起来的记忆 importance+1（不删除、不衰减任何条目）
        "reinforce": True,
        # 跨身份【回忆】的身份数上限（避免 O(身份 × 记忆) 的热点）
        "force_identity_max": 4,
        # 语义精度守门：向量引擎给出有信心的命中时，其余记忆必须有真实字面重叠
        "vector_guard": True,
        "vector_guard_overlap": 0.5,
        # 每轮注入块（重要记忆/回忆/时间衔接…）的字符预算（含世界书强调的预留）
        "max_inject_chars": 800,
        # 常驻记忆块的字符上限（超出按行丢弃，不腰斩）
        "persistent_max_chars": 600,
    },
    "affection": {"max_score": 400, "owner_initial": 300, "guest_initial": 100},
    "sleep": {
        "keywords": [],
        "chunk_size": 40,
        "auto_threshold_turns": 10,
        "max_important": 100,
        # 推理型模型会把大量 token 花在推理上，给足上限避免 content 为空
        "max_output_tokens": 8000,
        "summary_max_tokens": 8000,
        "jump_to_night": True,
    },
    "correct": {"dir": "data/corrected"},
    "seal": {"dir": "data/seal"},
    "summary_dir": "data/summary",
    "archive_dir": "data/archive",
}


def root_path():
    """项目根目录：优先取管理台注入的 LUOTAVERN_ROOT（EXE 便携模式）。"""
    return os.path.normpath(
        os.environ.get("LUOTAVERN_ROOT")
        or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def env_path():
    """项目根目录下的 .env 路径。"""
    return os.path.join(root_path(), ENV_FILE)


def load_env(path=None):
    """读取 .env，返回 {KEY: value}；文件不存在时返回空 dict。

    用 utf-8-sig 读：记事本"UTF-8 带 BOM"另存会让键名变成 '\\ufeffKEY'，
    旧实现下所有密钥静默失效。
    """
    path = path or env_path()
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip().lstrip("\ufeff")] = val.strip().strip('"').strip("'")
    return env


def _env_value_ok(key, value):
    """写入 .env 前的自检：换行/引号会让文件被解析成多行或截断。"""
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ValueError("%s 含换行符，无法安全写入 .env" % key)
    if '"' in text:
        raise ValueError("%s 含双引号，无法安全写入 .env" % key)
    return text


def save_env(values, path=None):
    """把 {ENV_KEY: value} 写回 .env（保留无关键，原子替换）。

    value 传 None 表示从 .env 中删除该键（密钥管理中删除时同步清理）。
    """
    path = path or env_path()
    env = load_env(path)
    for key, val in (values or {}).items():
        if val is None:
            env.pop(str(key), None)
            continue
        env[str(key)] = _env_value_ok(key, val)
    lines = ["# 敏感凭据文件：请勿提交到版本库", "# 由 LuoTavern 管理台自动维护", ""]
    ordered = [v for v in ENV_MAP.values() if v in env]
    for key in ordered:
        lines.append("%s=%s" % (key, _quote_env_value(env.pop(key))))
    for key in sorted(env):
        lines.append("%s=%s" % (key, _quote_env_value(env[key])))
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")
    os.replace(tmp, path)


def _quote_env_value(value):
    """含首尾空格的值加引号，避免读回时被 strip 掉（值内不允许引号，见 _env_value_ok）。"""
    text = str(value)
    if text != text.strip():
        return '"%s"' % text
    return text


# 这些字段以 .env 为准（配置文件里的残留旧值不得静默压制新密钥）
ENV_WINS = {
    "deepseek_api_key",
    "llm_providers.openai.api_key",
    "llm_providers.anthropic.api_key",
    "llm_providers.glm.api_key",
    "llm_providers.custom.api_key",
    "discord.token",
    "onebot_token",
    "memory.tencentdb.api_key",
}


def apply_env(cfg, env=None):
    """把 .env 中的值填入配置。

    - 普通字段：仅当配置里为空时填充（配置文件里显式填写的值优先）
    - ENV_WINS 里的密钥字段：**始终以 .env 为准**，并在覆盖时打印一行提示，
      避免"在管理台换了密钥却一直用 config.json 里的旧明文"这种静默失效
    """
    env = load_env() if env is None else env
    for dotted, envar in ENV_MAP.items():
        val = env.get(envar)
        if val is None or val == "":
            continue
        parts = dotted.split(".")
        target = cfg
        for p in parts[:-1]:
            target = target.setdefault(p, {})
        cur = target.get(parts[-1])
        if dotted in ENV_WINS:
            if cur and cur != val:
                print("[config] %s 以 .env 为准（config.json 里存在不同的旧值，建议清空）"
                      % dotted, flush=True)
            target[parts[-1]] = val
        elif not cur:
            target[parts[-1]] = val
    return cfg


def _deep_merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _resolve_project_file(name):
    """相对文件名按项目根解析（不依赖启动时的 CWD）。

    `python C:\\path\\bot.py` 从别的目录启动时，旧实现会把 config 当成不存在，
    静默用默认配置启动（表现为"密钥/允许用户全部丢失"）。
    """
    name = str(name or "")
    if not name or os.path.isabs(name):
        return name
    cand = os.path.normpath(os.path.join(root_path(), name))
    if os.path.exists(cand):
        return cand
    return name                             # 项目根没有时保留原相对路径（兼容旧用法）


def load_config(path=None, use_env=True):
    """加载配置。

    use_env=False 时不注入项目 .env（脚本/测试/子代理等不该顺带拿到真实密钥；
    旧实现无论如何都会注入，任何调用方都能读到明文凭据）。
    """
    if path is None:
        path = os.path.join(root_path(), CONFIG_PATH)
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            _deep_merge(cfg, json.load(f))
    cfg.setdefault("allowed_user_ids", [])
    return apply_env(cfg) if use_env else cfg


def load_platform_config(cfg, platform):
    """把平台专属配置文件（config.qq.json / config.discord.json）合并进 cfg。

    平台专属字段（QQ 的 onebot_ws_url/onebot_token、Discord 的 discord.*、
    allowed_user_ids / owner_user_ids 等）放在平台文件里，启动时自动加载，
    无需手动改动公共 config.json。
    """
    fname = _resolve_project_file(PLATFORM_CONFIG_FILES.get(platform or ""))
    if fname and os.path.exists(fname):
        with open(fname, "r", encoding="utf-8") as f:
            _deep_merge(cfg, json.load(f))
    return apply_env(cfg)


def atomic_write_json(path, data):
    """原子写 JSON（管理台保存配置用）。"""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def resolve_persona(cfg):
    """按配置返回系统提示词：优先 persona_dir 下的 md 文件，其次 system_prompt。"""
    persona_file = cfg.get("persona_file") or ""
    if persona_file:
        base = cfg.get("persona_dir") or "prompt"
        path = os.path.join(base, persona_file)
        if not os.path.isabs(base):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", path)
        path = os.path.normpath(path)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip(), path
    return cfg.get("system_prompt", ""), None
