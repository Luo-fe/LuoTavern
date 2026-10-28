# -*- coding: utf-8 -*-
"""跳出剧本纠错（【纠正：xxx】，仅主人可用）。

- ① 持久规则落盘（同【纠正规则】）
- ② 上一条机器人回复保留+登记作废（data/corrected/{身份}.json），记忆提炼/回忆检索跳过
- ③ 内存 history 撤销错误回复，一次性 LLM 调用重新回答

作废登记同时记**序号**与**稳定 id**：序号依赖聊天记录排序，重排后会指到别的条目；
id 是内容指纹，重排后依然指同一条（见 chatlog_store.entry_id）。
"""
import json
import os
import threading

from .chatlog_store import entry_id


class CorrectionManager:
    def __init__(self, bot, correct_dir=None):
        self.bot = bot
        cfg = bot.config.get("correct", {})
        base = os.path.dirname(os.path.abspath(__file__))
        rel = correct_dir or cfg.get("dir", "data/corrected")
        self.dir = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(base, "..", rel))
        self.lock = threading.Lock()
        # 作废登记缓存：检索时每个聊天条目都会问一次 is_corrected，
        # 旧实现每次都读盘 + 解析 JSON（长聊天记录 = 每轮成百上千次 IO）。
        # 用 (mtime_ns, size) 做签名，外部进程（管理台/手工编辑）改动会自动失效。
        self._cache = {}                 # identity -> (sig, id集合, 序号映射)
        self._cache_lock = threading.RLock()
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, identity):
        return os.path.join(self.dir, "%s.json" % identity)

    def _signature(self, identity):
        try:
            st = os.stat(self._path(identity))
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _read(self, identity):
        path = self._path(identity)
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _derive(data):
        ids, by_index = {}, {}
        for k, v in (data or {}).items():
            text = str(v.get("text") or "") if isinstance(v, dict) else str(v)
            if isinstance(v, dict) and v.get("id"):
                ids[str(v["id"])] = text
            try:
                by_index[int(k)] = text
            except (TypeError, ValueError):
                continue
        return ids, by_index

    def _view(self, identity):
        """返回 (稳定 id 集合, 序号映射)；文件未变时命中缓存。"""
        sig = self._signature(identity)
        with self._cache_lock:
            hit = self._cache.get(identity)
            if hit is not None and hit[0] == sig:
                return hit[1], hit[2]
        ids, by_index = self._derive(self._read(identity))
        with self._cache_lock:
            self._cache[identity] = (sig, ids, by_index)
        return ids, by_index

    # ---------- 作废登记 ----------
    def corrected_map(self, identity):
        """{条目序号: 纠正内容}（兼容新旧两种存储形态）。"""
        return dict(self._view(identity)[1])

    def corrected_ids(self, identity):
        """{稳定 id: 纠正内容}。"""
        return dict(self._view(identity)[0])

    def is_corrected(self, identity, entry_index, entry=None):
        """条目是否已被作废：优先按稳定 id 判断（重排后仍准确）。"""
        ids, by_index = self._view(identity)
        if entry is not None and entry_id(entry) in ids:
            return True
        return entry_index in by_index

    def register(self, identity, entry_index, correction, entry=None):
        with self.lock:
            data = self._read(identity)
            record = {"text": str(correction)[:200]}
            if entry is not None:
                record["id"] = entry_id(entry)
            data[str(int(entry_index))] = record
            tmp = self._path(identity) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path(identity))
            with self._cache_lock:
                self._cache.pop(identity, None)

    # ---------- 执行 ----------
    def run(self, user_id, identity, correction, persist_rule=True):
        """返回 (ack, re_answer 或 None)。规则落盘 + 登记作废 + 撤销重答。

        persist_rule=False（【只纠正：xxx】）：只作废当前消息并重答，
        不写规则文件——不改变青柠之后的言行。
        """
        if not identity:
            return self.bot.undeclared_reply, None
        correction = (correction or "").strip()
        if not correction:
            return "【纠正内容不能为空哦～】", None
        # ① 规则落盘（主人的 QQ）；【只纠正】跳过——不持久
        if persist_rule:
            self.bot.rules.set_rule(user_id, correction)
            ack = "【已纠正：%s，之前的说法作废～】" % correction
        else:
            ack = "【已临时纠正：%s，之前的说法作废～（不写入规则）】" % correction
        # ② 登记上一条青柠回复
        last = self.bot.chatlog_store.last_bot_entry(identity)
        if last is not None:
            self.register(identity, last[0], correction, entry=last)
        else:
            ack += "\n（上一条没有%s的回复可纠正，规则已记住）" % self.bot.bot_name
        # ③ 撤销重答
        reply = self._reanswer(identity, correction)
        if reply is None and last is not None:
            # 区分"没上下文可重答"（重启/共享期 history 为空）与"重答调用真失败"
            msgs = list(self.bot.history.get(identity, []))
            if len(msgs) < 2 or msgs[-1].get("role") != "assistant":
                ack += "\n（没有之前的对话上下文，无法重答，规则已生效）"
            else:
                ack += "\n（重新回答失败了，规则已生效）"
        return ack, reply

    def _reanswer(self, identity, correction):
        msgs = list(self.bot.history.get(identity, []))
        if len(msgs) < 2 or msgs[-1].get("role") != "assistant":
            return None
        base = msgs[:-1]  # 去掉错误回复
        prompt_msgs = base + [
            {
                "role": "user",
                "content": "【管理指令：你刚才的回复有误，已被纠正：%s。"
                "请以纠正后的设定重新回答我刚才的问题，只输出新的回复内容，不要解释。】" % correction,
            }
        ]
        try:
            reply = self.bot.llm.chat(prompt_msgs, temperature=0.7).strip()
        except Exception as exc:
            self.bot.log("[correct] 重答失败: %s" % exc)
            return None
        if not reply:
            return None
        self.bot.history[identity] = base + [{"role": "assistant", "content": reply}]
        return reply
