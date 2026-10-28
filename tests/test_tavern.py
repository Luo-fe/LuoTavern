# -*- coding: utf-8 -*-
"""酒馆融合测试：角色卡（JSON/PNG）、世界书、二次强调、通用化文本。"""
import base64
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot.bot_core import BotCore
from qqbot.config import load_config
from qqbot.sillytavern import (
    build_world_emphasis,
    card_system_prompt,
    entries_from_worldbook,
    load_character_card,
    match_world_entries,
)

OWNER = 999001
GUEST = 10001


def make_card(name="测试角色", extensions=None):
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": name,
            "description": "{{char}} 是 {{user}} 的专属角色，性格温柔。",
            "personality": "温柔、耐心",
            "scenario": "两人在房间里聊天",
            "system_prompt": "请用中文回复。",
            "mes_example": "",
            "first_mes": "你好",
            "character_book": None,
            "extensions": extensions or {},
        },
    }


def make_worldbook():
    return {
        "name": "测试世界书",
        "entries": {
            "1": {
                "keys": ["视频", "录像"],
                "content": "之前两人一起拍过一段视频，那是很重要的回忆。",
                "constant": True,
                "enabled": True,
                "insertion_order": 10,
            },
            "2": {
                "keys": ["公园", "散步"],
                "content": "两人约定周末去公园散步。",
                "constant": False,
                "enabled": True,
                "insertion_order": 20,
            },
        },
    }


def make_bot(tmp, card=None, wb=None, protect_names=None, bot_name="", user_names="",
             wb_cfg=None):
    cfg = load_config(os.path.join(tmp, "test-config.json"), use_env=False)
    cfg.update({
        "allow_all": True,
        "owner_user_ids": [OWNER],
        "allowed_user_ids": [OWNER, GUEST],
        "typing_indicator": False,
        "summary_dir": os.path.join(tmp, "summary"),
        "archive_dir": os.path.join(tmp, "archive"),
        "persona_file": "",
        "system_prompt": "你是一个角色扮演助手。",
        "bot_name": bot_name,
        "user_names": user_names,
        "character_card": os.path.join(tmp, "card.json"),
    })
    cfg["identity"].update({
        "map_path": os.path.join(tmp, "identity_map.json"),
        "owner_name": "主人",
        "dream_name": "",
        "reserved_names": ["主人"],
        "protect_names": protect_names if protect_names is not None else [],
        "undeclared_reply": "【请先告诉机器人你是谁～发【我是xxx】就行】",
    })
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
    cfg["plugins"] = {"memory": True, "chatlog": True, "vault_sync": True,
                      "proactive": False, "worldbook": True}
    cfg["worldbook"] = {
        "dir": tmp,
        "files": [],
        "base_mode": "constant",
        "base_max_chars": 2000,
        "emphasis_max_chars": 1500,
    }
    if wb_cfg:
        cfg["worldbook"].update(wb_cfg)
    with open(os.path.join(tmp, "card.json"), "w", encoding="utf-8") as f:
        json.dump(card or make_card(), f, ensure_ascii=False)
    with open(os.path.join(tmp, "wb.json"), "w", encoding="utf-8") as f:
        json.dump(wb or make_worldbook(), f, ensure_ascii=False)
    bot = BotCore(cfg, cfg["system_prompt"], None)
    return bot


def declare(bot, qq, name):
    bot._handle(qq, "【我是%s】" % name)


def last_user_content(bot):
    """最后一次"主对话"调用里发给模型的用户消息。

    跳过状态栏修复调用（`_repair_status_bar` 会再发一次 LLM 请求，其最后一条
    user 消息是修复指令而不是本轮提示词）——本文件的用例都在看提示词组装。
    """
    for msgs, _temp in reversed(bot.llm.chat_calls):
        sys_txt = " ".join(m["content"] for m in msgs if m["role"] == "system")
        if "回复后处理助手" in sys_txt:          # 廉价补状态栏调用（user 是回复正文）
            continue
        users = [m["content"] for m in msgs if m["role"] == "user"]
        if not users:
            continue
        last = users[-1]
        if "状态栏不合格" in last:                # 整段重生成的状态栏修复调用
            continue
        return last
    return ""


class FakeLLM:
    def __init__(self):
        self.chat_calls = []
        self.json_calls = []
        self.json_script = []

    def chat(self, messages, temperature=None, max_tokens=None, call_type="chat"):
        self.chat_calls.append((messages, temperature))
        return "（角色回复）"

    def chat_json(self, messages, temperature=0.2, call_type="chat", max_tokens=None):
        self.json_calls.append(messages)
        if self.json_script:
            return self.json_script.pop(0)
        return None


