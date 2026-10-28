# -*- coding: utf-8 -*-
"""进程重启保留平台参数测试：切换角色卡重启不得回退到 QQ/NapCat。"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.test_tavern import OWNER, make_bot


class _ExitSentinel(BaseException):
    """模拟 os._exit 的中断（BaseException，不被 _restart_now 的 except Exception 捕获）。"""


class RestartPlatformTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        import subprocess
        import os as _os
        self._popen = subprocess.Popen
        self._exit = _os._exit

    def tearDown(self):
        import subprocess
        import os as _os
        subprocess.Popen = self._popen
        _os._exit = self._exit
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _restart_args(self):
        import subprocess
        import os as _os
        calls = []
        subprocess.Popen = lambda *a, **k: (calls.append((a, k)) or True)

        def fake_exit(code):
            raise _ExitSentinel()

        _os._exit = fake_exit
        try:
            self.bot._restart_now(OWNER)
        except _ExitSentinel:
            pass
        finally:
            subprocess.Popen = self._popen
            _os._exit = self._exit
        self.assertEqual(len(calls), 1)
        return calls[0][0][0]

    def test_restart_keeps_discord_platform(self):
        self.bot.config["platform"] = "discord"
        args = self._restart_args()
        self.assertIn("--platform", args)
        self.assertEqual(args[args.index("--platform") + 1], "discord")

    def test_restart_keeps_qq_platform(self):
        self.bot.config["platform"] = "qq"
        args = self._restart_args()
        self.assertEqual(args[args.index("--platform") + 1], "qq")

    def test_restart_defaults_qq_when_missing(self):
        self.bot.config.pop("platform", None)
        args = self._restart_args()
        self.assertEqual(args[args.index("--platform") + 1], "qq")


if __name__ == "__main__":
    unittest.main()
