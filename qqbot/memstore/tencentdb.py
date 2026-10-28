# -*- coding: utf-8 -*-
"""TencentDB-Agent-Memory 适配器（MemoryCore 网关客户端）。

腾讯云 Agent Memory（TencentDB-Agent-Memory，仓库 TencentCloud/TencentDB-Agent-Memory）：
- 自托管 standalone：MemoryCore 网关 127.0.0.1:8420，SQLite + 本地文件，BM25 默认
  （详见 docs/tencentdb-agent-memory-research.md）
- 托管云：https://memory.tdai.tencentyun.com + 实例 API Key + service_id

本适配器双传输：
1. 官方 Python SDK（tencentdb_agent_memory.v3，支持 user/session 隔离）
2. HTTP 兼容端点（POST /capture 写、/recall、/search/memories 查）——SDK 未安装时使用

失败语义与 MemoryStore 契约一致：任何远端异常 → 写返回 False、查返回 []，
绝不抛异常中断聊天管线。
"""
import hashlib
import json
import os
import threading
import time

import requests

from qqbot.memstore.base import MemoryStore

# HTTP 传输的兼容端点（MemoryCore gateway types，见研究报告 S28）
CAPTURE_PATH = "/capture"
RECALL_PATH = "/recall"
SEARCH_PATH = "/search/memories"
HEALTH_PATH = "/health"


def _safe_id(name):
    """identity → 远端 user/session id：保留原文（JSON body 传输，无 URL 转义问题），
    仅去掉控制字符。"""
    return "".join(ch for ch in str(name or "") if ch >= " ") or "anonymous"


class _HttpTransport:
    """直接打 MemoryCore 网关的 HTTP 兼容端点（SDK 不可用时的兜底传输）。"""

    def __init__(self, cfg, log):
        self.endpoint = str(cfg.get("endpoint") or "http://127.0.0.1:8420").rstrip("/")
        self.api_key = str(cfg.get("api_key") or "")
        self.service_id = str(cfg.get("service_id") or "default")
        self.timeout = float(cfg.get("timeout_seconds", 10) or 10)
        # 回忆路径不能等 10 秒：远端慢/半挂时用短超时快速回退本地
        self.recall_timeout = min(
            self.timeout, float(cfg.get("recall_timeout_seconds", 2.5) or 2.5))
        self.log = log

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = "Bearer " + self.api_key
            h["x-tdai-service-id"] = self.service_id
        return h

    def _post(self, path, payload, timeout=None):
        resp = requests.post(self.endpoint + path, headers=self._headers(),
                             json=payload, timeout=timeout or self.timeout)
        resp.raise_for_status()
        return resp.json()

    def health(self):
        try:
            resp = requests.get(self.endpoint + HEALTH_PATH, timeout=min(3.0, self.timeout))
            return resp.status_code == 200
        except Exception:
            return False

    def append_conversation(self, user_id, session_key, messages):
        self._post(CAPTURE_PATH, {
            "messages": messages,
            "session_key": session_key,
            "user_id": user_id,
        })
        return True

    def recall(self, query, user_id, session_key, top):
        data = None
        try:
            data = self._post(SEARCH_PATH, {
                "query": query,
                "session_key": session_key,
                "user_id": user_id,
                "limit": top,
            }, timeout=self.recall_timeout)
        except Exception:
            data = self._post(RECALL_PATH, {
                "query": query,
                "session_key": session_key,
                "user_id": user_id,
            }, timeout=self.recall_timeout)
        return self._extract_hits(data, top)

    @staticmethod
    def _extract_hits(data, top):
        """兼容多种返回形态：list / {items:[{content|text|memory}]} /
        {memories:[...]} / {context: str} / {results:[...]}。"""
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = None
            for key in ("items", "memories", "results", "records", "data"):
                v = data.get(key)
                if isinstance(v, list):
                    rows = v
                    break
            if rows is None and data.get("context"):
                rows = [{"content": data.get("context")}]
            if rows is None:
                return []
        else:
            return []
        out = []
        for r in rows[: max(1, top)]:
            if isinstance(r, str):
                out.append((r, None))
            elif isinstance(r, dict):
                score = None
                for skey in ("score", "similarity", "relevance", "distance"):
                    sv = r.get(skey)
                    if isinstance(sv, (int, float)):
                        score = float(sv)
                        break
                for key in ("content", "text", "memory", "value"):
                    v = r.get(key)
                    if isinstance(v, str) and v.strip():
                        out.append((v.strip(), score))
                        break
        return out

    def close(self):
        pass


