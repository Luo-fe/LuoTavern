# -*- coding: utf-8 -*-
"""世界时间（全局唯一）：周一..周日 × 时段 × 可选 HH:MM，存 data/time/clock.json。

- 【时间：xxx】指令：正则解析（周X / 时段 / 几点 / HH:MM / 点半）
- 自然语言：LLM 抽取（"今天是周一" → weekday；"睡了一觉到第二天" → advance_day）
- 查询：现在几点了/今天周几 → query_answer
- advance_day：周日夜→周一，时段归"早上"，清空时刻
- stamp()：剧情时间戳单 token（"第1周周六下午"）；周日→周一自动周数+1；set 字段标记时钟是否已设置（时间门控用）
"""
import json
import os
import re
import threading
import time

from . import textutil

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
# 凌晨是一天的开始（0-5 点），排在早上之前；深夜是一天末尾
TIME_OF_DAY = ["凌晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "深夜"]
_WEEKDAY_MAP = {"一": "周一", "二": "周二", "三": "周三", "四": "周四", "五": "周五", "六": "周六", "日": "周日", "天": "周日"}

TIME_EXTRACT_PROMPT = """你是对话时间解析器。从用户消息中判断是否提到"世界时间"信息，只输出 JSON，不要其他内容：
{"weekday": null|"周一".."周日", "time_of_day": null|"早上"|"上午"|"中午"|"下午"|"傍晚"|"晚上"|"深夜"|"凌晨", "hhmm": null|"HH:MM", "query_time": false}
规则：
- 用户说"今天是周一/周二…" → weekday 填对应值
- 用户说"现在是8点/下午3点半" → time_of_day 与 hhmm（3点半→"15:30"，8点→"08:00"）
- 换天（第二天/天亮/睡醒）只能通过【青柠好好睡了一觉】指令触发，消息里出现这些词不要推进时间
- 用户问"现在几点了/今天周几/什么时间了" → query_time=true
- 只提取当前时刻；对过去事件的追述（"下午的时候""刚才""昨天""那天傍晚"）一律忽略，不填
- 没提到任何时间信息 → 全部 null/false"""


class Timeline:
    def __init__(self, bot, path=None):
        self.bot = bot
        cfg = bot.config.get("time", {})
        base = os.path.dirname(os.path.abspath(__file__))
        rel = path or cfg.get("path", "data/time/clock.json")
        self.path = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(base, "..", rel))
        self.enabled = cfg.get("enable", True)
        self.lock = threading.RLock()  # apply 持锁内会调 _save，需可重入
        self.default = {
            "week": 1,  # 剧情第几周（周日睡到周一自动 +1）
            "weekday": cfg.get("default_weekday", "周一"),
            "time_of_day": cfg.get("default_time_of_day", "早上"),
            "hhmm": None,
            "set": False,  # 时钟是否已被明确设置（时间门控）
        }
        self.clock = self._load()
        # 时间线配套文件：默认与 clock.json 同目录（测试/多环境自动隔离）
        clock_dir = os.path.dirname(self.path) or "."
        base = os.path.dirname(os.path.abspath(__file__))
        self.undo_size = max(1, int(cfg.get("undo_size", 10)))
        self.undo_path = self._sidecar_path(cfg, "undo_path", clock_dir, "undo.json", base)
        self.history_path = self._sidecar_path(cfg, "history_path", clock_dir, "history.json", base)
        self.last_seen_path = self._sidecar_path(cfg, "last_seen_path", clock_dir, "last_seen.json", base)
        self._undo = self._load_undo()
        self._history = self._load_history()
        self._last_seen = self._load_last_seen()

    @staticmethod
    def _sidecar_path(cfg, key, clock_dir, default_name, base):
        """配套文件路径：显式配置（绝对/相对项目根）优先，否则放 clock.json 同目录。"""
        rel = cfg.get(key)
        if rel:
            return rel if os.path.isabs(rel) else os.path.normpath(os.path.join(base, "..", rel))
        return os.path.join(clock_dir, default_name)

    def _load(self):
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                clock = dict(self.default)
                try:
                    w = int(data.get("week", 1))
                    clock["week"] = w if w >= 1 else 1
                except (TypeError, ValueError):
                    pass
                if data.get("weekday") in WEEKDAYS:
                    clock["weekday"] = data["weekday"]
                if data.get("time_of_day") in TIME_OF_DAY:
                    clock["time_of_day"] = data["time_of_day"]
                if data.get("hhmm"):
                    clock["hhmm"] = data["hhmm"]
                if isinstance(data.get("set"), bool):
                    clock["set"] = data["set"]
                elif "set" not in data:
                    # 旧版文件无 set 字段（旧版写文件即保存过）→ 视为已设置，迁移兼容
                    clock["set"] = True
                return clock
            except Exception:
                pass
        return dict(self.default)

    def _save(self):
        with self.lock:
            d = os.path.dirname(self.path)
            os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.clock, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def _load_undo(self):
        try:
            with open(self.undo_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data[-self.undo_size:]
        except Exception:
            pass
        return []

    def _save_undo(self):
        with self.lock:
            d = os.path.dirname(self.undo_path)
            os.makedirs(d, exist_ok=True)
            tmp = self.undo_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._undo[-self.undo_size:], f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.undo_path)

    def _load_history(self):
        try:
            with open(self.history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
        return []

    def _save_history(self):
        with self.lock:
            d = os.path.dirname(self.history_path)
            os.makedirs(d, exist_ok=True)
            tmp = self.history_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._history, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.history_path)

    def _load_last_seen(self):
        try:
            with open(self.last_seen_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _save_last_seen(self):
        with self.lock:
            d = os.path.dirname(self.last_seen_path)
            os.makedirs(d, exist_ok=True)
            tmp = self.last_seen_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._last_seen, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.last_seen_path)

    @staticmethod
    def _fmt_state(state):
        state = state or {}
        hh = (" %s" % state["hhmm"]) if state.get("hhmm") else ""
        return "第%d周%s%s%s" % (int(state.get("week", 1)), state.get("weekday", "?"),
                                 state.get("time_of_day", "?"), hh)

    def _append_history(self, before, after, source="", identity="", action="apply"):
        self._history.append({
            "ts": time.time(),
            "identity": identity or "",
            "source": source or "",
            "action": action,
            "before": self._fmt_state(before),
            "after": self._fmt_state(after),
        })
        self._history = self._history[-500:]
        self._save_history()

    def reload(self):
        """从文件重新加载时钟（【重启青柠】热重载时调用，外部改过 clock.json 后生效）。"""
        with self.lock:
            self.clock = self._load()
            self._undo = self._load_undo()
            self._history = self._load_history()
            self._last_seen = self._load_last_seen()

    # ---------- 查询 ----------
    def current(self):
        return dict(self.clock)

    def stamp(self):
        """剧情时间戳单 token，如"第1周周六下午"；时钟未设置时返回"未知时间"。"""
        return "第%d周%s%s" % (
            self.clock.get("week", 1),
            self.clock.get("weekday", "?"),
            self.clock.get("time_of_day", "?"),
        )

    def is_set(self):
        """时钟是否已明确设置；时间模块关闭时视为已设置（不门控）。"""
        if not self.enabled:
            return True
        return bool(self.clock.get("set"))

    def to_prompt_line(self):
        c = self.clock
        s = "【当前世界时间：第%d周 %s %s" % (c.get("week", 1), c["weekday"], c["time_of_day"])
        if c.get("hhmm"):
            s += " %s" % c["hhmm"]
        return s + "】"

    def query_answer(self):
        return "【现在是第%d周 %s %s%s】" % (
            self.clock.get("week", 1),
            self.clock["weekday"],
            self.clock["time_of_day"],
            (" %s" % self.clock["hhmm"]) if self.clock.get("hhmm") else "",
        )

    # ---------- 状态栏时间同步 ----------
    _STATUS_BAR_TIME_RE = re.compile(
        r"时间\s*[:：]\s*"
        r"(?:第\s*(\d+)\s*周\s*)?"
        r"(?:周?\s*([一二三四五六日天1-7]))?\s*"
        r"(凌晨|清晨|早晨|早上|上午|中午|午后|下午|傍晚|夜晚|睡前|放学后|晚上|深夜)?")

    @staticmethod
    def _norm_period(tod):
        """非标准时段词 → 标准时段（清晨/早晨→早上，夜晚/睡前→晚上，午后→下午）。"""
        if not tod:
            return tod
        return textutil.PERIOD_NORMALIZE.get(tod, tod)

    def _parse_status_bar_time(self, text):
        """从回复的 <状态栏> 时间行解析 (week, weekday序号, 时段)。

        容忍格式差异：第1周 周二 早上 / 第1周周二早上 / 周二 晚上 / 第2周 周2 清晨。
        状态栏标签缺失闭合时退化为扫描整段内容里的"时间:"行。
        返回三元组，缺失字段为 None。
        """
        for block in re.findall(r"<状态栏>(.*?)</状态栏>", text or "", re.S):
            for line in block.splitlines():
                m = self._STATUS_BAR_TIME_RE.search(line)
                if not m:
                    continue
                week = int(m.group(1)) if m.group(1) else None
                wd = None
                token = (m.group(2) or "").strip()
                if token:
                    if token.isdigit():
                        wd = int(token)
                    elif token in _WEEKDAY_MAP:
                        wd = WEEKDAYS.index(_WEEKDAY_MAP[token]) + 1
                tod = m.group(3)
                if week or wd or tod:
                    return week, wd, self._norm_period(tod)
        # 兜底：部分历史/异常回复的状态栏标签没有闭合
        for line in (text or "").splitlines():
            s = line.strip()
            if not s.startswith("时间"):
                continue
            m = self._STATUS_BAR_TIME_RE.search(s)
            if not m:
                continue
            week = int(m.group(1)) if m.group(1) else None
            wd = None
            token = (m.group(2) or "").strip()
            if token:
                if token.isdigit():
                    wd = int(token)
                elif token in _WEEKDAY_MAP:
                    wd = WEEKDAYS.index(_WEEKDAY_MAP[token]) + 1
            tod = m.group(3)
            if week or wd or tod:
                return week, wd, self._norm_period(tod)
        return None, None, None

    def sync_from_status_bar(self, text, identity=None):
        """回复生成后，让剧情时钟与状态栏时间同步（只允许前进）。

        状态栏由模型输出（如"时间: 第1周 周二 早上"）。若它比当前时钟更晚，
        就把时钟推进到该时刻，保证下一轮系统提示与聊天记录时间戳和状态栏一致；
        倒退或周数跳跃超过 2 周视为模型幻觉，忽略。
        """
        if not self.enabled or not self.is_set():
            return False
        week, wd, tod = self._parse_status_bar_time(text or "")
        if not week and not wd and not tod:
            return False
        cur_week = int(self.clock.get("week", 1))
        cur_wd = WEEKDAYS.index(self.clock.get("weekday", "周一")) + 1
        cur_tod = TIME_OF_DAY.index(self.clock.get("time_of_day", "早上"))
        target_week = week or cur_week
        target_wd = wd or cur_wd
        # 前进性校验：周数 ≥ 当前；同周则星期 ≥ 当前；同日则时段必须更晚
        if target_week < cur_week:
            return False
        if target_week > cur_week:
            if target_week - cur_week > 2:
                return False
        elif target_wd < cur_wd:
            return False
        elif target_wd == cur_wd:
            if tod not in TIME_OF_DAY or TIME_OF_DAY.index(tod) <= cur_tod:
                return False
        update = {"week": target_week}
        if wd:
            update["weekday"] = WEEKDAYS[target_wd - 1]
        if tod in TIME_OF_DAY:
            update["time_of_day"] = tod
        return self.apply(update, source="status_bar", identity=identity)

    # ---------- 更新 ----------
    def apply(self, update, guard=False, source=None, identity=None):
        """合并 weekday/time_of_day/hhmm；advance_day 推进到次日早上。返回是否发生变化。

        guard=True（自然语言抽取路径）：时间只能前进不能倒退——消息里"下午的时候"
        是对过去的追述，不能把时钟从晚上拉回下午；倒退的字段被丢弃，不应用。
        【时间：xxx】显式指令不设防（guard 默认 False，玩家上帝视角可任意设定）。
        source/identity 用于时间线变更留痕（history.json）。
        """
        with self.lock:
            before = dict(self.clock)
            changed = False
            if update.get("advance_day"):
                idx = WEEKDAYS.index(self.clock["weekday"])
                if idx == 6:  # 周日睡到周一 → 进入下一周
                    self.clock["week"] = int(self.clock.get("week", 1)) + 1
                self.clock["weekday"] = WEEKDAYS[(idx + 1) % 7]
                self.clock["time_of_day"] = "早上"
                self.clock["hhmm"] = None
                changed = True
            if update.get("week"):
                try:
                    week = int(update["week"])
                except (TypeError, ValueError):
                    week = 0
                if week >= 1 and week != int(self.clock.get("week", 1)):
                    self.clock["week"] = week
                    changed = True
            if guard:
                update = self._guard_forward(update)
            if update.get("weekday") in WEEKDAYS and update["weekday"] != self.clock["weekday"]:
                old_idx = WEEKDAYS.index(self.clock["weekday"])
                new_idx = WEEKDAYS.index(update["weekday"])
                self.clock["weekday"] = update["weekday"]
                # 跨周回绕：显式指令把星期设回较早的一天（如 周日→周一、周三→周一）→ 进入下一周
                # 显式给出 week 时按绝对周数定位，不再重复进位
                if new_idx < old_idx and not update.get("week"):
                    self.clock["week"] = int(self.clock.get("week", 1)) + 1
                changed = True
            if update.get("time_of_day") in TIME_OF_DAY and update["time_of_day"] != self.clock["time_of_day"]:
                self.clock["time_of_day"] = update["time_of_day"]
                changed = True
            if update.get("hhmm"):
                self.clock["hhmm"] = str(update["hhmm"])
                changed = True
            if changed:
                # 撤销栈：只存"实际变化"前的状态，连续相同状态去重
                if not self._undo or self._undo[-1] != before:
                    self._undo.append(before)
                    self._undo = self._undo[-self.undo_size:]
                    self._save_undo()
            if changed or update:
                # 显式设置（即使值未变，如【时间：周一 早上】恰为当前值）也视为已设置，解锁门控
                self.clock["set"] = True
                self._save()
                action = "advance_day" if update.get("advance_day") else "apply"
                self._append_history(before, dict(self.clock),
                                     source=source or "", identity=identity or "", action=action)
        return changed

    def undo(self, source="undo", identity=None):
        """撤销最近一次时间变更（恢复变更前状态）。无记录返回 None。"""
        with self.lock:
            if not self._undo:
                return None
            state = self._undo.pop()
            before = dict(self.clock)
            merged = dict(self.default)
            merged.update(state)
            self.clock = merged
            self._save()
            self._save_undo()
            self._append_history(before, dict(self.clock), source=source or "",
                                 identity=identity or "", action="undo")
            return "【已撤销时间：%s → %s】" % (self._fmt_state(before), self._fmt_state(self.clock))

    # ---------- 身份登场时间（时间衔接） ----------
    def get_last_seen(self, identity):
        """该身份最近一次登场（RP 成功回复时记录）的剧情时间；无记录返回 None。"""
        if not identity:
            return None
        with self.lock:
            data = self._last_seen.get(identity)
            return dict(data) if data else None

    def set_last_seen(self, identity, stamp):
        """记录该身份最近一次登场时间（时间衔接注入用）。"""
        if not identity or not stamp:
            return
        with self.lock:
            self._last_seen[identity] = {"stamp": stamp, "updated": time.time()}
            self._save_last_seen()

    def gap_block(self, identity):
        """时间衔接块：该身份上次出现时间与当前世界时间不同 → 返回注入文本，否则 None。"""
        last = self.get_last_seen(identity)
        if not last or not last.get("stamp"):
            return None
        now = self.stamp()
        if last["stamp"] == now:
            return None
        return ("【时间衔接】你上次出现是「%s」，现在已是「%s」。"
                "时间过去了，自然地接上就好，不要复述这句话。"
                % (last["stamp"], now))

    def _guard_forward(self, update):
        """自然语言抽取的防倒退保护：丢弃使时间倒退的字段。

        - time_of_day：时段序（早上<…<深夜<凌晨）只能前进；追述过去时段被拒
        - weekday：同周内只能前进（周一→周六合法，周六→周三拒绝）；跨天靠 advance_day
        """
        upd = dict(update)
        tod = upd.get("time_of_day")
        if tod in TIME_OF_DAY and tod != self.clock["time_of_day"]:
            if TIME_OF_DAY.index(tod) < TIME_OF_DAY.index(self.clock["time_of_day"]):
                upd.pop("time_of_day", None)
        wd = upd.get("weekday")
        if wd in WEEKDAYS and wd != self.clock["weekday"]:
            if WEEKDAYS.index(wd) <= WEEKDAYS.index(self.clock["weekday"]):
                upd.pop("weekday", None)
        return upd

    def _describe(self):
        return "【世界时间已改为：第%d周 %s %s%s】" % (
            self.clock.get("week", 1),
            self.clock["weekday"],
            self.clock["time_of_day"],
            (" %s" % self.clock["hhmm"]) if self.clock.get("hhmm") else "",
        )

    # ---------- 【时间：xxx】指令 ----------
    @staticmethod
    def _normalize_hhmm(h, m):
        """校验并规范化 HH:MM；非法（>23:59）返回 None。"""
        try:
            h, m = int(h), int(m)
        except (TypeError, ValueError):
            return None
        if 0 <= h <= 23 and 0 <= m <= 59:
            return "%02d:%02d" % (h, m)
        return None

    def apply_instruction(self, arg, source=None, identity=None):
        if not self.enabled:
            return "【时间模块已关闭～】"
        arg = (arg or "").strip()
        if not arg or any(k in arg for k in ("几点", "几点了", "什么时间", "周几", "星期几", "几号")):
            return self.query_answer()
        upd = {}
        m = re.search(r"周([一二三四五六日天])|星期([一二三四五六日天])", arg)
        if m:
            d = m.group(1) or m.group(2)
            upd["weekday"] = _WEEKDAY_MAP[d]
        for t in TIME_OF_DAY:
            if t in arg:
                upd["time_of_day"] = t
                break
        m = re.search(r"(\d{1,2}):(\d{2})", arg)
        if m:
            hh = self._normalize_hhmm(m.group(1), m.group(2))
            if hh:
                upd["hhmm"] = hh
        else:
            m = re.search(r"(\d{1,2})\s*点半", arg)
            if m:
                h = int(m.group(1))
                if 0 <= h <= 23:
                    upd["hhmm"] = "%02d:30" % h
            else:
                m = re.search(r"(\d{1,2})\s*点", arg)
                if m:
                    h = int(m.group(1))
                    if 0 <= h <= 23:
                        upd["hhmm"] = "%02d:00" % h
        if not upd:
            return "【没看懂这个时间哦，试试【时间：周二 下午】或【时间：晚上8点】这样～】"
        self.apply(upd, source=source or "instruction", identity=identity or "")
        return self._describe()

    # ---------- 自然语言抽取 ----------
    # 正则前置闸门：常见说法直接解析，不调 LLM（省 80%+ 消息级调用）
    # 注意：换天（advance_day）只能由【青柠好好睡了一觉】指令触发，自然语言一律不推进
    _QUERY_RE = re.compile(r"几点(了|钟)?|什么时间|现在几点了|周几|星期几|几号")
    _WEEKDAY_RE = re.compile(r"周([一二三四五六日天])|星期([一二三四五六日天])")
    _HHMM_RE = re.compile(r"(\d{1,2}):(\d{2})")
    _HALF_RE = re.compile(r"(\d{1,2})\s*点半")
    _HOUR_RE = re.compile(r"(\d{1,2})\s*点")

    def _regex_extract(self, raw_text):
        """正则解析；全部命中直接出结果；未命中返回 None（交由 LLM 兜底）。

        消息含回忆/追述/未来计划词（"昨天深夜""今晚"）→ 跳过日期/时段/时刻抽取
        （query 照常）——剧情文本里的历史时间不能推进当前时钟。
        """
        upd = {}
        query = False
        if self._QUERY_RE.search(raw_text):
            query = True
        if not (self._RECALL_CTX_RE.search(raw_text) or self._STORY_STAMP_RE.search(raw_text)):
            m = self._WEEKDAY_RE.search(raw_text)
            if m:
                upd["weekday"] = _WEEKDAY_MAP[m.group(1) or m.group(2)]
            for t in TIME_OF_DAY:
                if t in raw_text:
                    upd["time_of_day"] = t
                    break
            m = self._HHMM_RE.search(raw_text)
            if m:
                hh = self._normalize_hhmm(m.group(1), m.group(2))
                if hh:
                    upd["hhmm"] = hh
            else:
                m = self._HALF_RE.search(raw_text)
                if m:
                    h = int(m.group(1))
                    if 0 <= h <= 23:
                        upd["hhmm"] = "%02d:30" % h
                else:
                    m = self._HOUR_RE.search(raw_text)
                    if m:
                        h = int(m.group(1))
                        if 0 <= h <= 23:
                            upd["hhmm"] = "%02d:00" % h
        return (upd, query) if upd or query else None

    # 正则未命中时，仅当消息含"设时间信号词"才允许 LLM 兜底（配合 time.llm_fallback）。
    # "夜"保留："后半夜"这类纯时间词需 LLM 兜底；"昨晚/昨夜"会先被 _RECALL_CTX_RE 拦截
    _LLM_GATE_RE = re.compile(r"时间|几点|周|星期|点半|\d\s*点|睡|天亮|新的一天|几号|现在|夜")

    # 回忆/追述/未来计划上下文：剧情文本里"昨天深夜""昨天晚上""今晚"这类时间词
    # 是回忆过去或计划未来，不是"当前时间设定"——正则抽取一律忽略，防止把
    # 剧情里提到的历史时间误推进时钟（如周日早上聊"昨晚的事"被误推到晚上）。
    # 明确想推进时段请用【时间：xxx】指令，或说"现在是晚上"这类当前时间陈述。
    _RECALL_CTX_RE = re.compile(
        # 过去追述：昨天/前天/大前天/昨晚/那晚/那天/上次/之前/曾经/当初/当时/刚才/小时候
        r"昨天|前天|大前天|昨晚|昨夜|昨儿|昨个|今日|"
        r"上周|上个星期|上星期|上礼拜|这周|本周|这礼拜|这个星期|这星期|"
        r"那晚|那一晚|那夜|那晚上|那天|那天晚上|"
        r"上次|上回|上上次|上一回|之前|以前|先前|早先|从前|"
        r"曾经|当初|当年|起初|当时|那会|那时候|那阵子|那几天|"
        r"刚才|刚刚|方才|过去|过往|往日|小时候|幼时|童年|"
        # 回忆动词/梦境：回忆/记得/想起/回想/梦到/梦见/梦里/做梦/睡醒
        r"回忆|记得|记不清|想起|回想|回味|回顾|复盘|"
        r"梦到|梦见|梦中|梦里|睡梦|做梦|梦醒|睡醒|睡着|睡下|"
        # 未来计划：明天/明晚/今晚/下周/待会/一会/以后
        r"明天|明晚|明早|明日|今晚|下周|下礼拜|"
        r"待会|等会|一会|稍后|过会儿|改天|回头|以后"
    )

    # 剧情时间戳引用（剧本格式"第N周周X时段"，如"第1周周日深夜"/"第一周周日晚上"/
    # "第1周 周日 早上"带空格）：出现在消息里是引用剧情时间（玩家写剧本/追述），
    # 不是"当前时间设定"——同样抑制抽取。
    # 当前时间陈述请用【时间：xxx】指令或"现在是晚上"式说法（无"第N周"前缀）。
    _STORY_STAMP_RE = re.compile(
        r"第[一二三四五六七八九十百\d]+周\s*周?[一二三四五六日天]"
        r"(?:早上|上午|中午|下午|傍晚|晚上|深夜|凌晨)?"
    )

    def day_prefix(self, offset=0):
        """今天(0)/昨天(-1)/前天(-2)/明天(+1)的剧情日前缀（跨周自动进退，周数钳制 ≥1）。"""
        idx = WEEKDAYS.index(self.clock["weekday"]) + offset
        w = int(self.clock.get("week", 1))
        while idx < 0:
            idx += 7
            w -= 1
        while idx >= 7:
            idx -= 7
            w += 1
        if w < 1:  # 第一周周一往前回溯没有更早的剧情周，钳制在第 1 周，避免出现"第0周"
            w = 1
        return "第%d周%s" % (w, WEEKDAYS[idx])

    def story_date_of(self, stamp):
        """从剧情时间戳（"第1周周一下午"）提取剧情日期（"第1周周一"）；无法识别回退今天。"""
        m = re.match(r"^(第\d+周(?:周[一二三四五六日天]))", stamp or "")
        if m:
            return m.group(1)
        return self.day_prefix(0)

    def regex_extract(self, raw_text):
        """仅正则抽取，不调 LLM（时间门控阶段用）。返回 (update 或 None, query)。"""
        if not self.enabled or not raw_text:
            return None, False
        return self._regex_extract(raw_text) or (None, False)

    def maybe_extract(self, raw_text, identity=None):
        """先正则后 LLM；返回 (update_dict 或 None, query_time)。任何失败 → (None, False)。"""
        if not self.enabled or not raw_text:
            return None, False
        hit = self._regex_extract(raw_text)
        if hit:
            update, query = hit
            applied = self.apply(update, guard=True, source="natural", identity=identity or "") if update else None
            return (applied, query)
        # 正则未命中：LLM 兜底默认开启，但消息须含时间信号词（"今天累死了"不触发）
        if not self.bot.config.get("time", {}).get("llm_fallback", True):
            return None, False
        if not self._LLM_GATE_RE.search(raw_text):
            return None, False
        if self._RECALL_CTX_RE.search(raw_text) or self._STORY_STAMP_RE.search(raw_text):
            # 回忆性时间词（昨天深夜/今晚）或剧情时间戳引用（第1周周日深夜）：
            # 不是当前时间设定，不调 LLM
            return None, False
        data = self.bot.llm.chat_json(
            [
                {"role": "system", "content": TIME_EXTRACT_PROMPT},
                {"role": "user", "content": raw_text},
            ],
            temperature=0.2,
            call_type="extract",
        )
        if not isinstance(data, dict):
            return None, False
        query = bool(data.get("query_time"))
        update = {}
        if data.get("weekday") in WEEKDAYS:
            update["weekday"] = data["weekday"]
        if data.get("time_of_day") in TIME_OF_DAY:
            update["time_of_day"] = data["time_of_day"]
        if data.get("hhmm"):
            m = re.match(r"^(\d{1,2}):(\d{2})$", str(data["hhmm"]))
            if m:
                hh = self._normalize_hhmm(m.group(1), m.group(2))
                if hh:
                    update["hhmm"] = hh
        # 换天只能由【青柠好好睡了一觉】指令触发，LLM 返回的 advance_day 一律忽略
        applied = self.apply(update, guard=True, source="natural", identity=identity or "") if update else None
        return (applied, query)
