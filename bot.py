# -*- coding: utf-8 -*-
"""机器人入口。

用法:
    python bot.py                 # 正常运行
    python bot.py --check         # 配置自检后退出
    python bot.py --test-connect  # 测试与 NapCat 的连接后退出
"""
import argparse
import os
import sys

from qqbot.config import load_config, load_platform_config, resolve_persona


def _key_hint(provider):
    """密钥库里的键名提示（与 desktop_vault.KNOWN_KEYS 对齐）。"""
    return {
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "glm": "GLM_API_KEY",
        "custom": "CUSTOM_API_KEY",
    }.get(str(provider), "对应的 API Key")


def _apply_overrides(cfg, overrides):
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value


def _force_utf8_stdio():
    """Windows 下 stdout 经管道默认是 GBK，而管理台按 UTF-8 解码 → 中文日志乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="角色扮演机器人")
    parser.add_argument("--check", action="store_true", help="配置自检后退出")
    parser.add_argument("--test-connect", action="store_true", help="测试连接后退出")
    parser.add_argument("--platform", choices=["qq", "discord"], default=None,
                        help="覆盖 config.json 的 platform（qq / discord）")
    parser.add_argument("--no-admin", action="store_true",
                        help="不启动本地管理台（默认随机器人启动 http://127.0.0.1:8008）")
    args = parser.parse_args()

    cfg = load_config()
    if args.platform:
        cfg["platform"] = args.platform
    # 按平台自动合并账户配置（config.qq.json / config.discord.json）
    cfg = load_platform_config(cfg, cfg.get("platform", "qq"))
    # 人格切换：应用当前 persona 的角色卡与独立数据目录
    from qqbot import persona

    cur = persona.current()
    if cur and cur.get("card"):
        card_path = cur["card"]
        if not os.path.isabs(card_path):
            card_path = os.path.normpath(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), card_path))
        if os.path.isfile(card_path):
            cfg["character_card"] = card_path
            persona.ensure_persona_data(cur.get("name") or "默认")
            _apply_overrides(cfg, persona.data_overrides(cfg, cur.get("name") or "默认"))
    errors = []
    # 按当前生效厂家校验（旧实现硬编码 deepseek_api_key：切到别的厂家后
    # 明明配好了也拒绝启动，打印的模型名也不是实际请求的模型）
    provider = str(cfg.get("llm_provider") or "deepseek")
    prov = (cfg.get("llm_providers") or {}).get(provider) or {}
    active_key = prov.get("api_key") or (
        cfg.get("deepseek_api_key") if provider == "deepseek" else "")
    active_base = prov.get("base_url") or (
        cfg.get("deepseek_base_url") if provider == "deepseek" else "")
    if not active_key or str(active_key).startswith("sk-请填写"):
        errors.append("厂家 %s 未配置 API Key（管理台密钥页 → %s）"
                      % (provider, _key_hint(provider)))
    if not active_base:
        errors.append("厂家 %s 未配置 API 地址" % provider)
    if not (prov.get("model") or cfg.get("model")):
        errors.append("厂家 %s 未配置模型名" % provider)
    if not cfg.get("allowed_user_ids"):
        errors.append("config.json 的 allowed_user_ids 未填写")
    if cfg.get("platform") == "discord" and not (cfg.get("discord", {}) or {}).get("token"):
        errors.append("config.json 的 discord.token 未填写")

    system_prompt, persona_path = resolve_persona(cfg)
    if not system_prompt:
        errors.append("未找到人格提示词（persona_file 文件不存在且 system_prompt 为空）")

    if args.check or errors:
        print("=== 配置自检 ===")
        print("厂家:", provider)
        print("模型:", prov.get("model") or cfg.get("model"))
        print("API 地址:", active_base)
        print("平台:", cfg.get("platform", "qq"))
        print("WS:", cfg.get("onebot_ws_url"))
        print("允许的用户:", cfg.get("allowed_user_ids"))
        print("人格来源:", persona_path or "config.system_prompt")
        print("插件:", {k: ("开" if v else "关") for k, v in cfg.get("plugins", {}).items()})
        print("身份映射:", cfg.get("identity", {}).get("map_path"))
        print("世界时钟:", cfg.get("time", {}).get("path"))
        print("记忆库:", cfg.get("memory", {}).get("db_path"))
        print("摘要目录:", cfg.get("summary_dir"))
        print("聊天记录目录:", cfg.get("chatlog", {}).get("dir"))
        for err in errors:
            print("[错误]", err)
        if not errors:
            # 校验数据目录可写，并顺带报告记忆后端状态
            from qqbot.bot_core import BotCore

            print("数据目录初始化……")
            bot = BotCore(cfg, system_prompt, persona_path)
            try:
                st = bot.memory.stats() if bot.memory is not None else {}
                backend = st.get("backend", "?")
                vec = st.get("vector") or {}
                remote = st.get("remote") or {}
                print("记忆后端:", backend)
                if not vec.get("enabled", True):
                    print("本地向量嵌入: 已在配置中关闭")
                elif vec.get("loaded"):
                    print("本地向量嵌入: 可用（本次已加载）")
                elif vec.get("model_cached"):
                    print("本地向量嵌入: 可用（模型已缓存，首次回忆时加载）")
                else:
                    print("本地向量嵌入: 不可用（模型未下载/依赖缺失 → 回忆走关键词路径）")
                    if vec.get("reason"):
                        print("  原因:", vec["reason"])
                if backend in ("hybrid", "tencentdb"):
                    print("TencentDB 网关:", remote.get("endpoint"),
                          "| 健康:", ("是" if remote.get("healthy") else "否"))
                else:
                    print("TencentDB 远端:", "未启用（memory.tencentdb.enabled=false）")
            finally:
                bot.plugins.shutdown()
            print("OK：身份映射/聊天记录/规则/时钟/封存/纠正/摘要 目录均已就绪")
        return 1 if errors else 0

    from qqbot.bot_core import BotCore

    bot = BotCore(cfg, system_prompt, persona_path)
    if not args.no_admin:
        admin_cfg = cfg.get("admin") or {}
        if admin_cfg.get("enable", True):
            from qqbot.admin import start_admin
            start_admin(bot, port=int(admin_cfg.get("port", 8008)))
    bot.run(test_connect=args.test_connect)
    return 0


if __name__ == "__main__":
    sys.exit(main())
