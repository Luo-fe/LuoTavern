# -*- coding: utf-8 -*-
"""首次启用人格自动发送开场白 测试。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.test_tavern import CaptureClient, make_bot


def card_with_first_mes(first_mes):
    card = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "测试角色",
            "description": "测试",
            "first_mes": first_mes,
            "extensions": {},
        },
    }
    return card


class FirstMesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_root = os.environ.get("LUOTAVERN_ROOT")
        os.environ["LUOTAVERN_ROOT"] = self.tmp
        os.makedirs(os.path.join(self.tmp, "data"), exist_ok=True)
        with open(os.path.join(self.tmp, "data", "persona_current.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"name": "开场白角色",
                       "card": os.path.join(self.tmp, "card.json")}, f)
        self.bot = make_bot(self.tmp)
        self.bot.client = CaptureClient()

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        if self.old_root is None:
            os.environ.pop("LUOTAVERN_ROOT", None)
        else:
            os.environ["LUOTAVERN_ROOT"] = self.old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_card(self, first_mes):
        with open(os.path.join(self.tmp, "card.json"),
                  "w", encoding="utf-8") as f:
            json.dump(card_with_first_mes(first_mes), f, ensure_ascii=False)

    def _marker(self):
        return os.path.join(
            self.tmp, "data", "personas", "开场白角色", "first_mes_sent")

    def test_sends_first_mes_once(self):
        self._write_card("你好，我是测试角色。")
        self.assertTrue(self.bot._try_send_first_mes())
        self.assertEqual(
            [t for _, t in self.bot.client.sent], ["你好，我是测试角色。"])
        self.assertTrue(os.path.isfile(self._marker()))
        # 再次调用不再重复发送
        self.assertFalse(self.bot._try_send_first_mes())
        self.assertEqual(len(self.bot.client.sent), 1)

    def test_no_first_mes_waits_for_user(self):
        self._write_card("")
        self.assertFalse(self.bot._try_send_first_mes())
        self.assertEqual(self.bot.client.sent, [])
        self.assertFalse(os.path.exists(self._marker()))

    def test_existing_chatlog_suppresses_first_mes(self):
        """人格已有聊天记录时不再发送开场白（旧版无标记积累的历史）。"""
        self._write_card("你好，我是测试角色。")
        d = os.path.join(self.tmp, "data", "personas", "开场白角色", "chatlog")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "第1周周一.md"), "w", encoding="utf-8") as f:
            f.write("# 聊天记录\n\n- 第1周周一晚上 **主人**：旧聊天\n")
        self.assertFalse(self.bot._try_send_first_mes())
        self.assertEqual(self.bot.client.sent, [])

    def test_placeholders_filled(self):
        self._write_card("{{char}} 对 {{user}} 说：你好。")
        self.assertTrue(self.bot._try_send_first_mes())
        self.assertEqual(
            [t for _, t in self.bot.client.sent],
            ["测试角色 对 主人 说：你好。"])

    def test_switch_to_new_persona_sends_again(self):
        """不同人格各自独立：换新人格后首次激活仍会发送开场白。"""
        self._write_card("第一个角色的开场白。")
        self.assertTrue(self.bot._try_send_first_mes())
        # 模拟切换到另一个数据目录的人格
        with open(os.path.join(self.tmp, "data", "persona_current.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"name": "另一个角色",
                       "card": os.path.join(self.tmp, "card.json")}, f)
        marker2 = os.path.join(
            self.tmp, "data", "personas", "另一个角色", "first_mes_sent")
        self.assertFalse(os.path.isfile(marker2))
        self.assertTrue(self.bot._try_send_first_mes())
        self.assertEqual(len(self.bot.client.sent), 2)
        self.assertTrue(os.path.isfile(marker2))


if __name__ == "__main__":
    unittest.main()
