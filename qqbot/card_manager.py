# -*- coding: utf-8 -*-
"""角色卡与世界书的一站式管理：导入 / 新建 / 编辑 / 封面 / 导出。

- 导入方式同原酒馆：支持 .json 与 .png（PNG 内嵌 chara / worldbook 数据）
- 封面：默认 PNG 卡/书本身即封面；JSON 可自定义封面（存到 covers/ 目录）
- 导出：JSON 原样导出；PNG 用封面/原图作为图像并重新内嵌数据
"""
import base64
import io
import json
import os

from qqbot import sillytavern

COVERS_SUBDIR = "covers"
_CARD_FIELDS = [
    "name", "description", "personality", "scenario", "system_prompt",
    "mes_example", "first_mes", "post_history_instructions", "tags",
]


def _root():
    from qqbot import config
    return config.root_path()


def cards_dir():
    return os.path.join(_root(), "character")


def books_dir():
    return os.path.join(_root(), "book")


def _covers_dir(base):
    return os.path.join(base, COVERS_SUBDIR)


def _safe_stem(file):
    from qqbot.persona import safe_name
    stem = os.path.splitext(os.path.basename(str(file or "")))[0]
    return safe_name(stem)


def _resolve(base, file):
    """把文件名解析到 base 内的绝对路径（防目录穿越）。"""
    name = os.path.basename(str(file or ""))
    if not name or name in (".", ".."):
        return None
    base_norm = os.path.normpath(base)
    path = os.path.normpath(os.path.join(base_norm, name))
    if os.path.commonpath([base_norm, path]) != base_norm:
        return None
    return path


def _unique_path(base, filename):
    """并发安全的唯一路径：O_CREAT|O_EXCL 原子占位，避免两个导入同时选中
    同一路径互相覆盖（先 exists 再 open 存在竞态）。"""
    stem, ext = os.path.splitext(filename)
    path = os.path.join(base, filename)
    i = 1
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return path
        except FileExistsError:
            path = os.path.join(base, "%s (%d)%s" % (stem, i, ext))
            i += 1


MAX_COVER_PIXELS = 40_000_000      # ≈ 6300×6300
MAX_COVER_SIDE = 8192


def _to_png_bytes(raw):
    """封面/图像归一化为 PNG 字节。"""
    if raw[:8] == sillytavern.PNG_SIGNATURE and sillytavern.png_is_sane(raw):
        return raw
    try:
        from PIL import Image
        # 解压炸弹防护：小体积、巨像素的 PNG/JPG 会在 convert() 时申请 GB 级内存
        # （20MB 请求体限制拦不住"几百 KB 的 30000×30000 纯色图"）
        Image.MAX_IMAGE_PIXELS = MAX_COVER_PIXELS
        try:
            img = Image.open(io.BytesIO(raw))
            w, h = img.size
        except Image.DecompressionBombError:
            raise ValueError("封面尺寸过大（超过 %d 像素），请先缩小到 8192 以内"
                             % MAX_COVER_PIXELS)
        if w * h > MAX_COVER_PIXELS or max(w, h) > MAX_COVER_SIDE:
            raise ValueError("封面尺寸过大（%dx%d），请先缩小到 8192 以内" % (w, h))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG")
        return buf.getvalue()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("封面仅支持 PNG/JPG 图片：%s" % exc)


def _decode_b64(data_b64):
    if not data_b64:
        raise ValueError("缺少文件数据")
    try:
        return base64.b64decode(str(data_b64))
    except Exception as exc:
        raise ValueError("文件数据不是有效的 base64：%s" % exc)


def _card_parts(raw):
    """返回 (根 dict, 卡片内层 dict)。"""
    if not isinstance(raw, dict):
        raise ValueError("角色卡内容必须是 JSON 对象")
    inner = raw.get("data")
    if isinstance(inner, dict):
        return raw, inner
    return raw, raw


# ---------- 角色卡 ----------
def list_cards():
    d = cards_dir()
    out = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith((".json", ".png")):
            continue
        card = sillytavern.load_character_card(os.path.join(d, fn))
        if not card:
            continue
        out.append({
            "file": fn,
            "name": card["name"] or fn,
            "format": "png" if fn.lower().endswith(".png") else "json",
            "has_custom_cover": os.path.isfile(_cover_path_readonly(d, fn)),
        })
    return out


