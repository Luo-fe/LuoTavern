"""审核问题回归：真实存储/召回路径 + 隔离数据库和假远端。"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

from qqbot import admin, persona, vault_export
from qqbot.correct import CorrectionManager
from qqbot.memstore.base import KeywordMixin
from qqbot.memstore.hybrid import HybridMemoryStore
from qqbot.memstore.local import migrate_conn
from qqbot.memstore.tencentdb import _SdkTransport
from qqbot.recall import RecallAgent

import memory_ui


class FakeTransport:
    def __init__(self):
        self.rows = {}

    def append_conversation(self, user_id, session_key, messages):
        self.rows.setdefault((user_id, session_key), []).append(messages[0]["content"])
        return True

    def recall(self, query, user_id, session_key, top):
        return [(s, 0.9) for s in self.rows.get((user_id, session_key), [])][-top:]

    def close(self):
        pass


class MemoryRegressionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transport = FakeTransport()

    def store(self, name="a"):
        cfg = {"db_path": str(Path(self.tmp.name) / (name + ".db")),
               "vector": {"enable": False}, "tencentdb": {"enabled": True}}
        with mock.patch("qqbot.memstore.tencentdb._SdkTransport", side_effect=ImportError), \
                mock.patch("qqbot.memstore.tencentdb._HttpTransport", return_value=self.transport):
            store = HybridMemoryStore(cfg)
        self.addCleanup(store.close)
        return store

    def test_hybrid_candidates_and_reinforcement(self):
        store = self.store()
        store.save_important("owner", "park visit", keywords=["park"])
        store.save_longterm("owner", "park promise", keywords=["park"])
        agent = RecallAgent(SimpleNamespace(config={}, memory=store))
        with mock.patch.object(agent, "search_logs", return_value=[]), \
                mock.patch.object(agent, "vector_recall", return_value=[]):
            hits = agent.gather_candidates("owner", "park")
        self.assertEqual({h.source for h in hits}, {"important", "longterm"})
        agent._reinforce("owner", hits)
        self.assertEqual(store.important_candidates("owner")[0][7], 1)

    def test_roles_with_same_identity_do_not_share_remote_memory(self):
        a, b = self.store("role-a"), self.store("role-b")
        a.save_longterm("owner", "private memory")
        self.assertEqual(b.remote.recall_texts("owner", "private"), [])
        self.assertTrue(a.remote.recall_texts("owner", "private"))

    def test_deleted_memory_is_excluded_from_every_remote_recall_path(self):
        store = self.store()
        store.save_longterm("owner", "old memory")
        self.assertTrue(store.remote.recall_texts("owner", "old"))
        self.assertTrue(store.delete_longterm("owner", "old"))
        self.assertEqual(store.recall_longterm("owner", "old"), [])
        self.assertEqual(store.recall_important("owner", "old"), [])
        self.assertEqual(store.vector_recall("owner", "old"), [])
        self.assertEqual(store.vector_top_score("owner", "old"), 0)

    def test_external_edit_rotates_remote_version_and_preserves_other_identities(self):
        store = self.store()
        store.save_important("owner", "old event")
        store.save_longterm("guest", "guest event")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute("UPDATE important_memories SET content='correct event' WHERE identity='owner'")
        self.assertEqual(store.remote.recall_texts("owner", "old"), [])
        self.assertTrue(store.remote.recall_texts("guest", "guest"))
        store.sync_conversation("owner", "new event", "new reply")
        self.assertEqual(store.remote.recall_texts("owner", "new"), ["new event"])

    def test_external_delete_version_survives_reopening(self):
        store = self.store()
        store.save_longterm("owner", "old memory")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute("DELETE FROM memories WHERE identity='owner'")
        reopened = self.store()
        self.assertEqual(reopened.remote.recall_texts("owner", "old"), [])

    def test_reinforcement_does_not_hide_valid_remote_memories(self):
        store = self.store()
        store.save_important("owner", "valid event")
        store.mark_recalled("owner", ["valid event"])
        self.assertTrue(store.remote.recall_texts("owner", "valid"))

    def test_remote_failure_preserves_full_local_quota(self):
        store = self.store()
        for text in ("park one", "park two", "park three"):
            store.save_longterm("owner", text)
            store.save_important("owner", text)
        with mock.patch.object(self.transport, "recall", side_effect=RuntimeError("offline")):
            self.assertEqual(len(store.recall_longterm("owner", "park", 3)), 3)
            self.assertEqual(len(store.recall_important("owner", "park", 3)), 3)

    def test_delete_during_remote_request_discards_inflight_hits(self):
        store = self.store()
        store.save_longterm("owner", "old memory")
        def recall(*args):
            store.delete_longterm("owner", "old")
            return [("old memory", 0.9)]
        with mock.patch.object(self.transport, "recall", side_effect=recall):
            self.assertEqual(store.remote.recall_texts("owner", "old"), [])

    def test_keyword_edits_and_important_deletes_invalidate_remote(self):
        store = self.store()
        store.save_longterm("owner", "first memory")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute("UPDATE memories SET keywords='[\"new\"]'")
        self.assertEqual(store.remote.recall_texts("owner", "first"), [])
        store.save_important("owner", "second memory")
        with closing(sqlite3.connect(store.db_path)) as conn:
            with conn:
                conn.execute("DELETE FROM important_memories")
        self.assertEqual(store.remote.recall_texts("owner", "second"), [])


class MigrationRegressionTest(unittest.TestCase):
    def test_legacy_tables_preserved_and_migration_repeatable(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        for table in ("memories", "important_memories"):
            conn.execute("CREATE TABLE %s (id INTEGER PRIMARY KEY, user_id INTEGER, "
                         "content TEXT, created_at REAL)" % table)
            conn.execute("INSERT INTO %s VALUES (1, 123, 'legacy', 1)" % table)
        migrate_conn(conn)
        migrate_conn(conn)
        self.assertEqual(conn.execute("SELECT content FROM memories_legacy_userid").fetchone()[0], "legacy")
        self.assertEqual(conn.execute("SELECT content FROM important_memories_legacy").fetchone()[0], "legacy")
        self.assertIn("identity", [r[1] for r in conn.execute("PRAGMA table_info(important_memories)")])

    def test_existing_legacy_backup_is_not_overwritten(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE memories_legacy_userid (content TEXT)")
        conn.execute("INSERT INTO memories_legacy_userid VALUES ('first backup')")
        conn.execute("CREATE TABLE memories (user_id INTEGER, content TEXT, created_at REAL)")
        conn.execute("INSERT INTO memories VALUES (123, 'second backup', 1)")
        migrate_conn(conn)
        self.assertEqual(conn.execute("SELECT content FROM memories_legacy_userid").fetchone()[0], "first backup")
        self.assertEqual(conn.execute("SELECT content FROM memories_legacy_userid_1").fetchone()[0], "second backup")


class SdkIsolationTest(unittest.TestCase):
    def test_old_sdk_without_identity_support_is_not_used(self):
        package = ModuleType("tencentdb_agent_memory")
        package.MemoryClient = mock.Mock()
        with mock.patch.dict(sys.modules, {"tencentdb_agent_memory": package,
                                          "tencentdb_agent_memory.v3": None}):
            with self.assertRaises(ImportError):
                _SdkTransport({}, lambda msg: None)
        package.MemoryClient.assert_not_called()

    def test_v3_client_receives_scoped_user_and_session(self):
        v3 = ModuleType("tencentdb_agent_memory.v3")
        v3.MemoryClient = mock.Mock()
        with mock.patch.dict(sys.modules, {"tencentdb_agent_memory.v3": v3}):
            transport = _SdkTransport({"session_prefix": "custom"}, lambda msg: None)
            transport.append_conversation("role-a:owner:v1", "custom:role-a:owner:v1", [])
        kwargs = v3.MemoryClient.call_args.kwargs
        self.assertEqual(kwargs["user_id"], "role-a:owner:v1")
        self.assertEqual(kwargs["session_id"], "custom:role-a:owner:v1")
        transport.close()


class PersonaRegressionTest(unittest.TestCase):
    def test_colliding_and_case_variant_names_have_distinct_directories(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(persona, "_root", return_value=td):
            names = ["a/b", "a:b", "Alice", "alice", "x" * 41, "x" * 42]
            paths = [persona.ensure_persona_data(n) for n in names]
            self.assertEqual(len({p.casefold() for p in paths}), len(names))
            for n, p in zip(names, paths):
                self.assertEqual(persona.data_overrides({}, n)["memory"]["db_path"], os.path.join(p, "memory.db"))

    def test_existing_normal_directory_is_preserved(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(persona, "_root", return_value=td):
            old = Path(td) / "data" / "personas" / "Alice"
            old.mkdir(parents=True)
            (old / "keep.txt").write_text("keep", encoding="utf-8")
            self.assertEqual(persona.ensure_persona_data("Alice"), str(old))
            persona.set_current("alice", "character/alice.json")
            self.assertNotEqual(persona.ensure_persona_data("alice").casefold(), str(old).casefold())
            self.assertEqual((old / "keep.txt").read_text(encoding="utf-8"), "keep")


class SettingsRegressionTest(unittest.TestCase):
    def test_default_provider_can_be_saved_when_not_in_raw_config(self):
        cfg = {}
        with mock.patch.object(admin, "_config_path", return_value="missing-config.json"):
            admin._merge_settings(cfg, {"llm_provider": "openai", "llm_providers": {
                "openai": {"base_url": "https://example.invalid/v1", "model": "chosen-model"}}})
        self.assertEqual(cfg["llm_providers"]["openai"]["model"], "chosen-model")


class KeywordNormalizationTest(unittest.TestCase):
    """关键词编解码只能有一份实现（存储层），管理台/导出不能各写一套。"""

    def test_all_entrypoints_share_the_store_encoding(self):
        expected = KeywordMixin._dump_keywords(["甲", "乙", "丙"])
        self.assertEqual(memory_ui._dump_keywords("甲, 乙，丙"), expected)
        self.assertEqual(memory_ui._dump_keywords(["甲", "乙", "丙"]), expected)
        self.assertEqual(json.loads(expected), ["甲", "乙", "丙"])

    def test_limits_and_dedup_are_consistent(self):
        many = ["kw%d" % i for i in range(15)]
        self.assertEqual(len(json.loads(memory_ui._dump_keywords(many))), 10)
        self.assertEqual(json.loads(memory_ui._dump_keywords("a,a,b")), ["a", "b"])
        self.assertEqual(memory_ui._dump_keywords("超过十二个字符的关键词应该被拒绝"),
                         KeywordMixin._dump_keywords(["超过十二个字符的关键词应该被拒绝"]))

    def test_readers_accept_json_and_comma_text(self):
        self.assertEqual(memory_ui._load_keywords('["视频","录像"]'), ["视频", "录像"])
        self.assertEqual(memory_ui._load_keywords("视频，录像,拍摄"), ["视频", "录像", "拍摄"])
        self.assertEqual(vault_export._load_keywords("视频,录像"), ["视频", "录像"])


class CorrectionCacheTest(unittest.TestCase):
    """is_corrected 在长聊天记录里被逐条调用：只允许读盘一次，外部改动要能失效。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mgr = CorrectionManager(SimpleNamespace(
            config={"correct": {"dir": self.tmp.name}}))
        self.entry = (0, "第1周周一早上", "青柠", "早上好")

    def test_repeated_checks_read_file_once(self):
        self.mgr.register("主人", 0, "改成晚上", entry=self.entry)
        with mock.patch.object(self.mgr, "_read", wraps=self.mgr._read) as read:
            for _ in range(50):
                self.assertTrue(self.mgr.is_corrected("主人", 0, self.entry))
        self.assertEqual(read.call_count, 1)

    def test_external_rewrite_invalidates_cache(self):
        self.mgr.register("主人", 0, "改成晚上", entry=self.entry)
        self.assertTrue(self.mgr.is_corrected("主人", 0, self.entry))
        path = self.mgr._path("主人")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"1": {"text": "别的条目"}}, f)
        os.utime(path, (os.path.getmtime(path) + 5,) * 2)
        self.assertFalse(self.mgr.is_corrected("主人", 0, self.entry))
        self.assertEqual(self.mgr.corrected_map("主人"), {1: "别的条目"})
        # 返回副本：调用方修改不应污染缓存
        self.mgr.corrected_map("主人")[1] = "被改"
        self.assertEqual(self.mgr.corrected_map("主人"), {1: "别的条目"})

    def test_register_after_external_change_merges_not_overwrites(self):
        self.mgr.register("主人", 0, "第一次", entry=self.entry)
        path = self.mgr._path("主人")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"2": {"text": "外部写入"}}, f)
        os.utime(path, (os.path.getmtime(path) + 5,) * 2)
        self.mgr.register("主人", 0, "第二次", entry=self.entry)
        self.assertEqual(set(self.mgr.corrected_map("主人")), {0, 2})


if __name__ == "__main__":
    unittest.main()
