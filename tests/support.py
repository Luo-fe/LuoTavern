# -*- coding: utf-8 -*-
"""测试共用夹具：假 LLM / 捕获发送端 / 隔离 BotCore。

原先 test_recall_keywords / test_relative_time / test_vector_persistent 各自
维护一份完全相同的 make_bot / FakeLLM / CaptureClient / last_user_content，
改一处容易漏另外两处；统一到这里，测试文件只保留本文件特有的夹具。
"""
import json
import os
import sys

os.environ.setdefault("QQBOT_EMBED_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from qqbot.bot_core import BotCore  # noqa: E402
from qqbot.config import load_config  # noqa: E402

OWNER = 10000001
GUEST = 10001

# 默认剧情时钟：周六下午（与旧 test_recall_keywords / test_vector_persistent 一致）
DEFAULT_CLOCK = {"weekday": "周六", "time_of_day": "下午", "hhmm": None, "set": True}


class FakeLLM:
    def __init__(self):
        self.chat_calls = []
        self.json_calls = []
        self.json_script = []

    def chat(self, messages, temperature=None, max_tokens=None, call_type="chat"):
        self.chat_calls.append((messages, temperature))
        return "（青柠的默认回复）"

    def chat_json(self, messages, temperature=0.2, call_type="chat", max_tokens=None):
        self.json_calls.append(messages)
        if self.json_script:
            return self.json_script.pop(0)
        return None


class CaptureClient:
    def __init__(self):
        self.sent = []

    def send_private(self, user_id, text):
        self.sent.append((user_id, text))
        return True


def make_bot(tmp, clock=None):
    cfg = load_config(os.path.join(tmp, "test-config.json"), use_env=False)
    cfg.update({
        "allow_all": True,
        "owner_user_ids": [OWNER],
        "allowed_user_ids": [OWNER, GUEST],
        "typing_indicator": False,
        "summary_dir": os.path.join(tmp, "summary"),
        "archive_dir": os.path.join(tmp, "archive"),
    })
    # 这些用例用固定回复（不含状态栏）验证其他子系统；关掉软件内建的状态栏强制，
    # 避免"缺状态栏 → 修复调用"多出来的 LLM 请求干扰断言
    cfg["status_bar"] = {"enable": False}
    cfg["identity"]["map_path"] = os.path.join(tmp, "identity_map.json")
    cfg["time"]["path"] = os.path.join(tmp, "clock.json")
    with open(os.path.join(tmp, "clock.json"), "w", encoding="utf-8") as f:
        json.dump(clock or DEFAULT_CLOCK, f)
    cfg["rules"]["dir"] = os.path.join(tmp, "rules")
    cfg["correct"]["dir"] = os.path.join(tmp, "corrected")
    cfg["seal"]["dir"] = os.path.join(tmp, "seal")
    cfg["memory"]["db_path"] = os.path.join(tmp, "memory.db")
    cfg["chatlog"]["dir"] = os.path.join(tmp, "chatlog")
    cfg["vault"] = {"enable": True, "dir": os.path.join(tmp, "vault"), "check_seconds": 60}
    cfg["shared"] = {"dir": os.path.join(tmp, "shared")}
    cfg["proactive"]["score_file"] = os.path.join(tmp, "proactive_score.json")
    cfg["character_card"] = ""
    cfg["plugins"] = {"memory": True, "chatlog": True, "vault_sync": True,
                      "proactive": False, "worldbook": False}
    bot = BotCore(cfg, "你是温柔可爱的青柠。", None)
    bot.llm = FakeLLM()
    bot.client = CaptureClient()
    return bot


def declare(bot, qq, name):
    bot._handle(qq, "【我是%s】" % name)


def last_user_content(bot):
    """最后一次"主对话"调用里发给模型的用户消息。

    跳过状态栏修复调用（`_repair_status_bar` 会再发一次请求，其 user 消息是修复指令
    或回复正文），否则看到的是修复提示而不是本轮提示词。
    """
    for msgs, _temp in reversed(bot.llm.chat_calls):
        sys_txt = " ".join(m["content"] for m in msgs if m.get("role") == "system")
        if "回复后处理助手" in sys_txt:
            continue
        users = [m["content"] for m in msgs if m.get("role") == "user"]
        if not users:
            continue
        last = users[-1]
        if "状态栏不合格" in last:
            continue
        return last
    return ""
