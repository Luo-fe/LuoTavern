# -*- coding: utf-8 -*-
"""被动推送（方案 C）：被 QQ 踢下线 / 需要扫码时，把二维码推到手机。

支持 ntfy / Telegram / Bark / 通用 webhook。设计约束：

- **默认关闭**：`push.enable=false` 时完全不发请求
- **不阻塞**：推送在后台线程 + 队列里发，绝不卡住 NapCat 日志线程/机器人主循环
- **防刷屏**：同一类事件（kick/qr）有最小间隔（`push.min_interval`）
- **失败静默**：网络问题只记一行日志，不影响主流程
- 图片直传能力因渠道而异：ntfy/Telegram/webhook 直接上传 PNG 字节；
  Bark 只能带 URL，于是退化为发送 QQ 二维码的解码 URL 文本
"""
import base64
import json
import queue
import re
import threading
import time
import urllib.request

TIMEOUT = 12


def _cfg_push(config):
    cfg = (config or {}).get("push")
    return cfg if isinstance(cfg, dict) else {}


def _png_bytes(png_or_data_url):
    """接受 data URL 或裸 PNG 字节，返回裸 PNG 字节。"""
    if not png_or_data_url:
        return None
    if isinstance(png_or_data_url, bytes):
        return png_or_data_url
    text = str(png_or_data_url)
    if text.startswith("data:"):
        try:
            return base64.b64decode(text.split(",", 1)[1])
        except Exception:
            return None
    return None


