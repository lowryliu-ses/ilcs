"""人员资质判据。

分配任务时按预计执行时间校验，实际开始与恢复时再校验一次——资质会在这两个时刻之间
到期或被撤销。到期、撤销、离岗、停用账号都阻止新的受控操作；管理员也不例外。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class QualificationSpec:
    scope_kind: str  # capability | sop | safety
    scope_ref: str
    label: str
    effective_from: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    def valid_at(self, moment: datetime) -> bool:
        if self.revoked_at is not None and self.revoked_at <= moment:
            return False
        if self.effective_from > moment:
            return False
        if self.expires_at is not None and self.expires_at < moment:
            return False
        return True


@dataclass(frozen=True)
class PersonSpec:
    person_id: str
    name: str
    employment_state: str
    account_active: bool
    qualifications: tuple[QualificationSpec, ...] = ()

    @property
    def employable(self) -> bool:
        return self.employment_state == "on_duty" and self.account_active


def blockers_for(
    person: PersonSpec | None,
    requirements: list[tuple[str, str, str]],
    moment: datetime,
) -> list[str]:
    """requirements 是 (scope_kind, scope_ref, 显示名) 三元组。返回阻塞理由。"""
    if person is None:
        return ["执行人没有关联的人员档案，无法校验资质"]
    reasons: list[str] = []
    if not person.account_active:
        reasons.append(f"{person.name} 的账号已停用")
    if person.employment_state != "on_duty":
        reasons.append(f"{person.name} 当前不在岗（{person.employment_state}）")
    for scope_kind, scope_ref, label in requirements:
        matched = [
            row for row in person.qualifications
            if row.scope_kind == scope_kind and row.scope_ref == scope_ref
        ]
        if not matched:
            reasons.append(f"{person.name} 缺少资质：{label}")
            continue
        if not any(row.valid_at(moment) for row in matched):
            latest = max(matched, key=lambda row: row.effective_from)
            if latest.revoked_at is not None:
                reasons.append(f"{person.name} 的{label}资质已撤销")
            elif latest.expires_at is not None and latest.expires_at < moment:
                reasons.append(
                    f"{person.name} 的{label}资质已于 "
                    f"{latest.expires_at.date().isoformat()} 到期"
                )
            else:
                reasons.append(f"{person.name} 的{label}资质在该时间点尚未生效")
    return reasons


def requirements_for_steps(steps: list[dict], capability_names: dict[str, str]) -> list[tuple[str, str, str]]:
    """从步骤推出资质要求：设备步骤要能力资质，声明了 SOP 的步骤要 SOP 资质。"""
    from .steps import DEVICE, kind_of

    requirements: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for step in steps or []:
        if kind_of(step) == DEVICE:
            capability = step.get("cap") or ""
            if capability and ("capability", capability) not in seen:
                seen.add(("capability", capability))
                requirements.append(
                    ("capability", capability, f"设备操作「{capability_names.get(capability, capability)}」")
                )
        required = (step.get("qualification") or {})
        for kind in ("sop", "safety"):
            ref = required.get(kind)
            if ref and (kind, ref) not in seen:
                seen.add((kind, ref))
                requirements.append((kind, ref, f"{'SOP' if kind == 'sop' else '安全操作'} {ref}"))
    return requirements


def running_change_policy(has_running_device_step: bool) -> dict:
    """运行中人员资质变化的既定策略。

    不自动执行物理急停：那是设备与已批准恢复规则的事。系统做的是产生告警并阻止
    下一个受控操作，而不是替现场决定怎么停。
    """
    return {
        "emergency_stop": False,
        "raise_alarm": True,
        "block_next_controlled_action": True,
        "note": (
            "已运行设备不自动急停；产生告警并阻止下一受控操作，"
            "现场动作遵循设备与已批准的恢复规则"
            if has_running_device_step
            else "当前无在跑设备步骤，仅阻止后续受控操作"
        ),
    }
