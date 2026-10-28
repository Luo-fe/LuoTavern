# -*- coding: utf-8 -*-
"""OneBot 11 WebSocket 客户端（事件接收 + 动作发送）。"""
import json
import threading
import time

import websocket


class OneBotClient:
    def __init__(self, url, token="", on_event=None, on_log=None):
        self.url = url
        self.token = token
        self.on_event = on_event or (lambda raw: None)
        self.on_log = on_log or (lambda msg: None)
        self.ws = None
        self._send_lock = threading.Lock()
        self._watcher_started = False

    def _headers(self):
        if self.token:
            return {"Authorization": "Bearer " + self.token}
        return None

    def send(self, payload):
        """发送；返回是否实际送出（ws 未连接/发送异常 → False）。"""
        if self.ws is None:
            return False
        with self._send_lock:
            try:
                self.ws.send(json.dumps(payload, ensure_ascii=False))
                return True
            except Exception as exc:
                self.on_log("[发送失败] %s" % exc)
                return False

    def send_private(self, user_id, text):
        return self.send(
            {
                "action": "send_private_msg",
                "params": {
                    "user_id": int(user_id),
                    # 强制 text 段：避免 NapCat 把含 [CQ: 等样式的字符串误解析成消息类型
                    "message": [{"type": "text", "data": {"text": str(text)}}],
                },
            }
        )

    def _watch_stop(self, stop_event):
        """关停观察（全局只起一个线程）。"""
        while not stop_event.is_set():
            time.sleep(0.5)
        ws = self.ws
        try:
            if ws is not None:
                ws.close()
        except Exception:
            pass

    @staticmethod
    def _redact(url):
        """URL 里可能带 access_token，日志里必须脱敏（统一实现见 textutil）。"""
        from .textutil import redact
        return redact(url)

    def connect_loop(self, stop_event=None):
        failures = 0
        # 观察线程只创建一次：此前每轮重连都新建一个"永不退出"的线程，
        # NapCat 长时间离线会累积出成百上千个常驻线程
        if stop_event is not None and not self._watcher_started:
            self._watcher_started = True
            threading.Thread(target=self._watch_stop, args=(stop_event,),
                             daemon=True).start()
        while True:
            try:
                self.on_log("正在连接 NapCat: %s" % self._redact(self.url))
                ws = websocket.WebSocketApp(
                    self.url,
                    header=self._headers(),
                    on_message=lambda ws_, raw: self.on_event(raw),
                    on_open=lambda ws_: self._on_open(),
                    on_error=lambda ws_, err: self.on_log("连接错误: %s" % err),
                    on_close=lambda ws_, *a: self._on_close(),
                )
                self.ws = ws
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                self.on_log("连接异常: %s" % exc)
            if stop_event is not None and stop_event.is_set():
                break
            # 指数退避（3→6→12→24→30 秒封顶），避免 NapCat 离线时刷屏重连
            delay = min(30, 3 * (2 ** min(failures, 4)))
            failures = min(failures + 1, 5)
            self.on_log("与 NapCat 的连接关闭，%d 秒后重连..." % delay)
            time.sleep(delay)

    def _on_open(self):
        self._failures = 0
        self.on_log("已连接 NapCat，可以开始聊天了")

    def _on_close(self):
        self.ws = None
