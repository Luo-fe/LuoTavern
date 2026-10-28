# -*- coding: utf-8 -*-
"""NapCat 扫码登录：二维码刷新必须"说真话"，过期缓存图不能被当成当前二维码。

真实故障（用户报告"二维码过期，刷新无效"）：
1. NapCat 内部登录流程卡死时，`/QQLogin/RefreshQRcode` 依然回 `code=0 success`，
   但 `CheckLoginStatus` 的 `qrcodeurl` 仍为空 → 旧实现丢掉返回值、直接回状态，
   前端继续显示缓存里的旧二维码，看起来就是"刷新无效"
2. `cache/qrcode.png` 是一小时前的死码，旧实现无条件读它当"当前二维码"
   → 用户对着过期码一直扫
3. NapCat 卡死时唯一可靠解法是重启它；旧实现没有任何"重启 NapCat"入口
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from desktop_service import QRCODE_MAX_AGE, DesktopService  # noqa: E402


class _FakeService(DesktopService):
    """把 NapCat WebUI API 与进程枚举替换成可控桩。"""

    def __init__(self, responses, pids=(), root=None):
        self._responses = list(responses)       # 依次返回的 API dict
        self._pids = list(pids)
        self._calls = []
        self.project_root = root or os.getcwd()
        self.log = lambda *a, **k: None

    # --- 覆盖被替换的方法 ---
    def napcat_login_status(self, root=None):
        return dict(self._responses.pop(0) if self._responses else {"ok": True,
                                                                   "logged_in": False,
                                                                   "qrcode_url": ""})

    def _napcat_api(self, root, path, body=None):
        self._calls.append(path)
        return {"code": 0, "message": "success"}

    @classmethod
    def _napcat_dir(cls, root):
        return cls._tmp_dir          # 每个用例 setUp 里指到临时 NapCat 目录

    def invalidate_qrcode_cache(self, root=None):
        self._invalidated = True
        return super().invalidate_qrcode_cache(root)


class RefreshTruthTest(unittest.TestCase):
    """刷新必须如实反映"有没有新码"。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _FakeService._tmp_dir = self.tmp
        self.svc = _FakeService([
            # before：没有二维码
            {"ok": True, "logged_in": False, "qrcode_url": ""},
            # after：NapCat 假成功，依然没有二维码（真实故障场景）
            {"ok": True, "logged_in": False, "qrcode_url": ""},
        ], root=self.tmp)
        # 造一个"一小时前的死码"
        cache_dir = os.path.join(self.tmp, "napcat", "cache")
        os.makedirs(cache_dir, exist_ok=True)
        self.cache = os.path.join(cache_dir, "qrcode.png")
        with open(self.cache, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"stale")
        old = time.time() - 3600
        os.utime(self.cache, (old, old))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_refresh_reports_failure_when_no_new_qr(self):
        st = self.svc.napcat_refresh_qrcode(self.tmp)
        self.assertFalse(st["refresh_ok"])
        self.assertIn("重启 NapCat", st["refresh_error"])
        self.assertIn("/QQLogin/RefreshQRcode", self.svc._calls)

    def test_refresh_failure_invalidates_stale_cache(self):
        self.svc.napcat_refresh_qrcode(self.tmp)
        self.assertFalse(os.path.isfile(self.cache),
                         "刷新失败后必须作废旧码，否则前端继续显示死码")

    def test_refresh_reports_success_on_new_qr(self):
        svc = _FakeService([
            {"ok": True, "logged_in": False, "qrcode_url": "https://old"},
            {"ok": True, "logged_in": False, "qrcode_url": "https://new"},
        ], root=self.tmp)
        st = svc.napcat_refresh_qrcode(self.tmp)
        self.assertTrue(st["refresh_ok"])
        self.assertNotIn("refresh_error", st)

    def test_refresh_after_login_is_not_failure(self):
        svc = _FakeService([
            {"ok": True, "logged_in": True, "qrcode_url": ""},
            {"ok": True, "logged_in": True, "qrcode_url": ""},
        ], root=self.tmp)
        st = svc.napcat_refresh_qrcode(self.tmp)
        self.assertTrue(st["refresh_ok"])
        self.assertEqual(st.get("refresh_note"), "已登录")


