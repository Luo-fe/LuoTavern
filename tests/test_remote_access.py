# -*- coding: utf-8 -*-
"""远程访问（Tailscale/组网）与被动推送（方案 C）回归。

覆盖三件事：
1. Host/Origin 口径：只监听本机时外来 Host 一律拒绝；开启远程后 tailnet/私网放行、
   公网域名必须显式进 `admin.allowed_hosts`
2. 远程鉴权：非本机来源访问页面/接口都必须带 token（登录页**不含 token**）；
   通过后注入 token 并下发 cookie；失败过多触发限流
3. 推送：默认关闭不发请求；开启后 ntfy/Telegram 能带图发出；同类事件有最小间隔
"""
import base64
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot import admin  # noqa: E402
from qqbot.push import Pusher, build_pusher  # noqa: E402

TOKEN = "test-token-remote"


def _lan_ip():
    """本机对外地址（非回环）——用它访问可以让服务端看到"远程来源"。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HostOriginTest(unittest.TestCase):
    """Host/Origin 判定：本地模式严格，远程模式按可信网段放行。"""

    def test_local_bind_rejects_foreign_host(self):
        with mock.patch.object(admin, "_bind_host", return_value="127.0.0.1"):
            self.assertTrue(admin._host_name_ok("127.0.0.1"))
            self.assertTrue(admin._host_name_ok("localhost"))
            self.assertFalse(admin._host_name_ok("192.168.1.5"))
            self.assertFalse(admin._host_name_ok("evil.example.com"))

    def test_remote_bind_allows_tailnet_and_private(self):
        with mock.patch.object(admin, "_bind_host", return_value="0.0.0.0"):
            self.assertTrue(admin._host_name_ok("100.101.102.103"))   # Tailscale
            self.assertTrue(admin._host_name_ok("192.168.1.5"))       # 同 WiFi
            self.assertTrue(admin._host_name_ok("10.0.0.7"))
            self.assertFalse(admin._host_name_ok("evil.example.com"))  # 域名仍须白名单

    def test_allowed_hosts_whitelist(self):
        with mock.patch.object(admin, "_bind_host", return_value="0.0.0.0"), \
                mock.patch.object(admin, "_allowed_host_names",
                                  return_value={"myhost.ts.net"}):
            self.assertTrue(admin._host_name_ok("myhost.ts.net"))

    def test_origin_uses_same_rules(self):
        with mock.patch.object(admin, "_bind_host", return_value="0.0.0.0"):
            port = 8008
            self.assertTrue(admin._origin_allowed("", port))
            self.assertTrue(admin._origin_allowed("http://192.168.1.5:8008", port))
            self.assertFalse(admin._origin_allowed("http://evil.example.com", port))

    def test_best_local_ip_falls_back(self):
        with mock.patch.object(admin, "_tailnet_ip", return_value=""):
            ip = admin._best_local_ip()
        self.assertTrue(ip == "" or ip[0].isdigit())


class RemoteAuthTest(unittest.TestCase):
    """真起一个服务：用非回环地址访问，验证远程鉴权与限流。"""

    @classmethod
    def setUpClass(cls):
        cls.lan = _lan_ip()
        cls.port = _free_port()
        # 注意 _cfg_admin() 读的是磁盘上的 config.json —— 这里直接替换它，
        # 避免测试去改用户的真实配置
        cls.patches = [
            mock.patch.object(admin, "_bind_host", return_value="0.0.0.0"),
            mock.patch.object(admin, "_cfg_admin",
                              return_value={"auth_fail_limit": 3,
                                            "auth_fail_window": 300,
                                            "auth_lock_seconds": 300}),
        ]
        for p in cls.patches:
            p.start()
        admin._AUTH_FAILS.clear()
        cls.server = admin.start_admin(bot=None, port=cls.port, block=False,
                                       host="0.0.0.0")
        time.sleep(0.4)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
        except Exception:
            pass
        for p in cls.patches:
            p.stop()
        admin._AUTH_FAILS.clear()

    def _call(self, path, token=None, cookie=None, method="GET", body=None):
        req = urllib.request.Request(
            "http://%s:%d%s" % (self.lan, self.port, path),
            data=json.dumps(body).encode() if body is not None else None,
            method=method)
        if token:
            req.add_header("X-Admin-Token", token)
        if cookie:
            req.add_header("Cookie", cookie)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)

    def test_index_without_token_shows_login_page_without_secret(self):
        admin._AUTH_FAILS.clear()
        code, body, _ = self._call("/")
        self.assertEqual(code, 200)
        self.assertIn("需要访问令牌", body)
        self.assertNotIn(admin._admin_token(), body, "登录页绝不能带 token")

    def test_mobile_page_requires_token_then_sets_cookie(self):
        admin._AUTH_FAILS.clear()
        code, body, headers = self._call("/m?token=%s" % admin._admin_token())
        self.assertEqual(code, 200)
        self.assertIn("__ADMIN_TOKEN__", body.replace(admin._admin_token(), "__ADMIN_TOKEN__"))
        self.assertIn("lv_admin", headers.get("Set-Cookie", ""))
        code2, body2, _ = self._call("/m", cookie="lv_admin=%s" % admin._admin_token())
        self.assertEqual(code2, 200)
        self.assertIn(admin._admin_token(), body2)

    def test_mobile_page_without_token_is_gated(self):
        admin._AUTH_FAILS.clear()
        code, body, _ = self._call("/m")
        self.assertEqual(code, 200)
        self.assertNotIn(admin._admin_token(), body)

    def test_api_requires_token_from_remote(self):
        admin._AUTH_FAILS.clear()
        self.assertEqual(self._call("/api/status")[0], 403)
        self.assertEqual(self._call("/api/status", token=admin._admin_token())[0], 200)

    def test_write_api_requires_token_from_remote(self):
        admin._AUTH_FAILS.clear()
        self.assertEqual(
            self._call("/api/desktop/chat", method="POST", body={"text": "x"})[0], 403)

    def test_throttle_locks_after_repeated_failures(self):
        admin._AUTH_FAILS.clear()
        codes = [self._call("/?token=wrong")[0] for _ in range(6)]
        self.assertIn(429, codes, "连续错误 token 必须触发限流")
        admin._AUTH_FAILS.clear()

    def test_local_token_flow_unchanged(self):
        """本机（回环）访问依旧无需 token，桌面端不受影响。"""
        code, body, _ = self._call_loopback("/api/status", token=admin._admin_token())
        self.assertEqual(code, 200)

    def _call_loopback(self, path, token=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path))
        if token:
            req.add_header("X-Admin-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)


class PushTest(unittest.TestCase):
    """推送：默认静默、可带图、防刷屏、渠道适配。"""

    def setUp(self):
        self.calls = []

        class FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            self.calls.append({"method": req.get_method(), "url": req.full_url,
                               "headers": dict(req.headers), "body": req.data or b""})
            return FakeResp()

        # 只替换 push 模块的引用，其他后台线程的 HTTP 请求不混入推送断言。
        patcher = mock.patch("qqbot.push.urllib", SimpleNamespace(request=SimpleNamespace(
            Request=urllib.request.Request, urlopen=fake_urlopen)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _wait(self, n=1, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end and len(self.calls) < n:
            time.sleep(0.05)

    def _pusher(self, cfg):
        """构造推送器并登记 close：避免上一用例的后台线程在本用例里继续发请求。"""
        p = Pusher(cfg)
        self.addCleanup(p.close)
        return p

    def test_disabled_never_sends(self):
        p = self._pusher({"push": {"enable": False, "ntfy": {"url": "u", "topic": "t"}}})
        self.assertFalse(p.enabled())
        self.assertFalse(p.notify("kick", "t", "x"))
        self.assertTrue(p.flush(1.0))
        self.assertEqual(self.calls, [])

    def test_enabled_without_endpoint_is_noop(self):
        p = self._pusher({"push": {"enable": True, "provider": "ntfy", "ntfy": {}}})
        self.assertFalse(p.enabled())
        self.assertFalse(p.notify("kick", "t", "x"))
        self.assertEqual(self.calls, [])

    def test_ntfy_sends_image_as_attachment(self):
        p = self._pusher({"push": {"enable": True, "provider": "ntfy", "min_interval": 0,
                                   "ntfy": {"url": "https://ntfy.sh", "topic": "t1",
                                            "token": "tk"}}})
        self.assertTrue(p.notify("qr", "标题", "正文", image=b"\x89PNG123"))
        self.assertTrue(p.flush(), "推送必须在超时前发完")
        self._wait()
        call = self.calls[0]
        self.assertEqual(call["method"], "PUT")
        self.assertTrue(call["url"].endswith("/t1"))
        self.assertEqual(call["headers"].get("Filename"), "qrcode.png")
        self.assertEqual(call["body"], b"\x89PNG123")
        self.assertIn("Bearer tk", call["headers"].get("Authorization", ""))

    def test_telegram_uploads_photo_multipart(self):
        png = b"\x89PNG\x00\x01"
        p = self._pusher({"push": {"enable": True, "provider": "telegram",
                                   "min_interval": 0,
                                   "telegram": {"token": "1:a", "chat_id": "9"}}})
        self.assertTrue(p.notify("qr", "t", "文案",
                                 image="data:image/png;base64,"
                                       + base64.b64encode(png).decode()))
        self.assertTrue(p.flush())
        self._wait()
        call = self.calls[0]
        self.assertTrue(call["url"].endswith("/sendPhoto"))
        self.assertIn(b"png", call["body"])
        self.assertIn(b"9", call["body"])

    def test_min_interval_throttles_same_kind(self):
        p = self._pusher({"push": {"enable": True, "provider": "ntfy", "min_interval": 600,
                                   "ntfy": {"url": "https://ntfy.sh", "topic": "t"}}})
        self.assertTrue(p.notify("kick", "t", "a"))
        self.assertFalse(p.notify("kick", "t", "b"), "同类事件必须按最小间隔节流")
        self.assertTrue(p.notify("qr", "t", "c"), "不同类事件互不影响")

    def test_webhook_includes_base64_image(self):
        p = self._pusher({"push": {"enable": True, "provider": "webhook", "min_interval": 0,
                                   "webhook": {"url": "https://example.com/hook"}}})
        self.assertTrue(p.notify("qr", "t", "x", image=b"PNG"))
        self.assertTrue(p.flush())
        self._wait()
        payload = json.loads(self.calls[0]["body"].decode())
        self.assertEqual(base64.b64decode(payload["image_base64"]), b"PNG")
        self.assertEqual(payload["image_name"], "qrcode.png")

    def test_close_stops_sender_and_rejects_new_notify(self):
        p = self._pusher({"push": {"enable": True, "provider": "ntfy", "min_interval": 0,
                                   "ntfy": {"url": "https://ntfy.sh", "topic": "t"}}})
        self.assertTrue(p.notify("qr", "t", "x"))
        self.assertTrue(p.flush())
        p.close()
        self.assertFalse(p.notify("qr", "t", "y"), "已关闭的推送器不应再接受推送")
        self.assertEqual(len(self.calls), 1)

    def test_build_pusher_helper(self):
        self.assertIsInstance(build_pusher({"push": {"enable": False}}), Pusher)


class DesktopPushHookTest(unittest.TestCase):
    """被踢/二维码生成时，桌面服务要调用推送（且失败不影响主流程）。"""

    def setUp(self):
        from desktop_service import DesktopService
        self.D = DesktopService
        self.svc = DesktopService.__new__(DesktopService)
        self.logged = []
        self.svc.log = lambda kind, text=None: self.logged.append((kind, text))
        self.svc._napcat_kicked_ts = None
        self.svc._napcat_offline_seen_ts = None
        self.svc.napcat_last_receive_ts = None
        self.svc.project_root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.svc.project_root, ignore_errors=True))
        self.svc._push_obj = None

    def test_kick_triggers_push(self):
        pushed = []
        fake = mock.Mock()
        fake.enabled.return_value = True
        fake.config = {"push": {"on_kick": True, "on_qr": True}}
        fake.notify.side_effect = lambda *a, **k: pushed.append(a)
        self.svc._pusher = lambda: fake
        self.svc._log_napcat_line(
            "10-11 00:40:33 [error] [KickedOffLine] 你的账号当前登录已失效，请重新登录。")
        self.assertTrue(pushed, "被踢必须推送")
        self.assertTrue(any("被踢" in str(m) for m in self.logged if m[1]))

    def test_qr_line_triggers_push_with_image(self):
        pushed = []
        fake = mock.Mock()
        fake.enabled.return_value = True
        fake.config = {"push": {"on_kick": True, "on_qr": True}}
        fake.notify.side_effect = lambda kind, title, text, image=None: pushed.append(
            (kind, image))
        self.svc._pusher = lambda: fake
        # 造一个 NapCat 二维码缓存文件
        napcat = os.path.join(self.svc.project_root, "NapCat.Shell.Windows.Node")
        os.makedirs(os.path.join(napcat, "napcat", "cache"), exist_ok=True)
        with open(os.path.join(napcat, "index.js"), "w") as f:
            f.write("x")
        with open(os.path.join(napcat, "napcat", "cache", "qrcode.png"), "wb") as f:
            f.write(b"\x89PNGQ")
        self.svc._log_napcat_line("二维码已保存到 E:\\x\\qrcode.png")
        self.assertEqual(pushed and pushed[0][0], "qr")
        self.assertEqual(pushed[0][1], b"\x89PNGQ", "二维码图片要一起推过去")

    def test_push_failure_does_not_break_logging(self):
        def boom():
            raise RuntimeError("no network")
        self.svc._pusher = boom
        self.svc._log_napcat_line("[KickedOffLine] 登录已失效")
        self.assertTrue(any("被踢" in str(m[1]) for m in self.logged))


if __name__ == "__main__":
    unittest.main()
