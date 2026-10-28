# -*- coding: utf-8 -*-
"""Obsidian vault 同步插件：按周期检查 memory.db 变更，变化时重建 vault 导出。

- on_tick 节流检查（默认每 60 秒一次），数据库变更才重建
- WAL 模式下主库 mtime 只在 checkpoint 变化：用 data_version +
  -wal 文件 mtime 联合探测（与 memstore 的向量缓存失效一致）
- 退出时兜底同步一次
- 手动触发：python memory_ui.py vault
"""
import os
import sqlite3
import time

from qqbot.vault_export import export_vault


class Plugin:
    name = "vault_sync"

    def __init__(self, bot):
        self.bot = bot
        self.cfg = bot.config.get("vault", {})
        self._last_check = 0
        self._last_mtime = 0
        if self.cfg.get("enable", True):
            bot.log("[vault] Obsidian 同步已启用：%s" % self.vault_dir)

    @property
    def vault_dir(self):
        d = self.cfg.get("dir", "data/obsidian_vault")
        if not os.path.isabs(d):
            d = os.path.normpath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", d))
        return d

    @property
    def db_path(self):
        p = self.bot.config.get("memory", {}).get("db_path", "data/memory.db")
        if not os.path.isabs(p):
            p = os.path.normpath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", p))
        return p

    def _db_version(self):
        """数据库变更签名：data_version（任何连接提交都会自增）+ -wal 文件 mtime。"""
        sig = []
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                sig.append(("v", conn.execute("PRAGMA data_version").fetchone()[0]))
            finally:
                conn.close()
        except Exception:
            pass
        try:
            sig.append(("m", os.path.getmtime(self.db_path)))
            sig.append(("w", os.path.getmtime(self.db_path + "-wal")))
        except OSError:
            pass
        return tuple(sig)

    def sync(self):
        """立即重建导出（供 on_tick / on_shutdown / 外部调用）。"""
        # 先取签名再导出：导出耗时期间发生的写入会被归到"已同步"的旧签名上，
        # 那次变更就被永久跳过（直到下一次写入才补上）
        before = self._db_version()
        try:
            n, ids = export_vault(self.db_path, self.vault_dir)
            self._last_mtime = before
            self.bot.log("[vault] 已导出 %d 个文件（身份：%s）"
                         % (n, "、".join(ids) if ids else "无"))
        except Exception as exc:
            self.bot.log("[vault] 导出失败：%s" % exc)

    def on_tick(self):
        if not self.cfg.get("enable", True):
            return
        interval = float(self.cfg.get("check_seconds", 60))
        now = time.time()
        if now - self._last_check < interval:
            return
        self._last_check = now
        version = self._db_version()
        if version and version != self._last_mtime:
            self.sync()

    def on_shutdown(self):
        if self.cfg.get("enable", True):
            self.sync()
