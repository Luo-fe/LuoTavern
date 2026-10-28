# -*- coding: utf-8 -*-
"""本地记忆库 → TencentDB-Agent-Memory 一次性回填工具。

把 data/memory.db（当前 persona 生效路径）里的全部长期记忆与重要记忆
蒸馏进远端记忆引擎（MemoryCore 网关 / 腾讯云 Agent Memory 实例）。
幂等 best-effort：远端已存在的条目会重复蒸馏（append-only 日志），
本地数据不受影响；远端不可达时报错退出，可重试。

用法：
    python scripts/migrate_to_tencentdb.py                # 预览配置
    python scripts/migrate_to_tencentdb.py --apply        # 执行回填
    python scripts/migrate_to_tencentdb.py --apply --endpoint http://127.0.0.1:8420
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot import config as qconfig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="执行回填（默认仅预览）")
    ap.add_argument("--endpoint", default=None, help="覆盖 memory.tencentdb.endpoint")
    ap.add_argument("--api-key", default=None, help="覆盖网关密钥（也可用 .env TENCENTDB_MEMORY_API_KEY）")
    args = ap.parse_args()

    cfg = qconfig.load_config(os.path.join(ROOT, "config.json"))
    from qqbot import persona
    cur = persona.current()
    if cur and cur.get("card"):
        # 与 bot.py 一致：按当前 persona 的数据目录覆盖
        persona.ensure_persona_data(cur.get("name") or "默认")
        ov = persona.data_overrides(cfg, cur.get("name") or "默认")
        for key, value in ov.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value

    tcfg = cfg.setdefault("memory", {}).setdefault("tencentdb", {})
    if args.endpoint:
        tcfg["endpoint"] = args.endpoint
    if args.api_key:
        tcfg["api_key"] = args.api_key

    print("记忆库（本地）:", cfg["memory"].get("db_path"))
    print("远端 endpoint:", tcfg.get("endpoint"), "| team:", tcfg.get("team_id"),
          "| agent:", tcfg.get("agent_id"))
    if not tcfg.get("endpoint") and not tcfg.get("api_key"):
        print("[错误] 未配置 memory.tencentdb（endpoint / api_key），见 config.json")
        sys.exit(1)

    from qqbot.memstore.hybrid import HybridMemoryStore
    store = HybridMemoryStore(cfg["memory"], log=print)
    if not store.remote.available():
        print("[错误] TencentDB-Agent-Memory 传输不可用（SDK 未安装且 HTTP 不可达）")
        sys.exit(1)
    healthy = store.remote.probe()
    print("远端健康:", healthy)
    if not healthy:
        print("[错误] 网关 /health 探测失败：%s" % tcfg.get("endpoint"))
        sys.exit(1)

    if not args.apply:
        print("（预览：--apply 执行回填；本地数据只读不改）")
        return

    sent = store.migrate_local_to_remote(log=print)
    print("完成：已回填 %d 条" % sent)
    store.close()


if __name__ == "__main__":
    main()
