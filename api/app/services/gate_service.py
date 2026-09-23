from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.config import settings
from ..core.errors import ExecutionGateClosed
from ..domain import gate
from ..repositories.resources import AdapterRepository


class GateService:
    """全局执行门。会驱动设备或占用资源的写操作（排程、下发、续跑）先过 `require_open`。

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

    def require_open(self) -> dict:
        state = self.status()
        if not state["open"]:
            raise ExecutionGateClosed(
                "全局执行门已关闭，排程、下发与续跑被禁用；保持与终止仍可执行",
                {"reasons": state["reasons"]},
            )
        return state
