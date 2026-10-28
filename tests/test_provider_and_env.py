# -*- coding: utf-8 -*-
"""多厂家隔离 / 用量记账容错 / Anthropic 消息归一化 / 环境变量优先级。"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot import config as qconfig  # noqa: E402
from qqbot.llm import ProviderClient  # noqa: E402


def base_cfg():
    cfg = json.loads(json.dumps(qconfig.DEFAULTS))
    cfg["deepseek_api_key"] = "sk-deepseek"
    cfg["deepseek_base_url"] = "https://api.deepseek.com"
    return cfg


class ProviderIsolationTest(unittest.TestCase):
    def test_non_deepseek_provider_never_borrows_deepseek_key(self):
        cfg = base_cfg()
        cfg["llm_provider"] = "openai"   # 未配置 openai 自己的 key
        client = ProviderClient(cfg)
        self.assertEqual(client.api_key, "")
        with self.assertRaises(ValueError):
            client._require_config()
        self.assertNotIn("deepseek", client.base_url)

    def test_custom_provider_without_base_url_is_rejected(self):
        cfg = base_cfg()
        cfg["llm_provider"] = "custom"
        cfg["llm_providers"]["custom"] = {"api_key": "sk-custom", "base_url": "",
                                         "model": ""}
        client = ProviderClient(cfg)
        self.assertNotIn("api.deepseek.com", client.base_url)
        with self.assertRaises(ValueError):
            client._require_config()

    def test_deepseek_still_uses_its_own_fields(self):
        client = ProviderClient(base_cfg())
        self.assertEqual(client.api_key, "sk-deepseek")
        self.assertEqual(client.base_url, "https://api.deepseek.com")
        client._require_config()   # 不应抛异常

    def test_bad_timeout_falls_back_to_default(self):
        cfg = base_cfg()
        cfg["timeout_seconds"] = None
        self.assertEqual(ProviderClient(cfg).timeout, 180.0)
        cfg["timeout_seconds"] = -5
        self.assertEqual(ProviderClient(cfg).timeout, 180.0)


class UsageAccountingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "usage.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _client(self, path):
        cfg = base_cfg()
        cfg["usage_log"] = path
        return ProviderClient(cfg)

    def test_corrupt_usage_file_is_repaired_not_fatal(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"total": 5, "deepseek-v4-flash": "corrupt"}, f)
        client = self._client(self.path)
        # 损坏的桶被清理，记账不再抛异常（旧实现会让每次成功调用都被判失败）
        client._record_usage("m", 100, 10, 0)
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["total"]["calls"], 1)
        self.assertIsInstance(data["m"], dict)

    def test_usage_write_failure_is_swallowed(self):
        client = self._client(self.path)
        client.usage_path = os.path.join(self.tmp, "no_such_dir", "x", "usage.json")
        client._usage = {"total": object()}   # 故意让内部写入失败
        client._record_usage("m", 1, 1, 0)    # 不应抛异常


class AnthropicNormalizeTest(unittest.TestCase):
    def test_assistant_first_is_dropped_and_roles_merged(self):
        msgs = [{"role": "system", "content": "s"},
                {"role": "assistant", "content": "提前说的话"},
                {"role": "user", "content": "A"},
                {"role": "user", "content": "B"},
                {"role": "assistant", "content": "C"}]
        out = ProviderClient._normalize_anthropic(msgs[1:])
        self.assertEqual(out[0]["role"], "user")
        self.assertIn("A", out[0]["content"])
        self.assertIn("B", out[0]["content"])          # 连续同角色被合并
        self.assertEqual(out[-1]["role"], "assistant")

    def test_empty_messages_do_not_produce_empty_payload(self):
        out = ProviderClient._normalize_anthropic([])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["role"], "user")


class EnvPriorityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bom_env_file_is_read(self):
        path = os.path.join(self.tmp, ".env")
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write("DEEPSEEK_API_KEY=sk-from-env\n")
        self.assertEqual(qconfig.load_env(path).get("DEEPSEEK_API_KEY"), "sk-from-env")

    def test_env_wins_over_stale_config_value(self):
        cfg = base_cfg()
        cfg["deepseek_api_key"] = "sk-OLD-in-config"
        qconfig.apply_env(cfg, {"DEEPSEEK_API_KEY": "sk-NEW-in-env"})
        self.assertEqual(cfg["deepseek_api_key"], "sk-NEW-in-env")

    def test_non_secret_fields_keep_config_priority(self):
        cfg = base_cfg()
        cfg["deepseek_base_url"] = "https://custom.example.com"
        qconfig.apply_env(cfg, {"DEEPSEEK_BASE_URL": "https://api.deepseek.com"})
        self.assertEqual(cfg["deepseek_base_url"], "https://custom.example.com")

    def test_save_env_rejects_unsafe_values(self):
        path = os.path.join(self.tmp, ".env")
        with self.assertRaises(ValueError):
            qconfig.save_env({"ONEBOT_TOKEN": "line1\nMTIz"}, path)
        with self.assertRaises(ValueError):
            qconfig.save_env({"ONEBOT_TOKEN": 'has"quote'}, path)
        qconfig.save_env({"ONEBOT_TOKEN": "  padded  "}, path)
        # 含首尾空格的值加引号写回，读回不丢空格（未加引号时会被 strip 掉）
        self.assertEqual(qconfig.load_env(path)["ONEBOT_TOKEN"], "  padded  ")


if __name__ == "__main__":
    unittest.main()
