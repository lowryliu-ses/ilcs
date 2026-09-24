"""下发检查。服务端唯一裁决，前端只展示结果与阻塞原因。

每一项按步骤适用性返回三种结果：通过、阻塞、不适用。
「不适用」不是通过的近义词，它说明这个方法里没有需要检查的东西——合法空 BOM 的
方法显示「无需物料」，没有设备节点的流程不被工位许可误拦。只有「阻塞」会挡下发。
"""
from dataclasses import dataclass, field
from datetime import datetime

PASS = "pass"
WARN = "warn"
BLOCKED = "blocked"
NOT_APPLICABLE = "not_applicable"


@dataclass
class PreflightContext:
    recipe_state: str
    recipe_risk: str
    snapshot_version: str
    released_version: str
    steps_total: int
    steps_needing_station: int
    steps_allocated: int
    # 每个设备/占位步骤的资源许可结论，来自 domain/resources.evaluate_steps
    resource_checks: list[dict] = field(default_factory=list)
    reservations: list[dict] = field(default_factory=list)
    bom_items: list[dict] = field(default_factory=list)
    material_steps: int = 0
    bom_satisfied: bool = False
    expired_lots: list[str] = field(default_factory=list)
    first_station: dict | None = None
    station_alarm_active: bool = False
    gate_reasons: list[str] = field(default_factory=list)
    planned_start: datetime | None = None
    now: datetime | None = None
    expiry_min: int = 30
    # 允许比计划开始提前下发的分钟数；再早就会占用别的批次预约的设备
    early_tolerance_min: float = 15
    has_control_permission: bool = False
    role_name: str = ""
    manual_review_done: bool = False
    qualification_blockers: list[str] = field(default_factory=list)
    qualification_required: bool = True
    sop_snapshot: dict | None = None
    # 任务上游：None 表示任务没有声明依赖（不适用）；空列表表示依赖都已满足
    dependency_blockers: list[str] | None = None
    # 首工位之外的其余工位：[{id, status, cal_due, interlock, alarm}]。故障、离线、校准过期、联锁、活动报警都挡下发
    other_stations: list[dict] = field(default_factory=list)
    # 环境要求：None 表示没有步骤声明要求（不适用）
    environment_blockers: list[str] | None = None
    # 人员预占：None 表示没有人工步骤（不适用）；提醒项不挡下发
    personnel_blockers: list[str] | None = None
    personnel_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    state: str
    detail: str

    @property
    def ok(self) -> bool:
        """不适用不挡下发，所以它和通过一样算 ok。界面按 state 区分展示。"""
        return self.state != BLOCKED

    def as_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "ok": self.ok,
            "state": self.state, "detail": self.detail,
        }


def _state(ok: bool) -> str:
    return PASS if ok else BLOCKED


