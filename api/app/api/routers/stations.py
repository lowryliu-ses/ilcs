from fastapi import APIRouter
from fastapi.responses import Response

from ...schemas import (
    AcceptanceRequestIn, AcceptanceWaiveIn, AdapterConfigCheckIn, AdapterCreateIn, AdapterPatchIn, CapabilityIn,
    CapabilityPatchIn, CommandVerifyIn, IslandIn, LimitsIn, ManualReviewIn, PointWriteIn, ReadinessIn, RetireIn,
    StationCreateIn, StationPatchIn,
)
from ...services.acceptance_service import AcceptanceService, run_out
from ...services.batch_service import BatchService
from ...services.point_service import PointService
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


@router.put("/islands/{island_id}")
def name_island(island_id: int, payload: IslandIn, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    return StationService(db, ctx).name_island(island_id, payload.name, user)


@router.get("/commands")
def command_ledger(db: DbSession, ctx: Ctx, limit: int = 50):
    return StationService(db, ctx).command_ledger(limit)


@router.patch("/stations/{station_id}/limits")
def update_limits(station_id: str, payload: LimitsIn, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    return StationService(db, ctx).update_limits(
        station_id, payload.limits, payload.signature_id, user, payload.row_version, remove=payload.remove,
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
    """停用 / 启用工位。用过的工位不删：历史工步分配、指令与检查点都指向它。"""
    return StationService(db, ctx).set_station_retired(station_id, payload.retired, user)


@router.get("/stations/{station_id}/delete-blockers")
def station_delete_blockers(station_id: str, db: DbSession, ctx=require("station.edit")):
    """删之前先看为什么不能删：没停用、排过工步、发过指令、做过接入验收的都列出来。"""
    return StationService(db, ctx).delete_blockers(station_id)


@router.delete("/stations/{station_id}")
def delete_station(station_id: str, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    """删掉登记错了、从没用过的工位（连同适配器）。先停用；用过的只能停用。"""
    return StationService(db, ctx).delete_station(station_id, user)


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


@router.post("/stations/{station_id}/adapter", status_code=201)
def create_adapter(
    station_id: str, payload: AdapterCreateIn, db: DbSession, user: CurrentUser, ctx=require("station.edit"),
):
    """给还没接设备的工位登记适配器（登记工位时没填协议的，之后在这里接入）。已接入的走 PATCH。"""
    return StationService(db, ctx).create_adapter(
        station_id, payload.model_dump(exclude={"signature_id"}, exclude_unset=True), payload.signature_id, user,
    )


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


@router.get("/drivers")
def list_drivers(db: DbSession, ctx=require("station.edit"), station_id: str | None = None):
    """已登记的驱动：配置项说明（界面按它出表单）与起步模板（给了工位就按它的能力极限生成）。"""
    return StationService(db, ctx).drivers(station_id)


@router.post("/stations/{station_id}/adapter/check")
def check_adapter_config(station_id: str, payload: AdapterConfigCheckIn, db: DbSession, ctx=require("station.edit")):
    """保存之前先检查一份配置：按驱动登记的字段查缺项与类型，再构造一次驱动实例（不保存、不连设备）。"""
    return StationService(db, ctx).check_adapter_config(station_id, payload.model_dump())


@router.get("/stations/{station_id}/adapter/templates")
def adapter_template_options(station_id: str, db: DbSession, ctx=require("station.edit")):
    """这台工位能套用的设备接入模板：已发布的，适用型号一致的排在前面。"""
    return StationService(db, ctx).template_options(station_id)


@router.get("/stations/{station_id}/adapter/acceptance")
def list_acceptance(station_id: str, db: DbSession, ctx=require("station.edit"), limit: int = 20):
    """这台设备的接入验收记录与验收闸门（还欠什么级别）。"""
    return AcceptanceService(db, ctx).list_for_station(station_id, limit)


@router.post("/stations/{station_id}/adapter/acceptance", status_code=201)
def request_acceptance(
    station_id: str, payload: AcceptanceRequestIn, db: DbSession, user: CurrentUser, ctx=require("station.edit"),
):
    """申请接入验收：登记一条排队记录，由执行器执行（执行器是唯一驱动设备的进程）。"""
    return AcceptanceService(db, ctx).request(station_id, payload.model_dump(), user)


@router.post("/stations/{station_id}/adapter/acceptance/waive", status_code=201)
def waive_acceptance(
    station_id: str, payload: AcceptanceWaiveIn, db: DbSession, user: CurrentUser, ctx=require("station.edit"),
):
    """签名放行：检查清单证明不了的设备（不支持状态查询、要现场摆位的动作），现场核对后由人放行，放行记录存档。"""
    return AcceptanceService(db, ctx).waive(station_id, payload.reason, payload.signature_id, user)


@router.get("/acceptance-runs/{run_id}")
def acceptance_run(run_id: str, db: DbSession, ctx=require("station.edit")):
    return run_out(AcceptanceService(db, ctx).get(run_id), full=True)


@router.get("/acceptance-runs/{run_id}/report.md")
def acceptance_report(run_id: str, db: DbSession, ctx=require("station.edit")):
    run = AcceptanceService(db, ctx).get(run_id)
    return Response(
        content=run.report_md or f"# 设备接入验收：{run.station_id}\n\n{run.state}：{run.error or '还没有结论'}\n",
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="acceptance-{run.station_id}-{run.id}.md"'},
    )


@router.post("/acceptance-runs/{run_id}/cancel")
def cancel_acceptance(run_id: str, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    return AcceptanceService(db, ctx).cancel(run_id, user)


@router.get("/stations/{station_id}/adapter/points")
def read_points(station_id: str, db: DbSession, ctx=require("station.edit")):
    """按点表逐个读设备点位（只读、不动设备）。只读写点位的设备不配能力映射也能用；某个点读不到只在那一行写明。"""
    return PointService(db, ctx).read(station_id)


@router.get("/stations/{station_id}/adapter/point-writes")
def list_point_writes(station_id: str, db: DbSession, ctx=require("station.edit"), limit: int = 20):
    return PointService(db, ctx).writes(station_id, limit)


@router.post("/stations/{station_id}/adapter/points/{point}/write", status_code=201)
def write_point(
    station_id: str, point: str, payload: PointWriteIn, db: DbSession, user: CurrentUser, ctx=require("device.write"),
):
    """申请手动写一个点（点表里声明了可写的点）：签名、写明原因，登记一条排队记录，由执行器先读、写、再回读。"""
    return PointService(db, ctx).request_write(
        station_id, point, payload.value, payload.reason, payload.signature_id, user,
    )


@router.post("/point-writes/{write_id}/cancel")
def cancel_point_write(write_id: str, db: DbSession, user: CurrentUser, ctx=require("device.write")):
    return PointService(db, ctx).cancel(write_id, user)


@router.post("/stations/{station_id}/adapter/test")
def test_adapter(station_id: str, db: DbSession, ctx=require("station.edit")):
    return StationService(db, ctx).test_adapter(station_id)


@router.post("/capabilities", status_code=201)
def register_capability(payload: CapabilityIn, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    return StationService(db, ctx).register_capability(
        payload.id, payload.name, payload.params, payload.recovery, payload.stations, payload.signature_id, user,
        param_specs={key: spec.model_dump() for key, spec in payload.param_specs.items()},
    )