def import_card(filename, data_b64):
    ext = os.path.splitext(str(filename or ""))[1].lower().lstrip(".")
    if ext not in ("json", "png"):
        raise ValueError("仅支持 .json / .png 角色卡")
    raw = _decode_b64(data_b64)
    if ext == "png":
        if raw[:8] != sillytavern.PNG_SIGNATURE:
            raise ValueError("不是有效的 PNG 文件")
        if sillytavern.load_character_card_raw_from_bytes(raw) is None:
            raise ValueError("PNG 角色卡缺少内嵌 chara 数据，请使用原酒馆导出的卡片")
    d = cards_dir()
    os.makedirs(d, exist_ok=True)
    target = _unique_path(d, os.path.basename(str(filename)))
    try:
        with open(target, "wb") as f:
            f.write(raw)
        card = sillytavern.load_character_card(target)
        if not card:
            os.remove(target)
            raise ValueError("文件内容不是有效的角色卡")
    except BaseException:
        try:
            os.remove(target)
        except OSError:
            pass
        raise
    return {"file": os.path.basename(target), "name": card["name"],
            "format": ext}


def card_detail(file):
    path = _resolve(cards_dir(), file)
    if not path or not os.path.isfile(path):
        raise ValueError("找不到角色卡：%s" % file)
    raw = sillytavern.load_character_card_raw(path)
    if raw is None:
        raise ValueError("角色卡解析失败：%s" % file)
    _root_data, inner = _card_parts(raw)
    ext = inner.get("extensions") or {}
    fields = {k: inner.get(k, "") for k in _CARD_FIELDS}
    fields["alternate_greetings"] = inner.get("alternate_greetings") or []
    fields["bot_name"] = ext.get("bot_name", "")
    fields["user_names"] = ext.get("user_names", "")
    fields["creator_notes"] = ext.get("creator_notes", "")
    fields["character_version"] = ext.get("character_version", "")
    return {
        "file": os.path.basename(path),
        "name": fields.get("name") or os.path.basename(path),
        "format": "png" if str(path).lower().endswith(".png") else "json",
        "fields": fields,
    }


def save_card(file, fields):
    path = _resolve(cards_dir(), file)
    if not path or not os.path.isfile(path):
        raise ValueError("找不到角色卡：%s" % file)
    raw = sillytavern.load_character_card_raw(path)
    if raw is None:
        raise ValueError("角色卡解析失败：%s" % file)
    _root_data, inner = _card_parts(raw)
    fields = fields or {}
    for k in _CARD_FIELDS:
        if k in fields:
            inner[k] = fields[k] if k == "tags" else str(fields[k] or "")
    if "alternate_greetings" in fields:
        ag = fields["alternate_greetings"]
        inner["alternate_greetings"] = (
            ag if isinstance(ag, list) else
            [s for s in str(ag or "").splitlines() if s.strip()])
    ext = inner.setdefault("extensions", {})
    if not isinstance(ext, dict):
        # 导入的畸形卡 extensions 可能不是对象 → 重建，避免 TypeError
        ext = {}
        inner["extensions"] = ext
    for k in ("bot_name", "user_names", "creator_notes", "character_version"):
        if k in fields:
            ext[k] = str(fields[k] or "")
    sillytavern.save_character_card(path, raw)
    card = sillytavern.load_character_card(path)
    return {"file": os.path.basename(path),
            "name": (card or {}).get("name") or ""}


