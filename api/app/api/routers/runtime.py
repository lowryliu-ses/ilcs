"""设备与集成运行时入口。

全部走服务认证：`X-Service-Source` + `X-Service-Secret`。不保留匿名兼容入口——
回传与设备控制的身份是安全契约，不是可以「以后再加」的功能。
"""
from fastapi import APIRouter

from ...schemas import BatchSignalIn, CommandEventIn, HeartbeatIn, ProposalIn, TelemetryIn
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
    """适配器契约自述。界面据此禁用设备不支持的动作。"""
    from ...adapters.registry import contract_of

    service = StationService(db, ctx)
    return [
        {"station_id": record.station_id, **contract_of(record).as_dict()}
        for record in service.adapters.list()
    ]


@router.post("/plans/{plan_id}/proposals", status_code=201)
def optimizer_proposal(plan_id: str, payload: ProposalIn, db: DbSession, ctx: ServiceCtx):
    """外部优化器提交下一轮提案。服务身份须在 plan_proposals 范围内授权该方案。"""
    return ProposalService(db, ctx).submit(plan_id, payload.model_dump())


@router.post("/batches/{batch_id}/signals")
def batch_signal(batch_id: str, payload: BatchSignalIn, db: DbSession, ctx: ServiceCtx):
    """外部系统（LIMS、仓储、上位机）发出批次业务事件。服务身份须在 batch_signals 范围内授权该事件名。"""
    return WorkflowService(db, ctx).signal(batch_id, payload.name, payload.payload, payload.event_id, None)
