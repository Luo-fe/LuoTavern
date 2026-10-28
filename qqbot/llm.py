# -*- coding: utf-8 -*-
"""多厂家对话客户端（含 token 用量统计）。

支持：
- OpenAI 兼容风格（DeepSeek / OpenAI / Codex / GLM / 自定义接口）
- Anthropic 原生 Messages API（Claude）
通过 config.llm_provider + llm_providers 选择当前厂家。
"""
import json
import os
import threading
import time

import requests

from . import textutil

PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "openai": "OpenAI / Codex",
    "anthropic": "Claude (Anthropic)",
    "glm": "GLM 智谱",
    "custom": "自定义（OpenAI 兼容）",
}


def build_llm(cfg):
    """按 config.llm_provider 构造当前厂家的客户端。"""
    return ProviderClient(cfg=cfg)


class ProviderClient:
    def __init__(self, cfg=None, api_key=None, base_url=None, model=None,
                 api_style=None, temperature=None, timeout=None):
        cfg = cfg or {}
        provider = str(cfg.get("llm_provider") or "deepseek")
        prov = (cfg.get("llm_providers") or {}).get(provider) or {}
        self.provider = provider
        self.api_style = str(api_style or prov.get("api_style") or "openai").lower()
        # 按厂家隔离取值：只有 deepseek 厂家才允许读 deepseek_* 字段。
        # 旧实现让所有厂家回退到 deepseek_api_key/deepseek_base_url —— 切到别的厂家
        # 而没填该厂家配置时，会拿 DeepSeek 密钥去请求第三方域名（且不报错）。
        if provider == "deepseek":
            own_key = cfg.get("deepseek_api_key") or ""
            own_base = cfg.get("deepseek_base_url") or ""
        else:
            own_key = ""
            own_base = ""
        self.api_key = api_key or prov.get("api_key") or own_key
        self.base_url = (base_url or prov.get("base_url") or own_base).rstrip("/")
        self.model = (model or prov.get("model") or cfg.get("model")
                      or ("deepseek-v4-flash" if provider == "deepseek" else ""))
        self.temperature = (cfg.get("temperature", 0.8)
                            if temperature is None else temperature)
        # 超时必须为正数：None/0 会让 requests 变成"永不超时"，连接挂死时永久阻塞
        try:
            self.timeout = float(timeout or cfg.get("timeout_seconds") or 180)
        except (TypeError, ValueError):
            self.timeout = 180.0
        if self.timeout <= 0:
            self.timeout = 180.0
        # token 用量统计（线程安全，原子写 json）
        base = os.path.dirname(os.path.abspath(__file__))
        rel = cfg.get("usage_log", "data/usage.json")
        self.usage_path = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(base, "..", rel))
        self._usage = self._load_usage()
        self._usage_lock = threading.Lock()

    def _load_usage(self):
        try:
            with open(self.usage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 结构校验：历史/手工改坏的桶（例如 total 是数字）会让记账写入抛异常，
            # 而记账异常会冒到 chat() 之外，把"已成功计费"的调用判成失败
            if isinstance(data, dict):
                total = data.get("total")
                if total is not None and not isinstance(total, dict):
                    data["total"] = {"calls": 0, "prompt_tokens": 0,
                                     "completion_tokens": 0, "cache_hit_tokens": 0}
                for key in ("by_day", "by_type", "recent"):
                    if key in data and not isinstance(data[key], (dict, list)):
                        data.pop(key, None)
                for key, val in list(data.items()):
                    if key in ("total", "by_day", "by_type", "recent"):
                        continue
                    if not isinstance(val, dict):
                        data.pop(key, None)
                return data
        except Exception:
            pass  # 损坏的 usage.json 不应让机器人启动崩溃（下次写入自动重建）
        return {"total": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0}}

    def _save_usage(self):
        with self._usage_lock:
            d = os.path.dirname(self.usage_path)
            try:
                os.makedirs(d, exist_ok=True)
                tmp = self.usage_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._usage, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.usage_path)
            except OSError:
                pass

    def _record_usage(self, model, prompt_tokens, completion_tokens, cache_hit_tokens, call_type="chat"):
        try:
            self._record_usage_inner(model, prompt_tokens, completion_tokens,
                                     cache_hit_tokens, call_type)
        except Exception as exc:
            # 记账永远不能反噬对话：请求已成功且已计费，记账失败只告警
            log = getattr(self, "on_log", None)
            if log is not None:
                try:
                    log("[llm] 用量统计写入失败（已忽略）：%s" % exc)
                except Exception:
                    pass

    def _record_usage_inner(self, model, prompt_tokens, completion_tokens, cache_hit_tokens, call_type="chat"):
        with self._usage_lock:
            import datetime
            day = datetime.date.today().isoformat()
            self._usage.setdefault("total", {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0})
            t = self._usage["total"]
            t["calls"] = int(t.get("calls", 0)) + 1
            t["prompt_tokens"] = int(t.get("prompt_tokens", 0)) + int(prompt_tokens)
            t["completion_tokens"] = int(t.get("completion_tokens", 0)) + int(completion_tokens)
            t["cache_hit_tokens"] = int(t.get("cache_hit_tokens", 0)) + int(cache_hit_tokens)
            # 按天分桶
            by_day = self._usage.setdefault("by_day", {}).setdefault(
                day, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0})
            by_day["calls"] = int(by_day.get("calls", 0)) + 1
            by_day["prompt_tokens"] = int(by_day.get("prompt_tokens", 0)) + int(prompt_tokens)
            by_day["completion_tokens"] = int(by_day.get("completion_tokens", 0)) + int(completion_tokens)
            by_day["cache_hit_tokens"] = int(by_day.get("cache_hit_tokens", 0)) + int(cache_hit_tokens)
            # 最近调用流水（最多 300 条，管理台控制台/Token 页展示）
            recent = self._usage.setdefault("recent", [])
            recent.append({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "day": day,
                "type": call_type,
                "model": model,
                "prompt_tokens": int(prompt_tokens),
                "completion_tokens": int(completion_tokens),
                "cache_hit_tokens": int(cache_hit_tokens),
                "latency_ms": getattr(self, "_last_latency_ms", 0),
            })
            del recent[:-300]
            per = self._usage.setdefault(model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0})
            per["calls"] = int(per.get("calls", 0)) + 1
            per["prompt_tokens"] = int(per.get("prompt_tokens", 0)) + int(prompt_tokens)
            per["completion_tokens"] = int(per.get("completion_tokens", 0)) + int(completion_tokens)
            per["cache_hit_tokens"] = int(per.get("cache_hit_tokens", 0)) + int(cache_hit_tokens)
            # 按调用类型分桶（chat/extract/compress/recall/sleep/proactive），成本归因用
            bt = t.setdefault("by_type", {}).setdefault(call_type, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0})
            bt["calls"] = int(bt.get("calls", 0)) + 1
            bt["prompt_tokens"] = int(bt.get("prompt_tokens", 0)) + int(prompt_tokens)
            bt["completion_tokens"] = int(bt.get("completion_tokens", 0)) + int(completion_tokens)
            bt["cache_hit_tokens"] = int(bt.get("cache_hit_tokens", 0)) + int(cache_hit_tokens)
        self._save_usage()

    def usage_stats(self):
        """返回 {"total": {...}, 各模型: {...}}，total 带估算金额（按 DeepSeek 定价近似）。"""
        with self._usage_lock:
            stats = json.loads(json.dumps(self._usage))
        t = stats.get("total", {})
        # 各厂家每百万 token 定价近似（元）：(输入, 输出)；缓存命中按输入价 1/4 计
        rates = {
            "deepseek": (2.0, 8.0),
            "openai": (10.0, 30.0),
            "anthropic": (21.0, 105.0),
            "glm": (4.0, 4.0),
            "custom": (2.0, 8.0),
        }
        cost = 0.0
        for model, s in stats.items():
            # 只统计 per-model 桶；total/by_day/by_type/recent 不是价格聚合单元
            if model in ("total", "by_day", "by_type", "recent"):
                continue
            if not isinstance(s, dict) or "prompt_tokens" not in s:
                continue
            rate = rates.get(self.provider, rates["custom"])
            # 缓存的输入 token 按 1/4 价格（近似各家缓存折扣）
            cost += (
                int(s.get("prompt_tokens", 0)) * rate[0]
                - int(s.get("cache_hit_tokens", 0)) * rate[0] * 0.75
                + int(s.get("completion_tokens", 0)) * rate[1]
            ) / 1e6
        t["est_cost_yuan"] = round(cost, 4)
        return stats

    def chat(self, messages, temperature=None, max_tokens=None, call_type="chat"):
        started = time.time()
        if self.api_style == "anthropic":
            text, usage = self._chat_anthropic(messages, temperature, max_tokens)
        else:
            text, usage = self._chat_openai(messages, temperature, max_tokens)
        self._last_latency_ms = int((time.time() - started) * 1000)
        self._record_usage(
            self.model,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            usage.get("cache_hit_tokens", 0),
            call_type=call_type,
        )
        return text

    # 可重试状态码：限流与服务端错误；4xx（鉴权/参数/模型名）不重试
    RETRY_STATUS = (429, 500, 502, 503, 504)

    def _log(self, msg):
        try:
            print(msg, flush=True)
        except Exception:
            pass

    @staticmethod
    def _retry_after(resp, fallback):
        raw = (resp.headers or {}).get("Retry-After")
        try:
            return max(0.5, min(30.0, float(raw)))
        except (TypeError, ValueError):
            return fallback

    def _post(self, url, headers, payload):
        """POST + 退避重试（429/5xx），4xx 把响应体写日志后再抛。

        旧实现直接 raise_for_status()：用户只看得到 "401 Client Error: ... for url"，
        既不知道是限流(429)还是模型名写错(400)，也不知道该等多久。
        """
        attempts = 3
        last_exc = None
        for i in range(attempts):
            try:
                resp = requests.post(url, headers=headers, json=payload,
                                     timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                if i >= attempts - 1:
                    raise
                time.sleep(1.5 * (2 ** i))
                continue
            if resp.status_code in self.RETRY_STATUS and i < attempts - 1:
                wait = self._retry_after(resp, 1.5 * (2 ** i))
                self._log("[llm] %s 返回 HTTP %d，%.1fs 后重试（%d/%d）"
                          % (self.provider, resp.status_code, wait, i + 1, attempts))
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                body = (resp.text or "")[:300].replace("\n", " ")
                self._log("[llm] %s 返回 HTTP %d：%s" % (self.provider, resp.status_code, body))
                resp.raise_for_status()
            return resp
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("LLM 请求失败：%s" % url)

    def _require_config(self):
        """调用前校验厂家配置齐备，缺失时给出可执行的错误信息。"""
        if not self.api_key:
            raise ValueError("厂家 %s 未配置 API Key（请在管理台密钥页填写）" % self.provider)
        if not self.base_url:
            raise ValueError("厂家 %s 未配置 API 地址（设置页选择厂家后填写厂家 API 地址）" % self.provider)
        if not self.model:
            raise ValueError("厂家 %s 未配置模型名" % self.provider)

    def _chat_openai(self, messages, temperature, max_tokens):
        self._require_config()
        url = self.base_url + "/chat/completions"
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        resp = self._post(url, headers, payload)
        data = resp.json()
        usage = data.get("usage") or {}
        content = (data.get("choices") or [{}])[0].get("message", {}) or {}
        text = content.get("content")
        return (
            (str(text).strip() if text else ""),
            {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "cache_hit_tokens": usage.get("prompt_cache_hit_tokens", 0),
            },
        )

    def _chat_anthropic(self, messages, temperature, max_tokens):
        self._require_config()
        url = self.base_url + "/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        system = None
        msgs = messages
        if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system":
            system = msgs[0].get("content") or ""
            msgs = msgs[1:]
        # Anthropic 要求首条必须是 user、且不能连续同角色：上游裁剪/压缩后可能
        # 以 assistant 开头（那会让该会话每轮都 400），这里统一归一化
        msgs = self._normalize_anthropic(msgs)
        payload = {
            "model": self.model,
            "max_tokens": max_tokens or 4096,
            "messages": msgs,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if system:
            payload["system"] = system
        resp = self._post(url, headers, payload)
        data = resp.json()
        usage = data.get("usage") or {}
        text = "".join(block.get("text", "")
                       for block in data.get("content") or []
                       if block.get("type") == "text").strip()
        read_cache = usage.get("cache_read_input_tokens", 0)
        # Anthropic 的 input_tokens 已包含 cache 创建/读取 token，不要重复相加
        return text, {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "cache_hit_tokens": read_cache,
        }

    @staticmethod
    def _normalize_anthropic(msgs):
        """丢掉开头的 assistant、合并连续同角色消息（Anthropic Messages 硬要求）。"""
        out = []
        for m in msgs or []:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "user")
            content = m.get("content")
            if role not in ("user", "assistant"):
                role = "user"
            if not out:
                if role != "user":
                    continue  # 首条必须是 user
                out.append({"role": role, "content": content})
                continue
            if out[-1]["role"] == role:
                prev = out[-1]["content"]
                if isinstance(prev, str) and isinstance(content, str):
                    out[-1]["content"] = prev + "\n" + content
                else:
                    out[-1]["content"] = "%s\n%s" % (prev, content)
                continue
            out.append({"role": role, "content": content})
        if not out:
            out = [{"role": "user", "content": "…"}]
        return out

    def chat_json(self, messages, temperature=0.2, call_type="chat", max_tokens=None):
        """调用并稳健解析 JSON 输出；任何失败返回 None（失败原因写日志）。"""
        if isinstance(messages, str):
            messages = [
                {"role": "system", "content": messages},
                {"role": "user", "content": "请根据上面的要求输出 JSON。"},
            ]
        try:
            text = self.chat(messages, temperature=temperature, max_tokens=max_tokens,
                             call_type=call_type)
        except Exception as exc:
            # 静默 return None 会让"记忆/剧情不更新"完全无从排查
            self._log("[llm] %s 调用失败：%s: %s"
                      % (call_type, type(exc).__name__, exc))
            return None
        return textutil.json_extract(text)


# 兼容旧引用：DeepSeekClient 即通用客户端（默认走 deepseek 厂家配置）
DeepSeekClient = ProviderClient
