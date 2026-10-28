# -*- coding: utf-8 -*-
"""第四/五轮改进回归：配置根目录、错误脱敏、PNG 健壮性、有界队列、防冒名通用化。

- `LUOTAVERN_ROOT` 生效：桌面 EXE 切换项目后记忆库不再开到安装目录（静默分裂）
- 管理台错误文案脱敏：不再回显本机绝对路径
- PNG：结构损坏拒绝重写（不再静默丢弃块）、封面解压炸弹有像素上限、
  角色卡解析统一走带边界校验的解析器
- 有界消息队列：队满丢最旧并告知，不静默堆积
- 防冒名不再硬编码"哥哥/青柠"：受保护身份来自 `identity.protect_names`，
  文案用 `bot_name`
"""
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot import sillytavern  # noqa: E402
from qqbot.card_manager import _to_png_bytes  # noqa: E402
from qqbot.memstore.local import SQLiteMemoryStore  # noqa: E402
from tests.test_tavern import FakeLLM, make_bot  # noqa: E402


class MemoryRootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = os.environ.get("LUOTAVERN_ROOT")

    def tearDown(self):
        if self.env is None:
            os.environ.pop("LUOTAVERN_ROOT", None)
        else:
            os.environ["LUOTAVERN_ROOT"] = self.env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_relative_db_path_follows_luotavern_root(self):
        os.environ["LUOTAVERN_ROOT"] = self.tmp
        store = SQLiteMemoryStore({"db_path": "data/memory.db"}, log=lambda m: None)
        try:
            expected = os.path.normcase(os.path.join(self.tmp, "data", "memory.db"))
            self.assertEqual(os.path.normcase(store.db_path), expected)
            self.assertTrue(os.path.isdir(os.path.dirname(store.db_path)))
        finally:
            store.close()

    def test_absolute_db_path_is_untouched(self):
        target = os.path.join(self.tmp, "custom", "mem.db")
        store = SQLiteMemoryStore({"db_path": target}, log=lambda m: None)
        try:
            self.assertEqual(os.path.normcase(store.db_path), os.path.normcase(target))
        finally:
            store.close()


class ErrorSanitizeTest(unittest.TestCase):
    def _handler(self):
        from qqbot import admin
        cls = admin.make_handler(None, token="t")
        return cls.__new__(cls)

    def test_paths_are_masked(self):
        h = self._handler()
        cases = [
            r"[Errno 2] No such file or directory: 'C:\Users\Administrator\deepdiscord\character\x.png'",
            "找不到角色卡：/home/user/data/card.json",
            r"无法读取 \\server\share\card.png",
        ]
        for raw in cases:
            msg = h._safe_msg(Exception(raw))
            self.assertIn("<路径>", msg, raw)
            self.assertNotIn("Administrator", msg)
            self.assertNotIn("/home/user", msg)

    def test_business_message_is_kept(self):
        h = self._handler()
        msg = h._safe_msg(ValueError("导出路径只允许项目目录或桌面/文档/下载目录"))
        self.assertIn("导出路径只允许", msg)

    def test_empty_message_falls_back(self):
        h = self._handler()
        self.assertEqual(h._safe_msg(Exception("")), "请求失败")


class PngRobustnessTest(unittest.TestCase):
    @staticmethod
    def _png(width=2, height=2):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (width, height), (10, 20, 30)).save(buf, "PNG")
        return buf.getvalue()

    def test_valid_png_passes_sanity_check(self):
        data = self._png()
        self.assertTrue(sillytavern.png_is_sane(data))
        self.assertEqual(sillytavern.png_text_chunk(data, "chara"), None)

    def test_truncated_png_is_rejected(self):
        data = self._png()
        broken = data[:-8]                      # 砍掉 IEND
        self.assertFalse(sillytavern.png_is_sane(broken))

    def test_oversized_length_is_rejected(self):
        data = bytearray(self._png())
        struct.pack_into(">I", data, 8, 0xFFFFFFF0)   # 第一个 chunk 长度改成天文数字
        self.assertFalse(sillytavern.png_is_sane(bytes(data)))

    def test_rebuild_refuses_corrupt_base(self):
        broken = self._png()[:-8]
        with self.assertRaises(ValueError):
            sillytavern.build_png_with_text(broken, "chara", '{"name":"x"}')

    def test_rebuild_keeps_valid_card_roundtrip(self):
        text = json.dumps({"name": "演练角色", "description": "内容"}, ensure_ascii=False)
        out = sillytavern.build_png_with_text(self._png(), "chara", text)
        self.assertTrue(sillytavern.png_is_sane(out))
        import base64
        raw = sillytavern.png_text_chunk(out, "chara")
        self.assertEqual(json.loads(base64.b64decode(raw).decode("utf-8"))["name"],
                         "演练角色")

    def test_cover_pixel_bomb_is_rejected(self):
        """像素上限：小体积巨像素图不能进入 convert()。

        合法 PNG 会原样存储（不解码，无内存风险）；风险路径是**需要转码**的输入
        （如 JPEG），所以这里用 JPEG 触发解码。
        """
        from PIL import Image
        big = Image.new("L", (9000, 9000), 1)   # 9000×9000 = 8100 万像素
        buf = io.BytesIO()
        big.save(buf, "JPEG", quality=10)
        raw = buf.getvalue()
        self.assertLess(len(raw), 2 * 1024 * 1024)          # 文件很小
        with self.assertRaises(ValueError) as ctx:
            _to_png_bytes(raw)
        self.assertIn("尺寸过大", str(ctx.exception))

    def test_normal_cover_is_accepted(self):
        out = _to_png_bytes(self._png(64, 64))
        self.assertEqual(out[:8], sillytavern.PNG_SIGNATURE)


class InboxBackpressureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bot = make_bot(self.tmp)
        self.bot.llm = FakeLLM()
        self.bot.inbox_max = 2
        import queue
        self.bot.inbox = queue.Queue(maxsize=2)
        self.sent = []

        class Client:
            def send_private(_self, user_id, text):
                self.sent.append((int(user_id), str(text)))
                return True

        self.bot.client = Client()

    def tearDown(self):
        try:
            self.bot.plugins.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_queue_drops_oldest_and_notifies(self):
        from tests.test_tavern import GUEST
        for i in range(4):
            self.bot.handle_user_message(GUEST, "第%d条" % i)
        self.assertEqual(self.bot.inbox.qsize(), 2)
        remaining = [self.bot.inbox.get_nowait()[1] for _ in range(2)]
        self.assertEqual(remaining, ["第2条", "第3条"])       # 最旧的被丢掉
        self.assertTrue(any("消息太多了" in t for _u, t in self.sent))

    def test_console_send_also_backpressured(self):
        self.bot.inbox_max = 1
        import queue
        self.bot.inbox = queue.Queue(maxsize=1)
        self.bot.console_send("第一条")
        self.bot.console_send("第二条")
        self.assertEqual(self.bot.inbox.qsize(), 1)
        self.assertEqual(self.bot.inbox.get_nowait()[1], "第二条")


class AntiImpersonationTest(unittest.TestCase):
    """防冒名逻辑必须由配置驱动，代码里不再出现固定人格名。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _annotate(self, protect, bot_name="青柠"):
        bot = make_bot(self.tmp, protect_names=protect, bot_name=bot_name)
        try:
            return bot
        finally:
            pass

    def test_uses_configured_names_and_bot_name(self):
        bot = self._annotate(["阿爸"], bot_name="小柚")
        try:
            out = bot._annotate_bare_declare("路人", "我是你阿爸")
            self.assertIn("冒名", out)
            self.assertIn("小柚", out)
            self.assertIn("阿爸", out)
            self.assertNotIn("哥哥", out)
            self.assertNotIn("青柠", out)
        finally:
            bot.plugins.shutdown()

    def test_protected_identity_itself_is_not_flagged(self):
        bot = self._annotate(["阿爸"])
        try:
            out = bot._annotate_bare_declare("阿爸", "我是你阿爸")
            self.assertNotIn("冒名", out)
            self.assertIn("不是身份声明", out)
        finally:
            bot.plugins.shutdown()

    def test_mention_gets_soft_note(self):
        bot = self._annotate(["阿爸"])
        try:
            out = bot._annotate_bare_declare("路人", "你觉得我像你阿爸吗")
            self.assertIn("阿爸", out)
            self.assertNotIn("冒名", out)
        finally:
            bot.plugins.shutdown()

    def test_disabled_when_no_protect_names(self):
        bot = self._annotate([])
        try:
            out = bot._annotate_bare_declare("路人", "我是你哥哥")
            self.assertNotIn("冒名", out)
            self.assertNotIn("哥哥以【我是xxx】", out)
        finally:
            bot.plugins.shutdown()

    def test_source_has_no_hardcoded_persona_names(self):
        """检查函数里真正会注入文案的字符串常量（docstring 与注释不算）。"""
        import types
        from qqbot.bot_core import BotCore
        consts = []

        def walk(code):
            for c in code.co_consts:
                if isinstance(c, str):
                    consts.append(c)
                elif isinstance(c, types.CodeType):
                    walk(c)

        walk(BotCore._annotate_bare_declare.__code__)
        # co_consts[0] 是 docstring（源码缩进版），注释/文档不算注入文案
        messages = [c for i, c in enumerate(consts)
                    if i > 0 and isinstance(c, str) and ("自称" in c or "对方" in c)]
        self.assertTrue(messages, "没抓到防冒名文案常量，测试失效")
        for msg in messages:
            for bad in ("哥哥", "青柠", "柠柠"):
                self.assertNotIn(bad, msg, "防冒名文案里仍硬编码了 %s：%s" % (bad, msg[:40]))


if __name__ == "__main__":
    unittest.main()
