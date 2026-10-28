# -*- coding: utf-8 -*-
"""本机 QQ 接入（官方 QQ + NapCat Framework）回归。

不启动真实 QQ：只验证模式选择、目录探测、启动分支、停止不误杀、健康检查。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from desktop_service import DesktopService  # noqa: E402


def make_framework(root, name="NapCat.Framework"):
    fw = os.path.join(root, name)
    os.makedirs(fw, exist_ok=True)
    for fn in ("napimain.exe", "napiLoader.bat", "napiloader.dll", "nativeLoader.cjs"):
        with open(os.path.join(fw, fn), "wb") as f:
            f.write(b"x")
    os.makedirs(os.path.join(fw, "config"), exist_ok=True)
    with open(os.path.join(fw, "config", "webui.json"), "w", encoding="utf-8") as f:
        json.dump({"port": 6099, "token": "t"}, f)
    return fw


def write_mode(root, mode):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "config.qq.json"), "w", encoding="utf-8") as f:
        json.dump({"login_mode": mode, "onebot_ws_url": "ws://127.0.0.1:3001"}, f)


class QQFrameworkModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.svc = DesktopService(bot_port=8123)
        self.svc.project_root = self.tmp
        self.svc.log = lambda *a, **k: None

    def test_framework_dir_project_and_parent(self):
        fw = make_framework(self.tmp)
        self.assertEqual(DesktopService._framework_dir(self.tmp), fw)
        child = os.path.join(self.tmp, "deepdiscord")
        os.makedirs(child, exist_ok=True)
        self.assertEqual(DesktopService._framework_dir(child), fw)

    def test_login_mode_explicit_and_default(self):
        write_mode(self.tmp, "framework")
        self.assertEqual(DesktopService._qq_login_mode(self.tmp), "framework")
        write_mode(self.tmp, "shell")
        self.assertEqual(DesktopService._qq_login_mode(self.tmp), "shell")
        write_mode(self.tmp, "")
        self.assertEqual(DesktopService._qq_login_mode(self.tmp), "shell")
        write_mode(self.tmp, "bogus")
        self.assertEqual(DesktopService._qq_login_mode(self.tmp), "shell")

    def test_ensure_framework_launches_loader_when_qq_not_running(self):
        fw = make_framework(self.tmp)
        write_mode(self.tmp, "framework")
        fake = mock.Mock(pid=4321)
        with mock.patch.object(self.svc, "_port_open", return_value=False), \
                mock.patch.object(self.svc, "_qq_process_pids", return_value=set()), \
                mock.patch.object(self.svc, "_framework_watch") as watch, \
                mock.patch.object(self.svc, "_start_napcat_health"), \
                mock.patch("desktop_service.subprocess.Popen", return_value=fake) as popen:
            res = self.svc._ensure_napcat(self.tmp)
        self.assertTrue(res["ok"])
        self.assertTrue(res["started"])
        self.assertEqual(res["mode"], "framework")
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["cmd.exe", "/c", "napiLoader.bat"])
        self.assertEqual(kwargs["cwd"], fw)
        self.assertEqual(kwargs["env"]["NAPCAT_INJECT_PATH"],
                         os.path.join(fw, "napiloader.dll"))
        self.assertTrue(watch.called)

    def test_ensure_framework_adopts_existing_qq_without_duplicate(self):
        fw = make_framework(self.tmp)
        write_mode(self.tmp, "framework")
        with mock.patch.object(self.svc, "_port_open", return_value=False), \
                mock.patch.object(self.svc, "_qq_process_pids", return_value={555}), \
                mock.patch.object(self.svc, "_framework_watch"), \
                mock.patch.object(self.svc, "_start_napcat_health"), \
                mock.patch("desktop_service.subprocess.Popen") as popen:
            res = self.svc._ensure_napcat(self.tmp)
        self.assertTrue(res["ok"])
        self.assertFalse(res["started"])
        self.assertTrue(res["adopted"])
        self.assertEqual(res["pids"], [555])
        self.assertFalse(popen.called)
        self.assertEqual(self.svc.framework_pids, {555})
        self.assertFalse(self.svc.framework_owned)

    def test_ensure_framework_reports_missing_framework(self):
        write_mode(self.tmp, "framework")
        with mock.patch.object(self.svc, "_port_open", return_value=False):
            res = self.svc._ensure_napcat(self.tmp)
        self.assertFalse(res["ok"])
        self.assertIn("NapCat.Framework", res["error"])

    def test_stop_napcat_keeps_official_qq_running(self):
        write_mode(self.tmp, "framework")
        self.svc.napcat_started = True
        self.svc.framework_pids = {555}
        with mock.patch.object(self.svc, "_qq_process_pids", return_value={555}), \
                mock.patch("desktop_service.subprocess.run") as run:
            self.svc._stop_napcat()
        self.assertFalse(self.svc.napcat_started)
        self.assertFalse(run.called, "停止机器人不能结束用户正在用的 QQ")

    def test_health_tick_restarts_when_qq_exited(self):
        write_mode(self.tmp, "framework")
        self.svc.napcat_started = True
        self.svc.framework_pids = {555}
        self.svc.framework_owned = True
        self.svc._framework_launch_ts = time.time() - 120
        with mock.patch.object(self.svc, "_port_open", return_value=False), \
                mock.patch.object(self.svc, "_qq_process_pids", return_value=set()), \
                mock.patch.object(self.svc, "_restart_napcat") as restart:
            self.svc._framework_health_tick(self.tmp)
        self.assertTrue(restart.called)

    def test_health_tick_waits_for_login_window(self):
        write_mode(self.tmp, "framework")
        self.svc.napcat_started = True
        self.svc.framework_pids = {555}
        self.svc.framework_owned = True
        self.svc._framework_launch_ts = time.time() - 10
        with mock.patch.object(self.svc, "_port_open", return_value=False), \
                mock.patch.object(self.svc, "_qq_process_pids", return_value={555}), \
                mock.patch.object(self.svc, "_restart_napcat") as restart:
            self.svc._framework_health_tick(self.tmp)
        self.assertFalse(restart.called, "登录窗口/启动宽限期内不得重启")

    def test_webui_config_reads_framework_dir(self):
        fw = make_framework(self.tmp)
        write_mode(self.tmp, "framework")
        self.assertEqual(DesktopService._napcat_webui_config(self.tmp), (6099, "t"))
        self.assertEqual(DesktopService._qq_login_mode(self.tmp), "framework")
        self.assertTrue(os.path.isfile(os.path.join(fw, "config", "webui.json")))


class AdminQQModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        make_framework(self.tmp)
        write_mode(self.tmp, "framework")
        self.patcher_base = mock.patch("qqbot.admin._base", return_value=self.tmp)
        self.patcher_base.start()
        self.addCleanup(self.patcher_base.stop)

    def test_settings_payload_exposes_mode(self):
        from qqbot.admin import _settings_payload
        payload = _settings_payload()
        self.assertEqual(payload["qq_login_mode"], "framework")
        self.assertEqual(payload["qq_login_mode_effective"], "framework")
        self.assertTrue(payload["qq_framework_available"])

    def test_save_settings_writes_platform_config(self):
        from qqbot import admin
        cfg_path = os.path.join(self.tmp, "config.json")
        with mock.patch.object(admin, "_config_path", return_value=cfg_path), \
                mock.patch.object(admin, "_apply_settings_hot", return_value={"ok": True}):
            admin._save_settings(None, {"platform": "qq", "qq_login_mode": "shell"})
        with open(os.path.join(self.tmp, "config.qq.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["login_mode"], "shell")

    def test_ui_contains_mode_selector(self):
        from qqbot.admin_ui import HTML
        self.assertIn('id="set-qq-login-mode"', HTML)
        self.assertIn('value="framework"', HTML)


if __name__ == "__main__":
    unittest.main()