class _SdkTransport:
    """官方 v3 SDK 传输；不支持隔离的旧 SDK 交给 HTTP 传输兜底。"""

    def __init__(self, cfg, log):
        try:
            from tencentdb_agent_memory.v3 import MemoryClient as V3Client
            self._v3 = V3Client
        except Exception:
            self._v3 = None
        if self._v3 is None:
            raise ImportError("需要支持 user/session 隔离的 v3 SDK；回退 HTTP 传输")
        self.cfg = cfg
        self.log = log
        self._clients = {}          # user_id -> client
        self._lock = threading.Lock()

    def _client(self, user_id):
        with self._lock:
            c = self._clients.get(user_id)
            if c is not None:
                return c
            common = dict(
                endpoint=str(self.cfg.get("endpoint") or "http://127.0.0.1:8420").rstrip("/"),
                api_key=str(self.cfg.get("api_key") or ""),
                service_id=str(self.cfg.get("service_id") or "default"),
            )
            c = self._v3(
                team_id=str(self.cfg.get("team_id") or "deepdiscord"),
                agent_id=str(self.cfg.get("agent_id") or "luotavern"),
                user_id=user_id,
                session_id=str(self.cfg.get("session_prefix") or "rp") + ":" + user_id,
                **common)
            self._clients[user_id] = c
            return c

    def health(self):
        try:
            resp = requests.get(
                str(self.cfg.get("endpoint") or "http://127.0.0.1:8420").rstrip("/") + HEALTH_PATH,
                timeout=3.0)
            return resp.status_code == 200
        except Exception:
            return False

    def append_conversation(self, user_id, session_key, messages):
        client = self._client(user_id)
        client.add_conversation(messages=messages)
        return True

    def recall(self, query, user_id, session_key, top):
        client = self._client(user_id)
        try:
            data = client.search_atomic(query=query, limit=top)
        except TypeError:
            data = client.search_atomic(query, limit=top)
        if isinstance(data, dict):
            items = data.get("items") or data.get("memories") or data.get("results")
        else:
            items = data
        out = []
        if isinstance(items, list):
            for r in items[: max(1, top)]:
                if isinstance(r, str):
                    out.append((r, None))
                elif isinstance(r, dict):
                    score = None
                    for skey in ("score", "similarity", "relevance", "distance"):
                        sv = r.get(skey)
                        if isinstance(sv, (int, float)):
                            score = float(sv)
                            break
                    for key in ("content", "text", "memory", "value"):
                        v = r.get(key)
                        if isinstance(v, str) and v.strip():
                            out.append((v.strip(), score))
                            break
        return out

    def close(self):
        with self._lock:
            for c in self._clients.values():
                try:
                    c.close()
                except Exception:
                    pass
            self._clients.clear()


