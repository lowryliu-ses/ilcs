"""显式初始化数据。

幂等：已存在的行不覆盖，缺失的补齐。只在显式初始化或测试环境执行——
应用启动不再自动播种（见 `core/schema.py` 与 `scripts/migrate.py seed`）。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from ..core.clock import now, today_iso
from ..core.security import hash_password, hash_secret
from ..models import (
    Adapter, Alarm, Asset, CalibrationRecord, Capability, Island, Lot, Material, Membership,
    MetricDefinition, Organization, Person, Plan, PlanVersion, Qualification, Recipe,
    ServiceIdentity, Sop,
    SopVersion, Station, User, WasteTank,
)
from . import data


def seed(
    db: Session,
    org_id: str = "ORG-001",
    org_code: str = "MAIN",
    org_name: str = "本部电池实验室",
    org_timezone: str = "Asia/Shanghai",
    master_only: bool = False,
    initial_password: str | None = None,
    force_password_change: bool = False,
    include_service_identities: bool = True,
) -> dict:
    """播种。`master_only=True` 是正式最小初始化，只创建：

    - 一个由参数明确指定的组织与默认实验室；
    - 一个管理员账号及其组织成员关系。

    演示账号、能力、工位、适配器、方法、指标、物料、批号、期初库存、资质、校准、
    SOP、方案与报警一律不进入正式库。它们必须按真实资料从治理与业务页面建立。
    """
    summary: dict[str, int] = {}
    stamp = now()

    # ---------- 组织、实验室 ----------
    organization_rows = (
        ({
            "id": org_id, "code": org_code, "name": org_name,
            "timezone": org_timezone, "note": "正式最小初始化；业务主数据待受控录入",
        },)
        if master_only else (data.ORGANIZATION, data.ISOLATION_ORGANIZATION)
    )
    for row in organization_rows:
        if not db.get(Organization, row["id"]):
            db.add(Organization(created_at=stamp, state="active", **row))
            summary["organizations"] = summary.get("organizations", 0) + 1
    db.flush()
    lab_rows = (
        ((f"{org_id}-LAB-1", org_id, f"{org_name}默认实验室"),)
        if master_only else data.LABS
    )
    for lab_id, lab_org, name in lab_rows:
        from ..models import Lab

        if not db.get(Lab, lab_id):
            db.add(
                Lab(
                    id=lab_id,
                    org_id=lab_org,
                    name=name,
                    timezone=org_timezone if master_only else "Asia/Shanghai",
                )
            )

    # ---------- 账号与成员关系 ----------
    users: dict[str, User] = {}
    user_rows = (
        [row for row in data.USERS if row[0] == "admin"] if master_only else data.USERS
    )
    for username, display_name, role, password in user_rows:
        password = initial_password or password
        user = db.query(User).filter(User.username == username).first()
        if not user:
            user = User(
                username=username, display_name=display_name, role=role, roles=[role],
                password_hash=hash_password(password), state="active",
                must_change_password=force_password_change,
            )
            db.add(user)
            db.flush()
            summary["users"] = summary.get("users", 0) + 1
        users[username] = user
        # 成员关系才是访问范围的依据；只加入本部组织，不加入隔离组织
        existing = (
            db.query(Membership)
            .filter(Membership.org_id == org_id, Membership.user_id == user.id)
            .first()
        )
        if not existing:
            db.add(
                Membership(
                    org_id=org_id, user_id=user.id, state="active",
                    default_lab_id=f"{org_id}-LAB-1", granted_at=stamp,
                )
            )

    # ---------- 服务身份 ----------
    # 正式最小初始化必须无服务身份；即使调用方误传 True 也不能放宽。
    for row in data.SERVICE_IDENTITIES if include_service_identities and not master_only else []:
        if not db.query(ServiceIdentity).filter(ServiceIdentity.source == row["source"]).first():
            db.add(
                ServiceIdentity(
                    org_id=org_id, source=row["source"], name=row["name"],
                    secret_hash=hash_secret(row["secret"]), scopes=row["scopes"],
                    state="active", created_at=stamp,
                )
            )
            summary["service_identities"] = summary.get("service_identities", 0) + 1

    # ---------- 主数据 ----------
    for capability_id, name, params, recovery in (
        data.CAPABILITIES if not master_only else []
    ):
        if not db.get(Capability, capability_id):
            db.add(Capability(id=capability_id, name=name, params=params, recovery=recovery))

    for island_id, name in (data.ISLANDS if not master_only else []):
        if not db.get(Island, island_id):
            db.add(Island(id=island_id, name=name))

    for row in (data.STATIONS if not master_only else []):
        if not db.get(Station, row["id"]):
            db.add(Station(org_id=org_id, **row))
    db.flush()

    for station_id, protocol, version, note in (
        data.ADAPTERS if not master_only else []
    ):
        if not db.get(Adapter, station_id):
            db.add(
                Adapter(
                    station_id=station_id, protocol=protocol, version=version, note=note,
                    connected=True, accepts_commands=True, last_heartbeat=stamp,
                    kind="simulation",
                )
            )

    # ---------- 人员与资质 ----------
    people: dict[str, Person] = {}
    for row in (data.PEOPLE if not master_only else []):
        person = (
            db.query(Person)
            .filter(Person.org_id == org_id, Person.code == row["code"])
            .first()
        )
        if not person:
            account = users.get(row["username"])
            person = Person(
                org_id=org_id, code=row["code"], name=row["name"], title=row["title"],
                contact=row["contact"], lab_id=f"{org_id}-LAB-1",
                employment_state="on_duty", user_id=account.id if account else "",
                created_at=stamp, updated_at=stamp,
            )
            db.add(person)
            db.flush()
            summary["people"] = summary.get("people", 0) + 1
        people[row["code"]] = person

    for person_code, scope_kind, scope_ref, valid_days in (
        data.QUALIFICATIONS if not master_only else []
    ):
        person = people.get(person_code)
        if not person:
            continue
        existing = (
            db.query(Qualification)
            .filter(
                Qualification.person_id == person.id,
                Qualification.scope_kind == scope_kind,
                Qualification.scope_ref == scope_ref,
            )
            .first()
        )
        if existing:
            continue
        db.add(
            Qualification(
                org_id=org_id, person_id=person.id, scope_kind=scope_kind, scope_ref=scope_ref,
                label="", granted_by=users["admin"].id if "admin" in users else "",
                effective_from=stamp - timedelta(days=30),
                expires_at=stamp + timedelta(days=valid_days), created_at=stamp,
            )
        )
        summary["qualifications"] = summary.get("qualifications", 0) + 1

    # ---------- 资产与校准 ----------
    for row in (data.ASSETS if not master_only else []):
        asset = (
            db.query(Asset)
            .filter(Asset.org_id == org_id, Asset.asset_no == row["asset_no"])
            .first()
        )
        if not asset:
            asset = Asset(
                org_id=org_id, asset_no=row["asset_no"], name=row["name"], model=row["model"],
                serial=row["serial"], lab_id=f"{org_id}-LAB-1", location=row["location"],
                state="active", capacity=row.get("capacity", 1),
                calibration_applicable=row.get("calibration_applicable", True),
                calibration_exempt_reason=row.get("calibration_exempt_reason", ""),
                created_at=stamp,
            )
            db.add(asset)
            db.flush()
            summary["assets"] = summary.get("assets", 0) + 1
        for station_id in row["stations"]:
            station = db.get(Station, station_id)
            if station is not None and not station.asset_id:
                station.asset_id = asset.id
        if row.get("cal_days") and not db.query(CalibrationRecord).filter(
            CalibrationRecord.asset_id == asset.id
        ).first():
            db.add(
                CalibrationRecord(
                    org_id=org_id, asset_id=asset.id, capability_scope=[], result="pass",
                    effective_from=stamp - timedelta(days=10),
                    expires_at=stamp + timedelta(days=row["cal_days"]),
                    certificate_file_id="", registered_by=users["admin"].id if "admin" in users else "",
                    note="种子数据：初始校准记录", created_at=stamp,
                )
            )

    # ---------- 指标定义 ----------
    metrics: dict[str, MetricDefinition] = {}
    for row in (data.METRICS if not master_only else []):
        metric_id = f"METRIC-{row['code']}-{row['version']}"
        metric = db.get(MetricDefinition, metric_id)
        if not metric:
            metric = MetricDefinition(id=metric_id, org_id=org_id, created_at=stamp, **row)
            db.add(metric)
            db.flush()
            summary["metrics"] = summary.get("metrics", 0) + 1
        metrics[row["code"]] = metric

    # ---------- SOP ----------
    # 种子里的 SOP 版本是 published 但没有附件，作者与批准人还挂在真人账号上——
    # 一份「已发布、已生效、里面什么都没有」的受控文件。演示够用，正式库不能要。
    sop_versions: dict[str, SopVersion] = {}
    for row in (data.SOPS if not master_only else []):
        sop = db.query(Sop).filter(Sop.org_id == org_id, Sop.code == row["code"]).first()
        if not sop:
            sop = Sop(org_id=org_id, code=row["code"], title=row["title"], created_at=stamp)
            db.add(sop)
            db.flush()
        version = (
            db.query(SopVersion)
            .filter(SopVersion.sop_id == sop.id, SopVersion.version == row["version"])
            .first()
        )
        if not version:
            version = SopVersion(
                org_id=org_id, sop_id=sop.id, version=row["version"], state="published",
                capability_scope=row["capability_scope"], sample_types=row["sample_types"],
                requires_training_ack=row["requires_training_ack"],
                effective_from=stamp - timedelta(days=30),
                author_id=users["researcher"].id if "researcher" in users else "",
                approver_id=users["qa"].id if "qa" in users else "",
                published_at=stamp - timedelta(days=30), created_at=stamp,
            )
            db.add(version)
            db.flush()
            summary["sop_versions"] = summary.get("sop_versions", 0) + 1
        sop_versions[row["code"]] = version

    # ---------- 方法与方案 ----------
    for row in (data.RECIPES if not master_only else []):
        if not db.get(Recipe, row["id"]):
            payload = dict(row)
            steps = []
            for index, step in enumerate(payload.get("steps") or []):
                merged = dict(step)
                merged.setdefault("step_id", f"s{index + 1:02d}")
                merged.setdefault("kind", "device")
                steps.append(merged)
            payload["steps"] = steps
            db.add(Recipe(org_id=org_id, **payload))

    single = dict(data.SINGLE_CONDITION_RECIPE)
    if not master_only and not db.get(Recipe, single["id"]):
        db.add(
            Recipe(
                org_id=org_id,
                sop_version_id=sop_versions["SOP-SLR-01"].id if "SOP-SLR-01" in sop_versions else "",
                **single,
            )
        )
        summary["recipes"] = summary.get("recipes", 0) + 1

    # Plan.recipe_id 在 PostgreSQL 上是真外键。Recipe 与 Plan 之间没有 ORM relationship，
    # flush 无法仅靠对象图推断顺序，因此先把方法落库再插方案，否则外键会拒绝。
    db.flush()

    for row in (data.PLANS if not master_only else []):
        if db.get(Plan, row["id"]):
            continue
        # 种子是显式的演示初始化，不是迁移：已锁定的矩阵方案在这里同时给出批准版本，
        # 这样演示环境能直接建批次。历史数据迁移不做这件事（0003 里审批状态从 draft 起算）。
        approved = row["state"] == "locked"
        plan = Plan(
            org_id=org_id, plan_type="matrix",
            approval_state="approved" if approved else "draft", version=1,
            required_metrics=[
                metrics[code].id for code in ("areal_density", "discharge_capacity")
                if code in metrics
            ],
            **row,
        )
        db.add(plan)
        db.flush()
        if approved:
            db.add(
                PlanVersion(
                    org_id=org_id, plan_id=plan.id, version=1, state="approved",
                    snapshot={
                        "id": plan.id, "name": plan.name, "plan_type": plan.plan_type,
                        "version": 1, "factors": plan.factors, "control": plan.control,
                        "repeats": plan.repeats, "recipe_id": plan.recipe_id,
                    },
                    author_id=users["researcher"].id if "researcher" in users else "",
                    approver_id=users["qa"].id if "qa" in users else "",
                    approved_at=stamp, created_at=stamp,
                )
            )

    plan_row = dict(data.SINGLE_CONDITION_PLAN)
    metric_codes = plan_row.pop("metric_codes", [])
    if not master_only and not db.get(Plan, plan_row["id"]):
        db.add(
            Plan(
                org_id=org_id, approval_state="draft", version=1,
                required_metrics=[metrics[code].id for code in metric_codes if code in metrics],
                **plan_row,
            )
        )
        summary["plans"] = summary.get("plans", 0) + 1

    # ---------- 物料、批号与期初流水 ----------
    db.flush()
    for row in (data.LOTS if not master_only else []):
        if db.get(Lot, row["id"]):
            continue
        material = (
            db.query(Material)
            .filter(
                Material.org_id == org_id, Material.name == row["material"],
                Material.base_unit == row["unit"],
            )
            .first()
        )
        if not material:
            material = Material(
                org_id=org_id, code=f"{row['material']}@{row['unit']}", name=row["material"],
                base_unit=row["unit"], category=row["type"], cas=row["cas"], conversions={},
                ghs=row["ghs"], state="active", created_at=stamp,
            )
            db.add(material)
            db.flush()
        lot = Lot(org_id=org_id, material_id=material.id, opening_balance=row["qty"], **row)
        db.add(lot)
        db.flush()
        _opening_ledger(db, org_id, lot, material.id, stamp)
        summary["lots"] = summary.get("lots", 0) + 1

    for tank_id, kind, level, capacity in (data.WASTE if not master_only else []):
        if not db.get(WasteTank, tank_id):
            db.add(
                WasteTank(
                    id=tank_id, org_id=org_id, kind=kind, level_pct=level, capacity_l=capacity
                )
            )

    for row in (data.ALARMS if not master_only else []):
        if not db.get(Alarm, row["id"]):
            db.add(Alarm(org_id=org_id, raised_at=stamp, **row))

    db.commit()
    return summary


def _opening_ledger(db: Session, org_id: str, lot: Lot, material_id: str, stamp) -> None:
    """期初余额写一条 opening 流水。

    账面库存只能由流水推出来，种子也不例外——否则「流水累计 = 账面」这条核对规则
    在演示库上永远不成立。
    """
    from ..models import InventoryEvent, InventoryLedger

    event_id = f"opening-{lot.id}"
    existing = (
        db.query(InventoryEvent)
        .filter(
            InventoryEvent.org_id == org_id, InventoryEvent.source == "migration",
            InventoryEvent.event_id == event_id,
        )
        .first()
    )
    if existing is not None:
        return
    event = InventoryEvent(
        org_id=org_id, source="migration", event_id=event_id, event_type="receive",
        reason="期初余额：种子初始化", created_by="seed", created_at=stamp,
    )
    db.add(event)
    db.flush()
    db.add(
        InventoryLedger(
            org_id=org_id, event_row_id=event.id, source="migration", event_id=event_id,
            line_no=1, event_type="receive", lot_id=lot.id, material_id=material_id,
            batch_id="", step_run_id="", quantity=lot.qty, unit=lot.unit,
            balance_delta=lot.qty, balance_after=lot.qty, operator="seed",
            note="期初余额，非实际收货", created_at=stamp,
        )
    )
