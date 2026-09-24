"""并发执行器：控制回路串行、设备 I/O 按工位并发。

原来的执行器一轮里逐条阻塞地查询、投递所有工位的指令：一台设备网关卡住 10 s，整轮就慢
10 s；几台一起卡，执行器心跳过期、全站执行门关闭——单台设备的网络问题被放大成全站停摆。

现在每轮分两部分：

1. **控制回路**（本线程，只碰数据库）：执行器心跳、模拟心跳、设备监控报警、流程推进。
   它不等任何设备，所以心跳按轮询周期稳定写入。
2. **工位回路**（线程池）：每个有事要做的工位一个任务，做探测、对账、轮询、超时、投递。
   同一工位同一时刻只有一个任务在跑，工位内的指令顺序不变；上一轮的任务没做完，
   这一轮就不给它派新活，也不等它——卡住的只有它自己。

主备语义不变：仍然只有持有 advisory lock 的那个进程在工作，线程池是进程内的并发。
每个线程用自己的数据库会话；批次行锁、指令比较并交换、步骤行锁与推进去重表本来就是
为「API 与执行器同时写」设计的，工位之间的并发落在同一套保护之内。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.config import settings
from .execution_service import ExecutorLoop
from .gate_service import GateService

log = logging.getLogger("ilcs.executor")

SUMMED = ("probed", "reconciled", "polled", "executed", "overdue", "timed_out")
# 出向事件投递在线程池里的任务名；不是工位，不进工位卡住统计
WEBHOOK_JOB = "__webhooks__"


@dataclass
class _Running:
    future: Future
    started: float
    warned: bool = False


@dataclass
class ConcurrentExecutor:
    session_factory: Callable[[], Session]
    workers: int = field(default_factory=lambda: max(1, settings.executor_workers))
    station_wait_sec: float = field(default_factory=lambda: max(0.0, settings.executor_station_wait_sec))

    def __post_init__(self) -> None:
        self.pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="ilcs-station")
        self.running: dict[str, _Running] = {}

    def _webhooks_due(self) -> bool:
        from ..models import WebhookDelivery

        with self.session_factory() as db:
            return db.query(WebhookDelivery.id).filter(
                WebhookDelivery.state == "pending", WebhookDelivery.next_attempt_at <= now(),
            ).first() is not None

    def shutdown(self, wait_for_stations: bool = True) -> None:
        self.pool.shutdown(wait=wait_for_stations)

    def _station_job(self, station_id: str, dispatch_open: bool) -> dict:
        db = self.session_factory()
        try:
            return ExecutorLoop(db).station_pass(station_id, dispatch_open=dispatch_open)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _webhook_job(self) -> dict:
        """出向事件投递也是网络 I/O：放进线程池，接收方卡住不拖慢控制回路。"""
        from .integration_service import deliver_due

        db = self.session_factory()
        try:
            return deliver_due(db)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _reap(self, report: dict) -> None:
        """收下已结束的工位任务，汇总计数；异常只记日志，不影响其他工位。"""
        for station_id, running in list(self.running.items()):
            if not running.future.done():
                continue
            del self.running[station_id]
            try:
                result = running.future.result()
            except Exception:
                report["station_errors"] += 1
                log.exception(
                    "工位回路失败", extra={"fields": {"station_id": station_id}},
                )
                continue
            for key in SUMMED:
                report[key] += int(result.get(key) or 0)
            report["webhooks_sent"] = report.get("webhooks_sent", 0) + int(result.get("sent") or 0)

    def cycle(
        self, *, simulate_heartbeat: bool = True, monitor_assets: bool = False, cleanup_files: bool = False,
    ) -> dict:
        report: dict = {key: 0 for key in SUMMED}
        report.update(station_errors=0, stations_dispatched=0, stations_busy=0, stations_stuck=[])
        started = time.monotonic()

        with self.session_factory() as db:
            loop = ExecutorLoop(db)
            report.update(loop.control_pass(simulate_heartbeat=simulate_heartbeat, monitor_assets=monitor_assets))
            wanted = loop.stations_needing_work()
            dispatch_open = bool(GateService(db).status()["open"])
            db.commit()

        self._reap(report)
        submitted: list[Future] = []
        for station_id in sorted(wanted):
            if station_id in self.running:
                continue  # 上一轮的任务还在跑：不重复派活，也不等它
            future = self.pool.submit(self._station_job, station_id, dispatch_open)
            self.running[station_id] = _Running(future=future, started=time.monotonic())
            submitted.append(future)
        report["stations_dispatched"] = len(submitted)
        waiting = list(submitted)
        if WEBHOOK_JOB not in self.running and self._webhooks_due():
            future = self.pool.submit(self._webhook_job)
            self.running[WEBHOOK_JOB] = _Running(future=future, started=time.monotonic())
            waiting.append(future)
        if waiting and self.station_wait_sec > 0:
            wait(waiting, timeout=self.station_wait_sec)
        self._reap(report)

        moment = time.monotonic()
        for station_id, running in self.running.items():
            if station_id == WEBHOOK_JOB:
                continue
            age = moment - running.started
            if age >= settings.executor_station_stuck_sec:
                report["stations_stuck"].append(station_id)
                if not running.warned:
                    running.warned = True
                    log.warning(
                        "工位回路长时间未返回，设备网关可能卡住",
                        extra={"fields": {"station_id": station_id, "age_sec": round(age)}},
                    )
        report["stations_busy"] = len([key for key in self.running if key != WEBHOOK_JOB])

        with self.session_factory() as db:
            loop = ExecutorLoop(db)
            report["advanced"] = loop.advance()
            report["files_cleaned"] = loop.cleanup_orphan_files() if cleanup_files else 0
            report["telemetry_purged"] = loop.purge_telemetry() if cleanup_files else 0
            from .monitoring_service import ExecutorLiveness

            ExecutorLiveness(db).record_cycle(
                cycle_ms=round((time.monotonic() - started) * 1000),
                busy=sorted(key for key in self.running if key != WEBHOOK_JOB), stuck=report["stations_stuck"],
                workers=self.workers,
            )
            db.commit()
        report["cycle_ms"] = round((time.monotonic() - started) * 1000)
        report["at"] = now().isoformat(timespec="seconds")
        return report

    def drain(self, timeout: float | None = None) -> dict:
        """等所有在跑的工位任务结束（退出前、测试里用）。"""
        report: dict = {key: 0 for key in SUMMED}
        report.update(station_errors=0)
        futures = [running.future for running in self.running.values()]
        if futures:
            wait(futures, timeout=timeout)
        self._reap(report)
        return report
