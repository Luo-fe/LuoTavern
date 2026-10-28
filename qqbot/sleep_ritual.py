# -*- coding: utf-8 -*-
"""睡前仪式：消息含"好好睡觉吧"触发（说出者身份，处理其未封存记录）。

① 分块提炼重要记忆（LLM，跳过已纠正条目），每条带 affect_delta
② 更新会话摘要（复用压缩提示词）
③ 封存本次已提炼记录（只挡重复总结，回忆仍全文可搜）
④ 世界时钟推进到次日早上
"""
import re

from qqbot import textutil
from qqbot import worldlog
from qqbot.chatlog_store import entry_id

IMPORTANT_EXTRACT_PROMPT = """你是记忆提炼助手。从聊天记录中提炼对角色故事或情绪发展重要的事件（重要谈话、约定、事件转折、强烈情绪等），忽略日常琐事。
聊天记录每条自带剧情时间戳（形如"第1周周二晚上"）。事件发生时间必须取该条记录自己的时间戳，不得使用提炼时刻。
只输出 JSON 数组，不要其他内容：
[{"content": "一句话概括事件（含人物、事件、结果）", "affect_delta": 整数, "importance": 整数, "keywords": ["触发词1", "触发词2", ...]}]
- content 必须以记录中出现的剧情时间开头（如"第1周周二晚上"），禁止编造记录里没有的周数/星期
- affect_delta 表示该事件让角色对这个人的好感度变化：正面+5~+30，重大伤害-80~-100，普通-5~+5，范围 -100~+100
- importance 表示该记忆对关系的重要性：1~10（10=关系转折/重大承诺，5=普通重要事件，1=可有可无）
- keywords：5-10 个触发词——以后角色只要听到这些词就该想起这条记忆。写具体名词/动作/物品/地点/事件名/称呼/时间点（如"欢乐谷""过山车""门票""摩天轮""周末约定"），**必须包含同义词与不同说法**（如"视频/录像/拍摄"、"欢乐谷/游乐园/乐园"），确保口语化复述也能命中；不要写情绪词或空泛词
- 没有值得提炼的事件时输出 []"""

COMPRESS_FALLBACK = """你是记忆档案员。把【上一次摘要】与新的【聊天记录】合并成一份「剧情档案」摘要（第三人称、客观、禁止评价抒情），按以下结构输出：
【剧情摘要】按时间顺序记录关键事件：谁做了什么、说了什么、发生了什么、进展到哪一步；保留具体约定、物品、地点、称呼与关键台词片段，每条尽量带上剧情时间（如"周五傍晚"）。
【当前状态】最近剧情之后：人物在哪里、正在做什么、有什么未完成的事/待回答的话，并注明当前周几与时段。
要求：优先保留细节而非概括；旧摘要中仍有意义的事实必须并入；总字数 600-1200 字；只输出摘要本身。"""

CONSOLIDATE_PROMPT = """你是记忆整理助手。以下是角色对某人的重要记忆列表，每条带 [id]、好感度变动 affect_delta 与重要度 importance。
把语义重复、描述同一件事的条目合并成一条（保留信息更完整的表述），其余原样保留，输出精简后的列表。
只输出 JSON 数组，不要其他内容，每项两种形态之一：
{"merge": [源id列表], "content": "合并后的一句话"}   合并多条（≥2）为一条
{"id": 源id}                                       原样保留该条目
- merge 的源 id 必须来自输入列表，不能合并后再复用；content 保留信息更完整的表述
- 总条数不得超过 {max_count} 条；不要编造 id 或内容"""

WORLD_EXTRACT_PROMPT = """你是剧情档案员。把下面的"世界书剧情记录"提炼成对后续剧情重要的事件摘要，
忽略重复与琐碎内容，把多条讲同一件事的记录合并成一条。只输出 JSON 数组，不要其他内容：
[{"content": "一句话概括世界剧情事件（含人物、事件、结果）", "importance": 整数}]
- importance：1~10（10=世界重大转折，5=普通事件，1=可有可无）
- 没有值得保留的内容时输出 []"""


