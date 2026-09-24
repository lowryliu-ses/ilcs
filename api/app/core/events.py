"""变更通知：事务提交时经 PostgreSQL NOTIFY 广播「哪类对象变了」。

两个频道，用途不同：

- `ilcs_queue`：执行器唤醒。新指令入队、新推进事件产生时通知，执行器不必等满轮询周期。
- `ilcs_events`：界面推送。按组织广播变更主题（批次、指令、报警、工位、排程……），
  浏览器据此立刻重取对应数据，轮询只作兜底。

刻意的取舍：

1. **只广播「什么变了」，不广播数据本身。** 通知不经权限过滤就发给同组织的所有连接，
   载荷里只有主题与对象 ID；真正的数据仍由客户端带着自己的令牌去取，权限与作用域照旧。
2. **NOTIFY 与业务写入同一个事务。** 在 `before_commit` 里发，PostgreSQL 只在提交成功时
   投递、回滚时丢弃，所以不会出现「通知说变了、库里其实没变」。幂等边界把领域服务的
   `commit` 收敛成 `flush` 时不会提前发——只有真正提交的那一次发。
3. **通知可以丢。** 连接断开期间的通知不补发；客户端重连后整体重取一次。通知只是
   「该刷新了」的提示，正确性从不依赖它。
"""
from __future__ import annotations

import itertools
import json
from typing import Any

from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session

QUEUE_CHANNEL = "ilcs_queue"
EVENTS_CHANNEL = "ilcs_events"
ALL_ORGS = "*"
_PENDING = "ilcs_change_topics"
_WAKE = "ilcs_queue_wake"
_OUTBOX = "ilcs_webhook_events"

# 对外（Webhook）发布的业务事件主题。只发「发生了什么」与少量定位字段，详情由接收方带服务凭据来取
WEBHOOK_TOPICS = {
    "batch.created": "批次建立",
    "batch.state_changed": "批次状态变化",
    "step.state_changed": "步骤状态变化",
    "exception.opened": "异常登记",
    "exception.resolved": "异常收尾",
    "alarm.raised": "报警触发",
    "schedule.proposal_created": "生成重排建议",
    "schedule.proposal_decided": "重排建议被确认 / 驳回",
    "report.published": "报告发布",
    "batch.signal_received": "收到批次业务事件",
    "flow.notify": "流程中的消息通知节点",
}
# NOTIFY 载荷上限 8000 字节；一个事务改动太多对象时只发主题，不列 ID
_MAX_IDS = 40

# 只改这些字段的更新不值得推送：设备心跳每几秒一次，推出去只会让所有界面空转重取
_QUIET_FIELDS = {
    "Adapter": {"last_heartbeat", "updated_at", "note", "dedup_count"},
    "Command": {"updated_at"},
}


def _topics_of(obj: Any) -> list[tuple[str, str, str]]:
    """对象 → [(主题, 组织, 关联 ID)]。未列出的模型不推送。"""
    name = type(obj).__name__
    org = getattr(obj, "org_id", "") or ALL_ORGS
    if name == "Batch":
        return [("batches", org, obj.id)]
    if name == "StepRun":
        return [("batches", org, obj.batch_id or "")]
    if name == "Command":
        found = [("commands", org, obj.batch_id or ""), ("stations", ALL_ORGS, obj.station_id or "")]
        if getattr(obj, "type", "") == "transfer":
            found.append(("labware", org, getattr(obj, "labware_id", "") or ""))
        return found
    if name == "Checkpoint":
        return [("batches", org, getattr(obj, "batch_id", "") or "")]
    if name == "Alarm":
        return [("alarms", org, obj.id)]
    if name in {"Station", "Adapter"}:
        # 工位与适配器是全站共享的物理资源，执行门也由它们决定
        return [("stations", ALL_ORGS, getattr(obj, "station_id", None) or obj.id)]
    if name == "Allocation":
        return [("schedule", ALL_ORGS, obj.batch_id)]
    if name == "Telemetry":
        return [("telemetry", ALL_ORGS, obj.batch_id or "")]
    if name in {"Labware", "LabwareMove"}:
        return [("labware", org, getattr(obj, "labware_id", None) or obj.id)]
    if name == "Location":
        return [("labware", ALL_ORGS, obj.id)]
    return []


def _quiet_update(obj: Any) -> bool:
    quiet = _QUIET_FIELDS.get(type(obj).__name__)
    if not quiet:
        return False
    state = inspect(obj)
    changed = {attr.key for attr in state.attrs if attr.history.has_changes()}
    return bool(changed) and changed <= quiet


