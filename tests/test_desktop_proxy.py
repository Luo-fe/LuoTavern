# -*- coding: utf-8 -*-
"""桌面管理台 → 机器人管理台 的聊天转发链端到端测试。"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from desktop_service import DesktopService
from tests.test_tavern import FakeLLM, make_bot


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DesktopProxyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # 管理台写操作鉴权：整个测试类固定同一 token，避免写真实 data/admin_token
        self.token_patcher = mock.patch(
            "qqbot.admin._admin_token", return_value="test-token")
        self.token_patcher.start()
        self.bot = make_bot(self.tmp)
        self.bot.llm = FakeLLM()
        threading.Thread(target=self.bot._worker, daemon=True).start()
        self.bot_port = free_port()
        from qqbot.admin import start_admin
        self.bot_server = start_admin(self.bot, port=self.bot_port, block=False)
        self.svc = DesktopService(bot_port=self.bot_port)

    def tearDown(self):
        try:
            self.token_patcher.stop()
        except Exception:
            pass
        try:
            self.bot_server.shutdown()
        except Exception:
            pass
        try:
            self.bot_server.server_close()
        except Exception:
            pass
        try:
            self.bot.stop_event.set()
        except Exception:
            pass
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wait_outbox(self, after=0, want=2, timeout=15):
        deadline = time.time() + timeout
        items = []
        while time.time() < deadline:
            items = self.svc.outbox(after=after)
            if len(items) >= want:
                return items
            time.sleep(0.1)
        return items

    def test_chat_proxy_pushes_input_and_reply_to_outbox(self):
        res = self.svc.chat("你好，桌面管理台")
        self.assertTrue(res.get("ok"), res)
        items = self._wait_outbox(after=0, want=2)
        self.assertGreaterEqual(len(items), 2)
        kinds = [m["kind"] for m in items]
        self.assertIn("input", kinds)
        self.assertIn("reply", kinds)
        self.assertEqual(items[0]["from"], "user")
        self.assertEqual(items[0]["text"], "你好，桌面管理台")
        self.assertTrue(any(m.get("from") == "bot" for m in items))

    def test_desktop_http_chain(self):
        """桌面管理台（bot=None）HTTP 层转发到机器人管理台。"""
        from desktop_service import SERVICE
        old_port = SERVICE.bot_port
        SERVICE.bot_port = self.bot_port
        try:
            desk_port = free_port()
            from qqbot.admin import start_admin
            import urllib.request
            server = start_admin(bot=None, port=desk_port, block=False)
            try:
                body = json.dumps({"text": "HTTP 链路测试"}).encode("utf-8")
                req = urllib.request.Request(
                    "http://127.0.0.1:%d/api/desktop/chat" % desk_port,
                    data=body, method="POST",
                    headers={"Content-Type": "application/json",
                             "X-Admin-Token": "test-token"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
            self.assertTrue(payload.get("ok"), payload)
            items = self._wait_outbox(after=0, want=2)
            texts = [m["text"] for m in items]
            self.assertIn("HTTP 链路测试", texts)
        finally:
            SERVICE.bot_port = old_port

    def test_log_reentrant_inside_lock(self):
        """start_bot/install_deps 持锁期间调用 self.log() 不得死锁。"""
        svc = DesktopService(bot_port=self.bot_port)
        done = []

        def worker():
            with svc.lock:
                svc.log("test", "持锁内写日志")
            done.append(True)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=3)
        self.assertFalse(t.is_alive(), "持锁内 self.log() 死锁（应使用 RLock）")
        self.assertEqual(done, [True])

    def test_default_project_detects_parent_of_dist(self):
        """EXE 放在 dist/ 下时，应自动向上找到含 bot.py 的项目根目录。"""
        import desktop_service
        import desktop_vault
        root = tempfile.mkdtemp()
        dist = os.path.join(root, "dist")
        os.makedirs(dist)
        with open(os.path.join(root, "bot.py"), "w", encoding="utf-8") as f:
            f.write("")
        try:
            with mock.patch.object(desktop_vault, "exe_dir", return_value=dist), \
                    mock.patch.object(desktop_service.os, "getcwd",
                                      return_value=dist), \
                    mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LUOTAVERN_PROJECT", None)
                self.assertEqual(
                    desktop_service.DesktopService._default_project(object()),
                    os.path.normpath(root))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class SupervisorAdoptTest(unittest.TestCase):
    """聊天指令切换角色后，桌面服务应接管新进程（端口存活检测）。"""

    @staticmethod
    def _dead_proc():
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc

    def test_port_pid_parses_netstat(self):
        svc = DesktopService(bot_port=8008)
        fake = (
            "TCP    127.0.0.1:8008    0.0.0.0:0    LISTENING    4321\r\n"
            "TCP    127.0.0.1:9000    0.0.0.0:0    LISTENING    999\r\n")
        with mock.patch("desktop_service.subprocess.check_output",
                        return_value=fake):
            self.assertEqual(svc._port_pid(8008), 4321)
            self.assertEqual(svc._port_pid(9000), 999)
            self.assertIsNone(svc._port_pid(7000))

    def test_bot_status_adopts_restarted_bot(self):
        svc = DesktopService(bot_port=8123)
        svc.bot_proc = self._dead_proc()
        svc.bot_managed = True
        svc.bot_platform = "qq"
        svc.bot_started_at = time.time()
        with mock.patch.object(svc, "_port_open", return_value=True), \
                mock.patch.object(svc, "_port_pid", return_value=4321), \
                mock.patch.object(svc, "_pid_is_bot", return_value=True):
            st = svc.bot_status()
        self.assertTrue(st["running"])
        self.assertEqual(st["pid"], 4321)
        self.assertEqual(st["platform"], "qq")
        self.assertTrue(st.get("restarted"))

    def test_stop_bot_kills_adopted_pid(self):
        svc = DesktopService(bot_port=8123)
        svc.bot_proc = self._dead_proc()
        svc.bot_managed = True
        svc.bot_pid = 4321
        with mock.patch.object(svc, "_port_open", return_value=True), \
                mock.patch.object(svc, "_pid_is_bot", return_value=True), \
                mock.patch.object(svc, "_wait_port_closed",
                                  return_value=True), \
                mock.patch.object(svc, "_kill_pid", return_value=True) as kill:
            r = svc.stop_bot()
        self.assertTrue(r["ok"])
        kill.assert_called_once_with(4321)
        self.assertFalse(svc.bot_managed)
        self.assertIsNone(svc.bot_pid)

    def test_maybe_adopt_sets_pid(self):
        svc = DesktopService(bot_port=8123)
        with mock.patch.object(svc, "_port_open", return_value=True), \
                mock.patch.object(svc, "_port_pid", return_value=777), \
                mock.patch.object(svc, "_pid_is_bot", return_value=True):
            svc._maybe_adopt()
        self.assertEqual(svc.bot_pid, 777)

    def test_napcat_noisy_lines_filtered(self):
        svc = DesktopService(bot_port=8123)
        self.assertFalse(svc._napcat_line_ok(
            "[ERROR:light_quic_transport_session.cc(47)] LightQuicTransportSession() init"))
        self.assertFalse(svc._napcat_line_ok("OnNewEncryptionKeyAvailable() handshake"))
        self.assertTrue(svc._napcat_line_ok("WebSocket服务: 127.0.0.1:3001 已启动"))
        self.assertTrue(svc._napcat_line_ok("软软 | 登录成功"))

    def test_start_bot_rejects_occupied_port(self):
        svc = DesktopService(bot_port=8123)
        svc.python = sys.executable
        with mock.patch.object(svc, "_port_open", return_value=True), \
                mock.patch.object(svc, "_port_pid", return_value=9999), \
                mock.patch.object(svc, "_read_admin_port", return_value=8008), \
                mock.patch("desktop_service.desktop_vault.apply_to_project") as apply_mock:
            r = svc.start_bot("qq")
        self.assertFalse(r["ok"])
        self.assertIn("8008", r["error"])
        self.assertIn("PID 9999", r["error"])
        apply_mock.assert_not_called()

    def test_napcat_not_relaunched_while_alive(self):
        """NapCat 进程在但端口未就绪时，等待而不是重复拉起（避免双实例互踢掉线）。"""
        svc = DesktopService(bot_port=8123)
        svc.napcat_proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        svc.napcat_started = True
        with mock.patch.object(svc, "_port_open", return_value=False), \
                mock.patch.object(svc, "_napcat_watch"), \
                mock.patch("desktop_service.subprocess.Popen",
                           side_effect=AssertionError("不应重复拉起 NapCat")) as popen:
            r = svc._ensure_napcat(os.path.dirname(ROOT) or ROOT)
        self.assertTrue(r["ok"])
        self.assertFalse(r["started"])
        self.assertTrue(r.get("pending"))
        popen.assert_not_called()
        proc = svc.napcat_proc
        svc._stop_napcat()
        if proc is not None:
            proc.kill()

    def test_napcat_login_status_parses(self):
        svc = DesktopService(bot_port=8123)
        with mock.patch.object(
                svc, "_napcat_api",
                return_value={"isLogin": False, "isOffline": False,
                              "qrcodeurl": "https://txz.qq.com/p?k=abc",
                              "loginError": ""}):
            st = svc.napcat_login_status("C:\\tmp")
        self.assertTrue(st["ok"])
        self.assertFalse(st["logged_in"])
        self.assertEqual(st["qrcode_url"], "https://txz.qq.com/p?k=abc")

    def test_napcat_qrcode_png_generates_data_url(self):
        svc = DesktopService(bot_port=8123)
        url = "https://txz.qq.com/p?k=abc"
        png = svc.napcat_qrcode_png(url)
        self.assertIsNotNone(png)
        self.assertTrue(png.startswith("data:image/png;base64,"))
        self.assertIsNone(svc.napcat_qrcode_png(""))

    def test_napcat_qrcode_cache_fallback(self):
        svc = DesktopService(bot_port=8123)
        root = tempfile.mkdtemp()
        nap_dir = os.path.join(root, "NapCat.Shell.Windows.Node")
        os.makedirs(os.path.join(nap_dir, "napcat", "cache"))
        with open(os.path.join(nap_dir, "index.js"), "w",
                  encoding="utf-8") as f:
            f.write("")
        with open(os.path.join(nap_dir, "napcat", "cache", "qrcode.png"),
                  "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfake")
        try:
            png = svc.napcat_qrcode_png_from_cache(root)
            self.assertTrue(png and png.startswith("data:image/png;base64,"))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_napcat_webui_url_reads_config(self):
        svc = DesktopService(bot_port=8123)
        root = tempfile.mkdtemp()
        nap_dir = os.path.join(root, "NapCat.Shell.Windows.Node")
        os.makedirs(os.path.join(nap_dir, "napcat", "config"))
        with open(os.path.join(nap_dir, "index.js"), "w",
                  encoding="utf-8") as f:
            f.write("")
        with open(os.path.join(nap_dir, "napcat", "config", "webui.json"),
                  "w", encoding="utf-8") as f:
            f.write('{"port": 6099, "token": "abc123"}')
        try:
            self.assertEqual(
                svc._napcat_webui_url(root),
                "http://127.0.0.1:6099/webui?token=abc123")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_wait_port_closed_returns_when_free(self):
        svc = DesktopService(bot_port=8123)
        with mock.patch.object(svc, "_port_open", return_value=False):
            self.assertTrue(svc._wait_port_closed(8123, timeout=1))

    def test_stop_bot_kills_lingering_port_owner(self):
        """stop_bot 杀完进程后端口仍占用时，按端口再补刀。"""
        svc = DesktopService(bot_port=8123)
        svc.bot_proc = self._dead_proc()
        svc.bot_managed = True
        svc.bot_pid = 111
        with mock.patch.object(svc, "_port_open", return_value=True), \
                mock.patch.object(svc, "_wait_port_closed",
                                  return_value=False), \
                mock.patch.object(svc, "_port_pid", return_value=222), \
                mock.patch.object(svc, "_pid_is_bot", return_value=True), \
                mock.patch.object(svc, "_kill_pid",
                                  return_value=True) as kill:
            r = svc.stop_bot()
        self.assertTrue(r["ok"])
        killed = [c.args[0] for c in kill.call_args_list]
        self.assertIn(111, killed)
        self.assertIn(222, killed)

    # ---------- NapCat 接收静默看门狗 ----------
    class _AliveProc:
        def poll(self):
            return None

    def _health_svc(self, silent_minutes=None, login_minutes=60):
        """健康检查夹具。

        login_minutes：模拟"已登录这么久"。新增了登录宽限期——刚登录不判假死，
        否则会把用户正在扫二维码的 NapCat 重启掉（真实事故）。
        """
        svc = DesktopService(bot_port=8123)
        svc.napcat_started = True
        svc.napcat_proc = self._AliveProc()
        if login_minutes is not None:
            svc._napcat_login_seen_ts = time.time() - login_minutes * 60
        if silent_minutes is not None:
            svc.napcat_last_receive_ts = \
                time.time() - silent_minutes * 60
        return svc

    def test_health_never_restarts_before_first_login(self):
        """本次启动从未登录成功（正在等扫码）→ 绝不自动重启。"""
        svc = self._health_svc(silent_minutes=None, login_minutes=None)
        self.assertIsNone(svc._napcat_login_seen_ts)
        with mock.patch.object(svc, "_port_open", return_value=True), \
                mock.patch.object(svc, "napcat_login_status",
                                  return_value={"ok": True,
                                                "logged_in": False,
                                                "offline": True}), \
                mock.patch.object(svc, "_bot_last_activity_age",
                                  return_value=60), \
                mock.patch.object(svc, "_restart_napcat") as restart:
            svc._napcat_health_tick(svc.project_root)
        self.assertFalse(restart.called)

    def test_health_restarts_when_receive_silent_but_bot_active(self):
        svc = self._health_svc(silent_minutes=9)
        with mock.patch.object(svc, "_port_open", return_value=True), \
                mock.patch.object(svc, "napcat_login_status",
                                  return_value={"ok": True,
                                                "logged_in": True,
                                                "offline": False}), \
                mock.patch.object(svc, "_bot_last_activity_age",
                                  return_value=60), \
                mock.patch.object(svc, "_restart_napcat") as restart:
            svc._napcat_health_tick(svc.project_root)
        self.assertTrue(restart.called)

    def test_health_skips_restart_when_user_idle(self):
        svc = self._health_svc(silent_minutes=9)
        with mock.patch.object(svc, "_port_open", return_value=True), \
                mock.patch.object(svc, "napcat_login_status",
                                  return_value={"ok": True,
                                                "logged_in": True,
                                                "offline": False}), \
                mock.patch.object(svc, "_bot_last_activity_age",
                                  return_value=60 * 60), \
                mock.patch.object(svc, "_restart_napcat") as restart:
            svc._napcat_health_tick(svc.project_root)
        self.assertFalse(restart.called)

    def test_health_restarts_immediately_when_webui_offline(self):
        """登录过后被踢下线（WebUI 报离线）→ 自动重启恢复。"""
        svc = self._health_svc()          # 已登录 60 分钟
        with mock.patch.object(svc, "_port_open", return_value=True), \
                mock.patch.object(svc, "napcat_login_status",
                                  return_value={"ok": True,
                                                "logged_in": False,
                                                "offline": True}), \
                mock.patch.object(svc, "_restart_napcat") as restart:
            svc._napcat_health_tick(svc.project_root)
        self.assertTrue(restart.called)

    def test_health_respects_cooldown(self):
        svc = self._health_svc(silent_minutes=9)
        svc._napcat_last_auto_restart = time.time()
        with mock.patch.object(svc, "_port_open", return_value=True), \
                mock.patch.object(svc, "napcat_login_status",
                                  return_value={"ok": True,
                                                "logged_in": True,
                                                "offline": False}), \
                mock.patch.object(svc, "_bot_last_activity_age",
                                  return_value=60), \
                mock.patch.object(svc, "_restart_napcat") as restart:
            svc._napcat_health_tick(svc.project_root)
        self.assertFalse(restart.called)


if __name__ == "__main__":
    unittest.main()
