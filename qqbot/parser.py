# -*- coding: utf-8 -*-
"""入站消息解析。

- 【…】        管理指令（角色扮演外，由代码处理，不进 LLM 的 RP 内容）
- (…)（…）     动作
- "…" '…' “…” ‘…’  说的话（台词）
- 其余          旁白
- 【随机触发词】/【触发词解释：xxx】  代码级指令：单独拦截，由代码处理，不进 LLM

rebuilt = 原文去掉所有【…】块（括号/引号原样保留），供历史、日志、回忆、压缩使用。
"""
import re
from dataclasses import dataclass, field

_BRACKET_RE = re.compile(r"【([^【】]*)】")
_ACTION_RE = re.compile(r"[（(]([^（）()]*)[）)]")
_QUOTE_RE = re.compile(r'"(?P<dq>[^"\n]*)"|“(?P<fq>[^“”\n]*)”|\'(?P<sq>[^\'\n]*)\'')


def classify_instruction(raw):
    """按前缀识别指令类型（纠正 需在 纠正规则 之后判断）。"""
    s = raw.strip()
    if s.startswith("我是"):
        return "declare"
    if s.startswith("纠正规则"):
        return "rule"
    if s.startswith("只纠正"):
        return "correct_once"
    if s.startswith("纠正"):
        return "correct"
    if s == "撤销时间":
        return "time_undo"
    if s.startswith("时间"):
        return "time"
    if s.startswith("回忆"):
        return "recall"
    if s == "回放聊天":
        return "replay"
    if s.startswith(("记住", "记得", "记下")):
        return "remember"
    if s.startswith("忘记"):
        return "forget"
    if s.startswith("好感度"):
        return "affection"
    if s == "记忆状态":
        return "memstat"
    if s == "角色卡查看":
        return "persona_list"
    if s.startswith("切换为"):
        return "persona_switch"
    if s == "结束这一天":
        return "endday"
    if s in ("关闭青柠", "关闭机器人", "关闭"):
        return "shutdown"
    if s in ("重启青柠", "重启机器人", "重启"):
        return "restart"
    if s in ("青柠说明书", "说明书"):
        return "help"
    if s in ("青柠好好睡了一觉", "好好睡了一觉"):
        return "sleepday"
    if s.startswith(("随机触发词", "触发词解释")):
        return "trigger"
    return "unknown"


@dataclass
class Instruction:
    raw: str          # 【】内原文
    kind: str         # declare/rule/correct/time/recall/remember/forget/affection/shutdown/restart/help/unknown


@dataclass
class ParsedMessage:
    instructions: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    speech: list = field(default_factory=list)
    remainder: str = ""
    rebuilt: str = ""            # 原文去掉所有【…】
    is_management_only: bool = False


def parse_instructions(text):
    return [Instruction(m.group(1).strip(), classify_instruction(m.group(1)))
            for m in _BRACKET_RE.finditer(text)]


def rebuild_message(text):
    """去掉所有【…】块，保留其余内容（括号引号原样）。"""
    return _BRACKET_RE.sub("", text).strip()


def parse_message(text):
    pm = ParsedMessage()
    pm.instructions = parse_instructions(text)
    pm.rebuilt = rebuild_message(text)
    pm.actions = [m.group(1).strip() for m in _ACTION_RE.finditer(pm.rebuilt)]
    pm.speech = [g for m in _QUOTE_RE.finditer(pm.rebuilt) for g in m.groups() if g is not None]
    rest = _ACTION_RE.sub("", pm.rebuilt)
    rest = _QUOTE_RE.sub("", rest)
    pm.remainder = rest.strip()
    pm.is_management_only = not pm.rebuilt
    return pm
