"""执行权：同一时刻只有持有执行器单活锁的那个进程能让设备动作（主备见 executor/main.py）。

执行器以 PostgreSQL 会话级 advisory lock 做主备：持锁连接断了，锁随之释放，待命的副本几秒内接管。旧进程里的
工位线程不会因为主线程发现失锁就停下——`ThreadPoolExecutor.shutdown(wait=False)` 不打断在跑的任务，排队的任务
照样开始，解释器退出前还要等它们全部做完。所以失锁按三道挡：

1. 熔断（`LeaseWatch`）：看门狗每秒在持锁连接上核对一次；连接断了或锁不在了就熔断——还没开始的工位任务取消，
   进程立即退出，不等在跑的那些做完；
2. 领取前核对（`verify`）：每次领取「要让设备动作」的工作（投递指令、逐样本拆开的下一条、接入验收、手动写点）
   与把设备回执落到指令上之前，在同一个库会话里核对执行器锁仍由本进程的持锁连接持有：看门狗还没发现失锁时，
   工位线程也不再发出新的动作、不再改指令的结论；
3. 已经交给设备的那一下收不回来：照「执行器在投递中途崩溃」处理——领取时先把「可能已发出」落库，接管的副本
   按指令号去问设备，问不到就转人工核查，不自动重发。

接管的一方在开始对账之前先等一会儿（executor/main.py 的 TAKEOVER_GRACE_SEC），留给失锁的旧进程自我熔断。
没有登记执行权的进程（API、测试、脚本）不做这项核对。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from sqlalchemy import text

LOCK_KEY = "ilcs-executor"
LEASE_CHECK_SEC = 1.0
# 两参数形式的 advisory lock 在 pg_locks 里：第一个键在 classid、第二个在 objid、objsubid = 2
_HELD = text(
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted AND pid = :pid"
    " AND classid = CAST(:namespace AS oid) AND objid = CAST(hashtext(:key) AS oid) AND objsubid = 2)"
)
log = logging.getLogger("ilcs.executor")


class LeadershipLost(RuntimeError):
    """执行权已经丢了：不再领取工作，不再让设备动作。"""


class Leadership:
    """本进程的执行权：持锁连接在库里的后端进程号，加一个熔断标志。"""

    def __init__(self, pid: int, namespace: int, key: str = LOCK_KEY):
        self.pid = int(pid)
        self.namespace = int(namespace)
        self.key = key
        self.lost = threading.Event()
        self.reason = ""

    def fence(self, reason: str) -> None:
        if not self.lost.is_set():
            self.reason = reason
            self.lost.set()

    def holds(self, connection) -> bool:
        """执行器锁此刻仍由持锁连接（按后端进程号认）持有。`connection` 是任一库连接或会话，在它当前的事务里查。"""
        if self.lost.is_set():
            return False
        params = {"pid": self.pid, "namespace": self.namespace, "key": self.key}
        return bool(connection.execute(_HELD, params).scalar())


_current: Leadership | None = None


def install(leadership: Leadership | None) -> None:
    """执行器拿到锁后登记执行权；传 None 撤销（测试用）。"""
    global _current
    _current = leadership


def current() -> Leadership | None:
    return _current


def fenced() -> bool:
    return _current is not None and _current.lost.is_set()


def verify(db, action: str) -> None:
    """领取要让设备动作的工作、落设备回执之前调用（在同一个事务里）。没有登记执行权的进程直接放行。"""
    leadership = _current
    if leadership is None:
        return
    if not leadership.holds(db):
        leadership.fence("执行器锁已不在本进程的持锁连接上")
        raise LeadershipLost(f"执行器已失去执行权（{leadership.reason}），不再{action}")


class LeaseWatch:
    """看门狗：每隔 `interval` 秒在持锁连接上核对一次执行器锁。连接断了、锁不在了，或者工位线程领取前核对已经
    发现失锁，就熔断并调 `on_lost(原因)`——执行器里是取消排队的工位任务、立即退出进程。

    持锁连接只归它用（SQLAlchemy 的连接不能两个线程同时用）：主线程不再碰这条连接，退出前先 `stop()` 再关连接。
    """

    def __init__(self, connection, leadership: Leadership, on_lost: Callable[[str], None],
                 interval: float = LEASE_CHECK_SEC):
        self.connection = connection
        self.leadership = leadership
        self.on_lost = on_lost
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ilcs-lease-watch", daemon=True)

    def start(self) -> "LeaseWatch":
        self._thread.start()
        return self

    def check(self) -> str:
        """核对一次：返回失锁原因，空串表示锁还在。"""
        if self.leadership.lost.is_set():
            return self.leadership.reason or "执行权已熔断"
        try:
            held = self.leadership.holds(self.connection)
            # 不留「事务中空闲」：库若设了 idle_in_transaction_session_timeout，会断开这条连接、锁跟着丢
            self.connection.commit()
        except Exception as exc:  # noqa: BLE001  连不上就是失锁：库那边会话一断，锁随之释放
            return f"持锁连接中断（{exc.__class__.__name__}）"
        return "" if held else "执行器锁已不在持锁连接上"

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            reason = self.check()
            if reason:
                self.leadership.fence(reason)
                try:
                    self.on_lost(reason)
                except Exception:  # noqa: BLE001  熔断标志已经立起，领取前核对照样挡住新动作
                    log.exception("执行权熔断回调失败")
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self.interval + 10)
