# -*- coding: utf-8 -*-
"""模拟聊天端到端测试 + 封存/纠正的稳定 id 回归。

- 端到端：复用 `scripts/simulate_chat.py` 的剧本（18 轮，含异步 worker 路径），
  断言 22 项一致性检查全部通过（数据生命周期、注入内容、会话命令不误触发…）。
- 稳定 id：聊天记录被重排/迁移后，封存点与【纠正】作废仍指向同一条内容。
"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from qqbot.chatlog_store import entry_id  # noqa: E402
from simulate_chat import audit, build_bot, run_scenario  # noqa: E402

IDENT = "主人"


def write_chatlog(tmp, files):
    """files = {"第1周周一": ["- 第1周周一上午 **主人**：内容", ...]}"""
    d = os.path.join(tmp, "chatlog", IDENT)
    os.makedirs(d, exist_ok=True)
    for day, lines in files.items():
        with open(os.path.join(d, "%s.md" % day), "w", encoding="utf-8") as f:
            f.write("# 聊天记录 %s %s\n\n" % (IDENT, day))
            for ln in lines:
                f.write(ln + "\n")


class SimulatedChatE2ETest(unittest.TestCase):
    def test_full_conversation_scenario(self):
        tmp = tempfile.mkdtemp(prefix="luotavern-e2e-")
        try:
            bot = build_bot(tmp)
            try:
                lines, by_note = run_scenario(bot, verbose=False)
                checks = audit(bot, lines, by_note)
            finally:
                try:
                    bot.plugins.shutdown()
                except Exception:
                    pass
            failed = ["%s（%s）" % (name, detail)
                      for name, ok, detail in checks if not ok]
            self.assertEqual(failed, [], "端到端校验未通过：%s" % failed)
            self.assertGreaterEqual(len(lines), 15)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class StableEntryIdTest(unittest.TestCase):
    """S34：封存点/纠正登记依赖序号，重排聊天记录后会指错条目。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="luotavern-id-")
        self.bot = build_bot(self.tmp)

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_entry_id_is_content_based(self):
        a = (0, "第1周周一上午", "主人", "内容A")
        b = (7, "第1周周一上午", "主人", "内容A")
        self.assertEqual(entry_id(a), entry_id(b))          # 序号变化不影响 id
        self.assertNotEqual(entry_id(a),
                            entry_id((0, "第1周周一上午", "主人", "内容B")))
        self.assertNotEqual(entry_id(a),
                            entry_id((0, "第1周周一晚上", "主人", "内容A")))
        self.assertNotEqual(entry_id(a),
                            entry_id((0, "第1周周一上午", "青柠", "内容A")))

    def test_legacy_seal_migrates_and_survives_reorder(self):
        write_chatlog(self.tmp, {
            "第1周周一": ["- 第1周周一上午 **主人**：第一句",
                          "- 第1周周一晚上 **主人**：第二句"],
        })
        entries = self.bot.chatlog_store.read_entries(IDENT)
        self.assertEqual(len(entries), 2)
        # 旧格式：只有序号（模拟历史文件）
        import json
        seal_path = os.path.join(self.bot.seal.dir, "%s.json" % IDENT)
        with open(seal_path, "w", encoding="utf-8") as f:
            json.dump({"sealed_through": 0}, f)
        unsealed = self.bot.seal.unsealed(IDENT)
        self.assertEqual([e[3] for e in unsealed], ["第二句"])
        self.assertEqual(len(self.bot.seal.sealed_ids(IDENT)), 1)   # 自动迁移出 id

        # 重排：往更早的文件里插入一条新记录（序号整体后移）
        write_chatlog(self.tmp, {
            "第1周周一": ["- 第1周周一清晨 **主人**：插入的新记录",
                          "- 第1周周一上午 **主人**：第一句",
                          "- 第1周周一晚上 **主人**：第二句"],
        })
        unsealed = self.bot.seal.unsealed(IDENT)
        self.assertEqual([e[3] for e in unsealed], ["插入的新记录", "第二句"],
                         "重排后不应重复提炼已封存的内容")

    def test_seal_after_extraction_is_stable(self):
        write_chatlog(self.tmp, {
            "第1周周一": ["- 第1周周一上午 **主人**：第一句",
                          "- 第1周周一晚上 **主人**：第二句"],
        })
        entries = self.bot.chatlog_store.read_entries(IDENT)
        self.bot.seal.set(IDENT, entries[0][0], entry=entries[0])
        self.assertEqual([e[3] for e in self.bot.seal.unsealed(IDENT)], ["第二句"])
        # 再插入一条更早的：已封存的那条仍视为已处理
        write_chatlog(self.tmp, {
            "第1周周一": ["- 第1周周一清晨 **主人**：插入",
                          "- 第1周周一上午 **主人**：第一句",
                          "- 第1周周一晚上 **主人**：第二句"],
        })
        contents = [e[3] for e in self.bot.seal.unsealed(IDENT)]
        self.assertIn("插入", contents)
        self.assertNotIn("第一句", contents)

    def test_correction_survives_reorder(self):
        write_chatlog(self.tmp, {
            "第1周周一": ["- 第1周周一上午 **青柠**：错误的回复",
                          "- 第1周周一晚上 **主人**：第二句"],
        })
        entries = self.bot.chatlog_store.read_entries(IDENT)
        target = entries[0]
        self.bot.correct.register(IDENT, target[0], "更正内容", entry=target)
        self.assertTrue(self.bot.correct.is_corrected(IDENT, target[0], target))
        self.assertTrue(self.bot.correct.corrected_ids(IDENT))

        write_chatlog(self.tmp, {
            "第1周周一": ["- 第1周周一清晨 **主人**：插入",
                          "- 第1周周一上午 **青柠**：错误的回复",
                          "- 第1周周一晚上 **主人**：第二句"],
        })
        moved = [e for e in self.bot.chatlog_store.read_entries(IDENT)
                 if e[3] == "错误的回复"][0]
        self.assertNotEqual(moved[0], target[0])                 # 序号变了
        self.assertTrue(self.bot.correct.is_corrected(IDENT, moved[0], moved),
                        "重排后作废登记必须仍然生效")


if __name__ == "__main__":
    unittest.main()
