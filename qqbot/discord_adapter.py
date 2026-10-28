# -*- coding: utf-8 -*-
"""Discord 适配器：把 discord.py 网关桥接到 BotCore 的同步工作队列。

对外接口与 OneBotClient 对齐（send_private / connect_loop），BotCore 无需感知平台。
消息流：on_message（异步线程）→ on_user_message(user_id, text)（同步队列）
发送流：send_private（工作线程）→ run_coroutine_threadsafe → DM 分块发送。
"""
import asyncio
import threading
import time

import discord

MAX_CHUNK = 2000  # Discord 单条消息上限


def _chunk_text(text, limit=MAX_CHUNK):
    text = str(text)
    if len(text) <= limit:
        return [text]
    parts = []
    while len(text) > limit:
        idx = text.rfind("\n", 0, limit)
        if idx <= 0:
            idx = limit
        parts.append(text[:idx])
        text = text[idx:].lstrip("\n")
    parts.append(text)
    return parts


class _HandlerClient(discord.Client):
    def __init__(self, adapter, **kwargs):
        self._adapter = adapter
        super().__init__(**kwargs)

    async def on_ready(self):
        self._adapter._ready = True
        self._adapter.log("已连接 Discord：%s（%s）" % (
            self.user.name, self.user.id))

    async def on_message(self, message):
        try:
            self._adapter._on_message(message)
        except Exception as exc:
            self._adapter.log("[discord] on_message 出错: %s" % exc)


class DiscordClient:
    def __init__(self, token="", on_user_message=None, on_log=None,
                 reply_to_mention_only=True, proxy=None, intents_extra=None):
        self.token = token
        self.on_user_message = on_user_message or (lambda user_id, text: None)
        self.on_log = on_log or (lambda msg: None)
        self.reply_to_mention_only = reply_to_mention_only
        self.proxy = proxy
        self._loop = None
        self._ready = False
        self._start_future = None
        self._send_lock = threading.Lock()
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        if intents_extra:
            for name, val in intents_extra.items():
                setattr(intents, name, val)
        self.client = _HandlerClient(self, intents=intents, proxy=self.proxy or None)

    def log(self, msg):
        self.on_log(msg)

    # ---------- 事件 ----------
    def _on_message(self, message):
        """异步线程回调 → 同步队列。忽略 bot 消息；服务器频道仅响应 @提及。"""
        if message.author.bot:
            return
        if message.guild is not None:
            if self.reply_to_mention_only and self.client.user not in message.mentions:
                return
        text = (message.content or "").strip()
        if not text:
            return
        self.on_user_message(message.author.id, text)

    # ---------- 发送 ----------
    def send_private(self, user_id, text):
        """发送私信；未连接/失败返回 False（与 OneBot 接口对齐）。"""
        if self._loop is None or not self._ready:
            return False
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._send_dm(int(user_id), text), self._loop)
            fut.result(timeout=30)
            return True
        except Exception as exc:
            self.log("[discord] 发送失败: %s" % exc)
            return False

    async def _send_dm(self, user_id, text):
        user = self.client.get_user(user_id) or await self.client.fetch_user(user_id)
        channel = await user.create_dm()
        for part in _chunk_text(text):
            await channel.send(part)

    # ---------- 主循环 ----------
    def connect_loop(self, stop_event=None):
        """启动 Discord 网关；stop_event 置位后关闭退出。"""
        loop = asyncio.new_event_loop()
        self._loop = loop
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        self._start_future = asyncio.run_coroutine_threadsafe(
            self.client.start(self.token), loop)
        try:
            while stop_event is not None and not stop_event.is_set() \
                    and not self._start_future.done():
                time.sleep(0.5)
            if stop_event is not None and stop_event.is_set():
                asyncio.run_coroutine_threadsafe(self.client.close(), loop)
            self._start_future.result(timeout=30)
        except Exception as exc:
            self.log("[discord] 连接异常: %s" % exc)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            self._loop = None
            self._ready = False

    def test_connect(self):
        """快速验证 token：登录成功即退出。返回 (ok, error)。"""
        ok = [False]
        err = [None]

        async def _run():
            nonlocal ok
            c = discord.Client(intents=discord.Intents.default(), proxy=self.proxy or None)

            @c.event
            async def on_ready():
                ok[0] = True
                await c.close()

            try:
                await c.start(self.token)
            except Exception as exc:
                err[0] = exc
            await asyncio.sleep(0.5)  # 让 close 内部任务收尾，避免 pending 告警

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_run())
        except Exception as exc:
            err[0] = exc
        finally:
            loop.close()
        return ok[0], err[0]
