# -*- coding: utf-8 -*-
"""Obsidian vault 导出测试：结构 / frontmatter / 双链 / 幂等 / 文件名清理。"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qqbot.vault_export import export_vault, _safe_title


def make_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT, identity TEXT NOT NULL,
        content TEXT NOT NULL, created_at REAL NOT NULL);
    CREATE TABLE important_memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT, identity TEXT NOT NULL,
        content TEXT NOT NULL, affect_delta INTEGER NOT NULL DEFAULT 0,
        importance INTEGER NOT NULL DEFAULT 5, created_at REAL NOT NULL);
    CREATE TABLE affection (
        identity TEXT PRIMARY KEY, score INTEGER NOT NULL, updated_at REAL NOT NULL);
    """)
    t = 1750000000.0
    con.executemany(
        "INSERT INTO important_memories(identity, content, affect_delta, importance, created_at) "
        "VALUES(?,?,?,?,?)",
        [("梦梦", "梦梦生日是 6 月 1 日，约好一起过", 10, 9, t),
         ("梦梦", "梦梦被老板批评，情绪低落", -5, 6, t + 100),
         ("哥哥", "哥哥明天出差去上海", 0, 4, t)])
    con.executemany(
        "INSERT INTO memories(identity, content, created_at) VALUES(?,?,?)",
        [("梦梦", "喜欢小猫", t), ("梦梦", "讨厌香菜", t + 10)])
    con.execute("INSERT INTO affection(identity, score, updated_at) VALUES(?,?,?)",
                ("梦梦", 110, t))
    con.execute("INSERT INTO affection(identity, score, updated_at) VALUES(?,?,?)",
                ("哥哥", 300, t))
    con.commit()
    con.close()


class VaultExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "memory.db")
        self.vault = os.path.join(self.tmp, "vault")
        make_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self, rel):
        with open(os.path.join(self.vault, rel), encoding="utf-8") as f:
            return f.read()

    def test_export_structure_and_frontmatter(self):
        n, ids = export_vault(self.db, self.vault)
        self.assertEqual(n, 5)  # 3 重要记忆 + 2 身份索引
        self.assertEqual(ids, ["哥哥", "梦梦"])

        # 每条重要记忆一个文件，frontmatter 齐全
        mdir = os.path.join(self.vault, "青柠记忆", "梦梦")
        files = sorted(os.listdir(mdir))
        self.assertEqual(len(files), 2)
        mem = self.read(os.path.join("青柠记忆", "梦梦", files[0]))
        self.assertIn("identity: 梦梦", mem)
        self.assertIn("kind: important", mem)
        self.assertIn("importance: 9", mem)
        self.assertIn("affect_delta: +10", mem)
        self.assertIn("[[梦梦]]", mem)  # 双链回身份
        self.assertIn("梦梦生日是 6 月 1 日", mem)

    def test_identity_index(self):
        export_vault(self.db, self.vault)
        idx = self.read(os.path.join("青柠记忆", "梦梦.md"))
        self.assertIn("kind: identity", idx)
        self.assertIn("affection: 110", idx)
        # 双链列表 + 长期记忆列表
        self.assertIn("## 重要记忆", idx)
        self.assertIn("- [[", idx)
        self.assertIn("## 长期记忆", idx)
        self.assertIn("喜欢小猫", idx)
        self.assertIn("讨厌香菜", idx)

    def test_idempotent_rebuild(self):
        export_vault(self.db, self.vault)
        before = []
        for root, dirs, files in os.walk(self.vault):
            for f in files:
                p = os.path.join(root, f)
                before.append((p, open(p, encoding="utf-8").read()))
        n2, _ = export_vault(self.db, self.vault)
        after = []
        for root, dirs, files in os.walk(self.vault):
            for f in files:
                p = os.path.join(root, f)
                after.append((p, open(p, encoding="utf-8").read()))
        self.assertEqual(len(before), len(after))
        self.assertEqual(sorted(before), sorted(after))  # 内容一致

    def test_stale_file_removed(self):
        # 幂等重建会清掉库里已不存在的旧文件
        export_vault(self.db, self.vault)
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM important_memories WHERE content LIKE '%生日%'")
        con.commit()
        con.close()
        export_vault(self.db, self.vault)
        mdir = os.path.join(self.vault, "青柠记忆", "梦梦")
        self.assertEqual(len(os.listdir(mdir)), 1)  # 只剩被批评那条

    def test_manual_notes_preserved(self):
        # 人工在 vault 加的笔记不受重建影响；机器人旧文件被清理
        export_vault(self.db, self.vault)
        manual = os.path.join(self.vault, "青柠记忆", "我的整理笔记.md")
        with open(manual, "w", encoding="utf-8") as f:
            f.write("# 我的整理\n[[梦梦]] 的生日备忘\n")
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM important_memories WHERE content LIKE '%生日%'")
        con.commit()
        con.close()
        export_vault(self.db, self.vault)
        self.assertTrue(os.path.isfile(manual), "人工笔记被误删")
        # 机器人旧文件（被删除记忆的导出）已清理
        mdir = os.path.join(self.vault, "青柠记忆", "梦梦")
        files = os.listdir(mdir)
        self.assertNotIn("我的整理笔记.md", files)
        self.assertEqual(len(files), 1)
        # manifest 存在且只含机器人文件
        with open(os.path.join(self.vault, "青柠记忆", ".manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertNotIn("我的整理笔记.md", manifest)
        self.assertIn("梦梦.md", manifest)

    def test_empty_db(self):
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM important_memories")
        con.execute("DELETE FROM memories")
        con.commit()
        con.close()
        n, ids = export_vault(self.db, self.vault)
        self.assertEqual(n, 0)
        self.assertEqual(ids, [])
        root = os.path.join(self.vault, "青柠记忆")
        self.assertTrue(os.path.isdir(root))
        # 空库只留 manifest，无任何身份文件
        self.assertEqual(os.listdir(root), [".manifest.json"])

    def test_safe_title(self):
        self.assertEqual(_safe_title("老板:骂我\\话*?|<>"), "老板骂我话")
        self.assertEqual(_safe_title(""), "记忆")
        self.assertEqual(len(_safe_title("一二三四五六七八九十十一十二十三十四十五十六", max_len=12)), 12)


if __name__ == "__main__":
    unittest.main()
