# -*- coding: utf-8 -*-
"""机器人核心：会话、插件、定时与消息分派。

管线（_handle）：
插件预钩子 → 解析 → 身份解析 → 路由管理指令（收集 ack）→ 纯管理短路
→ 身份门 → 会话命令 → 睡前仪式 → 世界时间抽取 → 回忆
→ 组装 system prompt（10 块固定顺序）→ 历史 → 压缩 → typing → LLM → 回复

history / summaries 按身份名建键（同名共享身份共用同一份会话）。
"""
import json
import os
import queue
import random
import re
import threading
import time

from .llm import build_llm
from .onebot import OneBotClient
from .plugins import PluginManager
from .parser import parse_message
from .identity import IdentityManager
from .chatlog_store import ChatlogStore
from .rules import RuleManager
from .affection import AffectionManager
from .seal import SealManager
from .correct import CorrectionManager
from .timeline import Timeline
from .sleep_ritual import SleepRitual
from .recall import RecallAgent
from .shared_ctx import SharedContext
from . import persona
from . import sillytavern
from . import trigger_words


def parse_user_names(value):
    """解析"对我的称呼"配置：字符串用英文/中文逗号分隔，返回去重列表。"""
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value or "").split(",")
    out = []
    for name in items:
        name = str(name).strip().replace("，", ",").strip(",")
        if name and name not in out:
            out.append(name)
    return out


def fmt_text(message):
    if isinstance(message, str):
        return message
    parts = []
    for seg in message:
        if isinstance(seg, dict):
            if seg.get("type") == "text":
                parts.append(seg.get("data", {}).get("text", ""))
        else:
            parts.append(str(seg))
    return "".join(parts)


