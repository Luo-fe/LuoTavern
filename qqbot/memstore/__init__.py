# -*- coding: utf-8 -*-
"""记忆存储包：统一 seam（MemoryStore）+ 多后端适配器。

build_store(config, bot=None)：
  backend=local      → SQLiteMemoryStore（离线，与旧版行为一致）
  backend=hybrid     → HybridMemoryStore（本地 SQLite source of truth +
                       TencentDB-Agent-Memory 语义引擎；远端故障自动只剩本地）
  backend=tencentdb  → 同 hybrid（别名；远端为主、本地保留元数据/好感度）
  backend=auto（默认）→ TencentDB 已配置且连通 → hybrid；否则 local

所有调用方（recall / sleep_ritual / bot_core / worldbook / memory_ui / admin）
只依赖 MemoryStore 接口，后端可整体替换。
"""
from qqbot.memstore.base import KeywordMixin, MemoryStore  # noqa: F401  (供外部复用)
from qqbot.memstore.local import SCHEMA, SQLiteMemoryStore  # noqa: F401


def _tencent_cfg_ready(cfg):
    """TencentDB-Agent-Memory 是否已显式启用并配置（含 .env 注入的密钥）。

    auto 模式要求 enabled=true：默认配置不探测网络，也不被本机无关的
    8420 端口服务误触发。
    """
    tc = (cfg.get("tencentdb") or {}) if isinstance(cfg, dict) else {}
    if not bool(tc.get("enabled")):
        return False
    return bool(tc.get("endpoint") or tc.get("api_key") or tc.get("memory_id"))


def _probe_tencent(cfg, timeout=3.0):
    """快速探测 TencentDB-Agent-Memory 服务连通性（HTTP 健康检查）。"""
    try:
        from qqbot.memstore.tencentdb import TencentAgentMemoryStore
        store = TencentAgentMemoryStore(cfg, probe_only=True)
        return store.probe(timeout=timeout)
    except Exception:
        return False


def build_store(config, bot=None, log=None, prefer=None):
    """按配置构造记忆后端；prefer 可显式指定 local/tencentdb/hybrid（测试用）。"""
    cfg = dict(config) if isinstance(config, dict) else {}
    backend = str(prefer or cfg.get("backend", "auto")).strip().lower()
    if backend not in ("auto", "local", "tencentdb", "hybrid"):
        backend = "auto"
    if backend == "local":
        return SQLiteMemoryStore(cfg, log=log)
    if backend in ("tencentdb", "hybrid", "auto"):
        if not _tencent_cfg_ready(cfg):
            if backend == "auto":
                return SQLiteMemoryStore(cfg, log=log)
            raise ValueError("memory.backend=%s 需要配置 memory.tencentdb" % backend)
        try:
            from qqbot.memstore.hybrid import HybridMemoryStore
            if backend in ("hybrid", "tencentdb"):
                # 远端为主：本地仍保留元数据/好感度（remote-only 会失去可编辑性）
                return HybridMemoryStore(cfg, log=log)
            # auto：服务连通才启用混合，否则离线本地（不让远程故障拖垮启动）
            if _probe_tencent(cfg):
                return HybridMemoryStore(cfg, log=log)
            if log:
                log("[memory] TencentDB-Agent-Memory 不可达，回退本地存储")
            return SQLiteMemoryStore(cfg, log=log)
        except Exception as exc:
            if backend == "auto":
                if log:
                    log("[memory] TencentDB 后端构造失败（%s），回退本地存储" % exc)
                return SQLiteMemoryStore(cfg, log=log)
            raise ValueError("memory.backend=%s 后端构造失败: %s" % (backend, exc))
    return SQLiteMemoryStore(cfg, log=log)
