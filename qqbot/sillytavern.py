# -*- coding: utf-8 -*-
"""酒馆（SillyTavern/RisuAI）格式加载器：角色卡 + 世界书。

- 角色卡：spec v2 JSON（chara_card_v2），或 PNG 卡片（tEXt 块 "chara" 内嵌 base64 JSON）
- 世界书：ST/RisuAI 兼容的 entries（list 或 dict），支持 keys/key、constant、enabled、
  insertion_order/order、position、case_sensitive
- 两级注入：
  1. 基础注入：常驻世界书设定（constant 条目，或 base_mode=all 时全部条目）
  2. 二次强调：当前消息命中触发词时，把对应条目内容重新投喂给模型（紧贴用户消息）
"""
import base64
import json
import os
import re
import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# ---------- 角色卡 ----------
def png_text_chunk(data, keyword):
    """从 PNG 字节的 tEXt 块里取指定 keyword 的文本（统一走带边界校验的解析器）。"""
    kw = keyword.encode("latin-1")
    for ctype, payload in read_png_chunks(data):
        if ctype == b"tEXt":
            k, _, text = payload.partition(b"\x00")
            if k == kw:
                return text.decode("utf-8", "replace")
    return None


def _png_card_text(path):
    """从 PNG tEXt 块提取 keyword='chara' 的文本（base64 JSON）。

    与 read_png_chunks 共用同一套解析：旧实现另写了一份流式解析、
    不校验 chunk 长度边界，畸形 length 会让后续读取整体错位，
    卡片"看起来存在却读不出来"。
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return png_text_chunk(data, "chara")


def load_character_card(path):
    """读取角色卡 → 返回规范化 dict。

    支持：
    - JSON：chara_card_v2（spec / spec_version / data）
    - PNG：内嵌 chara 块（base64 JSON，同上结构）
    """
    if not path or not os.path.isfile(path):
        return None
    data = load_character_card_raw(path)
    if data is None:
        return None
    card = data.get("data") if isinstance(data, dict) and "data" in data else data
    if not isinstance(card, dict):
        return None
    return {
        "name": str(card.get("name") or "").strip(),
        "description": str(card.get("description") or "").strip(),
        "personality": str(card.get("personality") or "").strip(),
        "scenario": str(card.get("scenario") or "").strip(),
        "first_mes": str(card.get("first_mes") or "").strip(),
        "mes_example": str(card.get("mes_example") or "").strip(),
        "system_prompt": str(card.get("system_prompt") or "").strip(),
        "post_history_instructions": str(card.get("post_history_instructions") or "").strip(),
        "tags": card.get("tags") or [],
        "extensions": card.get("extensions") or {},
        "character_book": card.get("character_book") or None,
    }


def load_character_card_raw(path):
    """读取角色卡 → 返回原始 JSON dict（保留 spec v2 结构，供编辑/导出）。"""
    if not path or not os.path.isfile(path):
        return None
    if path.lower().endswith(".png"):
        raw = _png_card_text(path)
        if not raw:
            return None
        try:
            return json.loads(base64.b64decode(raw))
        except Exception:
            return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_character_card_raw_from_bytes(data):
    """从 PNG 字节读取角色卡原始 JSON（tEXt 块 chara）。"""
    if data[:8] != PNG_SIGNATURE:
        return None
    for ctype, payload in read_png_chunks(data):
        if ctype != b"tEXt":
            continue
        keyword, _, text = payload.partition(b"\x00")
        if keyword == b"chara":
            try:
                return json.loads(base64.b64decode(text).decode("utf-8"))
            except Exception:
                return None
    return None


def _fill_placeholders(text, user_display, char_name):
    if not text:
        return text
    # 用 lambda 替换：用户/角色名里的反斜杠（如 \g<1>）不会被当正则替换串解析
    text = re.sub(r"\{\{\s*user\s*\}\}",
                  lambda m: str(user_display), text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{\s*char\s*\}\}",
                  lambda m: str(char_name or "角色"), text, flags=re.IGNORECASE)
    return text


def fill_placeholders(text, user_display="用户", char_name="角色"):
    """公开占位符替换：{{user}} / {{char}}。"""
    return _fill_placeholders(text, user_display, char_name)


def card_system_prompt(card, user_display="用户"):
    """角色卡 → system prompt 文本。"""
    if not card:
        return ""
    name = card.get("name") or "角色"
    parts = []
    if card.get("description"):
        parts.append("【角色设定】\n" + card["description"])
    if card.get("personality"):
        parts.append("【性格】\n" + card["personality"])
    if card.get("scenario"):
        parts.append("【场景】\n" + card["scenario"])
    if card.get("system_prompt"):
        parts.append("【系统提示】\n" + card["system_prompt"])
    if card.get("mes_example"):
        parts.append("【对话示例】\n" + card["mes_example"])
    if card.get("post_history_instructions"):
        parts.append("【历史后置指令】\n" + card["post_history_instructions"])
    header = "【角色卡：%s】" % name
    body = "\n\n".join(parts) if parts else "（角色卡无设定内容）"
    text = header + "\n" + body
    return _fill_placeholders(text, user_display, name)


# ---------- 世界书 ----------
_KEY_SPLIT_RE = re.compile(r"[、，,；;|/\n]+")


def _norm_keys(entry):
    keys = entry.get("keys") or entry.get("key") or []
    return _split_keys(keys)


def _split_keys(raw):
    """触发词归一化：字符串/列表 → 去重关键词列表。

    兼容酒馆导出里常见的"顿号/逗号拼成一串"写法（如 keys: ["口交、手淫"]），
    拆成独立关键词，避免整串必须原样出现在消息里才命中的问题。
    """
    keys = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) else [])
    out = []
    for k in keys:
        if not isinstance(k, str):
            continue
        for piece in _KEY_SPLIT_RE.split(k):
            piece = piece.strip()
            if piece and piece not in out:
                out.append(piece)
    return out


def _norm_entry(raw):
    if not isinstance(raw, dict):
        return None
    content = str(raw.get("content") or "").strip()
    enabled = bool(raw.get("enabled", True)) and not bool(raw.get("disable", False))
    if not enabled or not content:
        return None
    order = raw.get("insertion_order", raw.get("order", 0))
    try:
        order = int(order)
    except (TypeError, ValueError):
        order = 0
    return {
        "keys": _norm_keys(raw),
        "secondary_keys": _split_keys(
            raw.get("keysecondary") or raw.get("secondary_keys")),
        "content": content,
        "constant": bool(raw.get("constant", False)),
        "position": raw.get("position", 0),
        "order": order,
        "case_sensitive": bool(raw.get("case_sensitive", False)),
        "name": str(raw.get("name") or "").strip(),
        "comment": str(raw.get("comment") or "").strip(),
    }


def load_world_book(path):
    """读取一个世界书文件（.json 或内嵌 PNG）→ 规范化条目列表。"""
    if not path or not os.path.isfile(path):
        return []
    if path.lower().endswith(".png"):
        with open(path, "rb") as f:
            data = load_worldbook_from_png_bytes(f.read())
        if data is None:
            return []
    else:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    return entries_from_worldbook(data)


def load_worldbook_from_png_bytes(data):
    """从 PNG 字节读取内嵌世界书 JSON（tEXt 块 worldbook / chara）。"""
    if data[:8] != PNG_SIGNATURE:
        return None
    for ctype, payload in read_png_chunks(data):
        if ctype != b"tEXt":
            continue
        keyword, _, text = payload.partition(b"\x00")
        if keyword in (b"worldbook", b"chara"):
            try:
                return json.loads(base64.b64decode(text).decode("utf-8"))
            except Exception:
                return None
    return None


def read_png_chunks(data):
    """解析 PNG 字节 → [(chunk_type, payload), ...]。"""
    chunks = []
    if data[:8] != PNG_SIGNATURE:
        return chunks
    pos = 8
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        # 校验 chunk 边界：畸形 PNG 的 length 会超出剩余数据导致错位解析
        if length > len(data) - (pos + 8):
            break
        payload = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        chunks.append((ctype, payload))
        if ctype == b"IEND":
            break
    return chunks


def png_is_sane(data):
    """PNG 结构自检：签名 + chunk 长度边界 + 见到 IHDR/IEND。

    CRC 不匹配只忽略（不少工具写得并不规范），但**长度越界/缺 IEND**
    说明文件被截断或损坏——这时重建 PNG 会丢掉后续块，必须拒绝而不是静默重写。
    """
    if not data or data[:8] != PNG_SIGNATURE:
        return False
    pos, saw_ihdr, saw_iend = 8, False, False
    while pos + 12 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        if length > len(data) - (pos + 12):
            return False
        if ctype == b"IHDR":
            saw_ihdr = True
        pos += 12 + length
        if ctype == b"IEND":
            saw_iend = True
            break
    return saw_ihdr and saw_iend


def _png_chunk(ctype, payload):
    return (struct.pack(">I", len(payload)) + ctype + payload
            + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF))


def _placeholder_png():
    """生成默认封面底图（PIL 可用时画深色底 + 描边，否则 1x1 PNG）。"""
    try:
        from PIL import Image, ImageDraw
        import io
        img = Image.new("RGB", (512, 512), (15, 23, 42))
        ImageDraw.Draw(img).rectangle(
            [0, 0, 511, 511], outline=(110, 139, 255), width=10)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def build_png_with_text(png_bytes, keyword, text, cover_bytes=None):
    """生成 PNG：保留原图（或使用封面图/占位图），内嵌 keyword=tEXt 数据。

    keyword 传 "chara"（角色卡）或 "worldbook"（世界书）；text 为 JSON 明文，
    写入时会按酒馆惯例做 base64。
    """
    base = cover_bytes or png_bytes
    if base and base[:8] == PNG_SIGNATURE and not png_is_sane(base):
        # 结构损坏（截断/长度越界）：重建会丢掉损坏点之后的块，宁可报错
        raise ValueError("PNG 结构损坏（chunk 越界或缺少 IEND），请重新导入原图后再保存")
    if not base or base[:8] != PNG_SIGNATURE:
        base = _placeholder_png()
    kw = keyword.encode("latin-1")
    payload = kw + b"\x00" + base64.b64encode(text.encode("utf-8"))
    kept = []
    for ctype, chunk_payload in read_png_chunks(base):
        if ctype == b"tEXt":
            k, _, _ = chunk_payload.partition(b"\x00")
            if k == kw:
                continue
        kept.append((ctype, chunk_payload))
    out = bytearray(PNG_SIGNATURE)
    inserted = False
    for ctype, chunk_payload in kept:
        if ctype == b"IEND" and not inserted:
            out += _png_chunk(b"tEXt", payload)
            inserted = True
        out += _png_chunk(ctype, chunk_payload)
    if not inserted:
        out += _png_chunk(b"tEXt", payload)
        out += _png_chunk(b"IEND", b"")
    return bytes(out)


def save_character_card(path, data):
    """把角色卡 dict 写回文件：JSON 直接写；PNG 保留原图并替换 chara 块。

    原子写（临时文件 + os.replace）：写入中断不会截断原卡片。"""
    text = json.dumps(data, ensure_ascii=False)
    if str(path).lower().endswith(".png"):
        base = None
        if os.path.isfile(path):
            with open(path, "rb") as f:
                base = f.read()
        blob = build_png_with_text(base, "chara", text, cover_bytes=base)
    else:
        blob = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    tmp = path + ".tmp"
    mode = "wb" if isinstance(blob, bytes) else "w"
    with open(tmp, mode) as f:
        f.write(blob)
    os.replace(tmp, path)


def entries_from_worldbook(data):
    """世界书 dict（文件顶层或角色卡内嵌 character_book）→ 规范化条目列表。"""
    raw_entries = data.get("entries", []) if isinstance(data, dict) else []
    if isinstance(raw_entries, dict):
        raw_entries = list(raw_entries.values())
    entries = [e for e in (_norm_entry(e) for e in raw_entries) if e]
    entries.sort(key=lambda e: e["order"])
    return entries


def load_world_books(book_dir=None, files=None, log=None):
    """合并世界书：book_dir 下所有 .json + files 显式列表（去重）。

    单个文件损坏不影响其余文件加载（坏文件跳过并记日志）。"""
    log = log or (lambda msg: None)
    entries = []
    paths = []
    if book_dir and os.path.isdir(book_dir):
        paths += sorted(
            os.path.join(book_dir, fn)
            for fn in os.listdir(book_dir)
            if fn.lower().endswith((".json", ".png"))
        )
    for fp in files or []:
        if fp and os.path.isfile(fp) and fp not in paths:
            paths.append(fp)
    for p in paths:
        try:
            entries += load_world_book(p)
        except Exception as exc:
            log("[worldbook] 跳过损坏文件 %s: %s" % (os.path.basename(p), exc))
    # 按插入顺序稳定排序
    entries.sort(key=lambda e: e["order"])
    return entries


def _entry_label(entry):
    return entry["name"] or (entry["keys"][0] if entry["keys"] else "设定")


def build_world_base(entries, mode="all", max_chars=6000):
    """基础注入块：constant 条目必入；mode=all 时全部条目入。按 order 截断。"""
    picked = [e for e in entries if e["constant"]]
    if mode == "all":
        for e in entries:
            if e not in picked:
                picked.append(e)
    picked.sort(key=lambda e: e["order"])
    lines = []
    used = 0
    for e in picked:
        label = _entry_label(e)
        text = "- [%s] %s" % (label, e["content"].replace("\n", " "))
        if used + len(text) > max_chars and lines:
            break
        lines.append(text)
        used += len(text)
    if not lines:
        return ""
    return ("【世界书·常驻设定】（优先级：世界书设定高于角色卡人格预设，冲突时以世界书为准）\n"
            + "\n".join(lines))


def match_world_entries(entries, text, case_sensitive=None,
                        extra_context=None, synonyms=None):
    """当前消息命中触发词的条目（含 constant，用于二次强调）。

    - 主触发词（keys/key）：匹配当前消息 + extra_context（可选近文，模拟酒馆递归扫描）
    - 次触发词（keysecondary/secondary_keys）：只匹配当前消息，避免近文过度命中
    - synonyms：{别名: 主触发词} 或 {别名: [主触发词...]}，消息命中别名等价于命中主触发词
    - 命中条目会标注 e["_via"] = key / secondary / alias，供日志与调试
    """
    if not text or not entries:
        return []
    alias_map = {}
    if isinstance(synonyms, dict) and synonyms:
        for alias, canons in synonyms.items():
            alias = str(alias).strip()
            if not alias:
                continue
            if isinstance(canons, str):
                canons = [canons]
            for c in canons:
                c = str(c).strip()
                if c:
                    alias_map.setdefault(alias, set()).add(c)
    hay = text if case_sensitive else text.lower()
    ctx = (extra_context or "") if case_sensitive else (extra_context or "").lower()
    matched = []
    for e in entries:
        keys = e["keys"]
        sks = e.get("secondary_keys") or []
        if not keys and not sks:
            continue
        cs = e["case_sensitive"] if case_sensitive is None else case_sensitive
        t_hay = hay if not cs else text
        t_ctx = ctx if not cs else (extra_context or "")
        hit = None
        for k in keys:
            kk = k if cs else k.lower()
            if kk and (kk in t_hay or (t_ctx and kk in t_ctx)):
                hit = "key"
                break
        if hit is None:
            for k in sks:
                kk = k if cs else k.lower()
                if kk and kk in t_hay:
                    hit = "secondary"
                    break
        if hit is None and alias_map:
            kset = {k.lower() for k in keys} if not cs else set(keys)
            for alias, canons in alias_map.items():
                aa = alias if cs else alias.lower()
                if not aa or not (aa in t_hay or (t_ctx and aa in t_ctx)):
                    continue
                cset = {c.lower() for c in canons} if not cs else set(canons)
                if kset & cset:
                    hit = "alias"
                    break
        if hit:
            e["_via"] = hit
            matched.append(e)
    return matched


def build_world_emphasis(entries, max_chars=1500):
    """二次强调块：触发词命中后，把对应条目内容再次投喂给模型。"""
    if not entries:
        return ""
    lines = []
    used = 0
    for e in entries:
        label = _entry_label(e)
        text = "- [%s] %s" % (label, e["content"].replace("\n", " "))
        if used + len(text) > max_chars:
            continue
        lines.append(text)
        used += len(text)
    if not lines:
        return ""
    return ("【世界书·强调】（本条消息命中触发词，优先级最高：与角色卡人格预设冲突时，"
            "以本条世界书内容为准并严格执行）\n" + "\n".join(lines))
