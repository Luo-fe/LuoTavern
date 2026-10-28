# -*- coding: utf-8 -*-
"""共享聊天上下文：青柠同时和哥哥、另一位角色两边相处的临时状态。

- 触发：青柠思念哥哥到临界点，给哥哥发消息/打电话/打视频（proactive 插件驱动）
- 启用：哥哥回复/接听后才算真正进入共享状态
- 期间：青柠-哥哥、青柠-另一位角色的对话全部写入共享记录（带标注），
  各自 chatlog / history 暂停；两边的摘要同步为同一份共享摘要
- 结束：哥哥说"停止发消息/挂断电话/挂断视频" → 分家：
  共享摘要+记录拆成两份侧重摘要、记录按标注归位各自 chatlog
- 全部数据落盘 data/shared/，重启不丢
"""
import json
import os
import threading

CHANNEL_TEXT = "短信"
CHANNEL_PHONE = "电话"
CHANNEL_VIDEO = "视频电话"

# 哥哥结束共享状态的关键词（消息包含任一即分家）
END_WORDS = ("停止发消息", "挂断电话", "挂断视频")


class SharedContext:
    def __init__(self, bot):
        self.bot = bot
        base = os.path.dirname(os.path.abspath(__file__))
        rel = bot.config.get("shared", {}).get("dir", "data/shared")
        self.dir = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(base, "..", rel))
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, "state.json")
        self.record_path = os.path.join(self.dir, "record.json")
        self.lock = threading.Lock()
        self.state = self._load_state()
        self.record = self._load_record()
        if self.state.get("other"):
            if self.state.get("active"):
                bot.log("[shared] 恢复共享状态: 哥哥 + %s（%s）" % (
                    self.state.get("other", "?"), self.state.get("channel", "?")))
            else:
                bot.log("[shared] 恢复等待期: 等哥哥回应（%s，%s）" % (
                    self.state.get("other", "?"), self.state.get("channel", "?")))

    # ---------- 持久化 ----------
    def _load_state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _load_record(self):
        try:
            with open(self.record_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_state(self):
        with self.lock:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)

    def _save_record(self):
        with self.lock:
            tmp = self.record_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.record, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.record_path)

    # ---------- 状态 ----------
    def is_active(self):
        """共享已启用（哥哥回复/接听后才算）。"""
        return bool(self.state.get("active"))

    def has_pending(self):
        """主动联系已发出、等哥哥回复的等待期。"""
        return bool(self.state.get("other")) and not self.state.get("active")

    def other_identity(self):
        return self.state.get("other") or ""

    def channel(self):
        return self.state.get("channel") or CHANNEL_TEXT

    def is_shared_for(self, identity):
        """该身份当前是否处于已启用的共享状态（哥哥或触发方另一角色）。"""
        return self.is_active() and identity in (
            self.bot.identity.owner_name, self.other_identity())

    def summary_text(self):
        return self.state.get("summary") or "（暂无共享摘要）"

    def ends(self, text):
        """哥哥消息是否包含结束共享的关键词。"""
        return any(w in text for w in END_WORDS)

    # ---------- 生命周期 ----------
    def pending(self, other, channel, summary_text):
        """主动联系发出 → 进入等待期（哥哥回复/接听才启用共享上下文）。"""
        with self.lock:
            self.state = {
                "active": False,
                "other": other,
                "channel": channel,
                "summary": summary_text,
            }
            self._save_state()
        self.bot.log("[shared] 等待%s回应（%s，%s）" % (
            self.bot.identity.owner_name, other, channel))

    def confirm(self):
        """哥哥回复/接听 → 共享状态正式启用。"""
        with self.lock:
            self.state["active"] = True
            self._save_state()
        self.bot.log("[shared] %s回应，共享上下文启用（+%s）" % (
            self.bot.identity.owner_name, self.other_identity()))

    def deactivate(self):
        with self.lock:
            self.state = {}
            self.record = []
            self._save_state()
            self._save_record()

    # ---------- 共享记录 ----------
    def append(self, speaker, target, content):
        """记录一轮对话。target: 哥哥 / 另一角色身份名；speaker: 说话者。"""
        with self.lock:
            self.record.append({
                "stamp": self.bot.timeline.stamp(),
                "target": target,
                "channel": self.channel(),
                "speaker": speaker,
                "content": content,
            })
            self._save_record()

    def last_entry(self):
        """共享记录最新一条（说话对象钉在当前消息上用）。"""
        return self.record[-1] if self.record else None

    def record_block(self):
        """共享记录注入块（青柠-哥哥 / 青柠-另一角色 双线流水，供 LLM 分辨说话对象）。"""
        if not self.record:
            return ""
        lines = []
        owner = self.bot.identity.owner_name
        for r in self.record:
            target = owner if r.get("target") == owner else r.get("target", "?")
            who = r.get("speaker", "?")
            text = (r.get("content") or "").replace("\n", " ")
            lines.append("（对%s·%s）%s：%s" % (target, r.get("channel", "?"), who, text))
        return "\n".join(lines)

    # ---------- 分家 ----------
    def settle(self):
        """共享状态结束：共享摘要+记录拆成两份摘要、记录按标注归位各自 chatlog。

        失败降级：LLM 拆分失败时两边摘要都保留共享摘要原样，聊天记录照常归位。
        """
        other = self.other_identity()
        channel = self.channel()
        summary = self.summary_text()
        record = list(self.record)
        chat_block = "\n".join(
            "（对%s·%s）%s：%s" % (
                self.bot.identity.owner_name
                if r.get("target") == self.bot.identity.owner_name
                else r.get("target", "?"),
                r.get("channel", "?"), r.get("speaker", "?"),
                (r.get("content") or "").replace("\n", " "),
            )
            for r in record
        )
        # ① 拆成两份侧重摘要
        owner_name = self.bot.identity.owner_name
        owner_summary, other_summary = self._split_summaries(other, summary, chat_block)
        # ② 覆盖两边的摘要（文件 + 内存）
        self.bot.summaries[owner_name] = owner_summary
        self.bot._save_summary(owner_name)
        if other:
            self.bot.summaries[other] = other_summary
            self.bot._save_summary(other)
        # ③ 共享记录按标注归位各自 chatlog
        self._archive_record(record, channel)
        # ④ 清理共享状态
        self.deactivate()
        self.bot.log("[shared] 分家完成：%s + %s" % (owner_name, other or "?"))

    def _split_summaries(self, other, summary, chat_block):
        prompt_file = self._load_section("## 五、")
        if not prompt_file or not other:
            return summary, summary
        variables = {
            "shared_summary": summary or "（无）",
            "shared_chat": chat_block or "（无）",
            "other_identity": other,
        }
        prompt = prompt_file
        for key, value in variables.items():
            prompt = prompt.replace("{{%s}}" % key, str(value))
        try:
            data = self.bot.llm.chat_json(prompt, temperature=0.2, call_type="digest")
            if isinstance(data, dict):
                brother = str(data.get("brother") or "").strip() or summary
                other_s = str(data.get("other") or "").strip() or summary
                return brother[:1500], other_s[:1500]
        except Exception as exc:
            self.bot.log("[shared] 分家摘要失败，保留共享摘要: %s" % exc)
        return summary, summary

    def _archive_record(self, record, channel):
        """共享记录按 target 标注写入各自 chatlog（内容前标 [短信]/[电话]/[视频电话]）。"""
        chatlog = self.bot.plugins.get("chatlog")
        if chatlog is not None:
            chatlog.refresh_counters()
        groups = {}
        for r in record:
            target = r.get("target")
            if target not in groups:
                groups[target] = []
            groups[target].append(r)
        for target, rows in groups.items():
            if not target:
                continue
            for r in rows:
                self._append_stamped(target, r.get("speaker", "?"),
                                     "[%s]%s" % (r.get("channel", channel), r.get("content", "")),
                                     r.get("stamp", ""))

    def _append_stamped(self, identity, who, content, stamp):
        """按记录里的剧情时间戳写入 chatlog（不经过 chatlog 插件，避免用当前时间）。"""
        text = str(content).strip()
        if not text or not identity or not stamp:
            return
        path = self.bot.chatlog_store.path_for(identity, self.bot.timeline.story_date_of(stamp))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        exists = os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if not exists:
                f.write("# 聊天记录 %s %s\n\n" % (identity, self.bot.timeline.story_date_of(stamp)))
            lines = text.split("\n")
            f.write("- %s **%s**：%s\n" % (stamp, who, lines[0]))
            for line in lines[1:]:
                f.write("  %s\n" % line)

    # ---------- 提示词段读取（复用主动消息提示词文件） ----------
    def _load_section(self, marker):
        """从 prompt/青柠_主动消息提示词.md 读指定段（按 "## " 标题切分）。"""
        base = os.path.dirname(os.path.abspath(__file__))
        rel = self.bot.config.get("proactive", {}).get("prompt_file", "青柠_主动消息提示词.md")
        path = os.path.join(base, "..", "prompt", rel)
        if not os.path.isfile(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return ""
        sections = {}
        current = None
        for line in text.split("\n"):
            if line.startswith("## "):
                current = line.strip()
                sections[current] = []
            elif current and line.startswith("## "):
                pass
            elif current:
                sections[current].append(line)
        for key in sections:
            if key.startswith(marker):
                return "\n".join(sections[key]).strip()
        return ""
