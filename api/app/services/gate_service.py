from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.config import settings
from ..core.errors import ExecutionGateClosed
from ..domain import gate
from ..repositories.resources import AdapterRepository


class GateService:
    """执行门。会驱动设备或占用资源的写操作（排程、下发、续跑）先过 `require_open`。

    联锁与执行器停止关全站；单台设备失联或心跳超时只挡用到它的批次。

    保持与终止是安全动作，不受执行门限制：门关着时现场恰恰最需要能停下来。
    """

    def __init__(self, db: Session):
        self.db = db
        self.adapters = AdapterRepository(db)

    def status(self) -> dict:
        from .monitoring_service import ExecutorLiveness

        state = gate.evaluate(
            self.adapters.health(),
            now(),
            settings.heartbeat_stale_sec,
            settings.heartbeat_degraded_sec,
        )
        # 执行器停了，门必须关：否则界面照常下发，却没有进程去投递、去推进
        executor = ExecutorLiveness(self.db).reasons()
        if executor:
            state["reasons"] = [*executor, *state["reasons"]]
            state["open"] = False
        return state

    def require_open(self, station_ids=None) -> dict:
        """全站门关着一律拒绝；给了工位就再看这些工位自己有没有失联 / 心跳超时。"""
        state = self.status()
        if not state["open"]:
            raise ExecutionGateClosed(
                "全局执行门已关闭，排程、下发与续跑被禁用；保持与终止仍可执行",
                {"reasons": state["reasons"]},
            )
        if station_ids:
            blocked = [
                reason for reason in gate.station_reasons(state, station_ids)
                if reason not in state["reasons"]
            ]
            if blocked:
                raise ExecutionGateClosed(
                    "本批次用到的设备不可用，下发与续跑被禁用；保持与终止仍可执行",
                    {"reasons": blocked},
                )
        return state

    def reasons_for(self, station_ids) -> list[str]:
        return gate.station_reasons(self.status(), station_ids)
