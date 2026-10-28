# -*- coding: utf-8 -*-
"""流动性时间关键词解析。

记忆里存的是固定剧情时间戳（"第1周周一上午"），用户说话时用的是相对说法
（"今天/昨天/前天/上周X/昨晚/第一周周一上午"）。本模块在**查询时**按当前
世界时钟把这些说法解析成具体剧情日前缀 + 可选时段集合：

    resolve_time_windows("昨天上午", clock) -> [("第1周周日", {"上午"})]

- 纯本地正则 + 算术，零 token；时钟一变，同一批记忆的"称呼"自动跟着变
- 支持显式剧情时间戳（含中文数字"第一周周一上午"）
"""
import re

from qqbot import textutil
from qqbot.timeline import TIME_OF_DAY, WEEKDAYS

_WEEKDAY_CN = {
    "一": "周一", "二": "周二", "三": "周三", "四": "周四",
    "五": "周五", "六": "周六", "日": "周日", "天": "周日",
}
_CN_DIGITS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100,
}

# 显式剧情时间戳："第1周周一上午" / "第一周 周日 深夜" / "第2周星期六晚上"
_STAMP_RE = re.compile(
    r"第([一二三四五六七八九十百\d零]+)周\s*(?:周|星期|礼拜)?([一二三四五六日天])"
    r"(凌晨|清晨|早晨|早上|上午|中午|午后|下午|傍晚|夜晚|睡前|放学后|晚上|深夜)?"
)

# 相对偏移锚点（按词长降序匹配，避免"大前天"被"前天"截胡）
_OFFSET_ANCHORS = [
    ("大前天", -3, None),
    ("前天", -2, None),
    ("前晚", -2, {"晚上", "深夜"}),
    ("前夜", -2, {"晚上", "深夜"}),
    ("昨晚", -1, {"晚上", "深夜"}),
    ("昨夜", -1, {"晚上", "深夜"}),
    ("昨早", -1, {"早上"}),
    ("昨天", -1, None),
    ("昨日", -1, None),
    ("今早", 0, {"早上"}),
    ("今晨", 0, {"早上"}),
    ("今晚", 0, {"晚上", "深夜"}),
    ("今天", 0, None),
    ("今日", 0, None),
]

_WEEK_PREV_RE = re.compile(r"上个星期|上星期|上礼拜|上周")
_WEEK_THIS_RE = re.compile(r"这个星期|这星期|这礼拜|本周|这周")
_DAY_AFTER_RE = re.compile(r"(?:周|星期|礼拜)?([一二三四五六日天])")


def _cn_to_int(s):
    """中文数字 → 整数（支持 一~九十九、一百、一百零五）；阿拉伯数字原样。"""
    if s.isdigit():
        return int(s)
    total = 0
    section = 0
    num = 0
    for ch in s:
        if ch == "零":
            continue
        v = _CN_DIGITS.get(ch)
        if v is None:
            return None
        if ch == "百":
            section = (num or 1) * 100
            num = 0
        elif ch == "十":
            section += (num or 1) * 10
            num = 0
        else:
            num = v
    total = section + num
    return total or None


def _day_prefix(week, weekday_idx, offset):
    """当前(week, weekday_idx) 往前/往后 offset 天的剧情日前缀（自动跨周，周数钳制 ≥1）。"""
    idx = weekday_idx + offset
    w = week
    while idx < 0:
        idx += 7
        w -= 1
    while idx >= 7:
        idx -= 7
        w += 1
    if w < 1:
        w = 1
    return "第%d周%s" % (w, WEEKDAYS[idx])


def _time_token_after(text, pos):
    """取锚点之后短窗口内的第一个时段词；没有返回 None。

    只扫锚点后 6 个字符：避免「昨天去了公园，晚上吃饭」把"晚上"误配给"昨天"
    （那个"晚上"修饰的是后面的句子，不是锚点）。
    """
    tail = text[pos:pos + 6]
    for t in TIME_OF_DAY:
        if t in tail:
            return {t}
    return None


def match_stamp(stamp, time_windows):
    """剧情时间戳（"第1周周一上午"）是否命中任一 (前缀, 时段集合)。"""
    if not stamp:
        return False
    for prefix, windows in time_windows:
        if not stamp.startswith(prefix):
            continue
        if windows is None:
            return True
        if stamp[len(prefix):] in windows:
            return True
    return False


def memory_stamp(content, story_time=None):
    """记忆条目 → 剧情时间戳（优先 content 头部，其次 story_time 列）。"""
    stamp = textutil.parse_story_time(content) if content else None
    return stamp or story_time or ""


def resolve_time_windows(text, clock):
    """把消息里的相对时间说法解析成 [(剧情日前缀, 时段集合|None)]；无时间引用返回 []。"""
    if not text or not clock:
        return []
    week = int(clock.get("week", 1))
    cur_wd = clock.get("weekday", "周一")
    cur_idx = WEEKDAYS.index(cur_wd) if cur_wd in WEEKDAYS else 0
    results = []

    # 1) 显式剧情时间戳（含中文数字）："第一周周一上午"
    for m in _STAMP_RE.finditer(text):
        w = _cn_to_int(m.group(1))
        if not w or m.group(2) not in _WEEKDAY_CN:
            continue
        windows = {textutil.PERIOD_NORMALIZE.get(m.group(3), m.group(3))} \
            if m.group(3) else None
        results.append(("第%d周%s" % (w, _WEEKDAY_CN[m.group(2)]), windows))

    # 2) 相对偏移（今天/昨天/前天/昨晚…），长词优先并跳过重叠区间
    ranges = []
    for word, offset, windows in sorted(_OFFSET_ANCHORS, key=lambda a: -len(a[0])):
        for m in re.finditer(re.escape(word), text):
            if any(s <= m.start() and m.end() <= e for s, e in ranges):
                continue
            ranges.append((m.start(), m.end()))
            tw = _time_token_after(text, m.end())
            win = windows or tw
            results.append((_day_prefix(week, cur_idx, offset), win))

    # 3) 上周X / 这周X（可带"周/星期/礼拜"前缀或裸日字；不带日 = 整周）
    for m in _WEEK_PREV_RE.finditer(text):
        dm = _DAY_AFTER_RE.search(text[m.end():])
        tw = _time_token_after(text, m.end())
        if dm and dm.group(1) in _WEEKDAY_CN:
            results.append(("第%d周%s" % (max(1, week - 1), _WEEKDAY_CN[dm.group(1)]), tw))
        else:
            w = max(1, week - 1)
            for i in range(7):
                results.append(("第%d周%s" % (w, WEEKDAYS[i]), tw))
    for m in _WEEK_THIS_RE.finditer(text):
        dm = _DAY_AFTER_RE.search(text[m.end():])
        tw = _time_token_after(text, m.end())
        if dm and dm.group(1) in _WEEKDAY_CN:
            results.append(("第%d周%s" % (week, _WEEKDAY_CN[dm.group(1)]), tw))
        else:
            for i in range(7):
                results.append(("第%d周%s" % (week, WEEKDAYS[i]), tw))

    # 4) 去重
    out = []
    seen = set()
    for prefix, windows in results:
        key = (prefix, frozenset(windows) if windows else None)
        if key not in seen:
            seen.add(key)
            out.append((prefix, windows))
    return out