class TencentAgentMemoryStore(MemoryStore):
    """只做「语义写入 + 语义召回」的远端记忆引擎。

    本地（SQLite）仍是元数据/好感度的 source of truth；本适配器不落好感度、
    不做替换/删除（远端是蒸馏日志，append-only），相关方法安全返回空/False。
    """

    backend = "tencentdb"

    def __init__(self, config, log=None, probe_only=False):
        cfg = dict(config or {})
        self.cfg = cfg
        self.tcfg = dict(cfg.get("tencentdb") or {})
        from qqbot.config import root_path
        db_path = cfg.get("db_path") or "data/memory.db"
        if not os.path.isabs(db_path):
            db_path = os.path.join(root_path(), db_path)
        scope = [os.path.normcase(os.path.realpath(db_path)),
                 self.tcfg.get("team_id") or "deepdiscord",
                 self.tcfg.get("agent_id") or "luotavern"]
        self._namespace = hashlib.sha256(json.dumps(scope).encode("utf-8")).hexdigest()[:24]
        self.revision_for = lambda identity: 0
        self.log = log or (lambda msg: None)
        self._transport = None
        self._transport_error = None
        self._health_cache = None  # (ts, ok)，首次探测强制走网络
        # 写入熔断：网关"端口在但无响应"时，每轮对话都要同步远端（超时 10s），
        # 单工作线程会被拖死。连续失败后冷却一段时间直接跳过写入。
        self._write_fail_count = 0
        self._write_block_until = 0.0
        if probe_only:
            self._transport = _HttpTransport(self.tcfg, self.log)
            return
        for factory in (_SdkTransport, _HttpTransport):
            try:
                self._transport = factory(self.tcfg, self.log)
                break
            except Exception as exc:
                self._transport_error = exc
        if self._transport is None:
            self.log("[memory.tencentdb] SDK 与 HTTP 传输均不可用: %s"
                     % self._transport_error)

    # ---------- 传输健康 ----------
    def available(self):
        return self._transport is not None

    def probe(self, timeout=3.0):
        """健康探测带 30 秒缓存：管理台每几秒轮询 stats 不会每次都打网络。"""
        if self._transport is None:
            return False
        import time as _time
        now = _time.time()
        if self._health_cache is not None and now - self._health_cache[0] < 30:
            return self._health_cache[1]
        try:
            ok = bool(self._transport.health())
        except Exception:
            ok = False
        self._health_cache = (now, ok)
        return ok

    # ---------- 身份/会话 ----------
    def _remote_scope(self, identity):
        """user/session 都隔离，兼容仅按 user 过滤的 HTTP/SDK 实现。"""
        revision = self.revision_for(identity)
        user = "%s:%s:v%d" % (self._namespace, _safe_id(identity), revision)
        prefix = str(self.tcfg.get("session_prefix") or "rp")
        return user, "%s:%s" % (prefix, user)

    def _session_key(self, identity):
        return self._remote_scope(identity)[1]

    # ---------- 写入（append-only，best-effort） ----------
    # ---------- 写入熔断 ----------
    def _write_allowed(self):
        return time.time() >= self._write_block_until

    def _note_write(self, ok):
        if ok:
            self._write_fail_count = 0
            self._write_block_until = 0.0
            return
        self._write_fail_count += 1
        if self._write_fail_count >= 2:
            cooldown = min(300.0, 30.0 * (2 ** (self._write_fail_count - 2)))
            self._write_block_until = time.time() + cooldown
            self.log("[memory.tencentdb] 写入连续失败 %d 次，%.0f 秒内跳过远端同步"
                     % (self._write_fail_count, cooldown))

    def _append_memory(self, identity, kind, content):
        if self._transport is None:
            return False
        if not self._write_allowed():
            return False
        try:
            user_id, session_key = self._remote_scope(identity)
            ok = bool(self._transport.append_conversation(
                user_id, session_key,
                [
                    {"role": "user", "content": "【%s】%s" % (kind, content)},
                    {"role": "assistant", "content": "记住了。"},
                ]))
            self._note_write(ok)
            return ok
        except Exception as exc:
            self._note_write(False)
            self.log("[memory.tencentdb] 写入失败(%s): %s" % (identity, exc))
            return False

    def save_longterm(self, identity, content, keywords=None):
        content = str(content).strip()[:300]
        if not content or not identity:
            return False
        kw = self._dump_keywords(keywords)
        text = content + ("（触发词：%s）" % "、".join(self._load_keywords(kw)) if kw else "")
        return self._append_memory(identity, "长期记忆", text)

    def delete_longterm(self, identity, keyword):
        # 远端是 append-only 蒸馏日志：本地库才是可删除的 source of truth
        return False

    def list_longterm(self, identity, limit=500):
        return []

    def recall_longterm(self, identity, query, top=3):
        return self.recall_texts(identity, query, top)

    def save_important(self, identity, content, affect_delta=0, importance=5,
                       story_time=None, keywords=None, track="story"):
        content = str(content).strip()[:300]
        if not content or not identity:
            return False
        try:
            delta = max(-100, min(100, int(affect_delta)))
            imp = max(0, min(10, int(importance)))
        except (TypeError, ValueError):
            delta, imp = 0, 5
        kw = self._dump_keywords(keywords)
        text = "%s（好感度%s%d，重要度%d，轨道%s%s）" % (
            content, "+" if delta > 0 else "", delta, imp, track or "story",
            "，触发词：" + "、".join(self._load_keywords(kw)) if kw else "")
        return self._append_memory(identity, "重要记忆", text)

    # ---------- 元数据类读取：远端不承载 → 空（由 hybrid 本地层兜底） ----------
    def list_important(self, identity, limit=500):
        return []

    def list_important_ids(self, identity, limit=500, track=None):
        return []

    def list_important_by_track(self, identity, track, limit=10):
        return []

    def list_important_story(self, identity, prefix, limit=100):
        return []

    def list_important_by_time(self, identity, time_windows, limit=500):
        return []

    def replace_important(self, identity, items, scope_ids=None):
        return 0

    def track_counts(self, identity):
        return {"story": 0, "world": 0}

    def get_affection(self, identity):
        return None

    def set_affection(self, identity, score):
        return

    # ---------- 语义召回 ----------
    def recall_texts(self, identity, query, top=5):
        """远端语义召回正文：返回 [content]；任何失败返回 []。"""
        return [t for t, _s in self.recall_scored(identity, query, top)]

    @staticmethod
    def _normalize_scores(hits):
        """把网关分数归一到 0~1。

        网关可能返回余弦（0~1）、BM25/距离（>1 或越小越好）或干脆没有分数：
        - 有分数且都 ≤1 → 直接用（负值截到 0）
        - 有分数但 >1 → 按本批最大值归一
        - 没有分数 → 按名次衰减（1.0 / 0.85 / 0.7…，下限 0.35）
        旧实现不管拿到什么都硬编码 0.5，导致 min_score/trigger_min_score 形同虚设。
        """
        out = []
        known = [s for _t, s in hits if isinstance(s, (int, float))]
        scale = max(known) if known and max(known) > 1.0 else 1.0
        for rank, (text, score) in enumerate(hits):
            if isinstance(score, (int, float)) and scale > 0:
                value = max(0.0, min(1.0, float(score) / scale))
            else:
                value = max(0.35, 1.0 - 0.15 * rank)
            out.append((text, value))
        return out

    def recall_scored(self, identity, query, top=5):
        """远端语义召回（带分数）：[(content, score 0~1)]；失败返回 []。"""
        if self._transport is None or not identity or not query:
            return []
        if not self.tcfg.get("enable_recall", True):
            return []
        try:
            user_id, session_key = self._remote_scope(identity)
            hits = self._transport.recall(
                query, user_id, session_key, int(top))
            # 查询期间管理台可能编辑/删除：不要把已失效的请求结果带回聊天。
            if (user_id, session_key) != self._remote_scope(identity):
                return []
        except Exception as exc:
            self.log("[memory.tencentdb] 召回失败(%s): %s" % (identity, exc))
            return []
        cleaned = [(str(t).strip()[:300], s) for t, s in hits if str(t).strip()]
        return self._normalize_scores(cleaned)

    def recall_important(self, identity, query, top=3):
        return [(c, 0) for c in self.recall_texts(identity, query, top)]

    def keyword_hits(self, identity, text):
        return []

    def has_keyword_hit(self, identity, text):
        return False

    def has_memory_trigger(self, identity, text):
        return False

    def content_overlap(self, identity, text):
        return 0

    def vector_recall(self, identity, query, top=3, min_score=0.3):
        """远端语义召回：用网关返回的真实分数（无分数则按名次衰减），
        低于 min_score 的条目丢弃——旧实现命中即硬编码 0.5，阈值完全失效。"""
        hits = self.recall_scored(identity, query, top)
        floor = float(min_score if min_score is not None else 0.3)
        return [("memory", c, 0, 5, s) for c, s in hits if s >= floor]

    def vector_top_score(self, identity, text):
        if not self.tcfg.get("enable_recall", True):
            return 0.0
        hits = self.recall_scored(identity, text, 1)
        return hits[0][1] if hits else 0.0

    def recall_consistent(self, identity, query, top=2):
        return []

    def list_persistent(self, identity, min_importance=9, limit=3):
        return []

    def sync_conversation(self, identity, user_text, bot_text):
        """把一轮真实对话写入远端 L0（MemoryCore 自动蒸馏 L1→L2→L3）。"""
        if self._transport is None or not self.tcfg.get("sync_chatlog", True):
            return False
        if not identity or not user_text or not bot_text:
            return False
        if not self._write_allowed():
            return False          # 熔断中：不阻塞聊天管线
        try:
            user_id, session_key = self._remote_scope(identity)
            ok = bool(self._transport.append_conversation(
                user_id, session_key,
                [
                    {"role": "user", "content": str(user_text)[:2000]},
                    {"role": "assistant", "content": str(bot_text)[:2000]},
                ]))
            self._note_write(ok)
            return ok
        except Exception as exc:
            self._note_write(False)
            self.log("[memory.tencentdb] 对话同步失败(%s): %s" % (identity, exc))
            return False

    # ---------- 生命周期 ----------
    def close(self):
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass

    def stats(self):
        return {
            "backend": self.backend,
            "endpoint": self.tcfg.get("endpoint", ""),
            "available": self.available(),
            "healthy": self.probe(timeout=1.0) if self.available() else False,
            "sync_chatlog": bool(self.tcfg.get("sync_chatlog", True)),
        }