class StaleCacheTest(unittest.TestCase):
    """缓存二维码只在新鲜时可用。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _FakeService._tmp_dir = self.tmp
        self.svc = _FakeService([], root=self.tmp)
        cache_dir = os.path.join(self.tmp, "napcat", "cache")
        os.makedirs(cache_dir, exist_ok=True)
        self.cache = os.path.join(cache_dir, "qrcode.png")
        with open(self.cache, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"qr")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_cache_is_used(self):
        png = self.svc.napcat_qrcode_png_from_cache(self.tmp)
        self.assertTrue(png.startswith("data:image/png;base64,"))
        self.assertLess(self.svc.qrcode_cache_age(self.tmp), QRCODE_MAX_AGE)

    def test_stale_cache_is_rejected(self):
        old = time.time() - (QRCODE_MAX_AGE + 60)
        os.utime(self.cache, (old, old))
        self.assertIsNone(self.svc.napcat_qrcode_png_from_cache(self.tmp),
                          "过期二维码不能作为当前二维码显示")

    def test_missing_cache_returns_none(self):
        os.remove(self.cache)
        self.assertIsNone(self.svc.napcat_qrcode_png_from_cache(self.tmp))
        self.assertIsNone(self.svc.qrcode_cache_age(self.tmp))


class NapcatPidGuardTest(unittest.TestCase):
    """重启 NapCat 只能杀本项目 NapCat 的 node 进程（按命令行双重匹配）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.napcat_dir = os.path.join(self.tmp, "NapCat.Shell.Windows.Node")
        os.makedirs(self.napcat_dir)
        with open(os.path.join(self.napcat_dir, "index.js"), "w") as f:
            f.write("// napcat")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pids(self, procs):
        import desktop_service as ds

        class FakeProc:
            def __init__(self, info):
                self.info = info

        class FakeIter:
            def __iter__(self_inner):
                return iter([FakeProc(p) for p in procs])

        class FakePsutil:
            NoSuchProcess = type("E", (Exception,), {})
            AccessDenied = type("E", (Exception,), {})

            @staticmethod
            def process_iter(_attrs):
                return FakeIter()

        real = ds.psutil
        ds.psutil = FakePsutil()
        try:
            return DesktopService._napcat_pids(self.tmp)
        finally:
            ds.psutil = real

    def test_matches_our_napcat_process(self):
        pids = self._pids([
            {"pid": 111, "name": "node.exe",
             "cmdline": ["node.exe", os.path.join(self.napcat_dir, "napcat", "napcat.mjs")]},
        ])
        self.assertEqual(pids, [111])

    def test_ignores_other_node_processes(self):
        pids = self._pids([
            {"pid": 222, "name": "node.exe", "cmdline": ["node.exe", "web", "--port", "3080"]},
            {"pid": 333, "name": "python.exe",
             "cmdline": ["python", os.path.join(self.napcat_dir, "napcat", "napcat.mjs")]},
            {"pid": 444, "name": "node.exe",
             "cmdline": ["node.exe", "E:\\other\\napcat\\napcat.mjs"]},
        ])
        self.assertEqual(pids, [], "别的 Node/别的 NapCat 目录都不能被误杀")

    def test_path_mentioned_in_log_text_is_not_matched(self):
        """命令行里"出现过该路径字符串"不等于就是 NapCat（曾误判诊断进程）。"""
        pids = self._pids([
            {"pid": 555, "name": "node.exe",
             "cmdline": ["node.exe", "runner.js", "--", "echo", self.napcat_dir + "\\x"]},
        ])
        self.assertEqual(pids, [])


