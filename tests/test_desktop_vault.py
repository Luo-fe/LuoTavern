# -*- coding: utf-8 -*-
"""桌面 EXE 密钥库：导入 / 写回 .env / 删除同步 测试。"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import desktop_vault
from qqbot import config as qconfig


class DesktopVaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.vault_file = os.path.join(self.tmp, "vault.json")
        self.env_file = os.path.join(self.tmp, ".env")
        qconfig.save_env({
            "DEEPSEEK_API_KEY": "sk-test-123456",
            "DISCORD_TOKEN": "discord-token",
            "OTHER_CUSTOM": "keep-me",
        }, self.env_file)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_vault_roundtrip(self):
        desktop_vault.save_vault(
            {"DEEPSEEK_API_KEY": "sk-test-123456",
             "DISCORD_TOKEN": "discord-token"}, self.vault_file)
        keys = desktop_vault.load_vault(self.vault_file)
        self.assertEqual(keys["DEEPSEEK_API_KEY"], "sk-test-123456")
        self.assertEqual(keys["DISCORD_TOKEN"], "discord-token")

    def test_import_from_project(self):
        imported = desktop_vault.import_from_project(
            self.tmp, {}, path=self.vault_file)
        self.assertIn("DEEPSEEK_API_KEY", imported)
        self.assertIn("DISCORD_TOKEN", imported)
        self.assertNotIn("OTHER_CUSTOM", imported)  # 未知键不导入
        keys = desktop_vault.load_vault(self.vault_file)
        self.assertEqual(keys["DEEPSEEK_API_KEY"], "sk-test-123456")

    def test_apply_writes_env(self):
        desktop_vault.save_vault(
            {"DEEPSEEK_API_KEY": "sk-vault-new",
             "DISCORD_TOKEN": "discord-token"}, self.vault_file)
        written = desktop_vault.apply_to_project(
            self.tmp, path=self.vault_file)
        self.assertIn("DEEPSEEK_API_KEY", written)
        env = qconfig.load_env(self.env_file)
        self.assertEqual(env["DEEPSEEK_API_KEY"], "sk-vault-new")
        self.assertEqual(env["OTHER_CUSTOM"], "keep-me")  # 无关键保留

    def test_remove_from_project(self):
        env = qconfig.load_env(self.env_file)
        self.assertIn("DEEPSEEK_API_KEY", env)
        ok = desktop_vault.remove_from_project(
            self.tmp, "DEEPSEEK_API_KEY")
        self.assertTrue(ok)
        env = qconfig.load_env(self.env_file)
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertEqual(env["OTHER_CUSTOM"], "keep-me")
        self.assertFalse(desktop_vault.remove_from_project(
            self.tmp, "DEEPSEEK_API_KEY"))

    def test_persona_switch_recomputes_memory_root(self):
        """桌面端切人格后 memory_ui 数据路径应跟随新人格。"""
        import memory_ui
        from qqbot import persona
        old_root = os.environ.get("LUOTAVERN_ROOT")
        old_base = memory_ui.BASE
        try:
            os.environ["LUOTAVERN_ROOT"] = self.tmp
            persona.set_current("测试人格", os.path.join(self.tmp, "card.json"))
            memory_ui.set_root(self.tmp)
            self.assertTrue(memory_ui.DB.endswith(
                os.path.join("personas", "测试人格", "memory.db")))
            self.assertEqual(memory_ui.USAGE_LOG,
                             os.path.join(self.tmp, "data", "usage.json"))
        finally:
            if old_root is None:
                os.environ.pop("LUOTAVERN_ROOT", None)
            else:
                os.environ["LUOTAVERN_ROOT"] = old_root
            memory_ui.set_root(old_base or ROOT)


if __name__ == "__main__":
    unittest.main()
