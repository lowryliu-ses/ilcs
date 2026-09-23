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

import json
from typing import Any

from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session

QUEUE_CHANNEL = "ilcs_queue"
EVENTS_CHANNEL = "ilcs_events"
ALL_ORGS = "*"
_PENDING = "ilcs_change_topics"
_WAKE = "ilcs_queue_wake"
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
    return False


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


def _emit(session: Session) -> None:
    if session.info.get("ilcs_notify_disabled"):
        return
    # before_commit 发生在提交前的最后一次自动 flush 之前；先 flush，才能收齐这次提交的所有变更
    session.flush()
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
    session.info.pop(_PENDING, None)
    session.info.pop(_WAKE, None)


def install(session_class: type[Session]) -> None:
    """在会话类上挂钩。重复调用无副作用。"""
    if getattr(session_class, "_ilcs_events_installed", False):
        return
    event.listen(session_class, "after_flush", _collect)
    event.listen(session_class, "before_commit", _emit)
    event.listen(session_class, "after_rollback", _discard)
    session_class._ilcs_events_installed = True
