# -*- coding: utf-8 -*-
"""通用文本工具：n-gram 分词、JSON 提取与剧情时间戳解析。"""
import json
import re

# 日志脱敏：NapCat/OneBot 的 token=xxx、Authorization、sk- 密钥不能进日志缓冲。
# 管理台与桌面服务共用这一份实现（两处各写一份正则容易漏掉新增渠道）。
_SECRET_PATTERNS = (
    (re.compile(r"(token=)[^&\s\"']+", re.I), r"\1***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+"), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9._\-]{6,}"), "sk-***"),
)


def redact(text):
    """把文本里的凭据替换成 ***（用于日志/异常信息）。"""
    out = str(text)
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out


STORY_TIME_RE = re.compile(
    r"^(?:第(\d+)周)?(周[一二三四五六日天]|星期[一二三四五六日天])"
    r"(早上|早晨|清晨|上午|中午|午后|下午|傍晚|晚上|夜晚|睡前|深夜|凌晨|放学后)"
)

# 非标准时段词 → 标准时段（用于 parse_story_time 归一化）
PERIOD_NORMALIZE = {
    "早晨": "早上",
    "清晨": "早上",
    "午后": "下午",
    "夜晚": "晚上",
    "睡前": "晚上",
    "放学后": "下午",
}


def ngrams(text, n=2):
    """去除标点/空白后按字符二元组切分（中文检索用）。"""
    text = re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’()（）\[\]【】-]+", "", text).lower()
    if len(text) <= n:
        return {text} if text else set()
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def chars_to_tokens(chars):
    """按中文为主的估算把字符预算换算成 token 上限（用于 max_tokens）。"""
    try:
        n = int(chars)
    except (TypeError, ValueError):
        n = 0
    return max(64, min(4096, int(n * 1.2 + 0.999)))


def parse_story_time(content):
    """从内容开头解析剧情时间戳（如"周六下午"或"第2周周六下午"）；无则返回 None。

    重要记忆 content 以剧情时间开头，UI/vault 导出用此恢复剧情时间。
    """
    if not content:
        return None
    m = STORY_TIME_RE.match(str(content).strip())
    if not m:
        return None
    week, wd, tod = m.group(1), m.group(2), m.group(3)
    return (("第%s周" % week) if week else "") + wd.replace("星期", "周") + \
        PERIOD_NORMALIZE.get(tod, tod)


_DEDUP_STRIP_RE = re.compile(r"[\s，。！？、,.!?;；:：\"'“”‘’()（）【】\[\]-]+")

# 关键词派生时过滤的高频虚词（只是启发式：派生词命中说明"用户提到了这条记忆里的东西"）
KEYWORD_STOPWORDS = {
    "这个", "那个", "什么", "怎么", "我们", "你们", "他们", "她们", "自己", "然后",
    "因为", "所以", "但是", "如果", "就是", "还是", "可以", "时候", "现在", "之后",
    "开始", "感觉", "有点", "真的", "非常", "其实", "这样", "那样", "一个", "不是",
    "没有", "今天", "昨天", "明天", "一下", "一起", "已经", "而且", "于是", "最后",
    "忽然", "突然", "似乎", "好像", "只是", "不过", "轻轻", "终于", "同时",
}


def dedup_key(text):
    """去重键：忽略空白与标点差异（"一起去 欢乐谷！" == "一起去欢乐谷"）。"""
    return _DEDUP_STRIP_RE.sub("", str(text or ""))


def overlap_ratio(query_grams, text):
    """查询 2-gram 在正文中的命中比例（0~1，长度无关）。

    用原始命中个数会让长文本系统性占优（长度偏置）：长条目更容易凑出命中。
    """
    if isinstance(query_grams, str):
        query_grams = ngrams(query_grams)
    if not query_grams:
        return 0.0
    return len(query_grams & ngrams(text)) / float(len(query_grams))


def parse_story_week(story_time):
    """从剧情时间戳取周数（"第3周周一晚上" → 3）；无周数返回 None。"""
    m = re.search(r"第\s*(\d+)\s*周", str(story_time or ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


_SEG_SPLIT_RE = re.compile(r"[\s，。！？、,.;:!?…—\-（）()「」『』【】《》\"'“”‘’/\\|]+")
# 中文常见虚词/量词/高频动词：切开后剩下的片段更接近实义词
_PARTICLE_CHARS = ("的了在给和与就都也很又还而把被让从对为以之其此吧呢啊呀哦嘛则等个着过"
                   "坐吃喝拍买做看说走跑去来叫想要会能可与跟向")
_STAMP_HEAD_RE = re.compile(
    r"^\s*(?:第\s*\d+\s*周)?\s*(?:周[一二三四五六日天]|星期[一二三四五六日天])?\s*"
    r"(?:早上|早晨|清晨|上午|中午|午后|下午|傍晚|晚上|夜晚|睡前|深夜|凌晨|放学后)?\s*[，,、:：]?\s*")


def derive_keywords(content, limit=5):
    """从内容派生关键词（提炼模型漏给 keywords 时的兜底索引）。

    规则：去掉剧情时间戳前缀 → 按标点/虚词切片段 → 2~4 字片段直接采用，
    更长的片段取**结尾** 2~3 字（中文偏正短语的中心词常在尾部：
    "坐了过山车"→过山车、"拍了一段视频"→视频）→ 过滤虚词/纯数字/互相包含 →
    长词优先，取前 limit 个。

    只是启发式（没有分词器的现实取舍）：派生词都真实出现在这条记忆里，
    命中即代表"用户提到了这条记忆里的东西"，所以对召回是增益而非噪声；
    旧数据的批量回填用 `python memory_ui.py keywordize`（LLM 生成，质量更高）。
    """
    text = _STAMP_HEAD_RE.sub("", str(content or ""), count=1)
    cands = []
    for seg in _SEG_SPLIT_RE.split(text):
        for piece in re.split("[" + _PARTICLE_CHARS + "]+", seg):
            piece = piece.strip("　 ")
            if len(piece) < 2:
                continue
            if len(piece) <= 4:
                cands.append(piece)
            else:
                cands.append(piece[-3:])
                cands.append(piece[-2:])
    out = []
    for word in sorted(set(cands), key=len, reverse=True):
        if word in KEYWORD_STOPWORDS or word.isdigit():
            continue
        if any(a in word for a in out):
            continue
        out.append(word)
        if len(out) >= max(1, int(limit)):
            break
    return out


def json_extract(text):
    """从 LLM 输出中稳健提取 JSON（对象或数组），失败返回 None。

    依次尝试：去掉 ```json 围栏 → 直接解析 → 平衡括号扫描。
    """
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = t.find(open_ch)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(t)):
            if t[i] == open_ch:
                depth += 1
            elif t[i] == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start : i + 1])
                    except Exception:
                        break
    return None
