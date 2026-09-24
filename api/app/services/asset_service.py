"""资产档案、校准与资源预约。

维护预约、人工预约和自动排程共用一套冲突判断；确认时在事务里重新校验，
不靠界面上看起来空着的时间段。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..core.clock import as_utc, now
from ..core.context import AccessContext
from ..core.errors import NotFound, StateConflict, ValidationFailed
from ..domain.resources import (
    AssetSpec, CalibrationSpec, Window, booking_blockers, calibration_blockers, governing_calibration,
)
from ..models import Asset, CalibrationRecord, ResourceBooking, User
from ..repositories.files import FileRepository
from ..repositories.batches import AllocationRepository, BatchRepository
from ..repositories.people import PersonRepository
from ..repositories.resources import (
    AssetRepository, BookingRepository, CalibrationRepository, StationRepository,
)
from .audit_service import AuditService

BOOKING_KINDS = {"maintenance", "manual", "calibration", "schedule"}
BOOKING_KIND_LABEL = {
    "maintenance": "维护", "manual": "人工预约", "calibration": "校准", "schedule": "排程占用",
}
ASSET_STATES = {"active", "maintenance", "retired"}


class AssetService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.assets = AssetRepository(db, ctx)
        self.calibrations = CalibrationRepository(db, ctx)
        self.bookings = BookingRepository(db, ctx)
        self.stations = StationRepository(db, ctx)
        self.allocations = AllocationRepository(db)
        self.batches = BatchRepository(db, ctx)
        self.people = PersonRepository(db, ctx)
        self.files = FileRepository(db, ctx)
        self.audit = AuditService(db, ctx)

    # ---------- 规格（供排程与开跑检查使用） ----------

    def spec_for(self, asset: Asset, at: datetime | None = None) -> AssetSpec:
        bookings = tuple(
            Window(row.starts_at, row.ends_at)
            for row in self.bookings.for_asset(asset.id)
            if row.state in {"pending", "confirmed"}
        )
        calibrations = tuple(
            CalibrationSpec(
                effective_from=row.effective_from, expires_at=row.expires_at, result=row.result,
                capability_scope=tuple(row.capability_scope or []),
            )
            for row in self.calibrations.for_asset(asset.id)
        )
        return AssetSpec(
            asset_id=asset.id, name=f"{asset.asset_no} {asset.name}", state=asset.state,
            capacity=max(1, asset.capacity), calibration_applicable=asset.calibration_applicable,
            calibration_exempt_reason=asset.calibration_exempt_reason,
            calibrations=calibrations, bookings=bookings,
        )

    def specs_by_station(self) -> dict[str, AssetSpec]:
        """工位 → 资产规格。多个工位映射同一资产时共享同一份容量与校准。"""
        cache: dict[str, AssetSpec] = {}
        mapping: dict[str, AssetSpec] = {}
        for station in self.stations.list():
            if not station.asset_id:
                continue
            if station.asset_id not in cache:
                asset = self.assets.get(station.asset_id)
                if not asset:
                    continue
                cache[station.asset_id] = self.spec_for(asset)
            mapping[station.id] = cache[station.asset_id]
        return mapping

    # ---------- 读 ----------

    def asset_out(self, asset: Asset, detail: bool = False) -> dict:
        stations = self.stations.for_asset(asset.id)
        records = self.calibrations.for_asset(asset.id)
        moment = now()
        spec = self.spec_for(asset)
        governing = governing_calibration(spec, "", moment)
        live = [
            row for row in records
            if governing is not None and governing.result == "pass"
            and row.result == "pass"
            and row.effective_from == governing.effective_from
            and row.expires_at is not None and row.expires_at >= moment
        ]
        owner = self.people.get(asset.owner_person_id) if asset.owner_person_id else None
        payload = {
            "id": asset.id,
            "asset_no": asset.asset_no,
            "name": asset.name,
            "model": asset.model,
            "vendor": asset.vendor,
            "serial": asset.serial,
            "firmware": asset.firmware,
            "lab_id": asset.lab_id,
            "location": asset.location,
            "state": asset.state,
            "capacity": asset.capacity,
            "calibration_applicable": asset.calibration_applicable,
            "calibration_exempt_reason": asset.calibration_exempt_reason,
            "owner_person_id": asset.owner_person_id,
            "owner_name": owner.name if owner else "",
            "note": asset.note,
            "row_version": asset.row_version,
            "station_ids": [s.id for s in stations],
            "calibration_valid": (
                not asset.calibration_applicable
                or not calibration_blockers(spec, "", Window(moment, moment + timedelta(minutes=1)))
            ),
            "calibration_due": (
                min(
                    (row.expires_at for row in live if row.expires_at is not None),
                    default=None,
                )
            ),
            "unavailable_reasons": self._unavailable_reasons(asset, spec),
        }
        if payload["calibration_due"]:
            payload["calibration_due"] = payload["calibration_due"].isoformat(timespec="minutes")
        if detail:
            payload["calibrations"] = [self.calibration_out(row) for row in records]
            payload["bookings"] = [self.booking_out(row) for row in self.bookings.for_asset(asset.id)]
            payload["audit"] = [
                {
                    "time": e.time.isoformat(timespec="seconds"), "user": e.user, "action": e.action,
                    "before": e.before, "after": e.after, "detail": e.detail,
                }
                for e in self.audit.for_target(asset.id)
            ]
        return payload

    def _unavailable_reasons(self, asset: Asset, spec: AssetSpec) -> list[str]:
        """不可用原因。界面直接显示这些，而不是一句「不可用」。"""
        reasons: list[str] = []
        if asset.state == "retired":
            reasons.append("资产已退役")
        elif asset.state == "maintenance":
            reasons.append("资产处于维护状态")
        window = Window(now(), now() + timedelta(minutes=1))
        if asset.state not in {"retired", "maintenance"}:
            reasons.extend(calibration_blockers(spec, "", window))
        current = [
            row for row in self.bookings.for_asset(asset.id)
            if row.state == "confirmed" and row.starts_at <= now() <= row.ends_at
        ]
        for row in current:
            reasons.append(
                f"{BOOKING_KIND_LABEL.get(row.kind, row.kind)}占用至 "
                f"{row.ends_at.isoformat(timespec='minutes')}"
            )
        return reasons

    def calibration_out(self, row: CalibrationRecord) -> dict:
        moment = now()
        valid = (
            row.result == "pass"
            and row.effective_from <= moment
            and row.expires_at is not None and row.expires_at >= moment
        )
        return {
            "id": row.id,
            "asset_id": row.asset_id,
            "capability_scope": row.capability_scope or [],
            "result": row.result,
            "effective_from": row.effective_from.isoformat(timespec="minutes"),
            "expires_at": row.expires_at.isoformat(timespec="minutes") if row.expires_at else None,
            "certificate_file_id": row.certificate_file_id,
            "registered_by": row.registered_by,
            "note": row.note,
            "valid_now": valid,
        }

    def booking_out(self, row: ResourceBooking) -> dict:
        asset = self.assets.get(row.asset_id) if row.asset_id else None
        return {
            "id": row.id,
            "asset_id": row.asset_id,
            "asset_name": f"{asset.asset_no} {asset.name}" if asset else "",
            "station_id": row.station_id,
            "kind": row.kind,
            "kind_label": BOOKING_KIND_LABEL.get(row.kind, row.kind),
            "starts_at": row.starts_at.isoformat(timespec="minutes"),
            "ends_at": row.ends_at.isoformat(timespec="minutes"),
            "reason": row.reason,
            "state": row.state,
            "batch_id": row.batch_id,
            "created_by": row.created_by,
            "row_version": row.row_version,
        }

    def page(self, offset: int, limit: int, keyword: str = "", state: str | None = None):
        rows, total = self.assets.page(offset, limit, keyword, state)
        return [self.asset_out(row) for row in rows], total

    def detail(self, asset_id: str) -> dict:
        asset = self.assets.get(asset_id)
        if not asset:
            raise NotFound("资产不存在")
        return self.asset_out(asset, detail=True)

    def list_bookings(self, asset_id: str = "") -> list[dict]:
        rows = self.bookings.for_asset(asset_id) if asset_id else self.bookings.live()
        return [self.booking_out(row) for row in rows]

    # ---------- 写 ----------

    def create_asset(self, payload: dict, user: User) -> dict:
        asset_no = (payload.get("asset_no") or "").strip()
        if not asset_no:
            raise ValidationFailed("资产号必填")
        if self.assets.by_no(asset_no):
            raise StateConflict(f"资产号 {asset_no} 已存在")
        if not payload.get("calibration_applicable", True) and not (
            payload.get("calibration_exempt_reason") or ""
        ).strip():
            raise ValidationFailed(
                "标记「不适用校准」必须写明理由；缺失不等同不适用",
                code="calibration_exempt_reason_required",
            )
        asset = Asset(
            asset_no=asset_no, name=payload["name"], model=payload.get("model", ""),
            vendor=payload.get("vendor", ""), firmware=payload.get("firmware", ""),
            serial=payload.get("serial", ""), lab_id=payload.get("lab_id", ""),
            owner_person_id=payload.get("owner_person_id", ""),
            location=payload.get("location", ""), state=payload.get("state", "active"),
            capacity=max(1, int(payload.get("capacity", 1))),
            calibration_applicable=payload.get("calibration_applicable", True),
            calibration_exempt_reason=payload.get("calibration_exempt_reason", ""),
            note=payload.get("note", ""),
        )
        self.assets.add(asset)
        self.audit.record(
            user, "登记资产", asset.id, before="—", after=asset.state,
            detail=f"{asset_no} {asset.name}；容量 {asset.capacity}",
            object_version=asset.row_version,
        )
        self.db.commit()
        return self.asset_out(asset)

    def update_asset(
        self, asset_id: str, changes: dict, expected_version: int | None, user: User,
    ) -> dict:
        asset = self.assets.get(asset_id)
        if not asset:
            raise NotFound("资产不存在")
        self.assets.check_version(asset, expected_version, "资产")
        if changes.get("state") and changes["state"] not in ASSET_STATES:
            raise ValidationFailed("资产状态取值不合法")
        merged_applicable = changes.get("calibration_applicable", asset.calibration_applicable)
        merged_reason = changes.get("calibration_exempt_reason", asset.calibration_exempt_reason)
        if not merged_applicable and not (merged_reason or "").strip():
            # 与登记路径同一个 code：界面按 code 分支提示，两条路不一致会让它只认一半
            raise ValidationFailed(
                "标记「不适用校准」必须写明理由",
                code="calibration_exempt_reason_required",
            )
        before = {key: getattr(asset, key) for key in changes}
        for key, value in changes.items():
            setattr(asset, key, value)
        self.assets.bump(asset)
        self.audit.record(
            user, "编辑资产", asset.id, object_version=asset.row_version,
            detail="；".join(f"{k}: {before[k]} → {v}" for k, v in changes.items()),
        )
        self.db.commit()
        return self.asset_out(asset)

    def link_station(self, asset_id: str, station_id: str, user: User) -> dict:
        asset = self.assets.get(asset_id)
        if not asset:
            raise NotFound("资产不存在")
        station = self.stations.get(station_id)
        if not station:
            raise NotFound("工位不存在")
        before = station.asset_id
        station.asset_id = asset.id
        self.audit.record(
            user, "关联工位到资产", asset.id, before=before or "—", after=station.id,
            detail=f"工位 {station.id} 共享资产 {asset.asset_no} 的容量与校准许可",
        )
        self.db.commit()
        return self.asset_out(asset, detail=True)

    def add_calibration(self, asset_id: str, payload: dict, user: User) -> dict:
        asset = self.assets.get(asset_id)
        if not asset:
            raise NotFound("资产不存在")
        effective_from = as_utc(payload.get("effective_from")) or now()
        expires_at = as_utc(payload.get("expires_at"))
        if expires_at and expires_at <= effective_from:
            raise ValidationFailed("校准到期时间必须晚于生效时间")
        result = payload.get("result", "pass")
        if result not in {"pass", "fail"}:
            raise ValidationFailed("校准结果只能是 pass 或 fail")
        if result == "pass" and asset.calibration_applicable and not expires_at:
            # 没有到期日的合格记录会永远有效，等于一张没有期限的许可
            raise ValidationFailed(
                "合格校准必须填写有效期（到期时间）", code="calibration_expiry_required",
            )
        if result == "pass" and not payload.get("certificate_file_id") and asset.calibration_applicable:
            raise ValidationFailed(
                "合格校准必须附证书文件；如该资源不适用校准，请在资产上明确标记并写明理由",
                code="certificate_required",
            )
        certificate_file_id = (payload.get("certificate_file_id") or "").strip()
        certificate = self.files.available(certificate_file_id) if certificate_file_id else None
        if certificate_file_id and certificate is None:
            raise NotFound("校准证书文件不存在、不可用或不在当前组织范围内")
        record = CalibrationRecord(
            asset_id=asset.id, capability_scope=payload.get("capability_scope") or [],
            result=result, effective_from=effective_from, expires_at=expires_at,
            certificate_file_id=certificate_file_id,
            registered_by=user.id, note=payload.get("note", ""),
        )
        self.calibrations.add(record)
        if certificate is not None:
            certificate.ref_type = "calibration"
            certificate.ref_id = record.id
        # 工位上的 cal_due 是展示字段，跟着有效校准走
        if result == "pass" and expires_at:
            for station in self.stations.for_asset(asset.id):
                station.cal_due = expires_at.date().isoformat()
        self.audit.record(
            user, "登记校准记录", asset.id, before="—", after=result,
            detail=(
                f"{'整台资产' if not record.capability_scope else '能力 ' + '、'.join(record.capability_scope)}；"
                f"有效至 {expires_at.isoformat(timespec='minutes') if expires_at else '未设'}"
            ),
        )
        self.db.commit()
        return self.calibration_out(record)

    def create_booking(self, payload: dict, user: User) -> dict:
        # 容量约束横跨多条 booking，不能只靠单行唯一约束。锁住资产后再读取
        # 现有占用，保证两个 PostgreSQL 事务不会同时看到“还有空位”。
        asset = self.assets.lock_for_update(payload["asset_id"])
        if not asset:
            raise NotFound("资产不存在")
        kind = payload.get("kind", "maintenance")
        if kind not in BOOKING_KINDS:
            raise ValidationFailed(f"占用类型只能是 {'、'.join(sorted(BOOKING_KINDS))}")
        starts_at, ends_at = payload["starts_at"], payload["ends_at"]
        if ends_at <= starts_at:
            raise ValidationFailed("结束时间必须晚于开始时间")
        window = Window(starts_at, ends_at)
        # 确认时在事务里重新算冲突，不信提交时界面上看到的空档
        spec = self.spec_for(asset)
        reasons = booking_blockers(spec, window)
        if reasons:
            raise StateConflict(
                "资源占用冲突",
                {"blocked": [{"key": "capacity", "label": reason} for reason in reasons]},
                code="booking_conflict",
            )
        booking = ResourceBooking(
            asset_id=asset.id, station_id=payload.get("station_id", ""), kind=kind,
            starts_at=starts_at, ends_at=ends_at, reason=payload.get("reason", ""),
            state=payload.get("state", "confirmed"), created_by=user.id,
        )
        self.bookings.add(booking)
        impacted = self.impacted_allocations(asset.id, window)
        self.audit.record(
            user, "创建资源占用", asset.id, before="—", after=BOOKING_KIND_LABEL.get(kind, kind),
            detail=(
                f"{starts_at:%m-%d %H:%M} → {ends_at:%m-%d %H:%M}；"
                f"{payload.get('reason', '')}；受影响工步 {len(impacted)} 个"
            ),
        )
        self.db.commit()
        return {**self.booking_out(booking), "impacted": impacted}

    def cancel_booking(self, booking_id: str, reason: str, user: User) -> dict:
        booking = self.bookings.get(booking_id)
        if not booking:
            raise NotFound("资源占用不存在")
        if booking.state in {"cancelled", "done"}:
            raise StateConflict("该占用已结束")
        if booking.kind == "schedule":
            raise StateConflict("排程产生的占用请通过重排或取消排程处理", code="schedule_owned")
        booking.state = "cancelled"
        self.bookings.bump(booking)
        self.audit.record(
            user, "取消资源占用", booking.asset_id, before="confirmed", after="cancelled",
            detail=reason,
        )
        self.db.commit()
        return self.booking_out(booking)

    def impacted_allocations(self, asset_id: str, window: Window) -> list[dict]:
        """维护插入会影响哪些已排程工步。不静默移动正在执行或不可中断的步骤。"""
        station_ids = {s.id for s in self.stations.for_asset(asset_id)}
        rows = []
        for batch in self.batches.active():
            for allocation in self.allocations.for_batch(batch.id):
                if allocation.station_id not in station_ids:
                    continue
                if not Window(allocation.starts_at, allocation.ends_at).overlaps(window):
                    continue
                executing = batch.state == "running" and allocation.step_index == batch.current_step
                rows.append(
                    {
                        "batch_id": batch.id,
                        "batch_state": batch.state,
                        "step_index": allocation.step_index,
                        "station_id": allocation.station_id,
                        "starts_at": allocation.starts_at.isoformat(timespec="minutes"),
                        "ends_at": allocation.ends_at.isoformat(timespec="minutes"),
                        "executing": executing,
                        "action": "需操作员确认重排" if not executing else "正在执行，不自动移动",
                    }
                )
        return rows