class MultiInstanceTest(unittest.TestCase):
    """多实例是真实事故（WebUI 打不开 / 快速登录超时 / 掉线）的根源，必须能识别清理。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.napcat_dir = os.path.join(self.tmp, "NapCat.Shell.Windows.Node")
        os.makedirs(self.napcat_dir)
        with open(os.path.join(self.napcat_dir, "index.js"), "w") as f:
            f.write("// napcat")
        self.terminated = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch(self, rows):
        """安装伪 psutil：process_iter 返回给定表，Process 记录被结束的 PID。"""
        import desktop_service as ds
        terminated = self.terminated

        class FakeProc:
            def __init__(self, info):
                self.info = info

            def cwd(self):
                return self.info.get("cwd", "")

        class FakeIter:
            def __iter__(self_inner):
                return iter([FakeProc(r) for r in rows])

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid

            def terminate(self):
                terminated.append(self.pid)

            def wait(self, timeout=None):
                return 0

            def kill(self):
                terminated.append(self.pid)

        class FakePsutil:
            NoSuchProcess = type("E", (Exception,), {})
            AccessDenied = type("E", (Exception,), {})
            CONN_LISTEN = "LISTEN"

            @staticmethod
            def process_iter(_attrs):
                return FakeIter()

            @staticmethod
            def Process(pid):
                return FakeProcess(pid)

            @staticmethod
            def net_connections(_kind):
                return []
        real = ds.psutil
        ds.psutil = FakePsutil()
        self.addCleanup(lambda: setattr(ds, "psutil", real))

    def _svc(self, port=3001, port_pid=None):
        svc = DesktopService.__new__(DesktopService)
        svc.project_root = self.tmp
        svc.log = lambda *a, **k: None
        svc._napcat_port = lambda root: port
        svc._napcat_port_pid = lambda p: port_pid
        return svc

    def test_finds_relative_index_js_launcher_by_cwd(self):
        """`node index.js`（相对路径）命令行里没有目录，只能靠 cwd 判据找到。"""
        self._patch([
            {"pid": 100, "name": "node.exe", "cmdline": ["node.exe", "index.js"],
             "cwd": self.napcat_dir, "ppid": 1},
        ])
        self.assertEqual(DesktopService._napcat_pids(self.tmp), [100])
        self.assertEqual(DesktopService.napcat_launcher_pids(self.tmp), [100])

    def test_includes_children(self):
        self._patch([
            {"pid": 100, "name": "node.exe", "cmdline": ["node.exe", "index.js"],
             "cwd": self.napcat_dir, "ppid": 1},
            {"pid": 101, "name": "node.exe",
             "cmdline": ["node.exe", os.path.join(self.napcat_dir, "napcat", "napcat.mjs")],
             "cwd": self.napcat_dir, "ppid": 100},
        ])
        self.assertEqual(DesktopService._napcat_pids(self.tmp), [100, 101])

    def test_pidfile_pid_is_included(self):
        """记录过的启动器 PID 即使判据都不匹配也要能被结束。"""
        os.makedirs(os.path.join(self.tmp, "data"), exist_ok=True)
        with open(DesktopService._napcat_pidfile(self.tmp), "w") as f:
            f.write("777")
        self._patch([
            {"pid": 777, "name": "node.exe", "cmdline": ["node.exe", "weird-name"],
             "cwd": r"C:\other", "ppid": 1},
        ])
        self.assertIn(777, DesktopService._napcat_pids(self.tmp))

    def test_unrelated_node_not_matched(self):
        self._patch([
            {"pid": 200, "name": "node.exe", "cmdline": ["node.exe", "web", "--port", "3080"],
             "cwd": r"C:\code", "ppid": 1},
            {"pid": 201, "name": "python.exe", "cmdline": ["python", "index.js"],
             "cwd": self.napcat_dir, "ppid": 1},
        ])
        self.assertEqual(DesktopService._napcat_pids(self.tmp), [])

    def test_cleanup_kills_extra_launcher_keeps_port_owner(self):
        self._patch([
            {"pid": 100, "name": "node.exe", "cmdline": ["node.exe", "index.js"],
             "cwd": self.napcat_dir, "ppid": 1},
            {"pid": 101, "name": "node.exe", "cmdline": ["node.exe", "index.js"],
             "cwd": self.napcat_dir, "ppid": 1},
        ])
        killed = self._svc(port_pid=101)._napcat_cleanup_duplicates(self.tmp)
        self.assertEqual(killed, [100], "只清理多余实例，端口占用者必须保留")
        self.assertEqual(self.terminated, [100])

    def test_cleanup_keeps_newest_when_nobody_owns_port(self):
        self._patch([
            {"pid": 100, "name": "node.exe", "cmdline": ["node.exe", "index.js"],
             "cwd": self.napcat_dir, "ppid": 1},
            {"pid": 101, "name": "node.exe", "cmdline": ["node.exe", "index.js"],
             "cwd": self.napcat_dir, "ppid": 1},
        ])
        killed = self._svc(port_pid=None)._napcat_cleanup_duplicates(self.tmp)
        self.assertEqual(killed, [100])
        self.assertEqual(DesktopService.napcat_launcher_pids(self.tmp), [100, 101])

    def test_cleanup_kills_child_tree_of_extra_instance(self):
        self._patch([
            {"pid": 100, "name": "node.exe", "cmdline": ["node.exe", "index.js"],
             "cwd": self.napcat_dir, "ppid": 1},
            {"pid": 102, "name": "node.exe",
             "cmdline": ["node.exe", os.path.join(self.napcat_dir, "napcat", "napcat.mjs")],
             "cwd": self.napcat_dir, "ppid": 100},
            {"pid": 101, "name": "node.exe", "cmdline": ["node.exe", "index.js"],
             "cwd": self.napcat_dir, "ppid": 1},
        ])
        killed = self._svc(port_pid=101)._napcat_cleanup_duplicates(self.tmp)
        self.assertEqual(killed, [100, 102], "多余实例的整个进程树都要清掉")

    def test_no_cleanup_when_single_instance(self):
        self._patch([
            {"pid": 100, "name": "node.exe", "cmdline": ["node.exe", "index.js"],
             "cwd": self.napcat_dir, "ppid": 1},
        ])
        self.assertEqual(self._svc()._napcat_cleanup_duplicates(self.tmp), [])
        self.assertEqual(self.terminated, [])


class KickNoticeTest(unittest.TestCase):
    """QQ 服务端踢下线（登录态失效）必须被识别并在管理台讲清楚。

    实测：本机三次踢号间隔 2:05:36 / 2:04:24 —— 第三方协议登录态有寿命（约 2 小时），
    到点被服务端作废；快速登录态也一起失效时只能扫码。
    """

    def setUp(self):
        self.svc = DesktopService.__new__(DesktopService)
        self.logged = []
        self.svc.log = lambda tag, msg=None: self.logged.append((tag, msg))
        self.svc._napcat_kicked_ts = None
        self.svc._napcat_offline_seen_ts = None
        self.svc.napcat_last_receive_ts = None

    def test_kick_line_is_detected_and_explained(self):
        self.svc._log_napcat_line(
            "10-11 00:40:33 [error] 软软 | [KickedOffLine] [下线通知] 你的账号当前登录已失效，请重新登录。")
        self.assertIsNotNone(self.svc._napcat_kicked_ts)
        notes = [m for _t, m in self.logged if m and "被踢下线" in m]
        self.assertTrue(notes, "必须给用户一条明确的解释")
        self.assertIn("扫码", notes[0])
        self.assertIn("密码", notes[0])

    def test_unrelated_line_does_not_set_kick(self):
        self.svc._log_napcat_line("10-11 00:40:33 [info] 接收 <- 群聊 (123) [文本]")
        self.assertIsNone(self.svc._napcat_kicked_ts)

    def test_payload_exposes_kick_age(self):
        from qqbot.admin import _napcat_login_payload

        class Svc:
            project_root = "x"
            _napcat_kicked_ts = time.time() - 754

            def napcat_login_status(_self, root=None):
                return {"ok": True, "logged_in": False, "qrcode_url": ""}

            def _napcat_webui_url(_self, root=None):
                return "http://127.0.0.1:6099/webui?token=t"

            def napcat_qrcode_png(_self, url):
                return None

            def napcat_qrcode_png_from_cache(_self, root=None):
                return "data:qr"

            def qrcode_cache_age(_self, root=None):
                return 3
        st = _napcat_login_payload(Svc())
        self.assertTrue(st["kicked"])
        self.assertAlmostEqual(st["kicked_ago"], 754, delta=5)
        self.assertIn("扫码", st["note"])

    def test_ui_shows_kick_hint(self):
        ui = open(os.path.join(ROOT, "qqbot", "admin_ui.py"), encoding="utf-8").read()
        self.assertIn("st.kicked", ui)
        self.assertIn("function fmtAgo(", ui)


class HealthTickLoginTest(unittest.TestCase):
    """等扫码/快速登录期间不得自动重启 NapCat（真实事故：扫码中途被重启打断）。"""

    def _svc(self, logged_in, last_receive_age=1200, login_seen_age=None):
        svc = DesktopService.__new__(DesktopService)
        svc.project_root = "x"
        svc.log = lambda *a, **k: None
        svc.napcat_started = True

        class Proc:
            @staticmethod
            def poll():
                return None
        svc.napcat_proc = Proc()
        svc._port_open = lambda port: True
        svc._napcat_port = lambda root: 3001
        svc._napcat_last_auto_restart = 0.0
        svc.napcat_login_status = lambda root=None: (
            {"ok": True, "logged_in": logged_in, "offline": False})
        svc._restarted = []
        svc._restart_napcat = lambda root: svc._restarted.append(True)
        svc.napcat_last_receive_ts = (time.time() - last_receive_age
                                      if last_receive_age is not None else None)
        svc._napcat_offline_seen_ts = None
        svc._napcat_login_seen_ts = (None if login_seen_age is None
                                     else time.time() - login_seen_age)
        svc._bot_last_activity_age = lambda: 60.0      # 机器人 1 分钟前还在回复
        return svc

    def test_not_logged_in_never_restarts(self):
        """未登录 = 正在等扫码：收不到消息很正常，绝不能重启。"""
        svc = self._svc(logged_in=False)
        svc._napcat_health_tick("x")
        self.assertEqual(svc._restarted, [])

    def test_recent_login_gets_grace_period(self):
        """刚登录不久（<静默阈值）也不该判假死重启。"""
        svc = self._svc(logged_in=True, last_receive_age=1200, login_seen_age=60)
        svc._napcat_health_tick("x")
        self.assertEqual(svc._restarted, [])

    def test_stale_login_and_silent_link_restarts(self):
        """登录已久 + 长时间收不到消息 + 机器人仍活跃 → 才自动重启。"""
        svc = self._svc(logged_in=True, last_receive_age=1200, login_seen_age=3600)
        svc._napcat_health_tick("x")
        self.assertEqual(svc._restarted, [True])

    def test_login_seen_ts_recorded_on_first_tick(self):
        svc = self._svc(logged_in=True, last_receive_age=0, login_seen_age=None)
        svc._napcat_health_tick("x")
        self.assertIsNotNone(svc._napcat_login_seen_ts)
        self.assertEqual(svc._restarted, [])


class RestartFallbackTest(unittest.TestCase):
    """重启时序：杀掉旧进程之后，绝不能因为拉起失败而"一个 NapCat 都没有"。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _FakeService._tmp_dir = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _svc(self, ensure_raises=True):
        svc = _FakeService([], root=self.tmp)
        svc.lock = threading.RLock()
        svc.napcat_proc = None
        svc._wait_port_closed = lambda port, timeout=10: True

        def _ensure(root):
            if ensure_raises:
                raise RuntimeError("boom：健康检查线程状态缺失")
            return {"ok": True, "started": True}
        svc._ensure_napcat = _ensure
        return svc

    def test_falls_back_to_direct_launch_when_ensure_raises(self):
        svc = self._svc(ensure_raises=True)
        seen = {}
        def _direct(root):
            seen["direct"] = True
            return {"ok": False, "error": "启动失败"}
        svc._launch_napcat_direct = _direct
        res = svc.napcat_restart(self.tmp, wait=0)
        self.assertTrue(seen.get("direct"), "必须走直接启动兜底，不能直接抛异常")
        self.assertFalse(res["ok"])
        self.assertIn("启动失败", res["error"])

    def test_direct_launch_returns_error_not_exception(self):
        svc = self._svc(ensure_raises=False)
        svc._ensure_napcat = lambda root: (_ for _ in ()).throw(RuntimeError("x"))
        svc.napcat_login_status = lambda root=None: {"ok": True, "logged_in": True,
                                                     "qrcode_url": ""}
        svc._launch_napcat_direct = lambda root: {"ok": True, "started": True}
        res = svc.napcat_restart(self.tmp, wait=1)
        self.assertTrue(res["ok"])
        self.assertTrue(res["logged_in"])
        self.assertTrue(res["restarted"])