class Pusher:
    """推送器：`notify(title, text, image=...)` 入队后立即返回。"""

    def __init__(self, config=None, log=None):
        self.config = config or {}
        self.log = log or (lambda msg: None)
        self._q = queue.Queue(maxsize=20)
        self._thread = None
        self._start_lock = threading.Lock()  # 线程退出与 notify 拉起之间的竞态保护
        self._stop = threading.Event()
        self._last = {}                     # kind → 上次推送时间
        self._lock = threading.Lock()
        self.sent = 0
        self.failed = 0

    # ---------- 对外 ----------
    def enabled(self):
        cfg = _cfg_push(self.config)
        if not cfg.get("enable"):
            return False
        return bool(self._endpoint(cfg))

    def notify(self, kind, title, text, image=None):
        """kind: kick | qr | custom（用于最小间隔分组）。"""
        if self._stop.is_set():
            return False
        cfg = _cfg_push(self.config)
        if not cfg.get("enable"):
            return False
        if not self._endpoint(cfg):
            self.log("[push] 未配置推送渠道，跳过")
            return False
        every = int(cfg.get("min_interval") or 120)
        now = time.time()
        with self._lock:
            if now - self._last.get(kind, 0) < every:
                return False
            self._last[kind] = now
        try:
            self._q.put_nowait((title, text, image))
        except queue.Full:
            self.log("[push] 队列已满，丢弃本次推送")
            return False
        self._start()
        return True

    # ---------- 后台发送 ----------
    def _start(self):
        with self._start_lock:
            if self._stop.is_set():
                return
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="push-sender",
                                            daemon=True)
            self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                title, text, image = self._q.get(timeout=0.2)
            except queue.Empty:
                # 空闲即退出；与 notify 的 put 用同一把锁串行，避免"刚退出但新
                # 消息已入队、却没有人再拉起线程"的丢失窗口。
                with self._start_lock:
                    if self._q.empty() and not self._stop.is_set():
                        self._thread = None
                        return
                continue
            try:
                ok = self._send(title, text, image)
                self.sent += 1 if ok else 0
                if not ok:
                    self.failed += 1
            except Exception as exc:
                self.failed += 1
                self.log("[push] 推送失败: %s" % exc)
            finally:
                self._q.task_done()

    def flush(self, timeout=5.0):
        """等待已入队的推送全部发完（测试与优雅退出用）；超时返回 False。"""
        end = time.time() + max(0.0, float(timeout))
        with self._q.all_tasks_done:
            while self._q.unfinished_tasks:
                remaining = end - time.time()
                if remaining <= 0:
                    return False
                self._q.all_tasks_done.wait(remaining)
        return True

    def close(self, timeout=5.0):
        """停止后台线程（幂等）；不再接受新的 notify。"""
        self._stop.set()
        with self._start_lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout)))
            with self._start_lock:
                if not thread.is_alive() and self._thread is thread:
                    self._thread = None

    def _send(self, title, text, image):
        cfg = _cfg_push(self.config)
        provider = str(cfg.get("provider") or "ntfy").lower()
        png = _png_bytes(image)
        if provider == "ntfy":
            return self._ntfy(cfg, title, text, png)
        if provider == "telegram":
            return self._telegram(cfg, title, text, png)
        if provider == "bark":
            return self._bark(cfg, title, text, png)
        if provider == "webhook":
            return self._webhook(cfg, title, text, png)
        self.log("[push] 未知渠道 %s" % provider)
        return False

    # ---------- 各渠道 ----------
    def _endpoint(self, cfg):
        provider = str(cfg.get("provider") or "ntfy").lower()
        if provider == "ntfy":
            n = cfg.get("ntfy") or {}
            return bool(n.get("url") and n.get("topic"))
        if provider == "telegram":
            t = cfg.get("telegram") or {}
            return bool(t.get("token") and t.get("chat_id"))
        if provider == "bark":
            b = cfg.get("bark") or {}
            return bool(b.get("key"))
        if provider == "webhook":
            w = cfg.get("webhook") or {}
            return bool(w.get("url"))
        return False

    def _post(self, url, data=None, headers=None, method="POST"):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status

    def _ntfy(self, cfg, title, text, png):
        n = cfg.get("ntfy") or {}
        url = str(n.get("url") or "https://ntfy.sh").rstrip("/")
        topic = str(n.get("topic") or "").strip()
        headers = {"Title": _ascii_safe(title), "Tags": "warning"}
        if n.get("token"):
            headers["Authorization"] = "Bearer " + str(n["token"])
        if png:
            # ntfy 附件：PUT 二进制 + Filename
            headers["Filename"] = "qrcode.png"
            headers["Message"] = _ascii_safe(text)
            headers["Content-Type"] = "image/png"
            return self._post("%s/%s" % (url, topic), data=png,
                              headers=headers, method="PUT") < 400
        return self._post("%s/%s" % (url, topic),
                          data=text.encode("utf-8"), headers=headers) < 400

    def _telegram(self, cfg, title, text, png):
        t = cfg.get("telegram") or {}
        api = "https://api.telegram.org/bot%s" % str(t.get("token") or "")
        chat = str(t.get("chat_id") or "")
        if png:
            boundary = "----luotavern%d" % int(time.time())
            parts = []
            parts.append(("--%s\r\n" % boundary).encode())
            parts.append(b'Content-Disposition: form-data; name="chat_id"\r\n\r\n')
            parts.append(chat.encode() + b"\r\n")
            if text:
                parts.append(("--%s\r\n" % boundary).encode())
                parts.append(b'Content-Disposition: form-data; name="caption"\r\n\r\n')
                parts.append(text.encode("utf-8") + b"\r\n")
            parts.append(("--%s\r\n" % boundary).encode())
            parts.append(b'Content-Disposition: form-data; name="photo"; '
                         b'filename="qrcode.png"\r\nContent-Type: image/png\r\n\r\n')
            parts.append(png + b"\r\n")
            parts.append(("--%s--\r\n" % boundary).encode())
            body = b"".join(parts)
            return self._post(api + "/sendPhoto", data=body,
                              headers={"Content-Type":
                                       "multipart/form-data; boundary=%s" % boundary}) < 400
        return self._post(api + "/sendMessage",
                          data=json.dumps({"chat_id": chat,
                                           "text": "%s\n%s" % (title, text)}).encode(),
                          headers={"Content-Type": "application/json"}) < 400

    def _bark(self, cfg, title, text, png):
        b = cfg.get("bark") or {}
        base = str(b.get("url") or "https://api.day.app").rstrip("/")
        key = str(b.get("key") or "")
        payload = {"title": title, "body": text, "group": "LuoTavern"}
        # Bark 不能上传图片：二维码给出解码 URL，手机端可自行转成二维码
        if png is None and _QR_URL_RE.search(text or ""):
            payload["url"] = _QR_URL_RE.search(text).group(0)
        from urllib.parse import quote
        return self._post("%s/%s/%s/%s" % (base, key, quote(title), quote(text)),
                          data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"}) < 400

    def _webhook(self, cfg, title, text, png):
        """通用 webhook：JSON 里带 base64 图片，方便对接自建服务。"""
        w = cfg.get("webhook") or {}
        body = {"title": title, "text": text}
        if png:
            body["image_base64"] = base64.b64encode(png).decode("ascii")
            body["image_name"] = "qrcode.png"
        return self._post(str(w.get("url")), data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"}) < 400


_QR_URL_RE = re.compile(r"https://txz\.qq\.com/\S+")
_ASCII = re.compile(r"[^\x20-\x7e]")


def _ascii_safe(text):
    """HTTP 头只能放 ASCII：中文标题转成 URL 编码（ntfy 会还原显示）。"""
    from urllib.parse import quote
    return quote(str(text or "")[:120], safe="")


def build_pusher(config, log=None):
    return Pusher(config, log=log)