def _wakes_executor(obj: Any, is_new: bool) -> bool:
    name = type(obj).__name__
    if name == "Command":
        # 新入队的指令；或转运完成——等着它的设备动作这时才能投递
        state = getattr(obj, "state", "")
        return state == "sent" or (getattr(obj, "type", "") == "transfer" and state == "done")
    if name == "WorkflowEvent":
        return getattr(obj, "state", "") == "pending"
    if name == "WebhookDelivery":
        return is_new
    return False


def _changed(obj: Any, attr: str) -> tuple[Any, Any] | None:
    history = inspect(obj).attrs[attr].history
    if not history.has_changes():
        return None
    old = history.deleted[0] if history.deleted else None
    new = history.added[0] if history.added else None
    return (old, new) if old != new else None


_TOKENS = itertools.count(1)


def _token(transaction) -> int:
    """事务的稳定标识。不用 id()：保存点结束后 CPython 会复用它的 id，后一个保存点回滚时会误删前一个的事件。"""
    token = getattr(transaction, "_ilcs_token", None)
    if token is None:
        token = next(_TOKENS)
        transaction._ilcs_token = token
    return token


def _chain(session: Session) -> tuple[int, ...]:
    """当前事务从最内层保存点到根事务的链。保存点回滚时，登记在它（及其内层）里的对外事件一并作废。"""
    current = session.get_nested_transaction() or session.get_transaction()
    chain: list[int] = []
    while current is not None:
        chain.append(_token(current))
        current = current.parent
    return tuple(chain)


def publish(session: Session, org: str, topic: str, object_type: str, object_id: str, data: dict | None = None) -> None:
    """登记一条对外事件，随本事务提交写进发件箱；事务（或它所在的保存点）回滚就不发。"""
    session.info.setdefault(_OUTBOX, []).append({
        "org": org, "topic": topic, "object": {"type": object_type, "id": object_id}, "data": data or {},
        "chain": _chain(session),
    })


def _business_events(session: Session, obj: Any, is_new: bool) -> None:
    """模型变化 → 对外业务事件。只认少数几种有业务含义的转换，心跳、时间戳这类不发。"""
    name = type(obj).__name__
    org = getattr(obj, "org_id", "") or ""
    if not org:
        return
    if name == "Batch":
        if is_new:
            publish(session, org, "batch.created", "batch", obj.id, {"state": obj.state, "recipe_id": obj.recipe_id})
        elif (change := _changed(obj, "state")):
            publish(session, org, "batch.state_changed", "batch", obj.id, {"from": change[0], "to": change[1]})
    elif name == "StepRun":
        change = (None, obj.state) if is_new else _changed(obj, "state")
        if change and change[1] not in {"pending"}:
            publish(session, org, "step.state_changed", "step_run", obj.id, {
                "batch_id": obj.batch_id, "step_id": obj.step_id, "kind": obj.kind, "attempt": obj.attempt,
                "from": change[0], "to": change[1],
            })
    elif name == "ExceptionEvent":
        if is_new:
            publish(session, org, "exception.opened", "exception", obj.id, {
                "category": obj.category, "batch_id": obj.batch_id, "station_id": obj.station_id, "state": obj.state,
            })
        if (change := (None, obj.state) if is_new else _changed(obj, "state")) and change[1] in {
            "auto_resolved", "resolved", "closed",
        }:
            publish(session, org, "exception.resolved", "exception", obj.id, {
                "category": obj.category, "batch_id": obj.batch_id, "state": change[1],
            })
    elif name == "Alarm" and is_new:
        publish(session, org, "alarm.raised", "alarm", obj.id, {
            "severity": obj.severity, "category": obj.category, "source_type": obj.source_type, "source_id": obj.source_id,
        })
    elif name == "ScheduleProposal":
        if is_new:
            publish(session, org, "schedule.proposal_created", "schedule_proposal", obj.id, {
                "trigger": obj.trigger, "batch_ids": list(obj.batch_ids or []),
            })
        elif (change := _changed(obj, "state")) and change[1] in {"applied", "dismissed"}:
            publish(session, org, "schedule.proposal_decided", "schedule_proposal", obj.id, {"state": change[1]})
    elif name == "ReportVersion" and not is_new and (change := _changed(obj, "state")) and change[1] == "published":
        publish(session, org, "report.published", "report_version", obj.id, {"report_id": getattr(obj, "report_id", "")})
    elif name == "BatchSignal" and is_new:
        publish(session, org, "batch.signal_received", "batch", obj.batch_id, {"name": obj.name, "signal_id": obj.id})


