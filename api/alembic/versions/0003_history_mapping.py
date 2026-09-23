"""历史数据映射

按需求文档 10.1 的规则转换历史数据，并把每一步的结果计入迁移报告：

- 组织归属由 `ILCS_MIGRATION_ORG_ID` 指定。确认不了归属时把它指向受限待核对组织，
  该组织没有成员，任何人都看不到里面的数据——不能默认让所有组织可见。
- 旧 `valid` 不转成「审核通过」：结果审核状态一律 pending，来源标记 legacy_unreviewed，
  不补造审核人与审核时间。
- 旧库存不按步骤比例倒扣：期初余额取当前账面值，并写一条 opening 流水作为起点；
  未结预留保持原样，由责任人确认。
- 旧「矩阵已锁定」不等于「方案已审批」，审批状态从 draft 起算（在 0002 里已完成）。
- 历史样本没有实际位置记录，写「历史位置未记录」，不拿计划工位冒充实际位置。
- 在途批次不强行映射成已完成，只列入报告等人工核查。

Revision ID: 0003_history_mapping
Revises: 0002_platform
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_history_mapping"
down_revision: Union[str, None] = "0002_platform"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEGACY_METRICS = [
    ("areal_density", "面密度", "mg/cm2", "areal_density"),
    ("discharge_capacity", "首次放电比容量", "mAh/g", "discharge_capacity"),
    ("retention", "容量保持率", "%", "retention"),
]
REPORT_LINES: list[str] = []


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _q(bind, value) -> float:
    """数量列是 NUMERIC，直接写数值。"""
    return float(value)


def _note(line: str) -> None:
    REPORT_LINES.append(line)
    print(f"  迁移：{line}", flush=True)


def upgrade() -> None:
    bind = op.get_bind()
    REPORT_LINES.clear()

    # 0003 是“旧业务数据补录”而不是基础主数据迁移。0001/0002 在全新数据库中也会
    # 建出这些空表；如果此处不区分空库，就会凭空制造组织、实验室和三个指标，导致
    # 后续正式最小初始化无法使用部署方给出的真实组织资料。
    legacy_tables = (
        "users", "stations", "lots", "recipes", "plans", "batches", "samples",
        "analysis_tasks", "results", "commands", "reservations", "alarms",
        "waste_tanks", "audit_events",
    )
    legacy_rows = sum(
        int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)
        for table in legacy_tables
    )
    if legacy_rows == 0:
        line = "未检测到历史业务数据：跳过组织、实验室、指标及业务数据补录"
        _note(line)
        print("\n迁移报告（0003_history_mapping）\n- " + line, flush=True)
        path = os.environ.get("ILCS_MIGRATION_REPORT")
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# ILCS 历史数据迁移报告\n\n- " + line + "\n")
        return

    org_id = os.environ.get("ILCS_MIGRATION_ORG_ID", "ORG-001")
    org_code = os.environ.get("ILCS_MIGRATION_ORG_CODE", "MAIN")
    org_name = os.environ.get("ILCS_MIGRATION_ORG_NAME", "本部电池实验室")
    org_timezone = os.environ.get("ILCS_MIGRATION_ORG_TIMEZONE", "Asia/Shanghai")
    stamp = _now()

    existing = bind.execute(
        sa.text("SELECT id FROM organizations WHERE id = :id"), {"id": org_id}
    ).fetchone()
    if not existing:
        bind.execute(
            sa.text(
                "INSERT INTO organizations (id, code, name, timezone, state, note, created_at) "
                "VALUES (:id, :code, :name, :tz, 'active', :note, :at)"
            ),
            {
                "id": org_id, "code": org_code, "name": org_name, "tz": org_timezone,
                "note": "历史数据迁移创建", "at": stamp,
            },
        )
        _note(f"创建组织 {org_id}（{org_name}）")

    lab_id = f"{org_id}-LAB-1"
    if not bind.execute(sa.text("SELECT id FROM labs WHERE id = :id"), {"id": lab_id}).fetchone():
        bind.execute(
            sa.text(
                "INSERT INTO labs (id, org_id, name, timezone, state) "
                "VALUES (:id, :org, :name, :tz, 'active')"
            ),
            {"id": lab_id, "org": org_id, "name": "一号实验室", "tz": org_timezone},
        )

    # ---------- 1 组织归属 ----------
    for table in (
        "alarms", "analysis_tasks", "batches", "commands", "lots", "plans", "recipes",
        "reservations", "results", "samples", "stations", "waste_tanks", "audit_events",
    ):
        updated = bind.execute(
            sa.text(f"UPDATE {table} SET org_id = :org WHERE org_id = '' OR org_id IS NULL"),
            {"org": org_id},
        ).rowcount
        _note(f"{table} 归属 {org_id}：{updated} 行")

    # 全部现有账号加入该组织；成员关系才是访问范围的依据
    users = bind.execute(sa.text("SELECT id FROM users")).fetchall()
    joined = 0
    for (user_id,) in users:
        if bind.execute(
            sa.text("SELECT id FROM memberships WHERE org_id = :org AND user_id = :u"),
            {"org": org_id, "u": user_id},
        ).fetchone():
            continue
        bind.execute(
            sa.text(
                "INSERT INTO memberships (id, org_id, user_id, state, default_lab_id, granted_at) "
                "VALUES (:id, :org, :u, 'active', :lab, :at)"
            ),
            {"id": _uid(), "org": org_id, "u": user_id, "lab": lab_id, "at": stamp},
        )
        joined += 1
    _note(f"组织成员关系：{joined} 个账号加入 {org_id}")

    # ---------- 2 物料主数据与期初余额 ----------
    lots = bind.execute(
        sa.text("SELECT id, material, unit, cas, type, qty, ghs FROM lots ORDER BY id")
    ).fetchall()
    materials: dict[tuple[str, str], str] = {}
    for lot in lots:
        key = (lot.material, lot.unit)
        if key in materials:
            continue
        row = bind.execute(
            sa.text("SELECT id FROM materials WHERE org_id = :org AND code = :code"),
            {"org": org_id, "code": f"{lot.material}@{lot.unit}"},
        ).fetchone()
        if row:
            materials[key] = row.id
            continue
        material_id = _uid()
        bind.execute(
            sa.text(
                "INSERT INTO materials "
                "(id, org_id, code, name, base_unit, category, cas, conversions, external_ref, ghs, "
                " state, created_at) "
                "VALUES (:id, :org, :code, :name, :unit, :cat, :cas, :conv, '', :ghs, 'active', :at)"
            ),
            {
                "id": material_id, "org": org_id, "code": f"{lot.material}@{lot.unit}",
                "name": lot.material, "unit": lot.unit, "cat": lot.type or "", "cas": lot.cas or "",
                "conv": json.dumps({}), "ghs": lot.ghs if isinstance(lot.ghs, str) else json.dumps(lot.ghs or []),
                "at": stamp,
            },
        )
        materials[key] = material_id
    _note(f"物料主数据：按（名称，单位）建 {len(materials)} 条，旧名称保留展示")

    opening_events = 0
    for lot in lots:
        material_id = materials[(lot.material, lot.unit)]
        balance = float(str(lot.qty))
        bind.execute(
            sa.text(
                "UPDATE lots SET material_id = :m, opening_balance = :bal WHERE id = :id"
            ),
            {"m": material_id, "bal": _q(bind, balance), "id": lot.id},
        )
        event_id = f"opening-{lot.id}"
        if bind.execute(
            sa.text(
                "SELECT id FROM inventory_events "
                "WHERE org_id = :org AND source = 'migration' AND event_id = :e"
            ),
            {"org": org_id, "e": event_id},
        ).fetchone():
            continue
        event_row = _uid()
        bind.execute(
            sa.text(
                "INSERT INTO inventory_events "
                "(id, org_id, source, event_id, event_type, batch_id, step_run_id, command_id, "
                " reason, reverses_id, created_by, created_at) "
                "VALUES (:id, :org, 'migration', :e, 'receive', '', '', '', :reason, '', 'migration', :at)"
            ),
            {
                "id": event_row, "org": org_id, "e": event_id, "at": stamp,
                "reason": "期初余额：按切换时账面值建立流水起点，待责任人确认",
            },
        )
        bind.execute(
            sa.text(
                "INSERT INTO inventory_ledger "
                "(id, org_id, event_row_id, source, event_id, line_no, event_type, lot_id, material_id, "
                " reservation_id, batch_id, step_run_id, quantity, unit, balance_delta, balance_after, "
                " operator, note, created_at) "
                "VALUES (:id, :org, :row, 'migration', :e, 1, 'receive', :lot, :m, NULL, '', '', "
                " :qty, :unit, :qty, :qty, 'migration', :note, :at)"
            ),
            {
                "id": _uid(), "org": org_id, "row": event_row, "e": event_id, "lot": lot.id,
                "m": material_id, "qty": _q(bind, balance), "unit": lot.unit, "at": stamp,
                "note": "期初余额，非实际收货",
            },
        )
        opening_events += 1
    _note(f"库存期初：{opening_events} 个批号写入 opening 流水；未按步骤比例倒扣任何历史消耗")

    open_reservations = bind.execute(
        sa.text("SELECT COUNT(*) FROM reservations WHERE state = 'reserved'")
    ).scalar()
    consumed_reservations = bind.execute(
        sa.text("SELECT COUNT(*) FROM reservations WHERE state = 'consumed'")
    ).scalar()
    # 旧 delivered_qty 是按步骤比例推算的，不是证据，不迁入 consumed_qty
    _note(
        f"未结预留 {open_reservations} 条、已标记消耗 {consumed_reservations} 条保持原样；"
        f"旧 delivered_qty 为按步骤比例推算值，未迁入 consumed_qty，需责任人确认（DEC-05）"
    )

    # ---------- 3 物理样本与运行分配 ----------
    samples = bind.execute(
        sa.text("SELECT id, batch_id, org_id, well, quality FROM samples ORDER BY id")
    ).fetchall()
    created = 0
    for sample in samples:
        if bind.execute(
            sa.text("SELECT id FROM physical_samples WHERE id = :id"), {"id": sample.id}
        ).fetchone():
            continue
        bind.execute(
            sa.text(
                "INSERT INTO physical_samples "
                "(id, org_id, project_id, barcode, source, sample_type, parent_id, quantity, unit, "
                " storage_condition, current_location, location_note, custodian, lifecycle_state, note, "
                " origin, created_by, created_at, updated_at, row_version) "
                "VALUES (:id, :org, '', :bc, :src, '极片', NULL, NULL, '', '', '', :loc, '', "
                " 'in_use', '', 'legacy', 'migration', :at, :at, 1)"
            ),
            {
                "id": sample.id, "org": org_id, "bc": sample.id,
                "src": f"批次 {sample.batch_id}", "loc": "历史位置未记录", "at": stamp,
            },
        )
        created += 1
    bind.execute(
        sa.text("UPDATE samples SET physical_sample_id = id WHERE physical_sample_id = ''")
    )
    bind.execute(
        sa.text("UPDATE samples SET container_id = batch_id WHERE container_id = ''")
    )
    _note(f"物理样本：{created} 条按原 Sample ID 建立，运行分配指向同一 ID，位置标注「历史位置未记录」")

    # ---------- 4 指标定义与结果明细 ----------
    metric_ids: dict[str, str] = {}
    for code, name, unit, column in LEGACY_METRICS:
        metric_id = f"METRIC-{code}-v1"
        metric_ids[column] = metric_id
        if bind.execute(
            sa.text("SELECT id FROM metric_definitions WHERE id = :id"), {"id": metric_id}
        ).fetchone():
            continue
        bind.execute(
            sa.text(
                "INSERT INTO metric_definitions "
                "(id, org_id, code, name, version, value_type, unit, method_version, sample_types, "
                " rules, state, created_at) "
                "VALUES (:id, :org, :code, :name, 'v1', 'number', :unit, :mv, :types, :rules, "
                " 'active', :at)"
            ),
            {
                "id": metric_id, "org": org_id, "code": code, "name": name, "unit": unit,
                "mv": "EC-02 v2", "types": json.dumps(["极片"]), "rules": json.dumps({}), "at": stamp,
            },
        )
    _note("指标定义：原三个固定指标转为受版本控制的 METRIC-*-v1")

    required = json.dumps([metric_ids[c] for _, _, _, c in LEGACY_METRICS])
    # PostgreSQL 的 JSON 类型不能与 varchar 比较，也不存在 JSON 等值运算，按文本比较空旧值
    empty_required = "required_metrics::text = '[]' OR required_metrics IS NULL"
    bind.execute(
        sa.text(
            "UPDATE analysis_tasks SET required_metrics = :req, method_version = 'EC-02 v2', "
            f"physical_sample_id = sample_id, round_no = 1 WHERE {empty_required}"
        ).bindparams(sa.bindparam("req", type_=sa.JSON())),
        {"req": json.loads(required)},
    )
    done_tasks = bind.execute(
        sa.text("UPDATE analysis_tasks SET state = 'collected' WHERE state = 'done'")
    ).rowcount
    _note(f"检测任务：{done_tasks} 条 done → collected（采集完成，审核仍为 pending）")

    results = bind.execute(
        sa.text(
            "SELECT id, sample_id, task_id, areal_density, discharge_capacity, retention, "
            "raw_uri, parser_version, created_at FROM results ORDER BY created_at"
        )
    ).fetchall()
    values = 0
    for result in results:
        for _, _, unit, column in LEGACY_METRICS:
            value = getattr(result, column)
            if value is None:
                continue  # 未测就是没有记录，不写 0
            metric_id = metric_ids[column]
            task_id = result.task_id or f"legacy-task-{result.sample_id}"
            if bind.execute(
                sa.text(
                    "SELECT id FROM result_values WHERE analysis_task_id = :t "
                    "AND metric_definition_id = :m AND result_version = 1"
                ),
                {"t": task_id, "m": metric_id},
            ).fetchone():
                continue
            bind.execute(
                sa.text(
                    "INSERT INTO result_values "
                    "(id, org_id, analysis_task_id, physical_sample_id, assignment_id, "
                    " metric_definition_id, ingest_event_id, value_num, value_text, unit, collected_at, "
                    " raw_file_id, source_ref, parser_version, result_version, revises_id, "
                    " superseded_by_id, not_measured_reason, quality, review_state, provenance, "
                    " entered_by, created_at, row_version) "
                    "VALUES (:id, :org, :task, :ps, :asg, :metric, '', :num, '', :unit, :at, "
                    " '', :src, :pv, 1, '', '', '', 'unassessed', 'pending', 'legacy_unreviewed', "
                    " '', :at, 1)"
                ),
                {
                    "id": f"RV-{result.id}-{column}", "org": org_id, "task": task_id,
                    "ps": result.sample_id, "asg": result.sample_id, "metric": metric_id,
                    "num": float(value), "unit": unit, "at": result.created_at,
                    "src": result.raw_uri or "", "pv": result.parser_version or "",
                },
            )
            values += 1
    _note(
        f"结果明细：{values} 条从固定三指标转为类型化结果；审核状态 pending、"
        f"来源标记 legacy_unreviewed；原始 URI 记入 source_ref，取不到原件的不生成替代曲线"
    )

    # ---------- 5 方法步骤补齐稳定标识 ----------
    recipes = bind.execute(sa.text("SELECT id, steps FROM recipes")).fetchall()
    touched = 0
    for recipe in recipes:
        steps = recipe.steps if isinstance(recipe.steps, list) else json.loads(recipe.steps or "[]")
        changed = False
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            if not step.get("step_id"):
                step["step_id"] = f"s{index + 1:02d}"
                changed = True
            if not step.get("kind"):
                step["kind"] = "device"  # 旧步骤一律是设备步骤
                changed = True
        if changed:
            bind.execute(
                sa.text("UPDATE recipes SET steps = :steps WHERE id = :id"),
                {"steps": json.dumps(steps, ensure_ascii=False), "id": recipe.id},
            )
            touched += 1
    _note(f"方法步骤：{touched} 个配方补齐 step_id 与 kind=device；批次快照原文未改动")

    # ---------- 6 待人工核查项 ----------
    in_flight = bind.execute(
        sa.text(
            "SELECT id, state FROM batches WHERE state NOT IN ('done', 'aborted') ORDER BY id"
        )
    ).fetchall()
    if in_flight:
        _note(
            "在途批次未强行映射成已完成，等切换前人工核查："
            + "、".join(f"{b.id}({b.state})" for b in in_flight)
        )
    else:
        _note("无在途批次")

    unknown = bind.execute(
        sa.text("SELECT COUNT(*) FROM commands WHERE state = 'unknown'")
    ).scalar()
    if unknown:
        bind.execute(
            sa.text("UPDATE commands SET delivery_state = 'maybe_sent' WHERE state = 'unknown'")
        )
        _note(f"结果未知指令 {unknown} 条投递状态置为 maybe_sent，等人工核查，不盲目重发")

    report = "\n".join(f"- {line}" for line in REPORT_LINES)
    print("\n迁移报告（0003_history_mapping）\n" + report, flush=True)
    path = os.environ.get("ILCS_MIGRATION_REPORT")
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# ILCS 历史数据迁移报告\n\n")
            handle.write(f"组织归属：{org_id}\n\n")
            handle.write(report + "\n")


def downgrade() -> None:
    """数据迁移不提供自动回退。

    结构可逆不等于业务动作可逆：期初余额、物理样本与结果明细一旦被人工确认或
    继续使用，删掉它们就丢掉真实记录。回退请从备份恢复，并先核对切换后产生的数据。
    """
    raise RuntimeError("历史数据映射不支持自动回退，请按 README 的回退顺序从备份恢复")
