# -*- coding: utf-8 -*-
"""Discord 适配器测试：平台选择、统一消息入口、分块、未连接时发送失败。"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot.bot_core import BotCore
from qqbot.config import load_config
from qqbot.discord_adapter import DiscordClient, _HandlerClient, _chunk_text

OWNER = 10000001
GUEST = 10001


def make_bot(tmp, platform="discord"):
    cfg = load_config(os.path.join(tmp, "test-config.json"), use_env=False)
    cfg.update({
        "platform": platform,
        "discord": {"token": "test-token", "reply_to_mention_only": True},
        "character_card": "",
        "plugins": {"memory": True, "chatlog": True, "vault_sync": True,
                    "proactive": False, "worldbook": False},
        "allow_all": False,
        "owner_user_ids": [OWNER],
        "allowed_user_ids": [OWNER, GUEST],
        "typing_indicator": False,
        "summary_dir": os.path.join(tmp, "summary"),
        "archive_dir": os.path.join(tmp, "archive"),
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
    return BotCore(cfg, "你是温柔可爱的青柠。", None)


class DiscordAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_platform_selects_discord_client(self):
        self.assertIsInstance(self.bot.client, DiscordClient)
        self.assertEqual(self.bot.platform, "discord")

    def test_platform_default_is_qq(self):
        from qqbot.onebot import OneBotClient

        tmp2 = tempfile.mkdtemp()
        try:
            bot2 = make_bot(tmp2, platform="qq")
            self.assertIsInstance(bot2.client, OneBotClient)
            bot2.plugins.shutdown()
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

    def test_handle_user_message_queues_allowed(self):
        self.bot.handle_user_message(GUEST, "你好呀")
        self.assertFalse(self.bot.inbox.empty())
        user_id, text = self.bot.inbox.get_nowait()
        self.assertEqual(user_id, GUEST)
        self.assertEqual(text, "你好呀")

    def test_handle_user_message_rejects_not_allowed(self):
        self.bot.handle_user_message(999999, "你好呀")
        self.assertTrue(self.bot.inbox.empty())

    def test_ready_handler_is_not_shadowed(self):
        """Client.event() 会把同名方法挂到实例上；不得用它覆盖 on_ready，否则连接日志丢失。"""
        client = self.bot.client.client
        self.assertIs(type(client).on_ready, _HandlerClient.on_ready)
        self.assertNotIn("on_ready", client.__dict__)

        adapter = mock.Mock()
        adapter._ready = False
        fake = SimpleNamespace(
            _adapter=adapter, user=SimpleNamespace(name="bot", id=1))
        import asyncio
        asyncio.run(_HandlerClient.on_ready(fake))
        self.assertTrue(adapter._ready)
        adapter.log.assert_called_once()

    def test_send_private_before_connect_returns_false(self):
        self.assertFalse(self.bot.client.send_private(OWNER, "测试"))

    def test_chunk_text(self):
        self.assertEqual(_chunk_text("短消息"), ["短消息"])
        long_text = "字" * 4500
        parts = _chunk_text(long_text)
        self.assertEqual("".join(parts), long_text)
        self.assertTrue(all(len(p) <= 2000 for p in parts))


if __name__ == "__main__":
    unittest.main()
