# -*- coding: utf-8 -*-
"""桌面管理台的进程归属：关窗不得杀别人的机器人；显式「停止」才按端口清理。

背景：`stop_bot` 会无条件按端口找 PID，只要命令行含 bot.py 就 taskkill，
于是"关闭管理台"会把用户自己用 `启动QQ机器人.bat` 起的机器人一起杀掉。
"""
import os
import subprocess
import sys
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from desktop_service import DesktopService  # noqa: E402


def make_service(bot_port=8008):
    """不走 __init__（避免探测系统 Python），只装出测试需要的字段。"""
    svc = DesktopService.__new__(DesktopService)
    svc.lock = threading.RLock()
    svc.bot_proc = None
    svc.bot_pid = None
    svc.bot_managed = False
    svc.bot_port = bot_port
    svc.bot_platform = None
    svc.bot_started_at = None
    svc.stop_event = threading.Event()
    svc._logs = []
    svc._seq = 0
    svc.napcat_started = False
    svc.napcat_proc = None
    return svc


class StopBotOwnershipTest(unittest.TestCase):
    def _patch(self, svc, kills):
        svc._port_open = staticmethod(lambda port: True)
        svc._wait_port_closed = lambda port, timeout=8: False
        svc._port_pid = staticmethod(lambda port: 4321)
        svc._pid_is_bot = staticmethod(lambda pid: True)
        svc._kill_pid = staticmethod(lambda pid: kills.append(pid) or True)

    def test_window_close_does_not_kill_unmanaged_bot(self):
        svc = make_service()
        kills = []
        self._patch(svc, kills)
        res = svc.stop_bot()          # 关窗/退出路径
        self.assertEqual(kills, [])
        self.assertTrue(res.get("unmanaged"))

    def test_explicit_stop_kills_unmanaged_bot_by_port(self):
        svc = make_service()
        kills = []
        self._patch(svc, kills)
        res = svc.stop_bot(explicit=True)   # 管理台「停止」按钮
        self.assertEqual(kills, [4321])
        self.assertTrue(res.get("ok"))

    def test_managed_bot_is_stopped(self):
        svc = make_service()
        kills = []
        self._patch(svc, kills)
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.pid = 111
        proc.wait.return_value = 0
        svc.bot_proc = proc
        svc.bot_pid = 111
        svc.bot_managed = True
        svc.stop_bot()
        proc.terminate.assert_called_once()

    def test_other_process_on_port_is_never_killed(self):
        svc = make_service()
        kills = []
        svc._port_open = staticmethod(lambda port: True)
        svc._wait_port_closed = lambda port, timeout=8: False
        svc._port_pid = staticmethod(lambda port: 999)
        svc._pid_is_bot = staticmethod(lambda pid: False)   # 不是本项目机器人
        svc._kill_pid = staticmethod(lambda pid: kills.append(pid) or True)
        svc.bot_managed = True
        svc.stop_bot()
        self.assertEqual(kills, [])


class PortPidParseTest(unittest.TestCase):
    def _netstat(self, text):
        with mock.patch.object(subprocess, "check_output", return_value=text):
            return DesktopService._port_pid(8008)

    def test_exact_port_match(self):
        text = ("  TCP    127.0.0.1:80080        0.0.0.0:0              LISTENING       111\n"
                "  TCP    127.0.0.1:8008         0.0.0.0:0              LISTENING       222\n")
        self.assertEqual(self._netstat(text), 222)

    def test_no_match(self):
        text = "  TCP    127.0.0.1:80080        0.0.0.0:0              LISTENING       111\n"
        self.assertIsNone(self._netstat(text))


class VaultLocationTest(unittest.TestCase):
    def test_vault_prefers_project_root(self):
        import tempfile
        import shutil
        import desktop_vault
        tmp = tempfile.mkdtemp()
        try:
            with mock.patch.dict(os.environ, {"LUOTAVERN_ROOT": tmp}, clear=False):
                desktop_vault._WRITABLE_CACHE.clear()
                self.assertEqual(os.path.normcase(desktop_vault._vault_dir()),
                                 os.path.normcase(os.path.abspath(tmp)))
        finally:
            desktop_vault._WRITABLE_CACHE.clear()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_apply_reports_overwritten_keys(self):
        import tempfile
        import shutil
        import desktop_vault
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
                f.write("DEEPSEEK_API_KEY=sk-old\n")
            written = desktop_vault.apply_to_project(
                tmp, vault={"DEEPSEEK_API_KEY": "sk-new", "ONEBOT_TOKEN": "tok"})
            self.assertIn("DEEPSEEK_API_KEY", written)
            from qqbot import config as qconfig
            env = qconfig.load_env(os.path.join(tmp, ".env"))
            self.assertEqual(env["DEEPSEEK_API_KEY"], "sk-new")
            self.assertEqual(env["ONEBOT_TOKEN"], "tok")
            # 值相同则不重复写
            again = desktop_vault.apply_to_project(
                tmp, vault={"DEEPSEEK_API_KEY": "sk-new"})
            self.assertEqual(again, [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