class CaptureClient:
    def __init__(self):
        self.sent = []

    def send_private(self, user_id, text):
        self.sent.append((user_id, text))
        return True


class TavernLoadersTest(unittest.TestCase):
    def test_card_json(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "card.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(make_card(), f, ensure_ascii=False)
            card = load_character_card(p)
            self.assertEqual(card["name"], "测试角色")
            self.assertIn("专属角色", card["description"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_card_png(self):
        tmp = tempfile.mkdtemp()
        try:
            raw = base64.b64encode(
                json.dumps(make_card("PNG角色"), ensure_ascii=False).encode()).decode()

            def chunk(ctype, data):
                return (struct.pack(">I", len(data)) + ctype + data +
                        struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF))

            ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
            png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
                   chunk(b"tEXt", b"chara\x00" + raw.encode()) + chunk(b"IEND", b""))
            p = os.path.join(tmp, "card.png")
            with open(p, "wb") as f:
                f.write(png)
            card = load_character_card(p)
            self.assertEqual(card["name"], "PNG角色")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_worldbook_dict_entries(self):
        entries = entries_from_worldbook(make_worldbook())
        self.assertEqual(len(entries), 2)
        by_keys = {tuple(e["keys"]): e for e in entries}
        self.assertTrue(by_keys[("视频", "录像")]["constant"])
        self.assertFalse(by_keys[("公园", "散步")]["constant"])

    def test_match_and_emphasis(self):
        entries = entries_from_worldbook(make_worldbook())
        matched = match_world_entries(entries, "还记得那段视频吗")
        self.assertEqual(len(matched), 1)
        block = build_world_emphasis(matched)
        self.assertIn("【世界书·强调】", block)
        self.assertIn("拍过一段视频", block)
        self.assertEqual(match_world_entries(entries, "今天天气不错"), [])

    def test_keys_split_joined(self):
        """顿号/逗号拼接的触发词拆成独立关键词后应能分别命中。"""
        wb = make_worldbook()
        wb["entries"]["3"] = {
            "keys": ["口交、手淫，潮吹"],
            "content": "三人各有各的玩法。",
            "enabled": True,
            "insertion_order": 30,
        }
        entries = entries_from_worldbook(wb)
        by_content = {e["content"]: e for e in entries}
        self.assertEqual(by_content["三人各有各的玩法。"]["keys"],
                         ["口交", "手淫", "潮吹"])
        self.assertEqual(len(match_world_entries(entries, "今晚想口交")), 1)
        self.assertEqual(len(match_world_entries(entries, "手淫给我看")), 1)
        self.assertEqual(match_world_entries(entries, "今天天气不错"), [])

    def test_secondary_keys_only_match_current_message(self):
        """keysecondary 次触发词只匹配当前消息，不匹配近文；主触发词可以匹配近文。"""
        wb = make_worldbook()
        wb["entries"]["3"] = {
            "keys": ["口交"],
            "keysecondary": ["吹箫", "吹喇叭"],
            "content": "口交相关设定。",
            "enabled": True,
            "insertion_order": 30,
        }
        entries = entries_from_worldbook(wb)
        self.assertEqual(len(match_world_entries(entries, "给我吹箫")), 1)
        self.assertEqual(
            match_world_entries(entries, "明天还来",
                                extra_context="昨天你吹箫的样子"), [])
        self.assertEqual(
            len(match_world_entries(entries, "明天还来",
                                    extra_context="昨天我们口交了")), 1)

    def test_synonyms_map_alias_to_key(self):
        """synonyms 别名命中等价于命中对应主触发词。"""
        entries = entries_from_worldbook(make_worldbook())
        syn = {"跳蛋": "视频"}
        self.assertEqual(len(match_world_entries(entries, "我的跳蛋呢", synonyms=syn)), 1)
        self.assertEqual(
            match_world_entries(entries, "今天天气不错", synonyms=syn), [])
        # 没有配置别名时，别名不触发
        self.assertEqual(match_world_entries(entries, "我的跳蛋呢"), [])

    def test_card_system_prompt_placeholders(self):
        card = make_card()
        prompt = card_system_prompt({
            "name": card["data"]["name"],
            "description": card["data"]["description"],
            "personality": card["data"]["personality"],
            "scenario": card["data"]["scenario"],
            "system_prompt": card["data"]["system_prompt"],
            "mes_example": "",
            "post_history_instructions": "",
        }, "主人")
        self.assertIn("【角色卡：测试角色】", prompt)
        self.assertIn("主人", prompt)
        self.assertNotIn("{{", prompt)


class TavernBotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["LUOTAVERN_WORLDLOG_DIR"] = os.path.join(self.tmp, "worldlog")
        self.bot = make_bot(self.tmp)
        self.bot.llm = FakeLLM()
        self.bot.client = CaptureClient()
        declare(self.bot, GUEST, "测试员")

    def tearDown(self):
        os.environ.pop("LUOTAVERN_WORLDLOG_DIR", None)
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_system_prompt_contains_card_and_worldbook(self):
        self.bot._handle(GUEST, "你好")
        sys_prompt = self.bot.history["测试员"][0]["content"]
        self.assertIn("【角色卡：测试角色】", sys_prompt)
        self.assertIn("专属角色", sys_prompt)
        self.assertIn("【世界书·常驻设定】", sys_prompt)
        self.assertIn("拍过一段视频", sys_prompt)
        self.assertNotIn("公园", sys_prompt)  # 非 constant 条目不再常驻（省 token）

    def test_trigger_word_reemphasis(self):
        self.bot._handle(GUEST, "还记得那段视频吗")
        user = last_user_content(self.bot)
        self.assertIn("【世界书·强调】", user)
        self.assertIn("拍过一段视频", user)

    def test_no_trigger_no_reemphasis(self):
        self.bot._handle(GUEST, "今天天气不错")
        user = last_user_content(self.bot)
        self.assertNotIn("【世界书·强调】", user)

    def test_match_context_recursive_scan(self):
        """开启 match_context 后，近文命中触发词也会触发强调；默认关闭不触发。"""
        tmp2 = tempfile.mkdtemp()
        try:
            bot2 = make_bot(tmp2, wb_cfg={"match_context": True,
                                          "match_context_turns": 4})
            bot2.llm = FakeLLM()
            bot2.client = CaptureClient()
            declare(bot2, GUEST, "测试员")
            bot2._handle(GUEST, "那段视频去哪了")
            self.assertIn("【世界书·强调】", last_user_content(bot2))
            # 这一轮不带触发词，但近文里有"视频"
            bot2._handle(GUEST, "我想再看看")
            self.assertIn("【世界书·强调】", last_user_content(bot2))
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

    def test_match_context_off_no_recursive(self):
        """默认不扫近文：上一轮命中后，这一轮不带触发词就不再强调。"""
        tmp2 = tempfile.mkdtemp()
        try:
            bot2 = make_bot(tmp2)
            bot2.llm = FakeLLM()
            bot2.client = CaptureClient()
            declare(bot2, GUEST, "测试员")
            bot2._handle(GUEST, "那段视频去哪了")
            self.assertIn("【世界书·强调】", last_user_content(bot2))
            bot2._handle(GUEST, "我想再看看")
            self.assertNotIn("【世界书·强调】", last_user_content(bot2))
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

    def test_trigger_records_worldlog(self):
        from qqbot import worldlog

        self.bot._handle(GUEST, "还记得那段视频吗")
        rows = worldlog.read_events("测试员")
        self.assertEqual(len(rows), 1)
        self.assertIn("拍过一段视频", rows[0]["content"])

    def test_world_track_memory_in_base(self):
        self.bot.memory.save_important(
            "测试员", "世界剧情：学校发生了停电事件", 0, 8, track="world")
        self.bot._handle(GUEST, "你好")
        sys_prompt = self.bot.history["测试员"][0]["content"]
        self.assertIn("【世界书剧情·记忆】", sys_prompt)
        self.assertIn("停电事件", sys_prompt)

    def test_endday_instruction_extracts_world(self):
        from qqbot import worldlog

        worldlog.append_events("测试员", [
            {"keys": ["女上位"], "content": "女上位体位细节被触发使用"}])
        self.bot.llm.json_script = [[
            {"content": "世界剧情：两人尝试了新的亲密体位", "importance": 8}
        ]]
        self.bot._handle(GUEST, "【结束这一天】")
        ack = [t for _, t in self.bot.client.sent][-1]
        self.assertIn("世界剧情 1 条", ack)
        mems = self.bot.memory.list_important_by_track("测试员", "world")
        self.assertEqual(len(mems), 1)
        self.assertIn("新的亲密体位", mems[0][0])
        self.assertEqual(worldlog.read_events("测试员"), [])
        self.assertEqual(self.bot.timeline.clock["time_of_day"], "深夜")

    def test_no_protect_names_no_guard_block(self):
        self.bot._handle(GUEST, "你好")
        sys_prompt = self.bot.history["测试员"][0]["content"]
        self.assertNotIn("【防冒名", sys_prompt)

    def test_protect_names_enables_guard(self):
        tmp2 = tempfile.mkdtemp()
        try:
            bot2 = make_bot(tmp2, protect_names=["哥哥"])
            bot2.llm = FakeLLM()
            bot2.client = CaptureClient()
            declare(bot2, GUEST, "测试员")
            bot2._handle(GUEST, "你好")
            sys_prompt = bot2.history["测试员"][0]["content"]
            self.assertIn("【防冒名", sys_prompt)
            bot2.plugins.shutdown()
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

    def test_manual_is_generic(self):
        self.bot._handle(GUEST, "【说明书】")
        text = [t for _, t in self.bot.client.sent][-1]
        self.assertIn("【我是xxx】", text)
        self.assertIn("仅主人", text)
        self.assertNotIn("哥哥", text)
        self.assertNotIn("青柠", text)

    def test_bot_name_from_card(self):
        self.assertEqual(self.bot.bot_name, "测试角色")

    def test_user_names_in_system_prompt(self):
        tmp2 = tempfile.mkdtemp()
        try:
            bot2 = make_bot(tmp2, user_names="主人,哥哥,亲爱的")
            bot2.llm = FakeLLM()
            bot2.client = CaptureClient()
            declare(bot2, GUEST, "测试员")
            bot2._handle(GUEST, "你好")
            sys_prompt = bot2.history["测试员"][0]["content"]
            self.assertIn("【对用户的称呼】", sys_prompt)
            self.assertIn("主人、哥哥、亲爱的", sys_prompt)
            self.assertEqual(bot2.user_names, ["主人", "哥哥", "亲爱的"])
            bot2.plugins.shutdown()
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

    def test_card_extensions_names_fallback(self):
        tmp2 = tempfile.mkdtemp()
        try:
            bot2 = make_bot(tmp2, card=make_card(
                name="卡片名",
                extensions={"bot_name": "小卡", "user_names": "宝贝,亲爱的"}))
            self.assertEqual(bot2.bot_name, "小卡")
            self.assertEqual(bot2.user_names, ["宝贝", "亲爱的"])
            bot2.plugins.shutdown()
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

    def test_config_overrides_card_extensions(self):
        tmp2 = tempfile.mkdtemp()
        try:
            bot2 = make_bot(tmp2, card=make_card(
                name="卡片名",
                extensions={"bot_name": "小卡", "user_names": "宝贝,亲爱的"}),
                bot_name="配置名", user_names="主人")
            self.assertEqual(bot2.bot_name, "配置名")
            self.assertEqual(bot2.user_names, ["主人"])
            bot2.plugins.shutdown()
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

    def test_memory_ui_worldbook_edit(self):
        import memory_ui

        tmp2 = tempfile.mkdtemp()
        old_base = memory_ui.BASE
        try:
            book = os.path.join(tmp2, "book")
            os.makedirs(book)
            with open(os.path.join(tmp2, "config.json"), "w", encoding="utf-8") as f:
                json.dump({"worldbook": {"dir": "book"}}, f)
            wb = {"name": "测试书",
                  "entries": {"1": {"id": 1, "keys": ["旧词"], "content": "旧内容",
                                    "constant": True, "insertion_order": 5}}}
            with open(os.path.join(book, "t.json"), "w", encoding="utf-8") as f:
                json.dump(wb, f, ensure_ascii=False)
            memory_ui.BASE = tmp2
            files = memory_ui.list_worldbook()
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0]["entries"][0]["keys"], ["旧词"])
            ok = memory_ui.update_worldbook_entry("t.json", 1, "新词,另一个", "新内容")
            self.assertTrue(ok)
            with open(os.path.join(book, "t.json"), encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["entries"]["1"]["keys"], ["新词", "另一个"])
            self.assertEqual(data["entries"]["1"]["content"], "新内容")
            self.assertTrue(data["entries"]["1"]["constant"])  # 其他字段保留
        finally:
            memory_ui.BASE = old_base
            shutil.rmtree(tmp2, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