class LoginPayloadTest(unittest.TestCase):
    """/api/desktop/napcat/login 的载荷必须带二维码来源与新鲜度。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        from qqbot.admin import _napcat_login_payload
        self.payload = _napcat_login_payload

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _svc(self, status, live_png=None, cache_png=None, age=None):
        class Svc:
            project_root = "x"

            def napcat_login_status(_self, root=None):
                return dict(status)

            def _napcat_webui_url(_self, root=None):
                return "http://127.0.0.1:6099/webui?token=t"

            def napcat_qrcode_png(_self, url):
                return live_png

            def napcat_qrcode_png_from_cache(_self, root=None):
                return cache_png

            def qrcode_cache_age(_self, root=None):
                return age
        return Svc()

    def test_live_qr_preferred(self):
        st = self.payload(self._svc({"ok": True, "logged_in": False,
                                     "qrcode_url": "u"}, live_png="data:live"))
        self.assertEqual(st["qr_source"], "live")
        self.assertNotIn("qr_expired", st)

    def test_stale_cache_yields_no_qr_and_hint(self):
        st = self.payload(self._svc({"ok": True, "logged_in": False, "qrcode_url": ""},
                                    cache_png=None, age=3600))
        self.assertEqual(st["qr_source"], "none")
        self.assertTrue(st["qr_expired"])
        self.assertIn("重启 NapCat", st["note"])
        self.assertEqual(st["cache_age"], 3600)

    def test_cache_used_only_as_fallback(self):
        st = self.payload(self._svc({"ok": True, "logged_in": False, "qrcode_url": ""},
                                    cache_png="data:cache", age=5))
        self.assertEqual(st["qr_source"], "cache")
        self.assertEqual(st["qrcode_png"], "data:cache")

    def test_logged_in_needs_no_qr(self):
        st = self.payload(self._svc({"ok": True, "logged_in": True, "qrcode_url": ""}))
        self.assertTrue(st["logged_in"])
        self.assertEqual(st["qr_source"], "none")
        self.assertNotIn("qr_expired", st)


if __name__ == "__main__":
    unittest.main()
