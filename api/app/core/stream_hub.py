"""API 进程内的变更分发中心：一条 LISTEN 连接，分发给本进程所有推送订阅者。

每个浏览器连接各占一条数据库连接是不可接受的（几十个标签页就能耗尽连接池），所以
一个 API 进程只有一个监听线程，它把 `ilcs_events` 通知按组织转给各订阅者的 asyncio 队列。
监听连接断开时自动重连，重连期间的通知不补发——客户端收到 `resync` 后整体重取。
"""
from __future__ import annotations

import asyncio
import json
import logging
import select
import threading
import time
from dataclasses import dataclass, field

from .events import ALL_ORGS, EVENTS_CHANNEL

log = logging.getLogger("ilcs.stream")


@dataclass(eq=False)
class Subscriber:
    org_id: str
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=500))

    def offer(self, message: dict) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            # 客户端处理不过来：丢掉积压，让它整体重取一次，比无限堆内存好
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait({"topic": "resync", "org": self.org_id, "ids": []})


class StreamHub:
    def __init__(self) -> None:
        self._subscribers: set[Subscriber] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.connected = False

    def subscribe(self, org_id: str) -> Subscriber:
        subscriber = Subscriber(org_id=org_id, loop=asyncio.get_running_loop())
        with self._lock:
            self._subscribers.add(subscriber)
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="ilcs-stream-hub", daemon=True)
                self._thread.start()
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish_local(self, message: dict) -> None:
        """把一条变更分给本进程订阅者（监听线程收到通知后调用；测试也可直接调用）。"""
        org = message.get("org") or ALL_ORGS
        with self._lock:
            targets = [s for s in self._subscribers if org == ALL_ORGS or s.org_id == org]
        for subscriber in targets:
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.offer, message)
            except RuntimeError:
                # 事件循环已关闭：连接早就断了，下次清理时移除
                self.unsubscribe(subscriber)

    def _broadcast_resync(self) -> None:
        with self._lock:
            targets = list(self._subscribers)
        for subscriber in targets:
            try:
                subscriber.loop.call_soon_threadsafe(
                    subscriber.offer, {"topic": "resync", "org": subscriber.org_id, "ids": []}
                )
            except RuntimeError:
                self.unsubscribe(subscriber)

    def _run(self) -> None:
        from .db import engine

        backoff = 1.0
        while not self._stop.is_set():
            if not self.subscriber_count:
                # 没人订阅就退出，下一个订阅者会重新拉起
                with self._lock:
                    if not self._subscribers:
                        self._thread = None
                        return
            raw = None
            try:
                raw = engine.raw_connection()
                dbapi = raw.dbapi_connection
                dbapi.autocommit = True
                with dbapi.cursor() as cursor:
                    cursor.execute(f"LISTEN {EVENTS_CHANNEL}")
                if not self.connected:
                    self.connected = True
                    self._broadcast_resync()
                backoff = 1.0
                while not self._stop.is_set() and self.subscriber_count:
                    readable, _, _ = select.select([dbapi], [], [], 5.0)
                    if not readable:
                        continue
                    dbapi.poll()
                    while dbapi.notifies:
                        note = dbapi.notifies.pop(0)
                        try:
                            message = json.loads(note.payload or "{}")
                        except ValueError:
                            continue
                        self.publish_local(message)
            except Exception:  # 监听断了：退避重连，订阅者照常保持
                self.connected = False
                log.exception("变更监听连接中断，%.0f s 后重连", backoff)
                time.sleep(backoff)
                backoff = min(30.0, backoff * 2)
            finally:
                if raw is not None:
                    try:
                        raw.invalidate()
                    except Exception:
                        pass


hub = StreamHub()
