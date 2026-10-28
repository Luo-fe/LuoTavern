# -*- coding: utf-8 -*-
"""管理台加固回归：读接口鉴权、Host 校验、安全响应头、导出路径白名单。

背景：`do_GET` 曾完全无鉴权（token 明文写在首页、记忆/角色卡/QQ 登录二维码
可直接被本机任意进程读走），导出白名单只判"在项目目录内"，
于是 `save_path=<项目>/.env` 就能把 .env 覆盖成 JSON。
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot import admin  # noqa: E402
from qqbot.card_manager import _write_export  # noqa: E402

TOKEN = "test-token-1234"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PollNoiseTest(unittest.TestCase):
    """前端轮询接口不该刷日志（真实问题会被淹没），但出错必须照常记录。"""

    def _handler(self):
        from qqbot import admin
        cls = admin.make_handler(None, token="t")
        h = cls.__new__(cls)
        h.path = "/api/outbox?after=3"
        h.requestline = "GET /api/outbox?after=3 HTTP/1.1"
        h.client_address = ("127.0.0.1", 1)
        h.request_version = "HTTP/1.1"
        h.logged = []
        h.log_message = lambda fmt, *a: h.logged.append(fmt % a)
        return h

    def test_normal_poll_is_silent(self):
        h = self._handler()
        h.log_request(200, 12)
        self.assertEqual(h.logged, [])

    def test_poll_errors_are_logged(self):
        h = self._handler()
        h.log_request(500, 12)
        self.assertTrue(h.logged, "轮询接口出错必须留下日志")

    def test_non_poll_paths_are_logged(self):
        h = self._handler()
        h.path = "/api/cards"
        h.requestline = "GET /api/cards HTTP/1.1"
        h.log_request(200, 12)
        self.assertTrue(h.logged)

    def test_chat_poll_only_while_chat_view_visible(self):
        ui = open(os.path.join(ROOT, "qqbot", "admin_ui.py"), encoding="utf-8").read()
        body = ui[ui.index("async function pollChat()"):]
        body = body[:body.index("const log=")]
        self.assertIn('view.classList.contains("active")', body)
        self.assertIn("document.hidden", body)


class SettingsPayloadTest(unittest.TestCase):
    """GET /api/settings 必须覆盖"保存时会提交的键"，否则保存即静默重置。

    真实事故：载荷不返回 status_bar → 设置页按默认值渲染 → 一保存就把用户
    自定义的字段/必填/模板覆盖掉（reply.mode 也因同类原因被写成默认值）。
    """

    # 管理台 saveSettings() 提交的键（改前端时同步这里）
    SAVED_KEYS = ("llm_provider", "llm_providers", "platform", "bot_name",
                  "user_names", "owner_name", "temperature", "max_context_chars",
                  "max_history_turns", "reply", "status_bar", "plugins",
                  "memory", "recall", "worldbook_files")

    def test_payload_covers_every_saved_key(self):
        from qqbot.admin import _settings_payload
        payload = _settings_payload()
        missing = [k for k in self.SAVED_KEYS if k not in payload]
        self.assertEqual(missing, [], "载荷缺少这些键，保存时会被重置：%s" % missing)

    def test_status_bar_payload_is_not_empty(self):
        from qqbot.admin import _settings_payload
        sb = _settings_payload().get("status_bar")
        self.assertIsInstance(sb, dict)
        self.assertIn("fields", sb)
        self.assertIn("required", sb)

    def test_status_bar_roundtrip_keeps_custom_values(self):
        """载荷读到的值原样提交回去，配置不得变化（幂等保存）。"""
        from qqbot.admin import _merge_settings, _settings_payload
        payload = _settings_payload()
        cfg = {"status_bar": dict(payload["status_bar"])}
        before = dict(cfg["status_bar"])
        _merge_settings(cfg, {"status_bar": dict(payload["status_bar"])})
        self.assertEqual(cfg["status_bar"]["fields"], before["fields"])
        self.assertEqual(cfg["status_bar"]["required"], before["required"])
        self.assertEqual(cfg["status_bar"]["template"], before["template"])


class AdminHardeningTest(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        handler = admin.make_handler(None, token=TOKEN)
        from http.server import ThreadingHTTPServer
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, token=None, host=None, method="GET", body=None):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(body).encode() if body is not None else None,
            method=method)
        if token:
            req.add_header("X-Admin-Token", token)
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8"), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8"), dict(exc.headers)

    # ---------- 读接口鉴权 ----------

    def test_api_get_requires_token(self):
        code, body, _ = self.call("/api/status")
        self.assertEqual(code, 403, body)
        code, body, _ = self.call("/api/status", token=TOKEN)
        self.assertEqual(code, 200, body)

    def test_sensitive_get_endpoints_require_token(self):
        for path in ("/api/memories", "/api/important", "/api/cards",
                     "/api/desktop/logs", "/api/desktop/status"):
            code, _body, _ = self.call(path)
            self.assertEqual(code, 403, "%s 未鉴权" % path)

    def test_index_page_is_public_and_injects_token(self):
        code, body, headers = self.call("/")
        self.assertEqual(code, 200)
        self.assertIn('window.__ADMIN_TOKEN__="%s"' % TOKEN, body)
        # 安全响应头
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertIn("frame-ancestors 'none'",
                      headers.get("Content-Security-Policy", ""))

    def test_cover_images_stay_public_for_img_tags(self):
        # <img src> 无法带自定义头：封面接口必须保持公开（否则列表缩略图全挂）
        code, _body, _ = self.call("/api/cards/cover?file=x.png")
        self.assertIn(code, (400, 404))

    # ---------- Host / Origin ----------

    def test_foreign_host_is_rejected(self):
        code, _body, _ = self.call("/", host="evil.example.com")
        self.assertEqual(code, 403)

    def test_local_host_is_accepted(self):
        code, _body, _ = self.call("/", token=TOKEN, host="127.0.0.1:%d" % self.port)
        self.assertEqual(code, 200)

    def test_cross_origin_write_is_rejected(self):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/chat" % self.port,
            data=b'{"text":"hi"}', method="POST")
        req.add_header("X-Admin-Token", TOKEN)
        req.add_header("Origin", "http://evil.example.com")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        self.assertEqual(code, 403)

    # ---------- 导出路径 ----------

    def test_export_path_denies_sensitive_targets(self):
        for name in (".env", "config.json", "data/admin_token",
                     "qqbot/bot_core.py", "run.bat", "luotavern_vault.json"):
            p = os.path.join(ROOT, name)
            with self.assertRaises(ValueError, msg=name):
                admin._check_export_path(p)
        # 正常落点仍然允许
        ok = admin._check_export_path(os.path.join(ROOT, "exports", "card.json"))
        self.assertTrue(ok.endswith("card.json"))

    def test_write_export_enforces_extension(self):
        target = os.path.join(self.tmp, ".env")
        with self.assertRaises(ValueError):
            _write_export(b"{}", target, want_ext="json")
        good = os.path.join(self.tmp, "card.json")
        res = _write_export(b"{}", good, want_ext="json")
        self.assertTrue(res["ok"])
        self.assertTrue(os.path.isfile(good))

    def test_log_redaction(self):
        from desktop_service import redact
        line = "WebUi User Panel Url http://127.0.0.1:6099/webui?token=abc123&x=1"
        self.assertNotIn("abc123", redact(line))
        self.assertIn("token=***", redact(line))
        self.assertNotIn("sk-abcdef123456", redact("key: sk-abcdef123456"))


if __name__ == "__main__":
    unittest.main()