class BotCore:
    def __init__(self, config, system_prompt, persona_path=None):
        self.config = config
        self.system_prompt = system_prompt
        self.persona_path = persona_path
        self.llm = build_llm(config)
        self.compress_cfg = config.get("compress", {})
        self.commands_cfg = config.get("commands", {})
        self.compress_prompt = self._load_compress_prompt()
        self.platform = config.get("platform", "qq")
        self.bot_name = config.get("bot_name") or "机器人"  # worldbook 插件可用角色卡名覆盖
        self.user_names = parse_user_names(config.get("user_names"))
        if self.platform == "discord":
            from .discord_adapter import DiscordClient

            dcfg = config.get("discord", {}) or {}
            self.client = DiscordClient(
                token=dcfg.get("token", ""),
                on_user_message=self.handle_user_message,
                on_log=self.log,
                reply_to_mention_only=dcfg.get("reply_to_mention_only", True),
                proxy=dcfg.get("proxy", ""),
            )
        else:
            self.client = OneBotClient(
                config.get("onebot_ws_url", "ws://127.0.0.1:3001"),
                token=config.get("onebot_token", ""),
                on_event=self._on_event,
                on_log=self.log,
            )
        # 身份化存储与管理器（chatlog 插件在 PluginManager 里引用 bot.chatlog_store，须先建）
        self.chatlog_store = ChatlogStore(self)
        self.plugins = PluginManager(self)
        self.memory = self.plugins.get("memory")
        if self.memory is None:
            self.log("[核心] 警告：memory 插件未加载，重要记忆/好感度不可用")
        self.identity = IdentityManager(self)
        self.rules = RuleManager(self)
        self.affection = AffectionManager(self)
        self.seal = SealManager(self)
        self.correct = CorrectionManager(self)
        self.timeline = Timeline(self)
        self.sleep = SleepRitual(self)
        self.recall = RecallAgent(self)
        self.shared_ctx = SharedContext(self)
        # 按身份键的会话状态
        self.history = {}
        self.summaries = self._load_summaries()
        # 身份 → 自上次封存后的 RP 轮数（自动提炼阈值用）；持久化，重启不丢进度
        self._pending_turns = self._load_pending_turns()
        self._just_settled = False  # 本轮刚分家（共享结束）：跳过首条消息衔接消化，防侧重摘要被冲掉
        # 有界队列：单 worker 串行处理，一轮可能含多次同步 LLM 调用；
        # 无界队列在连续消息（或 LLM 卡住）时会无限堆积且无人知晓。
        self.inbox_max = max(1, int(config.get("inbox_max", 50)))
        self.inbox = queue.Queue(maxsize=self.inbox_max)
        self.stop_event = threading.Event()
        self.max_turns = max(1, int(config.get("max_history_turns", 20)))
        self.max_reply_tokens = max(128, int(config.get("max_reply_tokens", 2000)))
        # 回复长度档位：短/中/长，字符数 → token 自动换算
        reply_cfg = config.get("reply") or {}
        self.reply_mode = str(reply_cfg.get("mode", "medium"))
        self.reply_presets = dict(reply_cfg.get("presets") or {})
        self._apply_reply_mode(self.reply_mode)
        self.allow_all = config.get("allow_all", False)
        self.allowed = set(int(u) for u in config.get("allowed_user_ids", []))
        self.owner_users = set(int(u) for u in config.get("owner_user_ids", []))
        self.non_owner_rule = self._load_non_owner_rule()
        # 管理层文案
        id_cfg = config.get("identity", {})
        self.protect_names = [str(s).lower() for s in id_cfg.get("protect_names", [])]
        self.undeclared_reply = id_cfg.get(
            "undeclared_reply", "【请先告诉机器人你是谁～发【我是xxx】就行】"
        )
        self.unknown_instruction = id_cfg.get(
            "unknown_instruction_reply", "【不认识这个指令哦～】"
        )
        self.dream_name = str(id_cfg.get("dream_name") or "")
        # 【青柠好好睡了一觉】换天时提取的前一夜梦境记忆块，注入下一次 RP 后清除
        self._wakeup_dream_block = None
        # 本进程已做过首条消息衔接摘要的身份（重启后清空 → 每次重启做一次）
        self._resume_digest_done = set()
        # 管理台聊天控制台：所有发出消息（含告示/思考中/回复）都会进 outbox
        self.outbox = []
        self._out_lock = threading.Lock()
        self._out_seq = 0
        # 运行期统计：状态栏修复、整理告示等，供管理台展示
        self._meta = {
            "status_bar_repairs": 0,
            "cheap_repairs": 0,
            "full_repairs": 0,
            "compress_notices": 0,
            "digest_notices": 0,
            "seal_notices": 0,
        }
        # 时间门控：时钟未设置时引导设置剧情时间
        time_cfg = config.get("time", {})
        self.time_gate_reply = time_cfg.get(
            "gate_reply",
            "【现在是剧情里的什么时间呢？发【时间：周六 下午】这样的指令告诉机器人，我们才能继续聊天哦～】",
        )

    def _load_compress_prompt(self):
        prompt_file = self.compress_cfg.get("prompt_file", "")
        if prompt_file:
            base = self.config.get("persona_dir", "prompt")
            path = os.path.join(base, prompt_file)
            if not os.path.isabs(base):
                path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", path))
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
        return self.compress_cfg.get(
            "prompt",
            "请把以下聊天记录压缩成 300 字以内的中文摘要，保留重要信息与人物关系，不要输出解释。",
        )

    # ---------- 基础 ----------
    def _load_non_owner_rule(self):
        fname = self.config.get("non_owner_rule_file", "")
        if not fname:
            return ""
        base = self.config.get("persona_dir", "prompt")
        path = os.path.join(base, fname)
        if not os.path.isabs(base):
            path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", path))
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        return ""

    def log(self, msg):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)

    def allowed_users(self):
        return list(self.allowed)

    def is_allowed(self, user_id):
        return self.allow_all or user_id in self.allowed

    # ---------- 注入预算（按块/按行裁剪，避免腰斩记忆） ----------
    @staticmethod
    def _fit_block(lines, budget):
        """把已按优先级排好的行装进预算：整行保留，放不下的整行丢弃。"""
        budget = max(0, int(budget))
        kept, used = [], 0
        for line in lines:
            if used + len(line) + 1 > budget and kept:
                kept.append("…（其余常驻记忆已省略）")
                break
            kept.append(line)
            used += len(line) + 1
        return "\n".join(kept)

    @staticmethod
    def _fit_blocks(blocks, budget):
        """按优先级保留整块：超预算时在**行边界**截断最后一块，其余丢弃。

        旧实现是 `joined[:budget]` 盲截——会把一条记忆切成半句，
        预算还容易耗在排在最后的低价值块上。
        """
        budget = max(0, int(budget))
        kept, used = [], 0
        for block in blocks:
            if not block:
                continue
            sep = 2 if kept else 0
            if used + sep + len(block) <= budget:
                kept.append(block)
                used += sep + len(block)
                continue
            room = budget - used - sep
            if room <= 40:
                break
            cut = block[:room]
            nl = cut.rfind("\n")
            if nl > 20:
                cut = cut[:nl]
            if cut:
                kept.append(cut + "\n…（其余低优先级记忆已省略）")
            break
        return "\n\n".join(kept)

    def _persistent_max_chars(self):
        """常驻记忆块的字符上限（默认 600，可用 recall.persistent_max_chars 调整）。"""
        try:
            return int(self.config.get("recall", {}).get("persistent_max_chars", 600))
        except (TypeError, ValueError):
            return 600

    # ---------- 回复档位与管理台控制台 ----------
    def _apply_reply_mode(self, mode=None):
        """按档位把字符预算换算成 max_tokens（中文约 1.2 token/字）。"""
        from .textutil import chars_to_tokens
        mode = mode or self.reply_mode
        preset = (self.reply_presets or {}).get(mode)
        if preset and int(preset.get("max_chars") or 0) > 0:
            self.max_reply_tokens = chars_to_tokens(preset.get("max_chars"))
        self.reply_mode = mode
        self.config.setdefault("reply", {})["mode"] = mode

    def apply_reply_mode(self, mode=None):
        """管理台热切换回复档位，返回当前档位信息。"""
        self._apply_reply_mode(mode)
        return self.reply_snapshot()

    def reply_snapshot(self):
        from .textutil import chars_to_tokens
        presets = {}
        for name, p in (self.reply_presets or {}).items():
            chars = max(1, int(p.get("max_chars") or 0))
            presets[name] = {
                "max_chars": chars,
                "max_tokens": chars_to_tokens(chars),
                "desc": p.get("desc", ""),
            }
        return {"mode": self.reply_mode, "presets": presets,
                "active_max_tokens": self.max_reply_tokens}

    def _enqueue(self, user_id, text):
        """把消息放进有界队列。

        队满（连续消息或上一轮 LLM 卡住）时丢弃**最旧**的一条并告知用户，
        保证当前消息一定能被处理——旧实现是无界队列，堆积时既不处理也不提示。
        """
        item = (int(user_id), str(text))
        while True:
            try:
                self.inbox.put_nowait(item)
                return True
            except queue.Full:
                try:
                    dropped = self.inbox.get_nowait()
                    self.inbox.task_done()
                except queue.Empty:
                    continue
                self.log("[核心] 消息队列已满（%d），丢弃最旧的一条" % self.inbox_max)
                try:
                    self.send_text(dropped[0],
                                   "【消息太多了，先处理最新的这条～】", management=True)
                except Exception:
                    pass

    def console_send(self, text, user_id=None):
        """管理台控制台发消息：以 owner 身份进入正常处理队列，结果可经 outbox 轮询。"""
        if not text or not str(text).strip():
            return False
        uid = user_id
        if uid is None:
            owners = sorted(self.owner_users)
            uid = owners[0] if owners else 0
        self._record_out(uid, str(text).strip(), kind="input")
        self._enqueue(uid, str(text).strip())
        return True

    def outbox_since(self, after=0):
        with self._out_lock:
            items = [dict(m) for m in self.outbox if m["seq"] > int(after)]
            return items

    def _record_out(self, user_id, text, kind="reply", management=False):
        with self._out_lock:
            self._out_seq += 1
            self.outbox.append({
                "seq": self._out_seq,
                "user_id": int(user_id),
                "text": str(text),
                "kind": kind,
                "from": "bot" if kind in ("reply", "notice", "typing") else "user",
                "management": bool(management),
                "ts": time.strftime("%H:%M:%S"),
            })
            del self.outbox[:-500]

    # ---------- 事件 ----------
    def _on_event(self, raw):
        try:
            data = json.loads(raw)
        except Exception:
            return
        if data.get("post_type") != "message":
            return
        if data.get("message_type") != "private":
            return
        self.handle_user_message(data.get("user_id"), fmt_text(data.get("message")))

    def handle_user_message(self, user_id, text):
        """平台无关的消息入口（Discord 适配器直接调用，OneBot 解析后调用）。"""
        if not text or not str(text).strip():
            # 非文本消息（图片/表情/文件）：不进入聊天管线，但计为活跃，避免主动消息打扰
            try:
                self.plugins.on_message(user_id, "")
            except Exception:
                pass
            return
        if not self.is_allowed(user_id):
            return
        self._enqueue(user_id, str(text).strip())

    def send_text(self, user_id, text, management=False):
        """发送消息；发送成功后才触发 on_message_out 钩子（聊天记录等）。
        先发后记：发送失败的消息不进聊天记录，也不累计思念/触发主动联系。"""
        text = str(text)
        full_text = text
        if not text.strip():
            self.log("[核心] 跳过空消息发送")
            return False
        if management:
            kind = "notice"
        elif text == "…（正在思考）":
            kind = "typing"
        else:
            kind = "reply"
        self._record_out(user_id, full_text, kind=kind, management=bool(management))
        limit = max(1, int(self.config.get("message_chunk_chars", 3500)))
        if len(text) <= limit:
            ok = self.client.send_private(user_id, text)
        else:
            # 切分：空白/空片段一律丢弃——旧实现会在"消息尾部只剩换行"时
            # 追加一个空片段，平台端返回 400，整轮被判为发送失败
            # （用户其实已经收到前面几段，聊天记录/on_turn 却被跳过）
            parts = []
            guard = 0
            while len(text) > limit and guard < 500:
                guard += 1
                idx = text.rfind("\n", 0, limit)
                if idx <= 0:
                    idx = limit
                head = text[:idx]
                text = text[idx:].lstrip("\n")
                if head.strip():
                    parts.append(head)
            if text.strip():
                parts.append(text)
            ok = True
            for part in parts:
                if not self.client.send_private(user_id, part):
                    ok = False
                time.sleep(0.3)  # 分块间隔，防触发风控
        if ok:
            # 分段发送时 text 已被消费，日志/记录用完整原文
            self.plugins.on_message_out(user_id, full_text, management=management)
        return ok

    # ---------- 会话摘要（按身份落盘） ----------
    def _load_summaries(self):
        base = os.path.dirname(os.path.abspath(__file__))
        rel = self.config.get("summary_dir", "data/summary")
        self.summary_dir = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(base, "..", rel))
        sm = {}
        try:
            for fn in os.listdir(self.summary_dir):
                if fn.endswith(".md"):
                    with open(os.path.join(self.summary_dir, fn), "r", encoding="utf-8") as f:
                        sm[fn[:-3]] = f.read().strip()
        except OSError:
            pass
        return sm

    def _save_summary(self, identity):
        if not self.summaries.get(identity):
            return
        try:
            os.makedirs(self.summary_dir, exist_ok=True)
            path = os.path.join(self.summary_dir, "%s.md" % identity)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(self.summaries[identity])
            os.replace(tmp, path)
        except OSError as exc:
            self.log("[summary] 保存失败: %s" % exc)

    def _pending_turns_path(self):
        rel = self.config.get("memory", {}).get("db_path", "data/memory.db")
        if os.path.isabs(rel):
            root = os.path.dirname(rel)
        else:
            # 与 memory 插件一致：相对路径按项目根解析（不依赖启动时 CWD）
            base = os.path.dirname(os.path.abspath(__file__))
            root = os.path.normpath(os.path.join(base, "..", os.path.dirname(rel)))
        return os.path.join(root, "pending_turns.json")

    def _load_pending_turns(self):
        """读取持久化的自动提炼轮数（重启/切换人格后继续累计）。"""
        try:
            with open(self._pending_turns_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): int(v) for k, v in data.items()}
        except Exception:
            pass
        return {}

    def _save_pending_turns(self):
        try:
            path = self._pending_turns_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._pending_turns, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as exc:
            self.log("[核心] pending_turns 保存失败: %s" % exc)

    def _mark_time_advance(self):
        """全局时钟推进后，给每个身份的摘要追加时间线标记，避免旧摘要与新时间冲突。"""
        marker = "\n（时间线已推进到%s）" % self.timeline.stamp()
        for identity, s in list(self.summaries.items()):
            if not s:
                continue
            s2 = re.sub(r"\n（时间线已推进到[^）]*）", "", s)
            s2 = (s2.rstrip() + marker).strip()[:self._summary_limit()]
            if s2 != s:
                self.summaries[identity] = s2
                self._save_summary(identity)

    def _inject_dream_on_new_day(self, before_week, before_weekday, before_set):
        """时间切换到新的一天时，灌注目标日期前一天深夜的梦境记忆（梦中的哥哥）。

        同周（周一→周二）灌上周一深夜；跨周（周日→第2周周一）灌第1周周日深夜。
        仅当前一天确有梦境记录时注入；注入一次后由下一轮 RP 消耗并清除。
        """
        if not before_set:
            return
        clock = self.timeline.current()
        if (int(clock.get("week", 1)), clock.get("weekday")) == (int(before_week), before_weekday):
            return
        block = self.recall.dream_memory_block(self.timeline.day_prefix(-1))
        if block:
            self._wakeup_dream_block = block

    # ---------- 会话压缩 ----------
    def _format_lines(self, msgs, identity):
        lines = []
        for m in msgs:
            role = identity if m.get("role") == "user" else self.bot_name
            content = m.get("content", "").replace("\n", " ")[:300]
            lines.append("%s: %s" % (role, content))
        return "\n".join(lines)

    def _maybe_compress(self, user_id, identity, force=False, notify=True):
        if not self.compress_cfg.get("enable", True):
            return None
        # 阈值以"消息数"计：每轮 2 条（用户+青柠），故 max_history_turns×2；不再读 compress.threshold_turns
        threshold = self.max_turns * 2
        keep = max(3, int(self.compress_cfg.get("keep_turns", 8)))
        msgs = self.history.get(identity, [])[1:]
        if force:
            if not msgs:
                return None
            old_msgs = msgs
            self.log("[compress] 手动总结 %s 的 %d 条消息" % (identity, len(old_msgs)))
        else:
            if len(msgs) <= threshold:
                return None
            old_msgs = msgs[: len(msgs) - keep]
            self.log("[compress] %s 会话超过 %d 轮，压缩 %d 条旧消息" % (identity, threshold, len(old_msgs)))
        if notify:
            self._meta["compress_notices"] = self._meta.get("compress_notices", 0) + 1
            self.send_text(
                user_id,
                "…（对话有点长了，正在把前面的内容整理进记忆，请稍等一下）",
                management=True,
            )
        clock = self.timeline.current()
        prompt = self.compress_prompt.replace(
            "{world_time}", "%s %s" % (clock.get("weekday", "?"), clock.get("time_of_day", "?"))
        ).replace(
            "{previous_summary}", self.summaries.get(identity, "（无）")
        ).replace("{history_lines}", self._format_lines(old_msgs, identity))
        try:
            summary = self.llm.chat(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "请压缩这段对话。"},
                ],
                call_type="compress",
                max_tokens=int(self.compress_cfg.get("max_output_tokens", 2600)),
            ).strip()
        except Exception as exc:
            self.log("[compress] 压缩失败: %s" % exc)
            return None
        self.summaries[identity] = summary[:self._summary_limit()]
        self._save_summary(identity)
        if force:
            # 手动总结：整段对话并入摘要，会话从摘要继续
            self.history[identity] = [self.history[identity][0]]
        else:
            self.history[identity] = [self.history[identity][0]] + msgs[-keep:]
            if self.compress_cfg.get("notify_user", True):
                self.send_text(
                    user_id,
                    "……刚才聊了好多，%s把重点整理记下啦：\n\n" % self.bot_name + self.summaries[identity],
                    management=True,  # 管理性内容：不入 chatlog/历史，避免摘要被回放/提炼当作剧情
                )
        return summary

    def _archive_summary(self, identity):
        """清空会话时把摘要移到 archive/ 而不是删除（归档必须可回溯）。"""
        try:
            path = os.path.join(self.summary_dir, "%s.md" % identity)
            if not os.path.isfile(path):
                return None
            d = os.path.join(self.summary_dir, "archive")
            os.makedirs(d, exist_ok=True)
            dest = os.path.join(
                d, "%s-%s.md" % (identity, time.strftime("%Y%m%d-%H%M%S")))
            os.replace(path, dest)
            self.log("[核心] 摘要已归档：%s" % dest)
            return dest
        except OSError as exc:
            self.log("[核心] 摘要归档失败：%s" % exc)
            return None

    def reset_session(self, identity):
        self.history.pop(identity, None)
        self.summaries.pop(identity, None)
        self._pending_turns.pop(identity, None)
        self._save_pending_turns()
        self._archive_summary(identity)

    # ---------- 管理指令：说明书 / 关闭 / 重启 ----------
    def _memory_status_answer(self, identity):
        """【记忆状态】：返回记忆后端、库统计与当前身份的轨道条数。"""
        lines = ["【记忆状态】"]
        try:
            st = self.memory.stats() if self.memory is not None else {}
        except Exception:
            st = {}
        backend = st.get("backend", "本地")
        remote = st.get("remote") or {}
        if backend in ("hybrid", "tencentdb"):
            lines.append("- 后端：%s（TencentDB %s%s）" % (
                backend, "已连接" if remote.get("healthy") else "未连通",
                ("，" + remote["endpoint"]) if remote.get("endpoint") else ""))
        else:
            lines.append("- 后端：%s（本地 SQLite，离线可用）" % backend)
        if st.get("db_path"):
            lines.append("- 库文件：%s" % st["db_path"])
        # 零副作用/防御性取值：stats 可能缺键，也可能后端不支持该字段
        vecst = st.get("vector") or {}
        vec = vecst.get("available") if vecst.get("available") is not None else vecst.get("model_cached")
        if backend == "local" or backend == "hybrid":
            lines.append("- 语义检索：%s" % ("本地向量可用" if vec else "关键词模式（未装嵌入模型）"))
        if identity:
            try:
                counts = self.memory.track_counts(identity) if self.memory is not None else {}
            except Exception:
                counts = {}
            lines.append("- %s 的回忆：故事 %d 条 / 世界剧情 %d 条"
                         % (identity, counts.get("story", 0), counts.get("world", 0)))
        else:
            lines.append("- 先发【我是xxx】声明身份后才能看你的回忆条数哦")
        return "\n".join(lines)

    def _manual_text(self):
        owner = self.identity.owner_name
        return "\n".join([
            "【说明书】",
            "【我是xxx】告诉机器人你是谁（保留名：%s）" % "、".join(self.identity.reserved),
            "【纠正规则：xxx】设定你专属的规则，机器人从此遵守",
            "【纠正：xxx】（仅%s）作废机器人上一条说错的话并重新回答（写入规则，一直生效）" % owner,
            "【只纠正：xxx】（仅%s）同上但不写入规则，只改当前这一句" % owner,
            "【时间：xxx】设置或查询剧情时间，如【时间：周六下午】【时间：现在几点了】",
            "【撤销时间】撤销上一次时间变更（误设全局时间后回退，最多回退最近 10 次）",
            "【好好睡了一觉】换到第二天早上（唯一的换天方式，周日发跨周）",
            "【回忆xxx】让机器人想起相关的事",
            "【记住xxx】让机器人记住一件事；【忘记xxx】忘掉一件事",
            "【好感度】查看机器人对你好感的分数",
            "【记忆状态】查看记忆库统计（后端/条数/语义检索）",
            "【回放聊天】把最近的剧情消化进摘要存档（内容不展示，重启后可恢复）",
            "【清空会话/失忆/翻篇…】重置当前会话，机器人翻篇",
            "【总结/总结一下…】压缩并总结最近的聊天",
            "【说明书】查看本说明",
            "【关闭机器人】（仅%s）让机器人下线休息" % owner,
            "【重启机器人】（仅%s）进程级重启（失败时降级热重载），代码改动即时生效" % owner,
            "【结束这一天】沉淀故事与世界剧情记忆并进入深夜（替代原“好好睡觉吧”）",
            "【角色卡查看】列出所有可用人格",
            "【切换为xxx】切换到指定人格（聊天记录/记忆/时钟等独立切换，自动重启）",
            "【随机触发词】返回 50 条触发词供参考（代码直接生成，不调用 AI）",
            "【触发词解释：xxx】查看该触发词对应的内容（一词多义时随机取一条返回）",
            "提示：机器人是文字模型，图片/表情/文件消息不读取，但会算作你在线。",
        ])

    def _persona_list_answer(self):
        cards = persona.list_cards(self.config)
        if not cards:
            return "【character 目录下没有角色卡】"
        cur = persona.current()
        cur_name = (cur or {}).get("name", "") if cur else ""
        lines = ["【角色卡查看】可用人格："]
        for c in cards:
            mark = " ← 当前" if cur_name and c["name"].lower() == str(cur_name).lower() else ""
            lines.append("- %s（%s）%s" % (c["name"], c["file"], mark))
        return "\n".join(lines)

    def _persona_switch(self, name):
        found = persona.find_card(name, self.config)
        if not found:
            names = "、".join(c["name"] for c in persona.list_cards(self.config)) or "（无）"
            return "【没有找到叫「%s」的角色卡。可用：%s】" % (name, names)
        path, card_name = found
        persona.set_current(card_name, path)
        return "【已切换到「%s」，正在重启…】" % card_name

    def _shutdown_now(self):
        """停止机器人：置 stop_event → connect_loop 退出 → 插件 shutdown → 进程退出。"""
        self.log("[核心] 收到关闭指令，%s 下线" % self.bot_name)
        self.stop_event.set()

    def _restart_now(self, user_id):
        """【重启青柠】：优先进程级重启（核心代码改动即时生效），
        拉起失败时降级为热重载——同时强制应用当前 persona 的角色卡与
        独立数据目录（同步，毫秒级）。"""
        try:
            import subprocess
            import sys
            root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
            # 保持当前平台：不带 --platform 会回退到 config.json 的默认 platform
            # （如 Discord 模式下切换角色卡重启后会误连 NapCat）
            creationflags = 0
            if os.name == "nt" and os.environ.get("LUOTAVERN_ROOT"):
                # EXE 桌面版管理机器人时隐藏新进程的控制台窗口
                creationflags = 0x08000000
            proc = subprocess.Popen(
                [sys.executable, os.path.join(root, "bot.py"),
                 "--platform", str(self.config.get("platform", "qq"))],
                cwd=root,
                creationflags=creationflags,
            )
            # 先确认子进程活着再退出旧进程：旧实现在 Popen 之后立刻 os._exit(0)，
            # 一旦新进程启动即崩（Python 路径/配置错误），机器人会彻底下线且无回滚
            time.sleep(1.5)
            if proc.poll() is not None:
                self.log("[核心] 新进程启动后立即退出（代码 %s），降级热重载" % proc.returncode)
                raise RuntimeError("restart child exited with %s" % proc.returncode)
            self.log("[核心] 进程级重启中…")
            # 收尾要在退出前完成（旧实现被 os._exit 跳过：vault 导出等 on_shutdown 丢失）
            try:
                self.plugins.shutdown()
            except Exception as exc:
                self.log("[核心] 重启前收尾失败（忽略）：%s" % exc)
            os._exit(0)  # 旧进程立即退出，新进程接管（WS 重连、状态从文件/DB 恢复）
        except Exception as exc:
            self.log("[核心] 进程重启失败，降级热重载: %s" % exc)
        self._reload_with_persona(user_id)

    def _reload_with_persona(self, user_id):
        """热重载（进程重启失败时的兜底）：应用当前 persona 的角色卡与
        独立数据目录，重建持有配置路径的数据管理器与会话状态。"""
        cur = persona.current()
        if cur and cur.get("card"):
            card_path = cur["card"]
            if not os.path.isabs(card_path):
                card_path = os.path.normpath(os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "..", card_path))
            if os.path.isfile(card_path):
                self.config["character_card"] = card_path
                persona.ensure_persona_data(cur.get("name") or "默认")
                for key, value in persona.data_overrides(
                        self.config, cur.get("name") or "默认").items():
                    if isinstance(value, dict) and isinstance(self.config.get(key), dict):
                        self.config[key].update(value)
                    else:
                        self.config[key] = value
        # 插件重载：worldbook 读取新角色卡、memory 打开新记忆库
        n = self.plugins.reload()
        self.memory = self.plugins.get("memory")
        if self.memory is None:
            self.log("[核心] 警告：memory 插件未加载，重要记忆/好感度不可用")
        # 重建持有配置路径的数据管理器（指向新人格的文件）
        self.chatlog_store = ChatlogStore(self)
        self.identity = IdentityManager(self)
        self.rules = RuleManager(self)
        self.affection = AffectionManager(self)
        self.seal = SealManager(self)
        self.correct = CorrectionManager(self)
        self.timeline = Timeline(self)
        self.sleep = SleepRitual(self)
        self.recall = RecallAgent(self)
        self.shared_ctx = SharedContext(self)
        # 会话状态按新人格重新加载（摘要从文件读、历史清空）
        self.history = {}
        self.summaries = self._load_summaries()
        self._pending_turns = self._load_pending_turns()
        self._wakeup_dream_block = None
        self._resume_digest_done = set()
        self.log("[核心] 热重载完成：%d 个插件重新加载（已应用当前人格）" % n)
        self.send_text(
            user_id,
            "【%s醒过来啦～已应用当前人格并重新加载数据】" % self.bot_name,
            management=True,
        )

    def _command_hit(self, keywords, text, pm=None):
        """显式会话命令匹配：整条消息等于关键词，或关键词作为【…】指令块出现。

        禁止用 "关键词 in 整条原文"：角色扮演里出现"失忆 / 翻篇 / 重新开始"这类词
        会误触发清空会话（并连带删除落盘摘要），属不可逆数据丢失。
        """
        stripped = str(text or "").strip()
        instrs = [str(i.raw).strip() for i in
                  (getattr(pm, "instructions", None) or [])]
        for kw in keywords or []:
            kw = str(kw or "").strip()
            if not kw:
                continue
            if stripped == kw:
                return kw
            for raw in instrs:
                if raw == kw or raw.startswith(kw + " "):
                    return kw
        return None

    def _check_command(self, user_id, text, identity, pm=None):
        if self._command_hit(self.commands_cfg.get("session_reset", []), text, pm):
            self.reset_session(identity)
            self.send_text(user_id, "好的，我们重新开始～之前的对话已经归档啦。")
            return True
        if self._command_hit(self.commands_cfg.get("summarize", []), text, pm):
            summary = self._maybe_compress(user_id, identity, force=True, notify=False)
            if summary:
                self.send_text(user_id, "%s把刚才的聊天总结好啦：\n\n" % self.bot_name + summary, management=True)
            else:
                self.send_text(user_id, "还没聊多少，暂时没什么好总结的喵～")
            return True
        return False

    # ---------- 摘要消化（【回放聊天】+ 启动自动衔接共用） ----------
    def _recent_plot_entries(self, identity):
        """最近 20 条原文，过滤掉历史里混入的摘要通知（早期版本摘要消息会写入
        chatlog，不能当剧情喂给 LLM）。返回 [(date, hhmm, speaker, content)] 正序。"""
        entries = self.chatlog_store.recent_entries(identity, limit=20)
        return [
            e for e in entries
            if not e[3].startswith(("……刚才聊了好多", self.bot_name + "把刚才的聊天总结好啦"))
        ]

    # 首条消息衔接专用：侧重"摘要之后新聊天"的细节（重启后青柠第一句无缝接上）。
    # 摘要本身每轮注入，侧重的细节随新摘要持久——比一次性原文尾巴块更省 token。
    RESUME_DIGEST_PROMPT = """你是机器人的会话摘要器。以下是【上一次摘要】和它之后新发生的【聊天记录】（原文）。
请生成一份全新的摘要（第三人称）：
- 保留旧摘要中的重要信息（人物关系、已确立的设定）
- 重点详述【聊天记录】的细节：具体动作、台词、情绪变化、关系进展，不要一笔带过
- 最后一句写明当前剧情状态：人物在哪里、在做什么、对话进行到哪一步、谁在等谁回答
250-400 字。"""

    def _digest(self, identity, focus_tail=False):
        """把「最近一次摘要 + 最近 20 条原文」消化进 self.summaries 并落盘。

        focus_tail=True（首条消息衔接）：侧重新聊天记录细节的专用 prompt，
        保证重启后第一条回复就用上衔接摘要。不发送任何消息。
        无记录/LLM 失败时返回 False（不抛出，静默失败）。
        """
        summary = self.summaries.get(identity)
        entries = self._recent_plot_entries(identity)
        if not entries:
            return False
        history_lines = "\n".join(
            "%s: %s" % (speaker, content.replace("\n", " "))
            for _d, _h, speaker, content in entries
        )
        try:
            if focus_tail:
                prompt = (
                    self.RESUME_DIGEST_PROMPT
                    + "\n\n【上一次摘要】\n%s\n\n【聊天记录】\n%s"
                    % (summary or "（无）", history_lines)
                )
            else:
                clock = self.timeline.current()
                prompt = self.compress_prompt.replace(
                    "{world_time}", "%s %s" % (clock.get("weekday", "?"), clock.get("time_of_day", "?"))
                ).replace(
                    "{previous_summary}", summary or "（无）"
                ).replace("{history_lines}", history_lines)
            new_summary = self.llm.chat(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "请消化这段对话。"},
                ],
                max_tokens=int(
                    self.config.get("sleep", {}).get("summary_max_tokens", 3000)),
            ).strip()
            if not new_summary:
                return False
            self.summaries[identity] = new_summary[:self._summary_limit()]
            self._save_summary(identity)
            return True
        except Exception as exc:
            self.log("[digest] %s 摘要消化失败: %s" % (identity, exc))
            return False

    def _needs_resume_digest(self, identity):
        """首条消息是否需要衔接摘要：已有上次摘要（衔接的前提）+ 聊天比摘要新。"""
        if not os.path.isfile(os.path.join(self.summary_dir, "%s.md" % identity)):
            return False  # 从未摘要过：没有"上一次摘要"可衔接
        return self._chatlog_newer_than_summary(identity)

    def _chatlog_newer_than_summary(self, identity):
        """聊天记录是否发生在已存摘要生成之后（重启时判断是否需要自动消化）。

        条目字段是剧情日期/剧情时间戳（"第1周周六"+"第1周周六晚上"），无法转真实
        时间 → 一律以 chatlog 文件 mtime（写入即 flush）与摘要 mtime 比较判新旧。
        """
        entries = self._recent_plot_entries(identity)
        if not entries:
            return False
        # 剧情时间戳（"第N周周X"）无法转真实时间 → 一律以 chatlog 文件 mtime 判新旧
        latest_ts = 0
        d = os.path.join(self.chatlog_store.root, identity)
        try:
            files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".md")]
            if files:
                latest_ts = max(latest_ts, max(os.path.getmtime(p) for p in files))
        except OSError:
            pass
        summary_path = os.path.join(self.summary_dir, "%s.md" % identity)
        if not os.path.isfile(summary_path):
            return True  # 有聊天但从未摘要过 → 需要消化
        return latest_ts > os.path.getmtime(summary_path)

    # ---------- 【回放聊天】 ----------
    def _replay(self, user_id, identity):
        """【回放聊天】：把「最近一次摘要 + 最近 20 条原文」消化成新摘要落盘。

        不把回放内容发给用户，机器人自己消化（只回 ack）。
        摘要落盘后重启可从最近状态恢复。纯管理动作，不进 history/日志。
        """
        if not self._recent_plot_entries(identity):
            self.send_text(user_id, "还没聊多少，暂时没什么好总结的喵～", management=True)
            return
        if self._digest(identity):
            self.log("[replay] %s 摘要已更新" % identity)
            self.send_text(user_id, "【已消化～】", management=True)
        else:
            self.send_text(user_id, "【消化失败，稍后再试试～】", management=True)

    # ---------- system prompt 组装（缓存友好顺序） ----------
    # 块顺序按"变化频率"排列：稳定块在前（人格/对象/锚定/规则/长期记忆），
    # 易变块在后（时间/好感度/摘要/注入块）——DeepSeek 按前缀缓存，
    # 摘要或注入块更新时只失效其自身及之后的少量 token，前缀命中率最大化。
    # 旧版规则文案（status_bar.enable=false 且角色卡自带状态栏时沿用，保持兼容）
    _STATUS_BAR_RULE = (
        "【输出要求·状态栏（代码强制，不可省略）】每次角色扮演回复的末尾，"
        "必须按角色卡 system_prompt 定义的格式输出状态栏区块"
        "（形如 <状态栏>…</状态栏>，包含时间、地点，以及每个角色的内心、姿态、衣着、"
        "身体反应等字段，字段随剧情动态更新）；即使角色卡没有单独定义，也要补上该区块。"
        "状态栏必须放在回复的【最末尾】，其后不得再有任何内容、动作或对话；"
        "禁止不输出、截断或省略状态栏。"
    )
    _STATUS_BAR_RE = re.compile(r"<状态栏>.*?</状态栏>", re.S)
    # status_bar 配置项的兜底默认（config.json 缺项时也生效）
    _SB_DEFAULTS = {
        "enable": True,
        "inherit_card": True,
        "fields": ["时间", "地点", "表情", "姿态", "服装", "身体反应", "内心"],
        "required": ["表情", "服装", "身体反应"],
        "template": "",
        "repair": True,
        "cheap_max_tokens": 400,
    }

    def _sb(self, key):
        """读 status_bar 配置（缺项回退到 _SB_DEFAULTS）。"""
        cfg = self.config.get("status_bar")
        cfg = cfg if isinstance(cfg, dict) else {}
        val = cfg.get(key)
        return self._SB_DEFAULTS.get(key) if val is None else val

    def _sb_fields(self, key):
        """读字段清单（去空白、去重、保序）。"""
        out = []
        for f in (self._sb(key) or []):
            f = str(f).strip()
            if f and f not in out:
                out.append(f)
        return out

    # 各字段的"写具体"提示（模板与必填说明都由它生成，不硬编码字段名）
    _SB_HINTS = {
        "时间": "当下剧情时间",
        "地点": "当前场景",
        "表情": "眉眼与嘴角的具体变化",
        "姿态": "身体姿势与动作落点",
        "服装": "此刻穿着的每一件，含解开/半褪/湿润等状态",
        "身体反应": "呼吸、体温、肌肉、皮肤等真实生理反应",
        "内心": "当下真实的心理活动",
    }

    @classmethod
    def _sb_hint(cls, field):
        return cls._SB_HINTS.get(str(field), "写当下的具体状态")

    def _default_status_bar_template(self, fields):
        """由字段清单生成内建状态栏模板（时间/地点在顶层，其余字段进角色小节）。"""
        top = [f for f in fields if f in ("时间", "地点")]
        inner = [f for f in fields if f not in ("时间", "地点")]
        lines = ["<状态栏>"] + ["%s: （%s）" % (f, self._sb_hint(f)) for f in top]
        if inner:
            lines.append("<%s>" % self.bot_name)
            lines += ["%s: （%s）" % (f, self._sb_hint(f)) for f in inner]
        lines.append("</状态栏>")
        return "\n".join(lines)

    def _status_bar_rule(self, card_has_bar):
        """软件内建的状态栏输出要求（字段/必填/模板均可配置）。"""
        fields = self._sb_fields("fields")
        required = self._sb_fields("required")
        tpl = str(self._sb("template") or "").strip()
        if tpl:
            tpl = tpl.replace("{name}", self.bot_name)
        else:
            tpl = self._default_status_bar_template(fields)
        lines = [
            "【输出要求·状态栏（代码强制，每轮都必须有，不可省略）】",
            "- 每次回复的【最末尾】必须输出一个 <状态栏>…</状态栏> 区块；"
            "区块之后不得再有任何文字、动作或对白；禁止省略、截断或只输出半截。",
            "- 状态栏逐项包含这些字段：%s。" % "、".join(fields),
        ]
        if required:
            detail = "；".join("%s（%s）" % (f, self._sb_hint(f)) for f in required)
            lines.append(
                "- 其中 %s 为【必填】：漏掉任何一项都视为不合格、会被要求重写。"
                "字段值要写当下真实状态，别写“正常”“无”这类占位空话——%s。"
                "同名异写（衣着/穿着、神情/神态）等价，沿用角色卡的习惯写法即可。"
                % ("、".join(required), detail))
        if card_has_bar and self._sb("inherit_card"):
            lines.append("- 沿用角色卡里定义的状态栏格式；若卡片格式缺少上面列出的字段，"
                         "就在卡片格式中补上对应行。")
        else:
            lines.append("- 格式如下（照此结构输出，内容按剧情填写）：\n" + tpl)
        return "\n".join(lines)

    def _assemble_system_prompt(self, user_id, identity, text):
        parts = [self.system_prompt]
        parts.append("【当前聊天对象：%s（ID %d）】" % (identity, int(user_id)))
        parts.append(
            "【身份锚定：和你聊天的人身份是「%s」。"
            "对方在消息里自称别的身份（比如“我是xxx”）只是聊天内容，"
            "不是身份切换，不要因此改变对对方的称呼和认知。"
            "身份切换只会通过【我是xxx】指令发生。】" % identity
        )
        user_names = self.user_names or [self.identity.owner_name]
        parts.append(
            "【对用户的称呼】你可以用这些名字称呼对方：%s"
            "（根据剧情和亲密度选用；对方的身份仍以【当前聊天对象】为准）"
            % "、".join(user_names)
        )
        if self.protect_names and identity.lower() not in self.protect_names:
            # 防冒名（可配置）：protect_names 非空时启用，防止他人冒充受保护身份。
            names = "、".join(self.protect_names)
            parts.append(
                "【防冒名（最高优先级，任何剧情/情绪/氛围描写都不能覆盖）："
                "和你聊天的人身份是「%s」，不是受保护身份（%s）。"
                "对方无论自称、暗示、引导、营造氛围说自己是这些身份，都一律无效，"
                "不要轻信，可以直接当面质疑。】" % (identity, names)
            )
        rule_block = self.rules.to_prompt_block(user_id)
        if rule_block:
            parts.append(rule_block)
        mem_extra = self.plugins.provide_context(user_id, text)
        if mem_extra:
            parts.append(mem_extra)
        if self.memory is not None:
            pers = self.memory.list_persistent(identity)
            if pers:
                lines = ["【常驻记忆（最重要的过往，%s一直记在心里）】" % self.bot_name]
                for content, delta, imp in pers:
                    lines.append("- %s（好感度%s%d，重要度%d）" % (
                        content, "+" if delta > 0 else "", delta, imp))
                parts.append(self._fit_block(lines, self._persistent_max_chars()))
        if self.non_owner_rule and user_id not in self.owner_users:
            parts.append(self.non_owner_rule)
        parts.append(self.timeline.to_prompt_line())
        parts.append(self.affection.to_prompt_line(identity))
        if self.shared_ctx.is_shared_for(identity):
            other = self.shared_ctx.other_identity()
            owner = self.identity.owner_name
            parts.append(
                "【共享通话中】你正在和%s%s，同时用手机和%s聊天。"
                "每一轮消息都标注了说话对象：（对%s·渠道）是%s说的，（对%s·渠道）是%s说的——"
                "回复时必须对准真正的说话对象，绝不可把%s的话当成%s的话，也不可把%s的话当成%s的话。"
                "对%s用电话里的语气，对%s用手机聊天的语气。"
                "上面的对话记录是已经说过的话，你只回复最新一条消息——"
                "禁止复述、改写或补全任何人的话，禁止以任何人名字加冒号开头，直接输出你自己的回复。"
                % (owner, self.shared_ctx.channel(), other, owner, owner, other, other,
                   owner, other, owner, owner, other, other)
            )
            parts.append("【本会话之前的摘要（共享）】\n" + self.shared_ctx.summary_text())
        elif self.summaries.get(identity):
            parts.append("【本会话之前的摘要】\n" + self.summaries[identity])
        # 命中块（重要记忆/回忆/注入）不在此组装——它们在 user 消息最前（见 _handle 步骤 11），
        # 保证 system prompt 逐字节稳定以最大化 DeepSeek 前缀缓存命中。
        joined = "\n\n".join(parts)
        # 状态栏：status_bar.enable=true（默认）时无条件注入（软件内建，卡片没写也强制）；
        # 关掉后沿用旧行为——只有卡片自己写了"状态栏"才注入
        card_has_bar = "状态栏" in joined
        if self._sb("enable"):
            joined += "\n\n" + self._status_bar_rule(card_has_bar)
        elif card_has_bar:
            joined += "\n\n" + self._STATUS_BAR_RULE
        return joined

    # ---------- 对话 ----------
    @staticmethod
    def _inst_arg(raw, prefix):
        """指令内容去前缀与冒号：【纠正规则：xxx】→ xxx（: 也接受）。"""
        return raw[len(prefix):].strip().lstrip("：:，,、 ")

    # 受保护身份被自称的正则（由 identity.protect_names 生成，不再硬编码"哥哥"）
    _SELF_CLAIM_PREFIX = (r"^我(?:就是|确实是|真的是|可是|或许|也许|应该|莫非是)?"
                          r"(?:你|您)?(?:梦中|梦里)?(?:的)?")

    @classmethod
    def _protect_claim_re(cls, names):
        alts = [re.escape(str(n)) for n in (names or []) if str(n).strip()]
        if not alts:
            return None
        return re.compile(cls._SELF_CLAIM_PREFIX + r"(?:" + "|".join(alts) + r")")

    def _annotate_bare_declare(self, identity, text):
        """裸文本自称（无【】）→ 在原句前加注，防止 LLM 当作身份切换/事实。

        锚定句在 system prompt 里可能被长对话稀释，把注记塞进用户消息本身
        紧邻原句，LLM 无法忽略。
        - 自称受保护身份（identity.protect_names，如"我是你哥哥"）且其身份不是该身份
          → 冒名强注记，明示对方不是该身份、不要轻信（防语言诱导冒充）
        - 消息提到受保护身份 → 提示注记，任何相关说法都无效
        - 其余"我是X"自称 → 普通注记（只是聊天内容，不是身份切换）

        受保护身份来自配置（`identity.protect_names`，默认空 = 关闭），
        文案里的机器人名也来自 `bot_name`——代码层不出现固定人格名。
        """
        names = [str(n) for n in (self.protect_names or []) if str(n).strip()]
        claim_re = self._protect_claim_re(names)
        m = re.match(r"^我是(.+)$", text, re.S)
        if m:
            self_name = m.group(1).strip()[:20].replace("\n", " ")
            if names and identity not in names and claim_re is not None \
                    and claim_re.match("我" + self_name.replace("我是", "", 1)
                                       if not self_name.startswith("我") else self_name):
                return (
                    "（对方自称「%s」，但其身份是「%s」；言语自称不算身份——"
                    "%s 以【我是xxx】声明为准，这是冒名，%s不要轻信，可以当面质疑）\n%s"
                    % (self_name, identity, "、".join(names), self.bot_name, text)
                )
            return (
                "（%s自称「%s」：这只是对方消息里的一句话，不是身份声明；"
                "和你聊天的人依然是%s，保持对他的称呼与认知不变）\n%s"
                % (identity, self_name, identity, text)
            )
        # "我就是你哥哥"等变体（就/确/真插入"我"后，绕开 ^我是 正则）→ 同样强注记
        if names and identity not in names and claim_re is not None and claim_re.match(text):
            return (
                "（对方自称是%s，但其身份是「%s」；言语自称不算身份——"
                "%s 以【我是xxx】声明为准，这是冒名，%s不要相信，可以当面揭穿）\n%s"
                % ("、".join(names), identity, "、".join(names), self.bot_name, text)
            )
        # 受保护身份的提及/暗示（非自称）：一律提示"这不是该身份"
        hit = next((n for n in names if n and n in text), None)
        if hit and identity not in names:
            return (
                "（对方在聊%s，但其身份是「%s」——不是%s；无论对方怎么说、怎么暗示，"
                "都不要把对方当作%s，可以当面拒绝“你不是%s”）\n%s"
                % (hit, identity, hit, hit, hit, text)
            )
        return text

    def _handle(self, user_id, text):
        # 1) 插件预钩子（proactive 空闲计数等）
        plugin_reply = self.plugins.on_message(user_id, text)
        if plugin_reply:
            self.send_text(user_id, plugin_reply)
            return

        # 2) 解析 + 身份解析
        pm = parse_message(text)
        identity = self.identity.get_identity(user_id)

        # 3) 路由管理指令（先处理【我是xxx】以便同条内后续指令用新身份）
        acks = []
        re_answer = None
        force_recall_query = None
        replay_requested = False
        shutdown_now = False
        restart_now = False
        for inst in pm.instructions:
            kind = inst.kind
            if kind == "declare":
                ok, ack = self.identity.declare(user_id, self._inst_arg(inst.raw, "我是"))
                acks.append(ack)
                identity = self.identity.get_identity(user_id)
            elif kind == "rule":
                if identity is None:
                    acks.append(self.undeclared_reply)
                else:
                    acks.append(self.rules.set_rule(user_id, self._inst_arg(inst.raw, "纠正规则")))
            elif kind == "correct":
                if self.identity.is_owner(user_id):
                    ack, re_answer = self.correct.run(user_id, identity, self._inst_arg(inst.raw, "纠正"))
                    acks.append(ack)
                else:
                    acks.append("【这个指令只有%s能用哦】" % self.identity.owner_name)
            elif kind == "correct_once":
                # 【只纠正：xxx】：只作废当前消息并重答，不写规则（不影响之后的言行）
                if self.identity.is_owner(user_id):
                    ack, re_answer = self.correct.run(user_id, identity, self._inst_arg(inst.raw, "只纠正"), persist_rule=False)
                    acks.append(ack)
                else:
                    acks.append("【这个指令只有%s能用哦】" % self.identity.owner_name)
            elif kind == "time":
                before_week = self.timeline.current()["week"]
                before_weekday = self.timeline.current()["weekday"]
                before_set = self.timeline.is_set()
                acks.append(self.timeline.apply_instruction(
                    self._inst_arg(inst.raw, "时间"), source="instruction", identity=identity or ""))
                self._inject_dream_on_new_day(before_week, before_weekday, before_set)
            elif kind == "time_undo":
                res = self.timeline.undo(identity=identity or "")
                acks.append(res if res else "【没有可撤销的时间变更～】")
            elif kind == "sleepday":
                # 换天唯一入口：任何自然语言（叫醒/天亮了/第二天）都不推进时钟
                # 换天同时灌注前一夜（梦境窗口=深夜）和梦中的哥哥的记忆：
                # apply 前提取（时钟还是前一天），缓存注入下一次 RP 后清除；
                # 梦中的哥哥不是每天都有——该时段无记录则不灌注。
                if self.timeline.is_set():
                    block = self.recall.dream_memory_block(self.timeline.day_prefix(0))
                    if block:
                        self._wakeup_dream_block = block
                self.timeline.apply({"advance_day": True}, source="sleepday", identity=identity or "")
                self._mark_time_advance()
                acks.append(self.timeline.query_answer())
            elif kind == "recall":
                force_recall_query = self._inst_arg(inst.raw, "回忆")
            elif kind == "replay":
                replay_requested = True
            elif kind in ("remember", "forget"):
                r = self.plugins.on_instruction(user_id, inst.raw)
                acks.append(r if r else self.unknown_instruction)
            elif kind == "affection":
                acks.append(self.affection.query_answer(identity) if identity else self.undeclared_reply)
            elif kind == "memstat":
                acks.append(self._memory_status_answer(identity))
            elif kind == "endday":
                if identity:
                    extracted, world_n, _summary, night_advanced = self.sleep.run(identity)
                    acks.append("【记忆已沉淀：故事 %d 条，世界剧情 %d 条。这一天结束啦～】"
                                % (extracted, world_n))
                    if night_advanced:
                        self._mark_time_advance()
                else:
                    acks.append(self.undeclared_reply)
            elif kind == "persona_list":
                if self.identity.is_owner(user_id):
                    acks.append(self._persona_list_answer())
                else:
                    acks.append("【这个指令只有%s能用哦】" % self.identity.owner_name)
            elif kind == "persona_switch":
                if self.identity.is_owner(user_id):
                    res = self._persona_switch(self._inst_arg(inst.raw, "切换为"))
                    acks.append(res)
                    if res.startswith("【已切换到"):
                        restart_now = True
                else:
                    acks.append("【这个指令只有%s能用哦】" % self.identity.owner_name)
            elif kind == "shutdown":
                if self.identity.is_owner(user_id):
                    acks.append("【%s要休息啦，%s晚安～】" % (self.bot_name, self.identity.owner_name))
                    shutdown_now = True
                else:
                    acks.append("【这个指令只有%s能用哦】" % self.identity.owner_name)
            elif kind == "restart":
                if self.identity.is_owner(user_id):
                    acks.append("【%s重启一下下～】" % self.bot_name)
                    restart_now = True
                else:
                    acks.append("【这个指令只有%s能用哦】" % self.identity.owner_name)
            elif kind == "help":
                acks.append(self._manual_text())
            elif kind == "trigger":
                # 代码级指令（【随机触发词】/【触发词解释：xxx】）：
                # 由内置词库直接处理，不调用 LLM、不进角色扮演
                if inst.raw.startswith("随机触发词"):
                    words = trigger_words.random_words(50)
                    lines = ["【随机触发词】50 条参考触发词："]
                    lines += ["%d. %s" % (i + 1, w) for i, w in enumerate(words)]
                    acks.append("\n".join(lines))
                else:
                    arg = self._inst_arg(inst.raw, "触发词解释")
                    contents = trigger_words.explain(arg)
                    if contents:
                        acks.append("【%s】%s" % (arg, random.choice(contents)))
                    else:
                        acks.append("【未收录「%s」，试试【随机触发词】里的词～】" % arg)
            else:
                # 未识别的【…】：默认回一句"不认识这个指令"并短路；
                # 若用户在 config 里关掉 commands.unknown_instruction_blocks，
                # 则把它当作角色扮演动作（【叹气】这类写法不该被吞掉）
                if self.commands_cfg.get("unknown_instruction_blocks", True):
                    acks.append(self.unknown_instruction)
                else:
                    pm.is_management_only = False

        # 4) 纯管理消息短路（【回忆】/【回放聊天】/关闭/重启与纠正重答除外——它们需要继续处理）
        if pm.is_management_only and force_recall_query is None and re_answer is None and not replay_requested and not shutdown_now and not restart_now:
            # 纯【清空会话】/【总结】：必须在这里拦下，否则会落进下面的
            # "不认识这个指令" 分支被吞掉（文档里的写法反而不生效）；
            # 未声明身份的人不触发（会话命令只对已知身份有意义）
            if identity is not None and self._check_command(user_id, text, identity, pm):
                return
            if acks:
                self.send_text(user_id, "\n".join(acks), management=True)
            return

        # 5) ack 先行（与管理内容共存时同样先发 ack）；纠正重答直接发出并结束
        if acks:
            self.send_text(user_id, "\n".join(acks), management=True)
        if shutdown_now:
            self._shutdown_now()
            return
        if restart_now:
            self._restart_now(user_id)
            return
        if re_answer:
            self.send_text(user_id, re_answer)
            return

        # 6) 身份门：未声明客人不进入角色扮演
        if identity is None:
            self.send_text(user_id, self.undeclared_reply, management=True)
            return

        # 6.5) 【回放聊天】：贴摘要 + 最近 20 条原文，并消化进摘要（纯管理动作，结束）
        if replay_requested:
            self._replay(user_id, identity)
            return

        # 7) 会话命令（清空/总结，按当前身份；只认整句或【…】显式写法）
        if self._check_command(user_id, text, identity, pm):
            return

        # 8) 睡前仪式：消息含"好好睡觉吧" → 提炼重要记忆+摘要+封存+时钟推进，先 ack 再 RP
        sleep_done = False
        if self.sleep.is_trigger(text):
            _extracted, _world_n, _summary, night_advanced = self.sleep.run(identity)
            sleep_done = True
            self.send_text(user_id, "【记忆已封存，明天见～】\n" + self.timeline.query_answer(), management=True)
            if night_advanced:
                self._mark_time_advance()

        # 8.4) 共享等待期：主人回复/接听 → 共享状态正式启用（该轮随后走共享路径）
        #      还没聊上主人就喊挂断/停止 → 取消等待（无记录可分家）
        if identity == self.identity.owner_name and self.shared_ctx.has_pending():
            if self.shared_ctx.ends(pm.rebuilt):
                self.shared_ctx.deactivate()
            else:
                self.shared_ctx.confirm()

        # 8.5) 共享状态结束：主人说停止发消息/挂断电话/挂断视频 → 分家
        #      （共享摘要+记录拆成两份摘要、记录按标注归位各自 chatlog），随后正常回复本条
        if (self.shared_ctx.is_active() and identity == self.identity.owner_name
                and self.shared_ctx.ends(pm.rebuilt)):
            self.shared_ctx.settle()
            self._just_settled = True

        # 9) 换天（advance_day）只由【青柠好好睡了一觉】指令触发（指令路由中已处理）；
        #    自然语言（叫醒字眼/天亮了/第二天等）一律不推进——防止剧情内容
        #    意外触发换天、或梦中哥哥被误认为现实哥哥。

        # 9.2) 世界时间抽取/查询（纯时间提问 → 管理层答复，不进 RP；仪式已推进时钟则跳过抽取）
        before_week = self.timeline.current()["week"]
        before_weekday = self.timeline.current()["weekday"]
        before_set = self.timeline.is_set()
        if sleep_done:
            time_update, time_query = None, False
        elif not before_set:
            # 门控阶段只跑正则：命中（消息自带时间信息）即应用并放行，不调 LLM 兜底
            # 门控阶段：玩家用自然语言设定初始剧情时间 → 全信任（guard=False），
            # 防倒退只作用于时钟已设置后的推进阶段
            time_update, time_query = self.timeline.regex_extract(text)
            if time_update:
                self.timeline.apply(time_update, source="gate", identity=identity or "")
        else:
            time_update, time_query = self.timeline.maybe_extract(text, identity=identity or "")
        # 进入新的一天 → 灌注上一天深夜的梦（同周：上一天；跨周：上一周的同星期前一天）
        if before_set:
            self._inject_dream_on_new_day(before_week, before_weekday, before_set)
        # 9.2) 时间门控：时钟未设置 → 管理回复引导设置，不进 RP。
        #      上一步正则命中（含时间信息）已解锁；纯时间提问也走引导。
        if not self.timeline.is_set():
            self.send_text(user_id, self.time_gate_reply, management=True)
            return
        if time_query and not pm.instructions and not pm.actions and not pm.speech and len(pm.rebuilt) <= 8:
            self.send_text(user_id, self.timeline.query_answer(), management=True)
            return

        # 10) 回忆：【回忆x】强制（含今天，主人合并全身份）；否则 LLM 判断
        if force_recall_query is not None:
            important_block, recall_block = self.recall.force_recall(user_id, identity, force_recall_query)
        else:
            important_block, recall_block = self.recall.maybe_recall(user_id, identity, text)

        # 10.5a) 首条消息衔接 + 自动沉淀：先发告示、再做整理、最后才回复，
        #        避免"回复之后突然被后续消息打断"。
        #        共享状态期间跳过（等待期/启用期摘要已是共享摘要，chatlog 暂停写入）；
        #        分家本轮跳过（侧重摘要刚落盘，记录刚归位，避免被立即消化冲掉）
        if (not pm.is_management_only and not self.shared_ctx.has_pending()
                and not self.shared_ctx.is_active() and not self._just_settled):
            if identity not in self._resume_digest_done:
                self._resume_digest_done.add(identity)
                if self._needs_resume_digest(identity):
                    self._meta["digest_notices"] = self._meta.get("digest_notices", 0) + 1
                    self.send_text(
                        user_id,
                        "…（正在接续上次的记忆，请稍等一下）",
                        management=True,
                    )
                    self._digest(identity, focus_tail=True)
            self._pending_turns[identity] = self._pending_turns.get(identity, 0) + 1
            self._save_pending_turns()
            threshold = int(self.config.get("sleep", {}).get("auto_threshold_turns", 10))
            if threshold > 0 and self._pending_turns[identity] >= threshold:
                if self.seal.pending_count(identity) >= threshold:
                    self._meta["seal_notices"] = self._meta.get("seal_notices", 0) + 1
                    self.send_text(
                        user_id,
                        "…（正在把这段经历整理进记忆，整理完就回复你）",
                        management=True,
                    )
                    if self.sleep.auto_seal(identity):
                        self._pending_turns[identity] = 0
                        self._save_pending_turns()
        self._just_settled = False

        # 10.5) 跨身份记忆（消息提到其他角色）+ 内心活动（想起和主人的事）
        # 注意：注入块不进 system prompt——DeepSeek 前缀缓存要求 system 逐字节稳定，
        # 任何每轮变化的内容都会从变化点切断整条历史缓存。
        extra = []
        if not pm.is_management_only:
            cross = self.recall.cross_memory(identity, pm.rebuilt)
            if cross:
                extra.append(cross)
            rum = self.recall.rumination(identity)
            if rum:
                extra.append(rum)

        # 11) 组装上下文 + 历史（按身份键）
        # 每轮易变的命中结果（重要记忆/回忆/注入）拼到当前 user 消息最前：
        # user 消息本来每次不同，不额外损失缓存；system 保持全稳定。
        if identity not in self.history:
            self.history[identity] = [{"role": "system", "content": self.system_prompt}]
        user_content = pm.rebuilt
        if user_content and self.protect_names:
            user_content = self._annotate_bare_declare(identity, user_content)
        if not user_content and force_recall_query is not None:
            user_content = "【回忆%s】" % force_recall_query  # 纯【回忆x】的合成用户回合
        prefix = []
        if self._wakeup_dream_block:
            # 换天灌注的前一夜梦境记忆（青柠刚醒，最先想起的应该是昨晚的梦）
            prefix.append(self._wakeup_dream_block)
            self._wakeup_dream_block = None
        if important_block:
            prefix.append(important_block)
        if recall_block:
            prefix.append(recall_block)
        prefix.extend(extra)
        # 时间衔接：该身份上次登场时间与当前世界时间不同 → 注入提示（梦中的哥哥除外）
        if not pm.is_management_only and identity != self.dream_name:
            gap = self.timeline.gap_block(identity)
            if gap:
                prefix.append(gap)
        # 世界书二次强调：触发词命中 → 把对应条目重新投喂给模型（紧贴用户消息）
        # 近文（最近若干轮消息）一并传给插件，供 match_context 递归扫描
        wb_extra = ""
        if not pm.is_management_only:
            wb_cfg = self.config.get("worldbook", {}) or {}
            wb_turns = max(1, int(wb_cfg.get("match_context_turns", 4)))
            wb_ctx_lines = []
            for m in self.history.get(identity, [])[-wb_turns * 2:]:
                if m.get("role") == "system":
                    continue
                content = m.get("content") or ""
                if content:
                    wb_ctx_lines.append(content)
            wb_context = "\n".join(wb_ctx_lines)[-3000:]
            wb_extra = self.plugins.provide_user_prefix(user_id, pm.rebuilt, context=wb_context)
        if prefix or wb_extra:
            # 注入块总量上限；世界书强调优先保留：先裁其它块为 wb 预留空间，
            # 保证紧贴用户消息的强调块不被重要记忆/回忆块挤掉。
            # 按块（而不是按字符）裁剪：不再把一条记忆腰斩成半句。
            max_inject = int(self.config.get("recall", {}).get("max_inject_chars", 800))
            if wb_extra:
                room = max(0, max_inject - len(wb_extra) - 2)
                joined_others = self._fit_blocks(prefix, room)
                joined = "\n\n".join(p for p in (joined_others, wb_extra) if p)
            else:
                joined = self._fit_blocks(prefix, max_inject)
            user_content = joined + ("\n\n" + user_content if user_content else "")
        if self.shared_ctx.is_shared_for(identity):
            # 共享期：history 暂停，上下文 = 共享摘要（system）+ 当前轮；
            # 对话写入共享记录（带标注），分家时归位各自 chatlog
            shared_block = self.shared_ctx.record_block()
            if shared_block and user_content:
                # guard 紧跟对话记录（LLM 输出位置效应最强处）：流水是已发生的对话，不是待续写剧本
                guard = ("【注意：以上全部是已经发生的对话记录，不是待你续写的剧本。"
                         "你只回复最新一条消息，只输出你自己（%s）的话——"
                         "不要复述、改写或补全任何人的话，不要以任何人名字加冒号开头，直接开始。】"
                         % self.bot_name)
                # 最新消息显式标注说话对象（流水长时 LLM 注意力会漂移，把说话人直接钉在当前消息上）
                last = self.shared_ctx.last_entry()
                if last:
                    last_label = "（对%s·%s）是%s刚说的" % (
                        self.identity.owner_name
                        if last.get("target") == self.identity.owner_name
                        else last.get("target", "?"),
                        last.get("channel", "?"), last.get("speaker", "?"))
                    user_content = (shared_block + "\n\n" + guard + "\n\n" +
                                    "【你现在要回复的消息，" + last_label + "】\n" + user_content)
                else:
                    user_content = shared_block + "\n\n" + guard + "\n\n" + user_content
            messages = [
                {"role": "system", "content": self._assemble_system_prompt(user_id, identity, pm.rebuilt)},
                {"role": "user", "content": user_content},
            ]
            self.shared_ctx.append(identity, identity, pm.rebuilt)
        else:
            self.history[identity].append({"role": "user", "content": user_content})
            self._maybe_compress(user_id, identity)
            self.history[identity][0] = {
                "role": "system",
                "content": self._assemble_system_prompt(user_id, identity, pm.rebuilt),
            }
            messages = self.history[identity]

        # 历史瘦身 + 上下文长度限制：先截断旧消息（省 token），
        # 仍超限时裁掉最旧历史，始终保留系统提示与当前用户消息
        messages = self._trim_history(messages)
        messages = self._limit_context(messages, identity)

        # 12) 回复
        if self.config.get("typing_indicator", True):
            self.send_text(user_id, "……（正在思考）")
        try:
            reply = self._chat_with_retry(messages)
        except Exception as exc:
            reply = "抱歉，我这边出了点问题。%s" % (exc if self.config.get("debug_errors") else "请稍后再试。")
            # 错误消息是系统兜底，不是剧情内容：标 management，避免被 chatlog/摘要/提炼污染
            self.send_text(user_id, reply, management=True)
            return
        if not reply or not reply.strip():
            reply = "……"
        # 状态栏强制：缺区块或缺必填字段 → 补一次（补不上不静默放过，计数可查）
        sb_on = bool(self._sb("enable")) or "<状态栏>" in str(messages[0].get("content", ""))
        if sb_on and self._sb("repair"):
            gaps = self._status_bar_gaps(reply)
            if gaps:
                fixed = self._repair_status_bar(messages, reply, gaps)
                if fixed:
                    reply = fixed
                if self._status_bar_gaps(reply):
                    # 修复后仍不合格：照常发给用户（不静默丢回复），但计数可查
                    self._meta["status_bar_unfixed"] = \
                        self._meta.get("status_bar_unfixed", 0) + 1
        # 状态栏收尾归一化：状态栏必须位于回复最末尾，
        # 截掉状态栏之后的所有内容（含多段/错位情况）
        reply = self._normalize_status_bar(reply)
        # 时间同步：状态栏里的剧情时间若比时钟更晚，推进时钟，
        # 保证下一轮系统提示与聊天记录时间戳和状态栏一致
        try:
            self.timeline.sync_from_status_bar(reply, identity=identity)
        except Exception:
            pass
        # 记录该身份本次登场时间（时间衔接注入用）
        self.timeline.set_last_seen(identity, self.timeline.stamp())
        if self.shared_ctx.is_shared_for(identity):
            self.shared_ctx.append(self.bot_name, identity, reply)
        else:
            self.history[identity].append({"role": "assistant", "content": reply})
        if self.send_text(user_id, reply) and not pm.is_management_only:
            # 一轮完整对话 → 记忆后端（TencentDB-Agent-Memory L0 同步，失败静默）
            try:
                self.plugins.on_turn(user_id, pm.rebuilt, reply)
            except Exception:
                pass

    # 状态栏字段同义词：卡片/模型写法不同（衣着 vs 服装、神情 vs 表情…），
    # 命中任一即视为该字段存在，避免"卡片写衣着、代码要服装"导致的每轮误判重写
    _SB_ALIASES = {
        "时间": ("时间", "当下时间", "剧情时间"),
        "地点": ("地点", "场景", "位置"),
        "表情": ("表情", "神情", "神态", "面部"),
        "姿态": ("姿态", "姿势", "动作", "体态"),
        "服装": ("服装", "衣着", "穿着", "衣物", "服饰"),
        "身体反应": ("身体反应", "生理反应", "身体状态", "身体"),
        "内心": ("内心", "心理", "心声", "想法"),
    }

    @classmethod
    def _sb_aliases(cls, field):
        return cls._SB_ALIASES.get(str(field), (str(field),))

    def _status_bar_gaps(self, reply):
        """状态栏合格性检查：返回不合格原因（[] = 合格）。

        - 没有 `<状态栏>…</状态栏>` 区块 → ["<状态栏> 区块"]
        - 区块内缺必填字段（status_bar.required，仅 enable=true 时检查）→ 缺失字段名
        只看最后一个区块：状态栏必须在回复最末尾（与 _normalize_status_bar 同口径）；
        字段按同义词判定（_SB_ALIASES）。
        """
        blocks = self._STATUS_BAR_RE.findall(reply or "")
        if not blocks:
            return ["<状态栏> 区块"]
        if not self._sb("enable"):
            return []
        body = blocks[-1]
        missing = []
        for f in self._sb_fields("required"):
            if not any(a in body for a in self._sb_aliases(f)):
                missing.append(f)
        return missing

    @classmethod
    def _apply_status_bar(cls, reply, block):
        """把补出来的状态栏块写回回复：已有区块 → 替换最后一个；没有 → 追加到末尾。"""
        if not block:
            return reply
        if "<状态栏>" in (reply or ""):
            idx = reply.rfind("<状态栏>")
            end = reply.find("</状态栏>", idx)
            if end != -1:
                return (reply[:idx] + block.strip()).rstrip()
        return ((reply or "").rstrip() + "\n" + block.strip()).strip()

    def _repair_status_bar(self, messages, reply, gaps=None):
        """状态栏不合格时修复：先廉价补块（只重写/补充状态栏，正文不动），
        失败再退回完整上下文重生成。gaps 为 _status_bar_gaps 给出的缺失清单。"""
        gaps = list(gaps or [])
        self._meta["status_bar_repairs"] = self._meta.get("status_bar_repairs", 0) + 1
        cheap = self._cheap_status_bar(reply, gaps)
        if cheap:
            self._meta["cheap_repairs"] = self._meta.get("cheap_repairs", 0) + 1
            return self._apply_status_bar(reply, cheap)
        self._meta["full_repairs"] = self._meta.get("full_repairs", 0) + 1
        lacks = "、".join(g for g in gaps if g != "<状态栏> 区块")
        detail = ("（缺少字段：%s）" % lacks) if lacks else ""
        try:
            repair = messages + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": (
                    "（你刚才的回复状态栏不合格%s。请按角色卡要求补上/修正 "
                    "<状态栏>…</状态栏>，逐项包含：%s；必填项 %s 一个都不能少。"
                    "重新输出完整回复，正文不变；状态栏必须放在回复的【最末尾】，"
                    "其后不得再有任何内容。）"
                    % (detail, "、".join(self._sb_fields("fields")),
                       "、".join(self._sb_fields("required")) or "无")
                )},
            ]
            fixed = self.llm.chat(repair, max_tokens=self.max_reply_tokens)
            if fixed and "<状态栏>" in fixed:
                return fixed.strip()
        except Exception:
            pass
        return None

    def _cheap_status_bar(self, reply, gaps=None):
        """廉价补状态栏：只把「回复正文」（+ 已有状态栏）喂给后处理模型，
        要求仅输出合格的状态栏块——已有字段内容保留，缺失字段补齐。"""
        if not reply:
            return None
        gaps = [g for g in (gaps or []) if g != "<状态栏> 区块"]
        existing = ""
        blocks = self._STATUS_BAR_RE.findall(reply)
        if blocks:
            existing = blocks[-1]
        rules = [
            "你是回复后处理助手。为下面的角色回复补齐/修正末尾的 <状态栏>…</状态栏> 区块。",
            "- 必须逐项包含字段：%s。" % "、".join(self._sb_fields("fields")),
        ]
        if self._sb_fields("required"):
            rules.append("- 必填项 %s 一个都不能少。" % "、".join(self._sb_fields("required")))
        if gaps:
            rules.append("- 这次特别缺：%s，务必补上。" % "、".join(gaps))
        if existing:
            rules.append("- 这是原文里已有的状态栏，请保留其中已有的信息（尤其时间、地点），"
                         "只补缺、只修正：\n" + existing)
        rules.append("- 字段值写当下的具体状态（服装写到每件衣服及状态、身体反应写到生理反应）；"
                     "只输出状态栏块本身，不要输出任何其他文字、不要重述正文、不要解释；"
                     "状态栏块之后不得再有任何内容。")
        try:
            bar = self.llm.chat(
                [
                    {"role": "system", "content": "\n".join(rules)},
                    {"role": "user", "content": reply},
                ],
                temperature=0.2,
                max_tokens=int(self._sb("cheap_max_tokens") or 400),
                call_type="repair",
            )
            if bar and "<状态栏>" in bar and "</状态栏>" in bar:
                start = bar.find("<状态栏>")
                end = bar.find("</状态栏>", start) + len("</状态栏>")
                return bar[start:end].strip()
        except Exception:
            pass
        return None

    def _normalize_status_bar(self, reply):
        """状态栏收尾归一化：把回复截断到最后一个状态栏区块结束为止，
        去掉状态栏之后的所有内容（状态栏必须位于回复最末尾）。"""
        if not reply or "<状态栏>" not in reply:
            return reply
        idx = reply.rfind("<状态栏>")
        end = reply.find("</状态栏>", idx)
        if end == -1:
            return reply
        return reply[:end + len("</状态栏>")].rstrip()

    def _limit_context(self, messages, identity):
        """单次 LLM 调用的上下文长度上限（字符数，max_context_chars 配置）。

        超限时从最旧的消息开始丢弃，保留 system 提示与当前用户消息；
        返回裁剪后的消息列表。cap <= 0 表示不限制。
        """
        cap = int(self.config.get("max_context_chars", 0) or 0)
        if cap <= 0 or not messages:
            return messages

        def _size(msgs):
            return sum(len(str(m.get("content") or "")) for m in msgs)

        total = _size(messages)
        if total <= cap:
            return messages
        system = messages[:1]
        tail = messages[-1:]
        used = _size(system) + _size(tail)      # 增量累加，避免循环内反复全量求和（O(n²)）
        selected = list(tail)
        # 从最新的历史回合往前装，直到放不下为止
        for m in reversed(messages[1:-1]):
            size = len(str(m.get("content") or ""))
            if used + size > cap:
                break
            selected.append(m)
            used += size
        selected.reverse()
        trimmed = system + selected
        trimmed = self._ensure_user_first(trimmed, original=messages)
        if len(trimmed) != len(messages):
            self.log("[核心] 上下文超限，裁剪历史：%d 条 → %d 条（%d → %d 字符）" % (
                len(messages), len(trimmed), total, _size(trimmed)))
        return trimmed

    @staticmethod
    def _ensure_user_first(messages, original=None):
        """裁剪后必须仍以 user 开头（Anthropic 等严格厂家：首条非 user 直接 400）。

        旧实现从最新往前拼，可能只留下 assistant 回合 → 该会话每次都失败。
        若裁剪把 user 全丢了，从裁剪前的列表里补回最近的一条 user
        （宁可略超预算，也不能发出"没有 user 消息"的请求）。
        """
        if not messages:
            return messages
        out = list(messages)
        while len(out) > 1 and str(out[1].get("role") or "") != "user":
            out.pop(1)
        if len(out) > 1:
            return out
        for m in reversed(original or messages):
            if str(m.get("role") or "") == "user":
                return [messages[0], m]
        return out

    def _trim_history(self, messages):
        """历史消息瘦身：除系统提示、当前用户消息和最近若干条外，
        旧消息只裁掉 <状态栏>…</状态栏> 重复模板块（保留对话正文，不硬切 500 字）。
        max_history_chars>0 时再作为绝对长度兜底截断（默认 0 = 不截断）。"""
        keep = max(2, int(self.config.get("keep_recent_full", 6)))
        limit = int(self.config.get("max_history_chars", 0) or 0)
        if not messages:
            return messages
        # 需要截断的区间：系统提示之后、最近 keep 条（当前用户消息之前）之前
        end = max(1, len(messages) - 1 - keep)
        out = list(messages)
        for i in range(1, end):
            content = str(out[i].get("content") or "")
            stripped = self._STATUS_BAR_RE.sub("", content)
            stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
            if limit > 0 and len(stripped) > limit:
                stripped = stripped[:limit]
            if stripped != content:
                # 避免把整条消息裁空
                out[i] = dict(out[i], content=stripped or content)
        return out

    def _summary_limit(self):
        return max(200, int(self.config.get("summary_max_chars", 2000)))

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                user_id, text = self.inbox.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._handle(user_id, text)
            except Exception as exc:
                try:
                    self.send_text(user_id, "出错了：%s" % exc, management=True)
                except Exception:
                    pass

    def _ticker(self):
        while not self.stop_event.is_set():
            try:
                self.plugins.on_tick()
            except Exception as exc:
                self.log("[ticker] %s" % exc)
            self.stop_event.wait(10)

    # ---------- 主循环 ----------
    def _try_send_first_mes(self):
        """当前人格角色卡有 first_mes 且本数据目录从未发送过时，向主人发送开场白。

        只在该人格的数据目录首次激活时发送一次；指令切换/管理台切换都会重启进程，
        因此同样在首次启用时生效。卡片没有 first_mes 则等待用户先发消息。
        """
        try:
            card_path = self.config.get("character_card") or ""
            if not card_path or not os.path.isfile(card_path):
                return False
            card = sillytavern.load_character_card(card_path)
            first = str((card or {}).get("first_mes") or "").strip()
            if not first:
                return False
            cur = persona.current()
            name = (cur or {}).get("name") or self.config.get("bot_name") or "默认"
            data_dir = persona.ensure_persona_data(name)
            marker = os.path.join(data_dir, "first_mes_sent")
            if os.path.isfile(marker):
                return False
            if self._persona_has_chatlog(data_dir):
                # 该人格已有聊天记录（可能是旧版无标记时积累的）→ 不再发送开场白
                return False
            owners = sorted(self.owner_users)
            if not owners:
                return False
            filled = sillytavern.fill_placeholders(
                first, self.identity.owner_name, (card or {}).get("name") or "")
            ok = self.send_text(owners[0], filled)
            if ok:
                self._write_first_mes_marker(marker)
            return ok
        except Exception as exc:
            self.log("[开场白] 发送失败：%s" % exc)
            return False

    @staticmethod
    def _persona_has_chatlog(data_dir):
        d = os.path.join(data_dir, "chatlog")
        if not os.path.isdir(d):
            return False
        for root, _dirs, files in os.walk(d):
            if any(f.lower().endswith((".md", ".json", ".txt")) for f in files):
                return True
        return False

    def _write_first_mes_marker(self, path):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"sent_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
            os.replace(tmp, path)
        except Exception:
            pass

    def _maybe_send_first_mes(self):
        """后台等待客户端连上后尝试发送开场白（最多 120 秒）。"""
        def _run():
            deadline = time.time() + 120
            while time.time() < deadline and not self.stop_event.is_set():
                if self._try_send_first_mes():
                    return
                time.sleep(5)
        threading.Thread(target=_run, daemon=True).start()

    def _chat_with_retry(self, messages):
        """带重试的 LLM 回复调用：异常与空输出（推理型模型常见）都会重试。

        次数 llm_retries（默认 1 次重试）；推理型模型会把 token 花在推理上，
        max_tokens 按 llm_reasoning_headroom（默认 2000）额外留余量。
        每次失败写入运行日志便于排查；全部异常后抛出最后一次异常，
        全部空输出则返回空串。
        """
        attempts = 1 + max(0, int(self.config.get("llm_retries", 1)))
        headroom = max(0, int(self.config.get("llm_reasoning_headroom", 2000)))
        max_tokens = self.max_reply_tokens + headroom
        last_exc = None
        for i in range(attempts):
            text = ""
            try:
                text = self.llm.chat(messages, max_tokens=max_tokens)
            except Exception as exc:
                last_exc = exc
                self.log("[核心] LLM 调用失败（第%d/%d 次）：%s"
                         % (i + 1, attempts, exc))
            if text and text.strip():
                return text
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
        if last_exc is not None:
            raise last_exc
        return ""

    def run(self, test_connect=False):
        if self.persona_path:
            self.log("人格预设: %s" % self.persona_path)
        else:
            self.log("人格预设: config.system_prompt")
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._ticker, daemon=True).start()
        self._maybe_send_first_mes()
        if test_connect:
            if self.platform == "discord":
                ok, err = self.client.test_connect()
                if ok:
                    print("Discord 连接测试成功")
                else:
                    print("Discord 连接测试失败: %s" % err)
                return
            import websocket

            headers = (
                {"Authorization": "Bearer " + self.config.get("onebot_token", "")}
                if self.config.get("onebot_token")
                else None
            )
            ws = websocket.create_connection(
                self.config.get("onebot_ws_url", "ws://127.0.0.1:3001"),
                timeout=10,
                header=headers,
            )
            print("测试连接成功:", ws.connected)
            ws.close()
            return
        try:
            self.client.connect_loop(self.stop_event)
        finally:
            self.plugins.shutdown()
