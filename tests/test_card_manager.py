# -*- coding: utf-8 -*-
"""角色卡 / 世界书管理：导入、编辑、封面、导出 测试。"""
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qqbot import card_manager
from qqbot import sillytavern


def make_card(name="测试角色"):
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": name,
            "description": "{{char}} 是 {{user}} 的专属角色。",
            "personality": "温柔",
            "scenario": "房间里",
            "system_prompt": "请用中文回复。",
            "mes_example": "",
            "first_mes": "你好",
            "extensions": {"bot_name": name, "user_names": "主人"},
        },
    }


def make_worldbook(name="测试世界书"):
    return {"name": name, "entries": {
        "1": {"keys": ["视频"], "content": "一起拍过视频。",
              "constant": True, "enabled": True},
    }}


def png_card_bytes(data):
    return sillytavern.build_png_with_text(
        None, "chara", json.dumps(data, ensure_ascii=False))


def png_worldbook_bytes(data):
    return sillytavern.build_png_with_text(
        None, "worldbook", json.dumps(data, ensure_ascii=False))


class CardManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_root = os.environ.get("LUOTAVERN_ROOT")
        os.environ["LUOTAVERN_ROOT"] = self.tmp
        os.makedirs(os.path.join(self.tmp, "character"), exist_ok=True)
        os.makedirs(os.path.join(self.tmp, "book"), exist_ok=True)

    def tearDown(self):
        if self.old_root is None:
            os.environ.pop("LUOTAVERN_ROOT", None)
        else:
            os.environ["LUOTAVERN_ROOT"] = self.old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def b64(self, data):
        return base64.b64encode(data).decode("ascii")

    # ---------- 角色卡 ----------
    def test_import_json_card(self):
        raw = json.dumps(make_card("JSON角色"), ensure_ascii=False).encode("utf-8")
        res = card_manager.import_card("json角色.json", self.b64(raw))
        self.assertEqual(res["name"], "JSON角色")
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp, "character", "json角色.json")))
        cards = card_manager.list_cards()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["format"], "json")

    def test_import_png_card(self):
        raw = png_card_bytes(make_card("PNG角色"))
        res = card_manager.import_card("png角色.png", self.b64(raw))
        self.assertEqual(res["name"], "PNG角色")
        self.assertEqual(res["format"], "png")
        self.assertEqual(card_manager.list_cards()[0]["format"], "png")

    def test_import_invalid_png_rejected(self):
        with self.assertRaises(ValueError):
            card_manager.import_card("bad.png", self.b64(b"not png"))

    def test_detail_and_save(self):
        raw = json.dumps(make_card(), ensure_ascii=False).encode("utf-8")
        res = card_manager.import_card("a.json", self.b64(raw))
        detail = card_manager.card_detail(res["file"])
        self.assertEqual(detail["fields"]["name"], "测试角色")
        self.assertEqual(detail["fields"]["user_names"], "主人")
        fields = detail["fields"]
        fields["description"] = "新设定"
        fields["bot_name"] = "新名字"
        saved = card_manager.save_card(res["file"], fields)
        self.assertEqual(saved["name"], "测试角色")
        detail2 = card_manager.card_detail(res["file"])
        self.assertEqual(detail2["fields"]["description"], "新设定")
        self.assertEqual(detail2["fields"]["bot_name"], "新名字")

    def test_save_png_card_preserves_image_and_chara(self):
        raw = png_card_bytes(make_card("PNG编辑"))
        res = card_manager.import_card("edit.png", self.b64(raw))
        fields = card_manager.card_detail(res["file"])["fields"]
        fields["personality"] = "冷静"
        card_manager.save_card(res["file"], fields)
        card = sillytavern.load_character_card(
            os.path.join(self.tmp, "character", "edit.png"))
        self.assertEqual(card["name"], "PNG编辑")
        self.assertEqual(card["personality"], "冷静")
        # 图像数据仍有效（可解析且包含 IDAT）
        with open(os.path.join(self.tmp, "character", "edit.png"), "rb") as f:
            data = f.read()
        self.assertTrue(data.startswith(sillytavern.PNG_SIGNATURE))
        self.assertTrue(any(c[0] == b"IDAT" for c in sillytavern.read_png_chunks(data)))

    def test_new_card_with_cover(self):
        cover = png_card_bytes({"name": "封面图", "data": {}})
        res = card_manager.new_card(
            {"name": "全新角色", "description": "介绍"},
            cover_b64=self.b64(cover))
        self.assertTrue(res["file"].endswith(".json"))
        cover_path = card_manager.card_cover(res["file"])
        self.assertIsNotNone(cover_path)
        self.assertTrue(cover_path.endswith("covers" + os.sep + "全新角色.png"))
        detail = card_manager.card_detail(res["file"])
        self.assertEqual(detail["fields"]["name"], "全新角色")

    def test_cover_set_get_remove(self):
        raw = json.dumps(make_card("带封面"), ensure_ascii=False).encode("utf-8")
        res = card_manager.import_card("cover.json", self.b64(raw))
        self.assertIsNone(card_manager.card_cover(res["file"]))
        img = png_card_bytes({"name": "x", "data": {}})
        card_manager.set_card_cover(res["file"], self.b64(img))
        self.assertIsNotNone(card_manager.card_cover(res["file"]))
        self.assertTrue(card_manager.remove_card_cover(res["file"]))
        self.assertIsNone(card_manager.card_cover(res["file"]))

    def test_export_card_json_and_png(self):
        raw = json.dumps(make_card("导出卡"), ensure_ascii=False).encode("utf-8")
        res = card_manager.import_card("export.json", self.b64(raw))
        js = card_manager.export_card(res["file"], "json")
        self.assertEqual(json.loads(js.decode("utf-8"))["data"]["name"], "导出卡")
        png = card_manager.export_card(res["file"], "png")
        self.assertTrue(png.startswith(sillytavern.PNG_SIGNATURE))
        parsed = sillytavern.load_character_card_raw_from_bytes(png)
        self.assertEqual(parsed["data"]["name"], "导出卡")

    def test_export_png_from_json_card_with_cover(self):
        """JSON 卡后期添加封面 → 导出 PNG 应同时含封面图像与完整角色卡数据。"""
        raw = json.dumps(make_card("JSON封面导出"), ensure_ascii=False).encode("utf-8")
        res = card_manager.import_card("cover_export.json", self.b64(raw))
        cover = sillytavern.build_png_with_text(
            None, "chara", json.dumps({"name": "img", "data": {}}))
        card_manager.set_card_cover(res["file"], self.b64(cover))
        png = card_manager.export_card(res["file"], "png")
        # 1) 导出的 PNG 可解析出完整角色卡
        parsed = sillytavern.load_character_card_raw_from_bytes(png)
        self.assertEqual(parsed["data"]["name"], "JSON封面导出")
        self.assertEqual(parsed["data"]["description"],
                         "{{char}} 是 {{user}} 的专属角色。")
        # 2) 图像与自定义封面一致（IDAT 数据块相同）
        cover_chunks = {c[0]: c[1] for c in sillytavern.read_png_chunks(cover)}
        png_chunks = {c[0]: c[1] for c in sillytavern.read_png_chunks(png)}
        self.assertEqual(png_chunks[b"IDAT"], cover_chunks[b"IDAT"])
        # 3) 该 PNG 可再导入回管理台，数据不丢失
        imported = card_manager.import_card(
            "roundtrip.png", self.b64(png))
        detail = card_manager.card_detail(imported["file"])
        self.assertEqual(detail["fields"]["name"], "JSON封面导出")

    def test_export_to_path(self):
        raw = json.dumps(make_card("路径导出"), ensure_ascii=False).encode("utf-8")
        res = card_manager.import_card("p.json", self.b64(raw))
        out = os.path.join(self.tmp, "out", "卡.json")
        r = card_manager.export_card(res["file"], "json", out_path=out)
        self.assertTrue(r["ok"])
        self.assertTrue(os.path.isfile(out))

    def test_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            card_manager.card_detail("../secret.json")

    # ---------- 世界书 ----------
    def test_import_worldbook_json(self):
        raw = json.dumps(make_worldbook("设定书"), ensure_ascii=False).encode("utf-8")
        res = card_manager.import_worldbook("设定书.json", self.b64(raw))
        self.assertEqual(res["name"], "设定书")
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp, "book", "设定书.json")))

    def test_import_worldbook_png(self):
        raw = png_worldbook_bytes(make_worldbook("PNG书"))
        res = card_manager.import_worldbook("png书.png", self.b64(raw))
        self.assertTrue(res["file"].endswith(".png"))
        entries = sillytavern.load_world_book(
            os.path.join(self.tmp, "book", res["file"]))
        self.assertEqual(len(entries), 1)

    def test_new_worldbook_and_export_png(self):
        res = card_manager.new_worldbook("新建书")
        self.assertTrue(res["file"].endswith(".json"))
        js = card_manager.export_worldbook(res["file"], "json")
        self.assertEqual(json.loads(js.decode("utf-8"))["name"], "新建书")
        png = card_manager.export_worldbook(res["file"], "png")
        parsed = sillytavern.load_worldbook_from_png_bytes(png)
        self.assertEqual(parsed["name"], "新建书")

    def test_worldbook_cover(self):
        raw = json.dumps(make_worldbook(), ensure_ascii=False).encode("utf-8")
        res = card_manager.import_worldbook("书.json", self.b64(raw))
        img = png_worldbook_bytes(make_worldbook("封面"))
        card_manager.set_worldbook_cover(res["file"], self.b64(img))
        self.assertIsNotNone(card_manager.worldbook_cover(res["file"]))
        self.assertTrue(card_manager.remove_worldbook_cover(res["file"]))


if __name__ == "__main__":
    unittest.main()
