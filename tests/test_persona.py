# -*- coding: utf-8 -*-
"""人格切换测试：角色卡列表/查找/状态/数据隔离/指令。"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot import parser, persona
from qqbot.bot_core import BotCore
from qqbot.config import load_config

OWNER = 999001
GUEST = 10001


def make_card(name):
    return {"spec": "chara_card_v2", "spec_version": "2.0",
            "data": {"name": name, "description": "测试角色：%s" % name}}


def make_bot(tmp, cards=("软软", "圆乃")):
    char_dir = os.path.join(tmp, "character")
    os.makedirs(char_dir, exist_ok=True)
    for i, name in enumerate(cards):
        with open(os.path.join(char_dir, "card%d.json" % i), "w", encoding="utf-8") as f:
            json.dump(make_card(name), f, ensure_ascii=False)
    cfg = load_config(os.path.join(tmp, "test-config.json"), use_env=False)
    cfg.update({
        "allow_all": True,
        "owner_user_ids": [OWNER],
        "allowed_user_ids": [OWNER, GUEST],
        "typing_indicator": False,
        "summary_dir": os.path.join(tmp, "summary"),
        "archive_dir": os.path.join(tmp, "archive"),
        "character_dir": "character",
        "character_card": os.path.join(char_dir, "card0.json"),
        "plugins": {"memory": True, "chatlog": True, "vault_sync": True,
                    "proactive": False, "worldbook": False},
        "worldbook": {"dir": "", "files": []},
    })
    cfg["identity"]["map_path"] = os.path.join(tmp, "identity_map.json")
    cfg["time"]["path"] = os.path.join(tmp, "clock.json")
    with open(os.path.join(tmp, "clock.json"), "w", encoding="utf-8") as f:
        json.dump({"weekday": "周六", "time_of_day": "下午", "hhmm": None, "set": True}, f)
    cfg["rules"]["dir"] = os.path.join(tmp, "rules")
    cfg["correct"]["dir"] = os.path.join(tmp, "corrected")
    cfg["seal"]["dir"] = os.path.join(tmp, "seal")
    cfg["memory"]["db_path"] = os.path.join(tmp, "memory.db")
    cfg["chatlog"]["dir"] = os.path.join(tmp, "chatlog")
    cfg["vault"] = {"enable": True, "dir": os.path.join(tmp, "vault"), "check_seconds": 60}
    cfg["shared"] = {"dir": os.path.join(tmp, "shared")}
    cfg["proactive"]["score_file"] = os.path.join(tmp, "proactive_score.json")
    return BotCore(cfg, "测试人格", None)


class FakeLLM:
    def __init__(self):
        self.chat_calls = []

    def chat(self, messages, temperature=None, max_tokens=None, call_type="chat"):
        self.chat_calls.append(messages)
        return "（回复）"

    def chat_json(self, messages, temperature=0.2, call_type="chat", max_tokens=None):
        return None


class CaptureClient:
    def __init__(self):
        self.sent = []

    def send_private(self, user_id, text):
        self.sent.append((user_id, text))
        return True


class PersonaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_root = persona._root
        persona._root = lambda: self.tmp  # 隔离状态文件/目录到临时目录

    def tearDown(self):
        persona._root = self._old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cards(self):
        d = os.path.join(self.tmp, "character")
        os.makedirs(d, exist_ok=True)
        for i, name in enumerate(["软软", "圆乃"]):
            with open(os.path.join(d, "c%d.json" % i), "w", encoding="utf-8") as f:
                json.dump(make_card(name), f, ensure_ascii=False)
        return d

    def test_list_and_find(self):
        self._cards()
        cards = persona.list_cards({"character_dir": "character"})
        self.assertEqual([c["name"] for c in cards], ["软软", "圆乃"])
        path, name = persona.find_card("圆乃", {"character_dir": "character"})
        self.assertEqual(name, "圆乃")
        self.assertIsNone(persona.find_card("不存在", {"character_dir": "character"}))

    def test_list_cards_cached_until_file_changes(self):
        """状态页轮询不应反复解码卡片；文件变化后必须重新读取。"""
        self._cards()
        cfg = {"character_dir": "character"}
        with mock.patch.object(persona.sillytavern, "load_character_card",
                               wraps=persona.sillytavern.load_character_card) as load:
            self.assertEqual(len(persona.list_cards(cfg)), 2)
            self.assertEqual(len(persona.list_cards(cfg)), 2)
            self.assertEqual(load.call_count, 2)
            p = os.path.join(self.tmp, "character", "c0.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(make_card("改名卡"), f, ensure_ascii=False)
            os.utime(p, (time.time() + 5,) * 2)
            names = [c["name"] for c in persona.list_cards(cfg)]
            self.assertIn("改名卡", names)
            self.assertEqual(load.call_count, 4)

    def test_state_roundtrip(self):
        self._cards()
        path, name = persona.find_card("软软", {"character_dir": "character"})
        persona.set_current(name, path)
        cur = persona.current()
        self.assertEqual(cur["name"], "软软")
        self.assertTrue(os.path.isfile(cur["card"]))

    def test_data_overrides_isolated(self):
        ov = persona.data_overrides({}, "软软")
        base = os.path.join(self.tmp, "data", "personas", "软软")
        self.assertEqual(ov["memory"]["db_path"], os.path.join(base, "memory.db"))
        self.assertEqual(ov["chatlog"]["dir"], os.path.join(base, "chatlog"))
        self.assertEqual(ov["time"]["path"], os.path.join(base, "time", "clock.json"))
        self.assertIn("worldlog", ov["worldlog_dir"])

    def test_safe_name(self):
        self.assertEqual(persona.safe_name("a/b:c"), "a_b_c")
        self.assertEqual(persona.safe_name("  "), "默认")

    def test_ensure_persona_data_migrates(self):
        os.makedirs(os.path.join(self.tmp, "data"))
        with open(os.path.join(self.tmp, "data", "memory.db"), "w") as f:
            f.write("x")
        os.makedirs(os.path.join(self.tmp, "data", "chatlog"))
        target = persona.ensure_persona_data("软软")
        self.assertTrue(os.path.isfile(os.path.join(target, "memory.db")))
        self.assertTrue(os.path.isdir(os.path.join(target, "chatlog")))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "data", "memory.db")))  # 原数据保留
        self.assertEqual(persona.ensure_persona_data("软软"), target)  # 幂等

    def test_parse_commands(self):
        self.assertEqual(parser.classify_instruction("角色卡查看"), "persona_list")
        self.assertEqual(parser.classify_instruction("切换为软软"), "persona_switch")

    def test_persona_list_command(self):
        self._cards()
        bot = make_bot(self.tmp, cards=("软软", "圆乃"))
        bot.llm = FakeLLM()
        bot.client = CaptureClient()
        bot._handle(OWNER, "【角色卡查看】")
        text = [t for _, t in bot.client.sent][-1]
        self.assertIn("【角色卡查看】", text)
        self.assertIn("软软", text)
        self.assertIn("圆乃", text)
        bot.plugins.shutdown()

    def test_switch_unknown(self):
        bot = make_bot(self.tmp, cards=("软软", "圆乃"))
        bot.llm = FakeLLM()
        bot.client = CaptureClient()
        bot._handle(OWNER, "【切换为不存在】")
        text = [t for _, t in bot.client.sent][-1]
        self.assertIn("没有找到", text)
        self.assertIn("软软", text)
        bot.plugins.shutdown()

    def test_switch_known_writes_state(self):
        self._cards()
        bot = make_bot(self.tmp, cards=("软软", "圆乃"))
        bot.llm = FakeLLM()
        bot.client = CaptureClient()
        res = bot._persona_switch("圆乃")
        self.assertTrue(res.startswith("【已切换到「圆乃」"))
        cur = persona.current()
        self.assertEqual(cur["name"], "圆乃")
        self.assertTrue(os.path.isfile(cur["card"]))
        bot.plugins.shutdown()


if __name__ == "__main__":
    unittest.main()
