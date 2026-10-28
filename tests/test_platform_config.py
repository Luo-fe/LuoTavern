# -*- coding: utf-8 -*-
"""平台配置自动合并测试：config.qq.json / config.discord.json。"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot import config as config_mod
from qqbot.config import load_config, load_platform_config


class PlatformConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_path = config_mod.CONFIG_PATH
        self._old_platform_files = dict(config_mod.PLATFORM_CONFIG_FILES)
        self._old_env_file = config_mod.ENV_FILE
        config_mod.ENV_FILE = ".env.__test_missing__"

    def tearDown(self):
        config_mod.CONFIG_PATH = self._old_path
        config_mod.PLATFORM_CONFIG_FILES = self._old_platform_files
        config_mod.ENV_FILE = self._old_env_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self):
        config_mod.PLATFORM_CONFIG_FILES = {
            "qq": os.path.join(self.tmp, "config.qq.json"),
            "discord": os.path.join(self.tmp, "config.discord.json"),
        }
        return load_config(os.path.join(self.tmp, "config.json"))

    def _write(self, name, data):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def _write_both(self):
        self._write("config.json", {"platform": "qq"})
        self._write("config.discord.json", {
            "discord": {"token": "dt", "proxy": "dp"},
            "allowed_user_ids": [10000002],
            "owner_user_ids": [10000002],
        })
        self._write("config.qq.json", {
            "onebot_ws_url": "ws://qq",
            "onebot_token": "qt",
            "allowed_user_ids": [10000001],
            "owner_user_ids": [10000001],
        })

    def test_discord_merge(self):
        self._write_both()
        cfg = load_platform_config(self._cfg(), "discord")
        self.assertEqual(cfg["discord"]["token"], "dt")
        self.assertEqual(cfg["discord"]["proxy"], "dp")
        self.assertEqual(cfg["allowed_user_ids"], [10000002])
        self.assertEqual(cfg["owner_user_ids"], [10000002])
        # QQ 的字段不被带入
        self.assertEqual(cfg["onebot_ws_url"], "ws://127.0.0.1:3001")

    def test_qq_merge(self):
        self._write_both()
        cfg = load_platform_config(self._cfg(), "qq")
        self.assertEqual(cfg["onebot_ws_url"], "ws://qq")
        self.assertEqual(cfg["onebot_token"], "qt")
        self.assertEqual(cfg["allowed_user_ids"], [10000001])
        self.assertEqual(cfg["owner_user_ids"], [10000001])
        # Discord 的字段不被带入
        self.assertEqual(cfg["discord"]["token"], "")

    def test_missing_platform_file_ok(self):
        self._write("config.json", {"platform": "qq"})
        cfg = load_platform_config(self._cfg(), "discord")
        self.assertEqual(cfg["platform"], "qq")
        self.assertEqual(cfg["allowed_user_ids"], [])

    def test_default_paths_follow_project_root_not_cwd(self):
        """从其他目录启动 bot.py 时，config.json / config.qq.json 仍按项目根读取。"""
        root = tempfile.mkdtemp()
        other = tempfile.mkdtemp()
        old_cwd = os.getcwd()
        try:
            with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as f:
                json.dump({"model": "root-model"}, f)
            with open(os.path.join(root, "config.qq.json"), "w", encoding="utf-8") as f:
                json.dump({"onebot_ws_url": "ws://from-root"}, f)
            os.chdir(other)
            with mock.patch.object(config_mod, "root_path", return_value=root):
                cfg = load_platform_config(load_config(use_env=False), "qq")
            self.assertEqual(cfg["model"], "root-model")
            self.assertEqual(cfg["onebot_ws_url"], "ws://from-root")
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(other, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