def _collect(session: Session, _flush_context) -> None:
    topics: dict[tuple[str, str], set[str]] = session.info.setdefault(_PENDING, {})
    for collection, is_new, is_dirty in (
        (session.new, True, False), (session.dirty, False, True), (session.deleted, False, False),
    ):
        for obj in collection:
            if is_dirty and _quiet_update(obj):
                continue
            for topic, org, ident in _topics_of(obj):
                topics.setdefault((topic, org), set()).add(ident or "")
                if topic == "stations" and type(obj).__name__ != "Command":
                    topics.setdefault(("gate", ALL_ORGS), set())
            if _wakes_executor(obj, is_new):
                session.info[_WAKE] = True
            if collection is not session.deleted:
                _business_events(session, obj, is_new)


def _outbox(session: Session) -> None:
    """把本事务登记的对外事件写进发件箱：给每个订阅了该主题的启用订阅排一条投递。同一事务提交。"""
    events = session.info.pop(_OUTBOX, None) or []
    if not events:
        return
    from ..models import WebhookDelivery, WebhookSubscription
    from ..models.base import uid

    orgs = {event["org"] for event in events}
    subscriptions = session.query(WebhookSubscription).filter(
        WebhookSubscription.org_id.in_(list(orgs)), WebhookSubscription.enabled.is_(True),
    ).all()
    if not subscriptions:
        return
    from .clock import now

    moment = now()
    for event in events:
        event_id = uid()
        payload = {
            "event_id": event_id, "topic": event["topic"], "org_id": event["org"],
            "occurred_at": moment.isoformat(timespec="seconds") + "Z", "object": event["object"], "data": event["data"],
        }
        for subscription in subscriptions:
            if subscription.org_id != event["org"]:
                continue
            wanted = set(subscription.topics or [])
            if "*" not in wanted and event["topic"] not in wanted:
                continue
            session.add(WebhookDelivery(
                org_id=event["org"], subscription_id=subscription.id, event_id=event_id, topic=event["topic"],
                payload=payload, next_attempt_at=moment,
            ))
    session.flush()


def _emit(session: Session) -> None:
    # before_commit 发生在提交前的最后一次自动 flush 之前；先 flush，才能收齐这次提交的所有变更
    session.flush()
    _outbox(session)
    if session.info.get("ilcs_notify_disabled"):
        session.info.pop(_PENDING, None)
        session.info.pop(_WAKE, None)
        return
    topics = session.info.pop(_PENDING, None) or {}
    wake = session.info.pop(_WAKE, False)
    if not topics and not wake:
        return
    connection = session.connection()
    if connection.dialect.name != "postgresql":
        return
    for (topic, org), ids in topics.items():
        clean = sorted(value for value in ids if value)
        payload = {"topic": topic, "org": org, "ids": clean[:_MAX_IDS] if len(clean) <= _MAX_IDS else []}
        connection.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": EVENTS_CHANNEL, "payload": json.dumps(payload, ensure_ascii=False)},
        )
    if wake:
        connection.execute(text("SELECT pg_notify(:channel, '')"), {"channel": QUEUE_CHANNEL})


def _discard(session: Session, *_args) -> None:
    """真正的整体回滚才清空。保存点回滚同样会触发 after_rollback——那时只该丢保存点自己的事件
    （由 `_discard_savepoint` 按事务链过滤），外层事务里已登记的通知与对外事件都要保留。"""
    if session.in_nested_transaction():
        return
    session.info.pop(_PENDING, None)
    session.info.pop(_WAKE, None)
    session.info.pop(_OUTBOX, None)


def _discard_savepoint(session: Session, previous_transaction) -> None:
    """保存点回滚：只丢掉在它里面登记的对外事件，外层事务里已登记的保留。"""
    events = session.info.get(_OUTBOX)
    if not events:
        return
    marker = _token(previous_transaction)
    session.info[_OUTBOX] = [event for event in events if marker not in event.get("chain", ())]


def install(session_class: type[Session]) -> None:
    """在会话类上挂钩。重复调用无副作用。"""
    if getattr(session_class, "_ilcs_events_installed", False):
        return
    event.listen(session_class, "after_flush", _collect)
    event.listen(session_class, "before_commit", _emit)
    event.listen(session_class, "after_rollback", _discard)
    event.listen(session_class, "after_soft_rollback", _discard_savepoint)
    session_class._ilcs_events_installed = True