def new_card(fields, cover_b64=None):
    fields = fields or {}
    name = str(fields.get("name") or "新角色").strip() or "新角色"
    from qqbot.persona import safe_name
    d = cards_dir()
    os.makedirs(d, exist_ok=True)
    target = _unique_path(d, safe_name(name) + ".json")
    raw = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": name,
            "description": str(fields.get("description") or ""),
            "personality": str(fields.get("personality") or ""),
            "scenario": str(fields.get("scenario") or ""),
            "system_prompt": str(fields.get("system_prompt") or ""),
            "mes_example": str(fields.get("mes_example") or ""),
            "first_mes": str(fields.get("first_mes") or ""),
            "post_history_instructions": str(
                fields.get("post_history_instructions") or ""),
            "alternate_greetings": [],
            "tags": [],
            "extensions": {
                "bot_name": str(fields.get("bot_name") or ""),
                "user_names": str(fields.get("user_names") or ""),
                "creator_notes": str(fields.get("creator_notes") or ""),
                "character_version": str(fields.get("character_version") or ""),
            },
        },
    }
    # 先校验封面：失败时不留孤儿卡（先写卡后设封面会留下"失败却已创建"的文件）
    cover_bytes = (_to_png_bytes(_decode_b64(cover_b64)) if cover_b64 else None)
    try:
        with open(target, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        if cover_bytes:
            _write_cover_file(cards_dir(), os.path.basename(target), cover_bytes)
    except BaseException:
        try:
            os.remove(target)
        except OSError:
            pass
        raise
    return {"file": os.path.basename(target), "name": name, "format": "json"}


def new_card_from_data(data, cover_b64=None):
    """把完整的角色卡 dict（chara_card_v2 或扁平结构）直接保存为新卡。"""
    if not isinstance(data, dict):
        raise ValueError("角色卡内容必须是 JSON 对象")
    if "data" in data and isinstance(data["data"], dict):
        inner = data["data"]
    else:
        inner = data
    if not isinstance(inner, dict):
        raise ValueError("角色卡内容必须是 JSON 对象")
    name = str(inner.get("name") or "新角色").strip() or "新角色"
    from qqbot.persona import safe_name
    d = cards_dir()
    os.makedirs(d, exist_ok=True)
    target = _unique_path(d, safe_name(name) + ".json")
    cover_bytes = (_to_png_bytes(_decode_b64(cover_b64)) if cover_b64 else None)
    try:
        with open(target, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if cover_bytes:
            _write_cover_file(cards_dir(), os.path.basename(target), cover_bytes)
    except BaseException:
        try:
            os.remove(target)
        except OSError:
            pass
        raise
    return {"file": os.path.basename(target), "name": name, "format": "json"}


# ---------- 封面 ----------
def _cover_path(base, file):
    d = _covers_dir(base)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _safe_stem(file) + ".png")


def _cover_path_readonly(base, file):
    """只判断路径不创建目录（list_cards 等读操作用，避免读操作产生副作用）。"""
    return os.path.join(_covers_dir(base), _safe_stem(file) + ".png")


def card_cover(file):
    base = cards_dir()
    custom = _cover_path(base, file)
    if os.path.isfile(custom):
        return custom
    path = _resolve(base, file)
    if path and str(path).lower().endswith(".png") and os.path.isfile(path):
        return path
    return None


def _write_cover_file(base, file, png_bytes):
    """把已归一化的 PNG 字节写成封面文件（new_* 的预校验路径复用）。"""
    target = _cover_path(base, file)
    with open(target, "wb") as f:
        f.write(png_bytes)
    return os.path.basename(target)


def set_card_cover(file, data_b64):
    raw = _to_png_bytes(_decode_b64(data_b64))
    return _write_cover_file(cards_dir(), file, raw)


def remove_card_cover(file):
    p = _cover_path(cards_dir(), file)
    if os.path.isfile(p):
        os.remove(p)
        return True
    return False


def worldbook_cover(file):
    base = books_dir()
    custom = _cover_path(base, file)
    if os.path.isfile(custom):
        return custom
    path = _resolve(base, file)
    if path and str(path).lower().endswith(".png") and os.path.isfile(path):
        return path
    return None


def set_worldbook_cover(file, data_b64):
    raw = _to_png_bytes(_decode_b64(data_b64))
    return _write_cover_file(books_dir(), file, raw)


def remove_worldbook_cover(file):
    p = _cover_path(books_dir(), file)
    if os.path.isfile(p):
        os.remove(p)
        return True
    return False


# ---------- 世界书 ----------
def import_worldbook(filename, data_b64):
    ext = os.path.splitext(str(filename or ""))[1].lower().lstrip(".")
    if ext not in ("json", "png"):
        raise ValueError("仅支持 .json / .png 世界书")
    raw = _decode_b64(data_b64)
    if ext == "png":
        if raw[:8] != sillytavern.PNG_SIGNATURE:
            raise ValueError("不是有效的 PNG 文件")
        data = sillytavern.load_worldbook_from_png_bytes(raw)
        if data is None:
            raise ValueError("PNG 世界书缺少内嵌数据，请使用原酒馆导出的文件")
    else:
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError("世界书 JSON 解析失败：%s" % exc)
    wrapped = False
    if not isinstance(data, dict) or not isinstance(
            data.get("entries", []), (dict, list)):
        stem = os.path.splitext(os.path.basename(str(filename)))[0]
        data = {"name": stem, "entries": data}
        wrapped = True
    d = books_dir()
    os.makedirs(d, exist_ok=True)
    target = _unique_path(d, os.path.basename(str(filename)))
    try:
        with open(target, "wb") as f:
            if ext == "png":
                f.write(raw)  # PNG 原样保存（内嵌数据已校验）
            elif wrapped:
                # 纯 entries 数组在内存里被包了一层：落盘必须写包装后的结构，
                # 否则下次 load 读到 list 会返回空
                f.write(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
            else:
                f.write(raw)
    except BaseException:
        try:
            os.remove(target)
        except OSError:
            pass
        raise
    return {"file": os.path.basename(target),
            "name": str(data.get("name") or target)}


def new_worldbook(name, cover_b64=None):
    name = str(name or "新世界书").strip() or "新世界书"
    from qqbot.persona import safe_name
    d = books_dir()
    os.makedirs(d, exist_ok=True)
    target = _unique_path(d, safe_name(name) + ".json")
    cover_bytes = (_to_png_bytes(_decode_b64(cover_b64)) if cover_b64 else None)
    try:
        with open(target, "w", encoding="utf-8") as f:
            json.dump({"name": name, "entries": {}}, f,
                      ensure_ascii=False, indent=2)
        if cover_bytes:
            _write_cover_file(books_dir(), os.path.basename(target), cover_bytes)
    except BaseException:
        try:
            os.remove(target)
        except OSError:
            pass
        raise
    return {"file": os.path.basename(target), "name": name}


def export_card(file, fmt="json", out_path=None):
    path = _resolve(cards_dir(), file)
    if not path or not os.path.isfile(path):
        raise ValueError("找不到角色卡：%s" % file)
    raw = sillytavern.load_character_card_raw(path)
    if raw is None:
        raise ValueError("角色卡解析失败：%s" % file)
    text = json.dumps(raw, ensure_ascii=False)
    if fmt == "png":
        cover = card_cover(file)
        cover_bytes = None
        if cover:
            with open(cover, "rb") as f:
                cover_bytes = f.read()
        base = None
        if str(path).lower().endswith(".png"):
            with open(path, "rb") as f:
                base = f.read()
        data = sillytavern.build_png_with_text(
            base, "chara", text, cover_bytes=cover_bytes)
    else:
        data = text.encode("utf-8")
    return _write_export(data, out_path, want_ext="png" if fmt == "png" else "json")


def export_worldbook(file, fmt="json", out_path=None):
    path = _resolve(books_dir(), file)
    if not path or not os.path.isfile(path):
        raise ValueError("找不到世界书：%s" % file)
    if str(path).lower().endswith(".png"):
        # PNG 世界书：内嵌数据是二进制，文本模式读取会 UnicodeDecodeError
        with open(path, "rb") as f:
            raw = sillytavern.load_worldbook_from_png_bytes(f.read())
        if raw is None:
            raise ValueError("PNG 世界书缺少内嵌数据：%s" % file)
    else:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    text = json.dumps(raw, ensure_ascii=False)
    if fmt == "png":
        cover = worldbook_cover(file)
        cover_bytes = None
        if cover:
            with open(cover, "rb") as f:
                cover_bytes = f.read()
        base = None
        if str(path).lower().endswith(".png"):
            with open(path, "rb") as f:
                base = f.read()
        data = sillytavern.build_png_with_text(
            base, "worldbook", text, cover_bytes=cover_bytes)
    else:
        data = text.encode("utf-8")
    return _write_export(data, out_path, want_ext="png" if fmt == "png" else "json")


def _write_export(data, out_path, want_ext=None):
    """落盘导出；out_path 为空则直接返回数据（由调用方决定，如 HTTP 下载）。

    want_ext 强制目标扩展名与导出格式一致：此前只校验"在项目目录内"，
    于是 save_path 传 <项目>/.env 就能把 .env/源码覆盖成 JSON。
    """
    if out_path:
        out_path = str(out_path)
        if want_ext and not out_path.lower().endswith("." + want_ext.lower()):
            raise ValueError("导出为 %s 时，保存路径必须以 .%s 结尾" % (want_ext, want_ext))
        d = os.path.dirname(os.path.abspath(out_path))
        os.makedirs(d, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(data)
        return {"ok": True, "path": os.path.abspath(out_path)}
    return data