class SleepRitual:
    def __init__(self, bot):
        self.bot = bot
        cfg = bot.config.get("sleep", {})
        self.keywords = cfg.get("keywords", ["好好睡觉吧"])
        self.chunk_size = max(10, int(cfg.get("chunk_size", 40)))
        self.max_output_tokens = max(128, int(cfg.get("max_output_tokens", 1000)))
        self.summary_max_tokens = max(512, int(cfg.get("summary_max_tokens", 3000)))
        self.jump_to_night = bool(cfg.get("jump_to_night", True))
        self.compress_prompt = getattr(bot, "compress_prompt", "") or COMPRESS_FALLBACK

    @staticmethod
    def is_trigger(text):
        return "好好睡觉吧" in (text or "")

    # ---------- 主流程 ----------
    def run(self, identity):
        """结束这一天：故事提炼 + 世界书剧情提炼 + 摘要 + 封存 +（可选）时钟推进到深夜。

        返回 (故事条数, 世界剧情条数, 摘要, 是否推进到深夜)。
        sleep.jump_to_night=False 时只提炼不推进。
        """
        extracted, summary = self._extract_and_summarize(identity)
        world_n = self._extract_world(identity)
        advanced = False
        if self.jump_to_night:
            self.bot.timeline.apply({"time_of_day": "深夜"}, source="sleep", identity=identity)
            advanced = True
        return extracted, world_n, summary, advanced

    def run_silent(self, identity):
        """静默提炼：故事+世界剧情，不推进时钟、无打扰。返回 (故事条数, 世界剧情条数, 摘要)。"""
        extracted, summary = self._extract_and_summarize(identity)
        world_n = self._extract_world(identity)
        return extracted, world_n, summary

    def _extract_world(self, identity):
        """把世界书剧情日志提炼成 world 轨道重要记忆；提炼后清空日志。"""
        base = self.bot.config.get("worldlog_dir")
        events = worldlog.read_events(identity, base=base)
        if not events:
            return 0
        # 只清理"本次真正提炼过"的事件：提炼期间 worldbook 插件仍会 append，
        # 旧实现直接 clear_events 会把这段时间新触发的事件一起删掉
        cutoff = max((float(e.get("ts") or 0) for e in events), default=0.0)
        total = 0
        failures = 0
        chunk_size = 20
        for start in range(0, len(events), chunk_size):
            chunk = events[start:start + chunk_size]
            lines = "\n".join(
                "[%s] %s" % ("/".join(e.get("keys") or ["?"]), e.get("content", ""))
                for e in chunk
            )
            data = self.bot.llm.chat_json(
                [
                    {"role": "system", "content": WORLD_EXTRACT_PROMPT},
                    {"role": "user", "content": lines},
                ],
                temperature=0.2,
                call_type="sleep",
                max_tokens=self.max_output_tokens,
            )
            if not isinstance(data, list):
                failures += 1
                continue
            for item in data:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content", "")).strip()
                if not content:
                    continue
                try:
                    importance = int(item.get("importance", 5))
                except (TypeError, ValueError):
                    importance = 5
                self.bot.memory.save_important(
                    identity, content, 0, max(1, min(10, importance)),
                    track="world")
                total += 1
        if failures:
            # 有块失败：只推进到成功部分之前的边界（保留失败块待重试）
            self.bot.log("[sleep] 世界书剧情提炼有 %d 块失败，日志保留待重试" % failures)
            return total
        leftover = worldlog.trim_events(identity, cutoff, base=base)
        if leftover:
            self.bot.log("[sleep] 世界书剧情日志保留 %d 条提炼期间新写入的事件" % leftover)
        return total

    @staticmethod
    def _clean_keywords(raw):
        """LLM 输出的 keywords → 干净列表（兼容字符串/列表，去重限长最多 10 个）。"""
        if isinstance(raw, str):
            raw = re.split(r"[,，、;；\s]+", raw)
        out = []
        for k in raw or []:
            k = str(k).strip().strip("【】[]\"'“”")
            if not k or len(k) > 12 or k in out:
                continue
            out.append(k)
            if len(out) >= 10:
                break
        return out

    def _extract_and_summarize(self, identity):
        """①提炼重要记忆 ②好感度累计 ③摘要 ④封存；失败安全降级。"""
        pending = [e for e in self.bot.seal.unsealed(identity)
                   if not self.bot.correct.is_corrected(identity, e[0], e)]
        if not pending:
            return 0, None

        # ① 分块提炼重要记忆
        extracted = 0
        total_delta = 0
        processed_until = None  # 只有成功处理的块才推进封存点，失败不封存
        processed_ids = []
        for chunk in self._chunks(pending, self.chunk_size):
            text = "\n".join(
                "[%s] %s: %s" % (e[1], e[2], e[3].replace("\n", " ")[:300]) for e in chunk
            )
            clock = self.bot.timeline.current()
            world_time = "%s %s" % (clock.get("weekday", "?"), clock.get("time_of_day", "?"))
            data = None
            for attempt in range(3):
                data = self.bot.llm.chat_json(
                    [
                        {"role": "system", "content": IMPORTANT_EXTRACT_PROMPT.replace(
                            "{world_time}", world_time)},
                        {"role": "user", "content": text},
                    ],
                    temperature=0.2,
                    call_type="sleep",
                    max_tokens=self.max_output_tokens,
                )
                if isinstance(data, list):
                    break
            if not isinstance(data, list):
                # LLM 空/异常：保留这批待提炼，下次触发重试，绝不静默封存
                self.bot.log("[sleep] 提炼失败（LLM 无有效输出），保留 %d 条待重试"
                             % len(chunk))
                break
            processed_until = chunk[-1][0]
            processed_ids = [entry_id(e) for e in chunk]
            for item in data:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content", "")).strip()
                if not content:
                    continue
                if textutil.parse_story_time(content) is None:
                    # 拒绝/非事件文本（无剧情时间）不入库，避免污染重要记忆
                    self.bot.log("[sleep] 跳过无剧情时间戳的记忆条目：%s"
                                 % content[:40])
                    continue
                try:
                    delta = int(item.get("affect_delta", 0))
                except (TypeError, ValueError):
                    delta = 0
                try:
                    importance = int(item.get("importance", 5))
                except (TypeError, ValueError):
                    importance = 5
                delta = max(-100, min(100, delta))
                keywords = self._clean_keywords(item.get("keywords"))
                # story_time 用事件时间（content 自带剧情时间戳），不传提炼时刻——
                # 同块多条事件可能发生在不同时段，统一记提炼时刻会让 Obsidian 时间线错位
                self.bot.memory.save_important(
                    identity, content, delta, importance,
                    story_time=textutil.parse_story_time(content),
                    keywords=keywords,
                )
                extracted += 1
                total_delta += delta

        # ② 好感度累计
        if total_delta:
            self.bot.affection.apply_delta(identity, total_delta)

        # ② 会话摘要（复用压缩提示词，合并旧摘要）
        summary = None
        try:
            lines = [
                "[%s] %s: %s" % (e[1], e[2], e[3].replace("\n", " ")[:300]) for e in pending
            ]
            clock = self.bot.timeline.current()
            prompt = self.compress_prompt.replace(
                "{world_time}", "%s %s" % (clock.get("weekday", "?"), clock.get("time_of_day", "?"))
            ).replace(
                "{previous_summary}", self.bot.summaries.get(identity, "（无）")
            ).replace("{history_lines}", "\n".join(lines))
            summary = self.bot.llm.chat(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "请压缩这段对话。"},
                ],
                call_type="sleep",
                max_tokens=self.summary_max_tokens,
            ).strip()
            if summary:
                self.bot.summaries[identity] = summary[:self.bot._summary_limit()]
                self.bot._save_summary(identity)
        except Exception as exc:
            self.bot.log("[sleep] 摘要失败: %s" % exc)

        # ③ 封存（只推进到成功处理的最后一条；失败块保留待重试）
        # 同时记录稳定 id：聊天记录重排/迁移后序号会错位，id 不会
        if processed_until is not None:
            self.bot.seal.set(identity, processed_until, entry_ids=processed_ids)

        # ③b 记忆合并去重（条数超上限时，LLM 合并相似条目）
        try:
            self.consolidate(identity)
        except Exception as exc:
            self.bot.log("[sleep] 记忆合并失败: %s" % exc)
        return extracted, summary

    def consolidate(self, identity):
        """重要记忆合并去重：超过 max_important 条时 LLM 合并相似条目后整体替换。

        LLM 只输出 merge 源 id 与合并后 content，delta/importance 由程序从源条目
        聚合（delta 取绝对值大者），并同步调整好感度（分数与 Σdelta 重新对齐，
        防止合并导致分数漂移）。返回新条数；未超限或失败返回 None。
        """
        cfg = self.bot.config.get("sleep", {})
        max_count = int(cfg.get("max_important", 100))
        # 只处理 story 轨道且读取全量：截断读取 + 整轨删除会让超限部分静默消失
        items = self.bot.memory.list_important_ids(identity, limit=None, track="story")
        if len(items) <= max_count:
            return None
        src = items[: max_count * 2]
        id_map = {i: (c, d, im, kw) for i, c, d, im, kw in src}
        lines = "\n".join(
            "[%d] %s（delta=%d imp=%d）" % (i, c[:150], d, im) for i, c, d, im, _kw in src
        )
        prompt = CONSOLIDATE_PROMPT.replace("{max_count}", str(max_count))
        data = self.bot.llm.chat_json(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": lines},
            ],
            temperature=0.2,
            call_type="sleep",
            max_tokens=self.max_output_tokens,
        )
        if not isinstance(data, list):
            return None
        new_items = []      # (content, delta, imp, keywords)
        used = set()
        for item in data[:max_count]:
            if not isinstance(item, dict):
                continue
            # 原样保留
            rid = item.get("id")
            if isinstance(rid, int) and rid in id_map and rid not in used:
                c, d, im, kw = id_map[rid]
                new_items.append((c, d, im, kw))
                used.add(rid)
                continue
            # 合并组
            mids = item.get("merge")
            if isinstance(mids, list) and len(mids) >= 2:
                group = [m for m in mids if isinstance(m, int) and m in id_map and m not in used]
                if len(group) >= 2:
                    content = str(item.get("content", "")).strip()
                    if content:
                        gd = [id_map[m][1] for m in group]
                        gi = [id_map[m][2] for m in group]
                        gkw = []
                        for m in group:
                            for k in id_map[m][3]:
                                if k not in gkw:
                                    gkw.append(k)
                        # content 无剧情时间戳时继承源条目时间（保持 Obsidian 时间线同步）
                        if not textutil.parse_story_time(content):
                            src_ts = next(
                                (textutil.parse_story_time(id_map[m][0])
                                 for m in group if textutil.parse_story_time(id_map[m][0])), None)
                            if src_ts:
                                content = "%s%s" % (src_ts, content.lstrip("，,：:"))
                        new_items.append((content, max(gd, key=abs), max(gi), gkw[:5]))
                        used.update(group)
        if not new_items:
            return None
        # 未被 LLM 采用/合并的源条目：原样保留（此前会被整轨删除而静默丢失）
        leftovers = [(c, d, im, kw) for i, c, d, im, kw in src if i not in used]
        # 分数对账：只对本次实际改写的 src 范围做差（尾部未参与的行不受影响，
        # 因为 replace_important 走 scope_ids 只删这些 id）
        old_total = sum(d for _, _, d, _, _ in src)
        new_total = sum(d for _, d, _, _ in new_items) + sum(d for _, d, _, _ in leftovers)
        self.bot.affection.adjust(identity, new_total - old_total)
        replaced = new_items + leftovers
        self.bot.memory.replace_important(
            identity, replaced, scope_ids=[i for i, _c, _d, _im, _kw in src])
        self.bot.log("[sleep] 记忆合并 %s：%d → %d 条，好感度对账 %+d" % (
            identity, len(src), len(replaced), new_total - old_total))
        return len(replaced)

    def auto_seal(self, identity):
        """自动提炼触发：达到轮数阈值且有未封存记录时静默封存。返回是否执行。"""
        if not identity:
            return False
        cfg = self.bot.config.get("sleep", {})
        threshold = int(cfg.get("auto_threshold_turns", 10))
        if threshold <= 0:
            return False
        if self.bot.seal.pending_count(identity) < threshold:
            return False
        extracted, world_n, _ = self.run_silent(identity)
        self.bot.log("[sleep] 自动提炼 %s：故事 %d 条 / 世界剧情 %d 条已沉淀" % (identity, extracted, world_n))
        return True

    @staticmethod
    def _chunks(items, size):
        for i in range(0, len(items), size):
            yield items[i : i + size]
