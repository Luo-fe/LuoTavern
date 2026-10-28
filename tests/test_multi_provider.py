# -*- coding: utf-8 -*-
"""多厂家 LLM 配置、AI 编写角色卡、密钥扩展、NapCat 探测 测试。"""
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

from qqbot import ai_card_writer
from qqbot import config as qconfig
from qqbot.llm import build_llm


def base_cfg():
    cfg = json.loads(json.dumps(qconfig.DEFAULTS))
    cfg["deepseek_api_key"] = "sk-deepseek"
    cfg["llm_providers"]["deepseek"]["api_key"] = "sk-deepseek"
    cfg["llm_providers"]["openai"]["api_key"] = "sk-openai"
    cfg["llm_providers"]["anthropic"]["api_key"] = "sk-anthropic"
    return cfg


class FakeClient:
    def __init__(self):
        self.calls = []

    def chat_json(self, messages, temperature=None, max_tokens=None,
                  call_type="chat"):
        self.calls.append({"messages": messages, "temperature": temperature,
                           "max_tokens": max_tokens, "call_type": call_type})
        return {"spec": "chara_card_v2", "spec_version": "2.0",
                "data": {"name": "AI 角色", "description": "由模型生成"}}


class MultiProviderTest(unittest.TestCase):
    def test_build_llm_selects_provider(self):
        cfg = base_cfg()
        cfg["llm_provider"] = "anthropic"
        client = build_llm(cfg)
        self.assertEqual(client.api_style, "anthropic")
        self.assertEqual(client.api_key, "sk-anthropic")
        self.assertEqual(client.model, cfg["llm_providers"]["anthropic"]["model"])
        self.assertIn("/v1/messages", client.base_url + "/v1/messages")

    def test_build_llm_deepseek_fallback(self):
        cfg = base_cfg()
        client = build_llm(cfg)
        self.assertEqual(client.api_style, "openai")
        self.assertEqual(client.api_key, "sk-deepseek")
        self.assertEqual(client.base_url, "https://api.deepseek.com")

    def test_ai_card_generate_normalizes_and_requires_name(self):
        cfg = base_cfg()
        fake = FakeClient()
        res = ai_card_writer.generate("想要一个傲娇女仆", cfg, client=fake)
        self.assertEqual(res["name"], "AI 角色")
        self.assertEqual(res["card"]["spec"], "chara_card_v2")
        self.assertEqual(res["card"]["data"]["name"], "AI 角色")
        self.assertEqual(fake.calls[0]["call_type"], "ai_card")
        self.assertIn("傲娇女仆", fake.calls[0]["messages"][1]["content"])

    def test_ai_card_generate_wraps_flat_dict(self):
        class FlatClient(FakeClient):
            def chat_json(self, messages, **kw):
                super().chat_json(messages, **kw)
                return {"name": "扁平卡", "description": "直接字段"}

        res = ai_card_writer.generate("需求", base_cfg(), client=FlatClient())
        self.assertEqual(res["card"]["data"]["name"], "扁平卡")

    def test_ai_card_generate_errors(self):
        cfg = base_cfg()
        with self.assertRaises(ValueError):
            ai_card_writer.generate("", cfg, client=FakeClient())

        class NoNameClient(FakeClient):
            def chat_json(self, messages, **kw):
                super().chat_json(messages, **kw)
                return {"name": "", "description": "无名字"}

        with self.assertRaises(ValueError):
            ai_card_writer.generate("需求", cfg, client=NoNameClient())

    def test_ai_card_settings_roundtrip(self):
        cfg = {}
        s = ai_card_writer.save_settings(
            cfg, prompt="新提示词", provider="glm", model="glm-4-air")
        self.assertEqual(s["prompt"], "新提示词")
        self.assertEqual(s["provider"], "glm")
        self.assertEqual(s["model"], "glm-4-air")
        s2 = ai_card_writer.load_settings(cfg)
        self.assertEqual(s2["prompt"], "新提示词")


class VaultProviderKeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.vault = os.path.join(self.tmp, "vault.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_import_and_apply_provider_keys(self):
        import desktop_vault
        qconfig.save_env({
            "OPENAI_API_KEY": "sk-openai-env",
            "ANTHROPIC_API_KEY": "sk-anthropic-env",
            "CUSTOM_API_KEY": "custom-token",
        }, os.path.join(self.tmp, ".env"))
        imported = desktop_vault.import_from_project(
            self.tmp, {}, path=self.vault)
        self.assertIn("OPENAI_API_KEY", imported)
        self.assertIn("ANTHROPIC_API_KEY", imported)
        self.assertIn("CUSTOM_API_KEY", imported)
        desktop_vault.save_vault(
            {"GLM_API_KEY": "glm-token"}, self.vault)
        desktop_vault.apply_to_project(self.tmp, path=self.vault)
        env = qconfig.load_env(os.path.join(self.tmp, ".env"))
        self.assertEqual(env["GLM_API_KEY"], "glm-token")
        self.assertEqual(env["OPENAI_API_KEY"], "sk-openai-env")


class NapCatProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_napcat_port_from_config(self):
        from desktop_service import DesktopService
        with open(os.path.join(self.tmp, "config.qq.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"onebot_ws_url": "ws://127.0.0.1:8888"}, f)
        self.assertEqual(DesktopService._napcat_port(self.tmp), 8888)
        self.assertEqual(DesktopService._napcat_port(self.tmp + "_missing"), 3001)

    def test_napcat_dir_detection(self):
        from desktop_service import DesktopService
        d = os.path.join(self.tmp, "NapCat.Shell.Windows.Node")
        os.makedirs(d)
        with open(os.path.join(d, "index.js"), "w", encoding="utf-8") as f:
            f.write("")
        self.assertEqual(DesktopService._napcat_dir(self.tmp), d)
        self.assertIsNone(DesktopService._napcat_dir(self.tmp + "_none"))

    def test_port_open_false_on_free_port(self):
        from desktop_service import DesktopService
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.assertFalse(DesktopService._port_open(port))


class AiProviderDetectTest(unittest.TestCase):
    def test_ai_provider_auto_detect_from_keys(self):
        from qqbot import admin as admin_mod
        cfg = {"llm_providers": {
            "deepseek": {"api_key": ""},
            "openai": {"api_key": "sk-openai"},
            "anthropic": {"api_key": ""},
        }}
        self.assertEqual(admin_mod._ai_provider(cfg), "openai")

    def test_ai_provider_explicit_wins(self):
        from qqbot import admin as admin_mod
        cfg = {"llm_provider": "deepseek", "llm_providers": {
            "deepseek": {"api_key": ""},
            "openai": {"api_key": "sk-openai"},
        }}
        # 配置里的默认值不算显式：有密钥的厂家优先
        self.assertEqual(admin_mod._ai_provider(cfg), "openai")
        # 显式传入才优先
        self.assertEqual(
            admin_mod._ai_provider(cfg, explicit="deepseek"), "deepseek")
        self.assertEqual(
            admin_mod._ai_provider(cfg, {"provider": "openai"}), "openai")

    def test_apply_vault_keys_merges_provider_keys(self):
        from qqbot import admin as admin_mod
        import desktop_vault
        with mock.patch.object(
                desktop_vault, "load_vault",
                return_value={"OPENAI_API_KEY": "sk-vault",
                              "CUSTOM_API_KEY": "ct"}):
            cfg = admin_mod._apply_vault_keys(
                {"llm_providers": {"openai": {}, "custom": {}}})
        self.assertEqual(cfg["llm_providers"]["openai"]["api_key"], "sk-vault")
        self.assertEqual(cfg["llm_providers"]["custom"]["api_key"], "ct")


if __name__ == "__main__":
    unittest.main()
