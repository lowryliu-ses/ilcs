from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.context import AccessContext
from ..core.errors import PermissionDenied, StateConflict, ValidationFailed
from ..models import Alarm, User
from ..repositories.governance import AlarmRepository
from .audit_service import AuditService
from .identity_service import user_may

STATE_LABEL = {"active": "未确认", "acked": "已确认", "shelved": "已搁置", "closed": "已关闭"}
SEVERITY_LABEL = {1: "紧急", 2: "高", 3: "中", 4: "低"}


class AlarmService:
    """确认 = 人员知晓；关闭 = 异常条件消除。两者分权限，不能互相替代。"""

    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.alarms = AlarmRepository(db, ctx)
        self.audit = AuditService(db, ctx)

    def out(self, alarm: Alarm) -> dict:
        return {
            "id": alarm.id,
            "severity": alarm.severity,
            "severity_label": SEVERITY_LABEL.get(alarm.severity, str(alarm.severity)),
            "state": alarm.state,
            "state_label": STATE_LABEL.get(alarm.state, alarm.state),
            "condition_active": alarm.condition_active,
            "owner": alarm.owner,
            "source_type": alarm.source_type,
            "source_id": alarm.source_id,
            "message": alarm.message,
            "response": alarm.response,
            "shelved_until": alarm.shelved_until,
            "raised_at": alarm.raised_at.isoformat(timespec="seconds"),
            "origin": alarm.origin,
            "condition_key": alarm.condition_key,
        }

    def list(self) -> list[dict]:
        return [self.out(alarm) for alarm in self.alarms.list()]

    def raise_alarm(
        self, severity: int, source_type: str, source_id: str, message: str, response: str = "",
        owner: str = "", origin: str = "system", condition_key: str = "",
    ) -> Alarm:
        """登记报警。

        `origin` 区分设备侧条件与软件判定：软件判定的报警设备不知道它的 ID，
        条件只能由人写明原因后清除。给了 `condition_key` 就按它去重——同一异常条件持续期间
        （报警未关闭且条件未恢复）不重复报。
        """
        if condition_key:
            existing = self.alarms.open_by_condition(condition_key)
            if existing is not None:
                return existing
        from sqlalchemy.exc import IntegrityError

        for _ in range(5):
            alarm = Alarm(
                id=self.alarms.next_id(), severity=severity, source_type=source_type,
                source_id=source_id, message=message, response=response, owner=owner,
                raised_at=now(), origin=origin, condition_key=condition_key,
                org_id=self.ctx.org_id if self.ctx else "",
            )
            try:
                # 编号按现有最大号 + 1；并发撞号时在保存点里重试，不连带回滚调用方的事务
                with self.db.begin_nested():
                    self.db.add(alarm)
                    self.db.flush()
                break
            except IntegrityError:
                continue
        else:
            raise StateConflict("报警编号连续冲突，请重试")
        self.audit.record(None, "触发报警", alarm.id, before="—", after="未确认", detail=message)
        return alarm

    def resolve_condition(self, condition_key: str, detail: str) -> Alarm | None:
        """条件由系统自己判定已消除（心跳恢复、联锁解除、指令已结束）时自动复位。

        只复位条件，不改确认 / 关闭状态：报警有没有人看过、要不要关闭仍由人决定。
        """
        alarm = self.alarms.open_by_condition(condition_key)
        if alarm is None:
            return None
        alarm.condition_active = False
        self.audit.record(
            None, "条件自动恢复", alarm.id, before="异常条件持续", after="条件已恢复", detail=detail,
        )
        return alarm

    def clear_condition(self, alarm_id: str, reason: str, signature_id: str, user: User) -> dict:
        """清除软件判定报警的异常条件。

        执行异常、推进卡住这类报警是软件生成的，设备侧不可能上报「条件恢复」。
        它们由操作员写明原因并签名后清除；设备侧条件报警仍只接受设备上报。
        """
        from .identity_service import IdentityService

        if not (user_may(self.ctx, user, "alarm.close") or self.ctx.has("batch.recover")):
            raise PermissionDenied("当前角色不能清除报警条件")
        alarm = self.alarms.require(alarm_id, "报警不存在")
        if alarm.origin != "system":
            raise StateConflict(
                "设备侧条件报警只能由设备上报条件恢复，不能人工清除", code="device_alarm",
            )
        if not alarm.condition_active:
            raise StateConflict("异常条件已清除")
        if not (reason or "").strip():
            raise ValidationFailed("必须写明异常原因已如何消除", code="clear_reason_required")
        signature = IdentityService(self.db, self.ctx).consume_signature(
            signature_id, user, "清除报警条件", object_ref=alarm.id, strict=True,
        )
        alarm.condition_active = False
        self.audit.record(
            user, "清除报警条件", alarm.id, sign=True, meaning=signature.meaning,
            before="异常条件持续", after="条件已清除", signature_id=signature.id,
            detail=f"{alarm.source_type}:{alarm.source_id}；{reason.strip()}",
        )
        self.db.commit()
        return self.out(alarm)

    def ack(self, alarm_id: str, user: User) -> dict:
        if not user_may(self.ctx, user, "alarm.ack"):
            raise PermissionDenied("当前角色不能确认报警")
        alarm = self.alarms.require(alarm_id, "报警不存在")
        if alarm.state != "active":
            raise StateConflict("只有未确认报警可确认")
        alarm.state = "acked"
        self.audit.record(
            user, "确认报警", alarm.id, before="未确认", after="已确认",
            detail="确认表示人员已知晓；异常条件仍保留，批次恢复需单独评估",
        )
        self.db.commit()
        return self.out(alarm)

    def shelve(self, alarm_id: str, until: str, user: User) -> dict:
        if not user_may(self.ctx, user, "alarm.shelve"):
            raise PermissionDenied("当前角色不能搁置报警")
        alarm = self.alarms.require(alarm_id, "报警不存在")
        if alarm.state == "closed":
            raise StateConflict("已关闭报警不能搁置")
        if not until:
            raise StateConflict("搁置必须设定到期时间")
        before = STATE_LABEL.get(alarm.state, alarm.state)
        alarm.state = "shelved"
        alarm.shelved_until = until
        self.audit.record(user, "搁置报警", alarm.id, before=before, after=f"已搁置至 {until}")
        self.db.commit()
        return self.out(alarm)

    def close(self, alarm_id: str, user: User) -> dict:
        if not user_may(self.ctx, user, "alarm.close"):
            raise PermissionDenied("当前角色不能关闭报警")
        alarm = self.alarms.require(alarm_id, "报警不存在")
        if alarm.condition_active:
            raise StateConflict("设备侧异常条件未恢复，不能关闭")
        before = STATE_LABEL.get(alarm.state, alarm.state)
        alarm.state = "closed"
        self.audit.record(user, "关闭报警", alarm.id, before=before, after="已关闭", detail="设备侧条件已恢复")
        self.db.commit()
        return self.out(alarm)

    def condition_cleared(self, alarm_id: str) -> dict:
        """设备侧条件恢复事件。操作者记为设备，不改变确认状态。"""
        alarm = self.alarms.require(alarm_id, "报警不存在")
        alarm.condition_active = False
        self.audit.record(
            None, "条件恢复反馈", alarm.id, before="异常条件持续", after="条件已恢复",
            detail=f"{alarm.source_id} 上报条件恢复；报警确认状态与批次恢复需人工分别处理",
        )
        self.db.commit()
        return self.out(alarm)
