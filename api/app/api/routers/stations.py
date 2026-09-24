from fastapi import APIRouter

from ...schemas import (
    AdapterPatchIn, CapabilityIn, CapabilityPatchIn, CommandVerifyIn, LimitsIn, ManualReviewIn,
    ReadinessIn, RetireIn, StationCreateIn, StationPatchIn,
)
from ...services.batch_service import BatchService
from ...services.station_service import StationService
from ..deps import Ctx, CurrentUser, DbSession, IdempotencyGuard, require

router = APIRouter(tags=["resource"])


@router.get("/stations")
def list_stations(db: DbSession, ctx: Ctx):
    return StationService(db, ctx).list_stations()


@router.get("/capabilities")
def list_capabilities(db: DbSession, ctx: Ctx):
    return StationService(db, ctx).list_capabilities()


@router.get("/islands")
def list_islands(db: DbSession, ctx: Ctx):
    return StationService(db, ctx).list_islands()


@router.get("/commands")
def command_ledger(db: DbSession, ctx: Ctx, limit: int = 50):
    return StationService(db, ctx).command_ledger(limit)


@router.patch("/stations/{station_id}/limits")
def update_limits(station_id: str, payload: LimitsIn, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    return StationService(db, ctx).update_limits(
        station_id, payload.limits, payload.signature_id, user, payload.row_version,
    )


@router.patch("/stations/{station_id}/readiness")
def set_readiness(station_id: str, payload: ReadinessIn, db: DbSession, user: CurrentUser, ctx=require("batch.control")):
    return StationService(db, ctx).set_readiness(
        station_id, payload.clean, payload.status, user, payload.row_version,
    )


@router.post("/stations", status_code=201)
def create_station(payload: StationCreateIn, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    """登记新工位。能力极限一并写入并立刻重校验受影响的流程。"""
    return StationService(db, ctx).create_station(
        payload.model_dump(exclude={"signature_id"}), payload.signature_id, user
    )


@router.patch("/stations/{station_id}")
def update_station(station_id: str, payload: StationPatchIn, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    """改台账信息。能力极限不走这里——那条路要签名。"""
    return StationService(db, ctx).update_station(
        station_id, payload.model_dump(exclude_unset=True, exclude_none=True), user
    )


@router.post("/stations/{station_id}/retire")
def retire_station(station_id: str, payload: RetireIn, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    """停用 / 启用工位。工位一律不删：历史工步分配与检查点都指向它。"""
    return StationService(db, ctx).set_station_retired(station_id, payload.retired, user)


@router.patch("/capabilities/{capability_id}")
def update_capability(capability_id: str, payload: CapabilityPatchIn, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    return StationService(db, ctx).update_capability(
        capability_id, payload.model_dump(exclude={"signature_id"}, exclude_unset=True, exclude_none=True),
        payload.signature_id, user,
    )


@router.post("/capabilities/{capability_id}/retire")
def retire_capability(capability_id: str, payload: RetireIn, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    """停用能力：新流程步骤不能再选，已有流程与批次快照不受影响。"""
    return StationService(db, ctx).set_capability_retired(capability_id, payload.retired, user)


@router.delete("/capabilities/{capability_id}")
def delete_capability(capability_id: str, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    """无工位实现且无流程引用才可删，否则只能停用。"""
    return StationService(db, ctx).delete_capability(capability_id, user)


@router.post("/commands/{command_id}/manual-review")
def command_to_manual_review(command_id: str, payload: ManualReviewIn, db: DbSession, user: CurrentUser, ctx=require("batch.control")):
    return StationService(db, ctx).mark_command_manual(command_id, payload.note, user)


@router.post("/commands/{command_id}/verify")
def verify_command(
    command_id: str, payload: CommandVerifyIn, db: DbSession, guard: IdempotencyGuard,
    user: CurrentUser, ctx=require("batch.recover"),
):
    """结果未知指令的现场核查结论：已执行 / 未执行 / 部分执行。签名负责，不自动推断。"""
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(
        BatchService(db, ctx).verify_command(
            command_id, payload.conclusion, payload.note, payload.delivered,
            payload.signature_id, user,
        )
    )


@router.post("/stations/{station_id}/adapter/reconnect")
def reconnect_adapter(station_id: str, db: DbSession, user: CurrentUser, ctx=require("batch.control")):
    return StationService(db, ctx).reconnect_adapter(station_id, user)


@router.get("/stations/{station_id}/adapter")
def adapter_detail(station_id: str, db: DbSession, ctx=require("station.edit")):
    """连接配置与凭据引用属于管理信息，不随普通工位列表下发。"""
    return StationService(db, ctx).adapter_detail(station_id)


@router.patch("/stations/{station_id}/adapter")
def update_adapter(
    station_id: str, payload: AdapterPatchIn, db: DbSession, user: CurrentUser,
    ctx=require("station.edit"),
):
    changes = payload.model_dump(
        exclude={"signature_id", "row_version"}, exclude_unset=True, exclude_none=True,
    )
    return StationService(db, ctx).update_adapter(
        station_id, changes, payload.row_version, payload.signature_id, user,
    )


@router.post("/stations/{station_id}/adapter/describe")
def describe_adapter(station_id: str, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    """读驱动自报的厂商、固件、方法目录与指令类型。工位匹配据此判断能否按某条设备方法执行。"""
    return StationService(db, ctx).describe_adapter(station_id, user)


@router.post("/stations/{station_id}/adapter/test")
def test_adapter(station_id: str, db: DbSession, ctx=require("station.edit")):
    return StationService(db, ctx).test_adapter(station_id)


@router.post("/capabilities", status_code=201)
def register_capability(payload: CapabilityIn, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    return StationService(db, ctx).register_capability(
        payload.id, payload.name, payload.params, payload.recovery, payload.stations, payload.signature_id, user
    )
