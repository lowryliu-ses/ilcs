"""设备点位读写：不参与自动流程也能用的那一层。

映射驱动（Modbus / OPC UA 点表、REST、串口命令）只配点表就能接：

- **读点**（界面「读取点位」）：在 API 进程里按点表逐个读，和「测试已保存配置」一样只读、不动设备；
  接入验收正在驱动这台设备时不读（会和它抢同一个串口 / 会话）；
- **手动写点**：只对点表里声明了可写（`writable: true`，可带 `min` / `max`）的点。人签名、写明原因申请，登记一条排队记录；
  **执行器执行**（执行器是唯一驱动设备的进程，同一台设备的 I/O 在它那里串行，不会和投递、轮询抢设备）：
  先读当前值、写、再回读，前后值与签名一起留痕。执行前再核一遍：配置没变（申请时看到的点定义还是那个）、
  没有接入验收在跑（等它跑完）、工位上没有可能在动作的指令、设备不在运行或保持（有状态点的）——不满足就不写；
- 任务用的控制信号（启动、状态、复位、指令号）不能手动写：要让设备动作请走指令，免得绕过作业台账。

结论：done 已写入（`matches` 为假说明设备收下了、但回读值不同：被限幅或换算）/ failed 没有写，设备没动 /
unknown 写出去了却没拿到结论，要人到现场核对 / cancelled 申请人撤回。出了结论的记录不许改、不许删（数据库触发器）。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from ..adapters.base import AdapterError, AdapterUnreachable
from ..adapters.catalog import has_tasks
from ..adapters.registry import adapter_for
from ..core.clock import now
from ..core.context import AccessContext, system_context
from ..core.errors import NotFound, StateConflict, ValidationFailed
from ..models import Adapter, PointWrite, User
from ..repositories.execution import CommandRepository
from ..repositories.resources import AdapterRepository, StationRepository
from .acceptance_service import running_stations
from .audit_service import AuditService
from .identity_service import IdentityService

ACTIVE = ("queued", "running")
STATE_LABELS = {"queued": "排队中", "running": "写入中", "done": "已写入", "failed": "没有写", "unknown": "结果未知",
                "cancelled": "已撤回"}
MEANING = "手动写入设备点位"
LOG = logging.getLogger("ilcs.executor")


def write_out(row: PointWrite) -> dict[str, Any]:
    return {
        "id": row.id, "station_id": row.station_id, "point": row.point, "value": (row.value or {}).get("value"),
        "reason": row.reason, "requested_by": row.requested_by, "config_version": row.config_version,
        "state": row.state, "state_label": STATE_LABELS.get(row.state, row.state),
        "before": (row.before or {}).get("value"), "after": (row.after or {}).get("value"), "matches": row.matches,
        "error": row.error,
        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
        "finished_at": row.finished_at.isoformat(timespec="seconds") if row.finished_at else None,
    }


def _points_driver(adapter: Adapter):
    """这台设备的驱动实例（要有点表）：映射驱动登记了 points，或 SiLA 设备服务实现了 PointAccess。
    内置模拟、其余按 ILCS 契约接的驱动没有点表。"""
    if adapter.kind != "real":
        raise StateConflict("内置模拟没有点表：接成真实设备（映射驱动）后才能读写点位", code="points_unavailable")
    try:
        implementation = adapter_for(adapter)
    except (NotImplementedError, AdapterError) as exc:
        raise StateConflict(str(exc), code="adapter_driver_unavailable") from exc
    if not callable(getattr(implementation, "read_points", None)):
        raise StateConflict(
            f"{adapter.driver} 没有点表：点位读写要用映射驱动（Modbus / OPC UA 点表、REST、串口命令）并登记 points，"
            "或接实现了 PointAccess 的 SiLA 设备服务",
            code="points_unavailable",
        )
    try:
        specs = implementation.point_specs()
    except AdapterUnreachable as exc:
        raise StateConflict(f"连不上设备服务，读不到点表：{exc}", code="device_unreachable") from exc
    if not specs:
        raise StateConflict(f"{adapter.driver} 没有登记点表（或设备服务没有实现 PointAccess）", code="points_unavailable")
    return implementation


class PointService:
    """界面侧：读点、申请手动写、查看写入记录。"""

    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.stations = StationRepository(db, ctx)
        self.adapters = AdapterRepository(db)
        self.identity = IdentityService(db, ctx)
        self.audit = AuditService(db, ctx)

    def _adapter(self, station_id: str) -> Adapter:
        if self.stations.get(station_id) is None:
            raise NotFound("工位不存在")
        adapter = self.adapters.get(station_id)
        if adapter is None:
            raise NotFound("适配器未登记")
        if not adapter.enabled:
            raise StateConflict("适配器已停用，不能读写点位", code="adapter_disabled")
        return adapter

    def read(self, station_id: str) -> dict[str, Any]:
        adapter = self._adapter(station_id)
        if station_id in running_stations(self.db):
            raise StateConflict(f"{station_id} 的接入验收正在执行，结束后再读点位", code="acceptance_running")
        implementation = _points_driver(adapter)
        return {
            "station_id": station_id, "driver": adapter.driver, "config_version": adapter.config_version,
            "tasks": has_tasks(adapter.driver, adapter.config), "read_at": now().isoformat(timespec="seconds"),
            "points": implementation.read_points(),
        }

    def writes(self, station_id: str, limit: int = 20) -> list[dict[str, Any]]:
        if self.stations.get(station_id) is None:
            raise NotFound("工位不存在")
        rows = (self.db.query(PointWrite).filter(PointWrite.station_id == station_id)
                .order_by(PointWrite.created_at.desc()).limit(max(1, min(limit, 100))).all())
        return [write_out(row) for row in rows]

    def request_write(self, station_id: str, point: str, value: Any, reason: str, signature_id: str,
                      user: User) -> dict[str, Any]:
        adapter = self._adapter(station_id)
        reason = (reason or "").strip()
        if not reason:
            raise ValidationFailed("手动写设备点位要写明原因", code="point_write_reason_required")
        implementation = _points_driver(adapter)
        try:
            implementation.check_manual_write(point, value)
        except AdapterError as exc:
            raise ValidationFailed(str(exc), code="point_write_refused") from exc
        pending = self.db.query(PointWrite).filter(
            PointWrite.station_id == station_id, PointWrite.state.in_(ACTIVE)).first()
        if pending is not None:
            raise StateConflict(f"{station_id} 上一条手动写入（{pending.point}）还没执行完，等它出结论再写",
                                code="point_write_pending")
        signature = self.identity.consume_signature(signature_id, user, MEANING, object_ref=station_id)
        station = self.stations.get(station_id)
        row = PointWrite(
            org_id=station.org_id, station_id=station_id, point=point, value={"value": value}, reason=reason,
            signature_id=signature.id, requested_by=user.display_name, requested_by_id=user.id,
            config_version=adapter.config_version, state="queued", created_at=now(),
        )
        self.db.add(row)
        self.db.flush()
        self.audit.record(
            user, "申请写入设备点位", station_id, sign=True, meaning=signature.meaning, signature_id=signature.id,
            after=f"{point} = {value!r}（排队，由执行器写入）", detail=f"原因：{reason}；配置 v{adapter.config_version}",
        )
        self.db.commit()
        return write_out(row)

    def cancel(self, write_id: str, user: User) -> dict[str, Any]:
        row = self.db.get(PointWrite, write_id)
        if row is None or self.stations.get(row.station_id) is None:
            raise NotFound("写入记录不存在")
        if row.state != "queued":
            raise StateConflict(f"这条写入已经{STATE_LABELS.get(row.state, row.state)}，撤回不了", code="point_write_not_queued")
        # 比较并交换：执行器此刻可能刚领走它
        changed = self.db.query(PointWrite).filter(PointWrite.id == write_id, PointWrite.state == "queued").update(
            {"state": "cancelled", "finished_at": now(), "error": f"{user.display_name} 撤回，没有写"},
            synchronize_session=False)
        if not changed:
            raise StateConflict("执行器已经开始写这一条，撤回不了", code="point_write_not_queued")
        self.audit.record(user, "撤回设备点位写入", row.station_id, before="排队中", after="已撤回",
                          detail=f"{row.point} = {(row.value or {}).get('value')!r}")
        self.db.commit()
        self.db.refresh(row)
        return write_out(row)


class PointWriteRunner:
    """执行器侧：领取排队的手动写入，核对之后写、回读、留痕。"""

    def __init__(self, db: Session):
        self.db = db

    def due_stations(self) -> set[str]:
        return {row[0] for row in self.db.query(PointWrite.station_id).filter(PointWrite.state == "queued").all()}

    def interrupt_orphans(self) -> int:
        """执行器启动时：上一个执行器写到一半的，结论只能是未知（写没写进去不知道）。"""
        rows = self.db.query(PointWrite).filter(PointWrite.state == "running").all()
        for row in rows:
            row.state, row.finished_at = "unknown", now()
            row.error = "执行器在写入过程中重启：这次写入有没有到设备不知道，请现场核对设备上的值"
        self.db.commit()
        return len(rows)

    def run_due(self, station_id: str | None = None) -> int:
        query = self.db.query(PointWrite).filter(PointWrite.state == "queued")
        if station_id is not None:
            query = query.filter(PointWrite.station_id == station_id)
        finished = 0
        for write_id in [row.id for row in query.order_by(PointWrite.created_at).all()]:
            if self._execute(write_id):
                finished += 1
        return finished

    def _refuse(self, row: PointWrite) -> str:
        """执行前的核对：返回不写的原因（空串表示可以写）；返回 None 表示先不执行、留在队列里。"""
        adapter = self.db.get(Adapter, row.station_id)
        if adapter is None or not adapter.enabled or adapter.kind != "real":
            return "适配器已停用或不是真实设备，没有写"
        if adapter.config_version != row.config_version:
            return (f"申请时是配置 v{row.config_version}，现在是 v{adapter.config_version}：点的定义可能变了，没有写；"
                    "请按新配置重新申请")
        acting = CommandRepository(self.db).acting_on_station(row.station_id)
        if acting:
            return (f"工位上有指令可能还在动作（{acting[0].id[:8]}）：手动写会干扰它，没有写；指令结束后再申请")
        return ""

    def _execute(self, write_id: str) -> bool:
        row = self.db.get(PointWrite, write_id)
        if row is None or row.state != "queued":
            return False
        if row.station_id in running_stations(self.db):
            return False  # 接入验收正在驱动这台设备：等它跑完，下一轮再写
        refusal = self._refuse(row)
        if refusal:
            return self._close(row, "failed", error=refusal)
        adapter = self.db.get(Adapter, row.station_id)
        changed = self.db.query(PointWrite).filter(PointWrite.id == write_id, PointWrite.state == "queued").update(
            {"state": "running", "started_at": now()}, synchronize_session=False)
        self.db.commit()
        if not changed:
            return False  # 申请人刚撤回
        self.db.refresh(row)
        value = (row.value or {}).get("value")
        try:
            implementation = _points_driver(adapter)
            if implementation.tasks and getattr(implementation, "status", None):
                state = implementation.device_state()
                if state in {"running", "held"}:
                    return self._close(row, "failed", error=f"设备在{'运行' if state == 'running' else '保持'}中：手动写会干扰它，没有写")
            outcome = implementation.write_point_manually(row.point, value, request_id=row.id)
        except StateConflict as exc:
            return self._close(row, "failed", error=f"{exc}，没有写")
        except AdapterError as exc:
            return self._close(row, "failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001  写出去了没有结论（含读不回来）：结果未知，要人核对
            LOG.warning("%s 手动写 %s 没有结论：%s", row.station_id, row.point, exc)
            return self._close(row, "unknown", error=str(exc) or exc.__class__.__name__)
        note = "" if outcome["matches"] else (f"设备收下了，但回读值是 {outcome['after']!r}，不是写入的 {value!r}"
                                              "（被设备限幅或换算）：请核对")
        return self._close(row, "done", before=outcome["before"], after=outcome["after"], matches=outcome["matches"],
                           error=note)

    def _close(self, row: PointWrite, state: str, *, before: Any = None, after: Any = None, matches: bool | None = None,
               error: str = "") -> bool:
        row.state, row.finished_at, row.error, row.matches = state, now(), error[:2000], matches
        if before is not None or state == "done":
            row.before = {"value": before}
        if after is not None or state == "done":
            row.after = {"value": after}
        value = (row.value or {}).get("value")
        label = STATE_LABELS.get(state, state)
        AuditService(self.db, system_context(row.org_id, "执行器写设备点位")).record(
            None, "写入设备点位", row.station_id, before=f"{row.point} = {before!r}" if state == "done" else "",
            after=f"{row.point} = {after!r}（{label}）" if state == "done" else f"{row.point} = {value!r}：{label}",
            detail=f"{row.requested_by} 申请（签名 {row.signature_id[:8]}），原因：{row.reason}"
                   + (f"；{error}" if error else ""),
            org_id=row.org_id,
        )
        self.db.commit()
        return True
