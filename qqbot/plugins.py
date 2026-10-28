# -*- coding: utf-8 -*-
"""轻量插件管理器：加载 plugins/ 目录下带 Plugin 类的脚本。"""
import importlib.util
import inspect
import os
import sys


class PluginManager:
    def __init__(self, bot):
        self.bot = bot
        self.plugins = []
        self._load_all()

    def _load_all(self):
        """扫描 plugins/ 目录加载带 Plugin 类的脚本（初始化与热重载共用）。"""
        self._enabled = self.bot.config.get("plugins", {})
        base = os.path.dirname(os.path.abspath(__file__))
        plugins_dir = os.path.join(base, "..", "plugins")
        if not os.path.isdir(plugins_dir):
            return
        if plugins_dir not in sys.path:
            sys.path.insert(0, plugins_dir)
        for fn in sorted(os.listdir(plugins_dir)):
            if not fn.endswith(".py") or fn.startswith("_"):
                continue
            name = fn[:-3]
            if not self._enabled.get(name, True):
                self.bot.log("[插件] %s 已在配置中禁用" % name)
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    "qqbot_plugin_" + name, os.path.join(plugins_dir, fn)
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                cls = getattr(mod, "Plugin", None)
                if cls is None:
                    self.bot.log("[插件] %s 缺少 Plugin 类，跳过" % name)
                    continue
                self.plugins.append(cls(self.bot))
                self.bot.log("[插件] 已加载: %s" % name)
            except Exception as exc:
                self.bot.log("[插件] %s 加载失败: %s" % (name, exc))

    def reload(self):
        """热重载：shutdown 现有插件 → 重新扫描加载（代码更新即时生效，不用重启进程）。

        返回新加载的插件数。核心模块需自行重绑 bot.memory 等引用。
        """
        self.shutdown()
        self.plugins = []
        self._load_all()
        return len(self.plugins)

    def on_message(self, user_id, text):
        """插件可返回一条立即回复；按顺序取第一个非空结果。"""
        for plugin in self.plugins:
            handler = getattr(plugin, "on_message", None)
            if handler:
                try:
                    result = handler(user_id, text)
                    if result:
                        return str(result)
                except Exception as exc:
                    self.bot.log("[插件] %s.on_message 出错: %s" % (getattr(plugin, "name", "?"), exc))
        return None

    def get(self, name):
        """按插件名取实例（供核心模块引用，如 memory）。"""
        for plugin in self.plugins:
            if getattr(plugin, "name", None) == name:
                return plugin
        return None

    @staticmethod
    def _accepts_kwarg(handler, name):
        try:
            return name in inspect.signature(handler).parameters
        except (TypeError, ValueError):
            return True  # 无法检查签名时按接受处理

    def on_message_out(self, user_id, text, management=False):
        """机器人发出消息时的钩子（记录/统计用，不拦截）。

        management=True 表示管理层消息（指令 ack/时间答复等），不入角色日志。
        旧插件 on_message_out 不接受 management 参数时自动降级为两参调用。
        """
        for plugin in self.plugins:
            handler = getattr(plugin, "on_message_out", None)
            if handler:
                try:
                    if self._accepts_kwarg(handler, "management"):
                        handler(user_id, text, management=management)
                    else:
                        handler(user_id, text)
                except Exception as exc:
                    self.bot.log("[插件] %s.on_message_out 出错: %s" % (getattr(plugin, "name", "?"), exc))

    def on_instruction(self, user_id, inst_raw):
        """【管理指令】钩子；插件可返回一条 ack；按顺序取第一个非空结果。"""
        for plugin in self.plugins:
            handler = getattr(plugin, "on_instruction", None)
            if handler:
                try:
                    result = handler(user_id, inst_raw)
                    if result:
                        return str(result)
                except Exception as exc:
                    self.bot.log("[插件] %s.on_instruction 出错: %s" % (getattr(plugin, "name", "?"), exc))
        return None

    def provide_context(self, user_id, text):
        parts = []
        for plugin in self.plugins:
            provider = getattr(plugin, "provide_context", None)
            if provider:
                try:
                    extra = provider(user_id, text)
                    if extra:
                        parts.append(str(extra))
                except Exception as exc:
                    self.bot.log("[插件] %s.provide_context 出错: %s" % (getattr(plugin, "name", "?"), exc))
        return "\n\n".join(parts)

    def provide_user_prefix(self, user_id, text, context=None):
        """按用户消息动态生成注入块（世界书二次强调等）；拼到 user 消息最前。

        context：近文文本（可选），供支持近文匹配的插件（如 worldbook.match_context）
        扫描；不支持的插件自动降级为两参调用。
        """
        parts = []
        for plugin in self.plugins:
            provider = getattr(plugin, "provide_user_prefix", None)
            if provider:
                try:
                    if self._accepts_kwarg(provider, "context"):
                        extra = provider(user_id, text, context=context)
                    else:
                        extra = provider(user_id, text)
                    if extra:
                        parts.append(str(extra))
                except Exception as exc:
                    self.bot.log("[插件] %s.provide_user_prefix 出错: %s" % (getattr(plugin, "name", "?"), exc))
        return "\n\n".join(parts)

    def on_turn(self, user_id, user_text, bot_text):
        """一轮完整对话（用户消息 + 机器人回复已成功发送）后的钩子。

        供记忆后端把对话同步到远端（TencentDB-Agent-Memory L0）；不拦截、不抛异常。
        """
        for plugin in self.plugins:
            handler = getattr(plugin, "on_turn", None)
            if handler:
                try:
                    handler(user_id, user_text, bot_text)
                except Exception as exc:
                    self.bot.log("[插件] %s.on_turn 出错: %s" % (getattr(plugin, "name", "?"), exc))

    def on_tick(self):
        for plugin in self.plugins:
            ticker = getattr(plugin, "on_tick", None)
            if ticker:
                try:
                    ticker()
                except Exception as exc:
                    self.bot.log("[插件] %s.on_tick 出错: %s" % (getattr(plugin, "name", "?"), exc))

    def shutdown(self):
        for plugin in self.plugins:
            closer = getattr(plugin, "on_shutdown", None)
            if closer:
                try:
                    closer()
                except Exception:
                    pass
