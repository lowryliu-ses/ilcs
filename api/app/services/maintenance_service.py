"""维护 / 点检工单：计划 → 执行 → 完成。

工单不是一张备忘录，它要真正挡住设备使用：
- 建单：同时登记一条维护占用。排程把维护占用当成占满整台资产，自动绕开这段时间。
- 开工：资产转入维护状态。开跑检查据此拦截，已排在这台资产上的批次在恢复前不能下发。
- 完成：写明维护记录并签名；合格恢复资产原状态，不合格资产保持维护状态、不回到可用。
- 取消：结束占用，资产回到开工前的状态。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.clock import as_utc, now
from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..models import MaintenanceOrder, ResourceBooking, User
from ..repositories.base import ScopedRepository
from ..repositories.resources import AssetRepository, BookingRepository
from .audit_service import AuditService
from .identity_service import IdentityService, user_may

KINDS = {"preventive": "预防性维护", "corrective": "故障维修", "inspection": "点检"}
STATES = {"planned": "已计划", "in_progress": "执行中", "done": "已完成", "cancelled": "已取消"}


class MaintenanceOrderRepository(ScopedRepository[MaintenanceOrder]):
    model = MaintenanceOrder

    def for_asset(self, asset_id: str) -> list[MaintenanceOrder]:
        return list(
            self.query().filter(MaintenanceOrder.asset_id == asset_id)
            .order_by(MaintenanceOrder.planned_start.desc()).all()
        )

    def open_orders(self) -> list[MaintenanceOrder]:
        return list(
            self.query().filter(MaintenanceOrder.state.in_(["planned", "in_progress"]))
            .order_by(MaintenanceOrder.planned_start).all()
        )


class MaintenanceService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.orders = MaintenanceOrderRepository(db, ctx)
        self.assets = AssetRepository(db, ctx)
        self.bookings = BookingRepository(db, ctx)
        self.audit = AuditService(db, ctx)
        self.identity = IdentityService(db, ctx)

    def _require_permission(self, user: User) -> None:
        if not user_may(self.ctx, user, "maintenance.edit"):
            raise PermissionDenied("当前角色不能处理维护工单")

    def _require(self, order_id: str) -> MaintenanceOrder:
        order = self.orders.get(order_id)
        if order is None:
            raise NotFound("维护工单不存在")
        return order

    def out(self, order: MaintenanceOrder) -> dict:
        asset = self.assets.get(order.asset_id)
        return {
            "id": order.id, "asset_id": order.asset_id,
            "asset_label": f"{asset.asset_no} {asset.name}" if asset else order.asset_id,
            "kind": order.kind, "kind_label": KINDS.get(order.kind, order.kind),
            "title": order.title, "detail": order.detail,
            "planned_start": order.planned_start.isoformat(timespec="minutes"),
            "planned_end": order.planned_end.isoformat(timespec="minutes"),
            "state": order.state, "state_label": STATES.get(order.state, order.state),
            "booking_id": order.booking_id, "assignee_user_id": order.assignee_user_id,
            "started_at": order.started_at.isoformat(timespec="seconds") if order.started_at else None,
            "completed_at": order.completed_at.isoformat(timespec="seconds") if order.completed_at else None,
            "result": order.result, "record": order.record, "row_version": order.row_version,
        }

    def list(self, asset_id: str = "") -> list[dict]:
        rows = self.orders.for_asset(asset_id) if asset_id else self.orders.open_orders()
        return [self.out(row) for row in rows]

    def create(self, payload: dict, user: User) -> dict:
        from .asset_service import AssetService

        self._require_permission(user)
        kind = payload.get("kind") or "preventive"
        if kind not in KINDS:
            raise ValidationFailed(f"工单类型只能是 {'、'.join(KINDS)}")
        title = (payload.get("title") or "").strip()
        if not title:
            raise ValidationFailed("工单标题必填")
        starts_at, ends_at = as_utc(payload["planned_start"]), as_utc(payload["planned_end"])
        if ends_at <= starts_at:
            raise ValidationFailed("计划结束时间必须晚于开始时间")
        asset = self.assets.get(payload["asset_id"])
        if asset is None:
            raise NotFound("资产不存在")
        if asset.state == "retired":
            raise StateConflict("已退役资产不再安排维护")
        # 维护占用走资产预约同一套冲突判断：与已有占用冲突就拒绝，而不是悄悄叠上去
        booking = AssetService(self.db, self.ctx).create_booking(
            {
                "asset_id": asset.id, "kind": "maintenance", "starts_at": starts_at, "ends_at": ends_at,
                "reason": f"维护工单：{title}",
            },
            user,
        )
        order = MaintenanceOrder(
            org_id=self.ctx.org_id, asset_id=asset.id, kind=kind, title=title,
            detail=payload.get("detail") or "", planned_start=starts_at, planned_end=ends_at,
            booking_id=booking["id"], assignee_user_id=payload.get("assignee_user_id") or "",
            created_by=user.id,
        )
        self.orders.add(order)
        self.audit.record(
            user, "新建维护工单", asset.id, before="—", after="已计划",
            detail=(
                f"{KINDS[kind]}「{title}」{starts_at:%m-%d %H:%M} → {ends_at:%m-%d %H:%M}；"
                f"已登记维护占用，受影响工步 {len(booking.get('impacted') or [])} 个"
            ),
        )
        self.db.commit()
        return {**self.out(order), "impacted": booking.get("impacted") or []}

    def start(self, order_id: str, user: User) -> dict:
        self._require_permission(user)
        order = self._require(order_id)
        if order.state != "planned":
            raise StateConflict(f"只有已计划的工单可以开工（当前 {STATES.get(order.state)}）")
        asset = self.assets.get(order.asset_id)
        order.asset_state_before = asset.state if asset else ""
        if asset is not None and asset.state != "maintenance":
            asset.state = "maintenance"
            self.assets.bump(asset)
        order.state = "in_progress"
        order.started_at = now()
        order.started_by = user.id
        self.orders.bump(order)
        self.audit.record(
            user, "维护工单开工", order.asset_id, before="已计划", after="执行中",
            detail=f"「{order.title}」；资产转入维护状态，其上的设备步骤在完工前不能开跑",
        )
        self.db.commit()
        return self.out(order)

    def complete(
        self, order_id: str, result: str, record: str, signature_id: str, user: User,
    ) -> dict:
        self._require_permission(user)
        order = self._require(order_id)
        if order.state != "in_progress":
            raise StateConflict("只有执行中的工单可以完工")
        if result not in {"pass", "fail"}:
            raise ValidationFailed("维护结论只能是 pass 或 fail")
        if not (record or "").strip():
            raise ValidationFailed("必须写明维护记录", code="maintenance_record_required")
        signature = self.identity.consume_signature(
            signature_id, user, "维护工单完工", object_ref=order.id, strict=True,
        )
        asset = self.assets.get(order.asset_id)
        if asset is not None and result == "pass":
            # 合格才回到开工前的状态；不合格保持维护状态，不能被当成可用
            asset.state = order.asset_state_before if order.asset_state_before != "maintenance" else "active"
            self.assets.bump(asset)
        self._close_booking(order, "done")
        order.state = "done"
        order.result = result
        order.record = record.strip()
        order.completed_at = now()
        order.completed_by = user.id
        order.signature_id = signature.id
        self.orders.bump(order)
        self.audit.record(
            user, "维护工单完工", order.asset_id, sign=True, meaning=signature.meaning,
            signature_id=signature.id, before="执行中", after="合格" if result == "pass" else "不合格",
            detail=(
                f"「{order.title}」；{record.strip()}"
                + ("" if result == "pass" else "；资产保持维护状态，需再次维护或处置")
            ),
        )
        self.db.commit()
        return self.out(order)

    def cancel(self, order_id: str, reason: str, user: User) -> dict:
        self._require_permission(user)
        order = self._require(order_id)
        if order.state not in {"planned", "in_progress"}:
            raise StateConflict("工单已结束")
        if not (reason or "").strip():
            raise ValidationFailed("取消必须写明原因")
        asset = self.assets.get(order.asset_id)
        if order.state == "in_progress" and asset is not None and order.asset_state_before:
            asset.state = order.asset_state_before
            self.assets.bump(asset)
        before = STATES.get(order.state)
        self._close_booking(order, "cancelled")
        order.state = "cancelled"
        order.record = reason.strip()
        self.orders.bump(order)
        self.audit.record(user, "取消维护工单", order.asset_id, before=before, after="已取消", detail=reason)
        self.db.commit()
        return self.out(order)

    def _close_booking(self, order: MaintenanceOrder, state: str) -> None:
        booking = self.db.get(ResourceBooking, order.booking_id) if order.booking_id else None
        if booking is not None and booking.state in {"pending", "confirmed"}:
            booking.state = state
            if state == "done" and booking.ends_at > now():
                # 提前完工：把没用完的维护时间还给排程
                booking.ends_at = now()
            booking.row_version = int(booking.row_version or 0) + 1
