"""设备与集成运行时入口。

全部走服务认证：`X-Service-Source` + `X-Service-Secret`。不保留匿名兼容入口——
回传与设备控制的身份是安全契约，不是可以「以后再加」的功能。
"""
from fastapi import APIRouter, Response

from ...schemas import (
    AnalysisRunIn, BatchSignalIn, CommandEventIn, DatasetSnapshotIn, HeartbeatIn, ProposalIn, TelemetryIn,
)
from ...services.proposal_service import ProposalService
from ...services.station_service import StationService
from ...services.workflow_service import WorkflowService
from ..deps import DbSession, ServiceCtx

router = APIRouter(prefix="/runtime", tags=["runtime"])


@router.post("/stations/{station_id}/heartbeat")
def heartbeat(station_id: str, payload: HeartbeatIn, db: DbSession, ctx: ServiceCtx):
    """设备心跳。只能上报自己被授权的工位。"""
    return StationService(db, ctx).heartbeat(
        station_id, payload.connected, payload.site_interlock, payload.accepts_commands,
        payload.instrument_serial,
    )


@router.post("/stations/{station_id}/telemetry")
def telemetry(station_id: str, payload: TelemetryIn, db: DbSession, ctx: ServiceCtx):
    """设备遥测上报。同一 event_id 重发只入库一次；超前服务器时钟的整批拒收。"""
    return StationService(db, ctx).ingest_telemetry(station_id, payload.model_dump())


@router.post("/commands/{command_id}/events")
def command_event(command_id: str, payload: CommandEventIn, db: DbSession, ctx: ServiceCtx):
    """设备回执。绑定原 command_id；重复回执回放原结论，不二次推进。"""
    return StationService(db, ctx).command_ack(
        command_id, payload.outcome,
        {
            "device_ts": payload.device_ts.isoformat() if payload.device_ts else None,
            "quality": payload.quality,
            "delivered": payload.delivered,
            "error": payload.error,
        },
    )


@router.get("/adapters")
def adapter_contracts(db: DbSession, ctx: ServiceCtx):
    """适配器契约自述：支持的能力、保持 / 终止 / 查询 / 去重，以及驱动自报的方法目录与指令类型。只列本组织、且在该服务凭据授权范围内的工位。"""
    return StationService(db, ctx).adapter_contracts()


@router.post("/plans/{plan_id}/proposals", status_code=201)
def optimizer_proposal(plan_id: str, payload: ProposalIn, db: DbSession, ctx: ServiceCtx):
    """外部优化器提交下一轮提案。服务身份须在 plan_proposals 范围内授权该方案。"""
    return ProposalService(db, ctx).submit(plan_id, payload.model_dump())


@router.post("/plans/{plan_id}/datasets", status_code=201)
def optimizer_snapshot(plan_id: str, payload: DatasetSnapshotIn, db: DbSession, ctx: ServiceCtx):
    """外部优化器固化训练数据快照（授权同提案：plan_proposals）。"""
    return ProposalService(db, ctx).create_snapshot(plan_id, payload.model_dump())


@router.get("/plans/{plan_id}/datasets/{snapshot_id}/export.csv")
def optimizer_snapshot_export(plan_id: str, snapshot_id: str, db: DbSession, ctx: ServiceCtx):
    """外部优化器按快照取训练数据：内容固化，重复读取完全一致。"""
    service = ProposalService(db, ctx)
    service.require_service_grant(plan_id)
    return Response(
        content=service.snapshot_csv(plan_id, snapshot_id), media_type="text/csv; charset=utf-8",
        headers={"X-ILCS-Dataset": snapshot_id},
    )


@router.post("/plans/{plan_id}/analysis-runs", status_code=201)
def optimizer_analysis_run(plan_id: str, payload: AnalysisRunIn, db: DbSession, ctx: ServiceCtx):
    """外部优化器登记一次分析运行：输入快照、程序与模型版本、参数、随机种子、输出摘要。"""
    return ProposalService(db, ctx).record_run(plan_id, payload.model_dump())


@router.post("/batches/{batch_id}/signals")
def batch_signal(batch_id: str, payload: BatchSignalIn, db: DbSession, ctx: ServiceCtx):
    """外部系统（LIMS、仓储、上位机）发出批次业务事件。服务身份须在 batch_signals 范围内授权该事件名。"""
    return WorkflowService(db, ctx).signal(batch_id, payload.name, payload.payload, payload.event_id, None)