def evaluate(context: PreflightContext) -> list[Check]:
    checks: list[Check] = []

    # ---------- 1 发布快照 ----------
    snapshot_note = ""
    if context.released_version and context.released_version != context.snapshot_version:
        snapshot_note = f"；当前发布版本已是 v{context.released_version}，本批次仍按快照执行"
    sop_note = ""
    if context.sop_snapshot:
        sop_note = (
            f"；SOP {context.sop_snapshot.get('code', '')} "
            f"v{context.sop_snapshot.get('version', '')}"
        )
    checks.append(
        Check(
            "snapshot", "流程快照已发布且有风险评估",
            _state(bool(context.recipe_risk) and context.recipe_state == "released"),
            f"v{context.snapshot_version}；{context.recipe_risk or '缺少风险评估'}"
            f"{snapshot_note}{sop_note}",
        )
    )

    # ---------- 2 物料 ----------
    if not context.bom_items:
        detail = "该流程未定义物料需求" + (
            "，且没有消耗物料的步骤，无需投料许可" if not context.material_steps
            else "；但存在声明消耗物料的步骤，请补齐 BOM"
        )
        checks.append(
            Check(
                "material", "物料已按批次预留，批号已放行",
                NOT_APPLICABLE if not context.material_steps else BLOCKED,
                detail,
            )
        )
    else:
        lots = "、".join(f"{r['lot_id']} {r['qty']}{r['unit']}" for r in context.reservations) or "无预留"
        all_released = bool(context.reservations) and all(
            r.get("release") == "已放行" for r in context.reservations
        )
        if context.expired_lots:
            lots += f"；已过有效期或开封超期：{'、'.join(context.expired_lots)}"
        checks.append(
            Check(
                "material", "物料已按批次预留，批号已放行且在有效期内",
                _state(context.bom_satisfied and all_released and not context.expired_lots),
                lots,
            )
        )

    # ---------- 3 资源预约 ----------
    if context.steps_needing_station == 0:
        checks.append(
            Check(
                "allocation", "需要占用的步骤已预约资源", NOT_APPLICABLE,
                f"{context.steps_total} 步全部是人工 / 等待 / 审核节点，不占工位",
            )
        )
    else:
        checks.append(
            Check(
                "allocation", "需要占用的步骤已预约资源",
                _state(context.steps_allocated >= context.steps_needing_station),
                f"{context.steps_allocated}/{context.steps_needing_station} 个需占用步骤有工位与时间窗"
                f"（共 {context.steps_total} 步，含转运与清洗）",
            )
        )

    # ---------- 4 设备许可（校准、维护、容量、退役） ----------
    applicable_resources = [row for row in context.resource_checks if row.get("applicable")]
    if not applicable_resources:
        checks.append(
            Check("resource", "设备校准与占用许可", NOT_APPLICABLE, "本流程没有需要设备许可的步骤")
        )
    else:
        failing = [row for row in applicable_resources if not row.get("ok")]
        detail = (
            "；".join(
                f"第 {row['step_index'] + 1} 步「{row['step_name']}」：{'、'.join(row['reasons'])}"
                for row in failing
            )
            if failing
            else f"{len(applicable_resources)} 个设备步骤的校准、预约与容量均覆盖其执行区间"
        )
        checks.append(Check("resource", "设备校准与占用许可", _state(not failing), detail))

    # ---------- 5 首工位实际状态 ----------
    station = context.first_station
    if station is None:
        state = NOT_APPLICABLE if context.steps_needing_station == 0 else BLOCKED
        detail = "首个节点不占工位" if state == NOT_APPLICABLE else "未分配首工位"
        checks.append(Check("station", "首工位执行许可", state, detail))
    else:
        today = (context.now or datetime.utcnow()).date().isoformat()
        station_ok = (
            station.get("clean") is True
            and station.get("status") != "fault"
            and station.get("status") != "offline"
            and not context.station_alarm_active
            and (station.get("cal_due") in {"", "-"} or str(station.get("cal_due")) >= today)
            and not station.get("interlock")
        )
        detail = (
            f"{station['id']}：{'已清洗' if station.get('clean') else '未清洗'}、"
            f"校准至 {station.get('cal_due') or '—'}、"
            f"{'离线' if station.get('status') == 'offline' else ('故障' if station.get('status') == 'fault' else '状态正常')}、"
            f"{'安全联锁触发' if station.get('interlock') else '联锁未触发'}、"
            f"{'存在活动报警' if context.station_alarm_active else '无活动报警'}"
        )
        checks.append(Check("station", "首工位执行许可", _state(station_ok), detail))

    # ---------- 5b 其余工位 ----------
    if not context.other_stations:
        checks.append(Check("stations", "后续工位执行许可", NOT_APPLICABLE, "没有首工位之外的工位"))
    else:
        today = (context.now or datetime.utcnow()).date().isoformat()
        failing = []
        for row in context.other_stations:
            reasons = []
            if row.get("status") in {"fault", "offline"}:
                reasons.append("离线" if row.get("status") == "offline" else "故障")
            if row.get("cal_due") not in {"", "-", None} and str(row.get("cal_due")) < today:
                reasons.append(f"校准已于 {row.get('cal_due')} 到期")
            if row.get("interlock"):
                reasons.append("安全联锁触发")
            if row.get("alarm"):
                reasons.append("存在活动报警")
            if reasons:
                failing.append(f"{row['id']}：{'、'.join(reasons)}")
        checks.append(Check(
            "stations", "后续工位执行许可", _state(not failing),
            "；".join(failing) or f"{len(context.other_stations)} 个后续工位在线、校准有效、无联锁与活动报警"
            "（清洗状态到步骤开工前再核对）",
        ))

    # ---------- 5c 环境条件 ----------
    if context.environment_blockers is None:
        checks.append(Check("environment", "环境条件", NOT_APPLICABLE, "没有步骤声明环境要求"))
    else:
        checks.append(Check(
            "environment", "环境条件", _state(not context.environment_blockers),
            "；".join(context.environment_blockers) or "各步骤要求的环境指标都有最新读数且在范围内",
        ))

    # ---------- 5d 人员预占 ----------
    if context.personnel_blockers is None:
        checks.append(Check("personnel", "执行人时间预占", NOT_APPLICABLE, "流程没有人工步骤"))
    else:
        if context.personnel_blockers:
            state, detail = BLOCKED, "；".join(context.personnel_blockers)
        elif context.personnel_warnings:
            state, detail = WARN, "提醒：" + "；".join(context.personnel_warnings[:5])
        else:
            state, detail = PASS, "执行人在人工步骤的时间里没有别的批次、请假或培训"
        checks.append(Check("personnel", "执行人时间预占", state, detail))

    # ---------- 6 执行门 ----------
    checks.append(
        Check(
            "gate", "数据质量与公共保护", _state(not context.gate_reasons),
            "；".join(context.gate_reasons) or "遥测在线，联锁未触发",
        )
    )

    # ---------- 7 计划时间 ----------
    if context.planned_start is None:
        state = NOT_APPLICABLE if context.steps_needing_station == 0 else BLOCKED
        checks.append(
            Check("schedule", "计划开始时间", state, "无需排程的流程" if state == NOT_APPLICABLE else "未排程")
        )
    else:
        expired_min = (
            (context.now - context.planned_start).total_seconds() / 60 if context.now else None
        )
        detail = context.planned_start.isoformat(timespec="minutes")
        schedule_ok = expired_min is None or expired_min <= context.expiry_min
        early = expired_min is not None and -expired_min > context.early_tolerance_min
        if not schedule_ok:
            detail += f"，已过期 {expired_min:.0f} min，需重排"
        elif early:
            # 提前下发会占用别的批次预约在这段时间里的设备
            schedule_ok = False
            detail += (
                f"，比计划提前 {-expired_min:.0f} min，超过允许的 {context.early_tolerance_min:.0f} min；"
                f"请到点再下发或先重排"
            )
        checks.append(Check("schedule", "计划开始时间", _state(schedule_ok), detail))

    # ---------- 8 人员资质 ----------
    if not context.qualification_required:
        checks.append(
            Check("qualification", "执行人资质", NOT_APPLICABLE, "本流程没有需要资质的节点")
        )
    else:
        checks.append(
            Check(
                "qualification", "执行人资质",
                _state(not context.qualification_blockers),
                "；".join(context.qualification_blockers) or "执行人具备全部节点所需的有效资质",
            )
        )

    # ---------- 9 上游任务 ----------
    if context.dependency_blockers is None:
        checks.append(Check("upstream", "上游任务", NOT_APPLICABLE, "任务没有声明上游依赖"))
    else:
        checks.append(
            Check(
                "upstream", "上游任务", _state(not context.dependency_blockers),
                "；".join(context.dependency_blockers) or "上游任务的批次都已运行结束",
            )
        )

    # ---------- 10 操作者权限与人工核对 ----------
    checks.append(
        Check(
            "authority", "操作者权限与人工复核",
            _state(context.has_control_permission and context.manual_review_done),
            f"当前角色 {context.role_name}"
            + ("；已勾选托盘与物料复核" if context.manual_review_done else "；缺少人工复核勾选"),
        )
    )
    return checks


def blocked(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.state == BLOCKED]


def summary(checks: list[Check]) -> dict:
    return {
        "total": len(checks),
        "passed": len([c for c in checks if c.state == PASS]),
        "blocked": len([c for c in checks if c.state == BLOCKED]),
        "warn": len([c for c in checks if c.state == WARN]),
        "not_applicable": len([c for c in checks if c.state == NOT_APPLICABLE]),
    }
