"""迁移核对。

`scripts/migrate.py verify` 与 `/api/admin/migration-report` 共用它：
逐项核对记录数、关联完整性、数量余额与待人工处理项，差异逐条说明。
「有备份文件」不等于「恢复过」，所以这里只报账面核对，恢复演练另有流程。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.db import dec
from ..core.schema import EXPECTED_REVISION, current_revision
from ..models import (
    AnalysisTask, Asset, Batch, Command, InventoryLedger, Lot, Organization, Person,
    PhysicalSample, Plan, Recipe, Reservation, Result, ResultReview, ResultValue, Sample,
    Station, User,
)


# 转运能力：实现它的工位是搬运车，不承接工步，也就没有校准档案要核。
TRANSPORT_CAPABILITY = "cap.transfer"


class MigrationReportService:
    def __init__(self, db: Session):
        self.db = db

    def reconcile(self) -> dict:
        lines: list[dict] = []

        revision = current_revision(self.db.bind)
        lines.append(
            {
                "key": "schema",
                "label": "数据库版本",
                "ok": revision == EXPECTED_REVISION,
                "detail": f"当前 {revision or '无'}，应用期待 {EXPECTED_REVISION}",
            }
        )

        counts = {
            name: self.db.query(model).count()
            for name, model in (
                ("组织", Organization), ("账号", User), ("方法", Recipe), ("方案", Plan),
                ("批次", Batch), ("运行分配", Sample), ("物理样本", PhysicalSample),
                ("批号", Lot), ("预留", Reservation), ("历史结果", Result),
                ("结果明细", ResultValue), ("检测任务", AnalysisTask),
            )
        }
        lines.append(
            {
                "key": "counts", "label": "记录数", "ok": True,
                "detail": "、".join(f"{name} {value}" for name, value in counts.items()),
            }
        )

        # 归属完整性：不能有业务行落在组织之外
        orphan_org = 0
        org_ids = {row.id for row in self.db.query(Organization).all()}
        for model in (Batch, Plan, Recipe, Lot, Sample, Result, AnalysisTask):
            for row in self.db.query(model).all():
                if getattr(row, "org_id", "") not in org_ids:
                    orphan_org += 1
        lines.append(
            {
                "key": "org_scope", "label": "组织归属完整", "ok": orphan_org == 0,
                "detail": "全部业务行都有有效组织归属" if orphan_org == 0
                          else f"{orphan_org} 行的 org_id 不指向任何已登记组织",
            }
        )

        # 运行分配都要指向存在的物理样本
        missing_physical = [
            row.id for row in self.db.query(Sample).all()
            if row.physical_sample_id
            and self.db.get(PhysicalSample, row.physical_sample_id) is None
        ]
        lines.append(
            {
                "key": "sample_link", "label": "运行分配指向物理样本",
                "ok": not missing_physical,
                "detail": "全部对应" if not missing_physical
                          else f"{len(missing_physical)} 个分配的物理样本缺失：{'、'.join(missing_physical[:5])}",
            }
        )

        # 数量余额：流水累计应当等于账面库存
        mismatch: list[str] = []
        for lot in self.db.query(Lot).all():
            total = dec(0)
            rows = self.db.query(InventoryLedger).filter(InventoryLedger.lot_id == lot.id).all()
            if not rows:
                mismatch.append(f"{lot.id} 没有流水记录")
                continue
            for row in rows:
                total += dec(row.balance_delta)
            if total != dec(lot.qty):
                mismatch.append(f"{lot.id} 流水累计 {total:f} ≠ 账面 {dec(lot.qty):f}")
        lines.append(
            {
                "key": "balance", "label": "库存流水与账面一致", "ok": not mismatch,
                "detail": "全部批号对平" if not mismatch else "；".join(mismatch[:6]),
            }
        )

        # 历史标记：不能出现「凭迁移得到的审核通过」。
        # 判据不是「状态还是不是 pending」——迁移之后 QA 本来就会陆续把它们审掉，那是正常
        # 工作，不是篡改。真正要抓的是「状态不是 pending 却没有任何审核记录」：审核记录里
        # 有审核人与结果版本，补造的状态拿不出这个。
        moved = self.db.query(ResultValue).filter(
            ResultValue.provenance == "legacy_unreviewed",
            ResultValue.review_state != "pending",
        ).all()
        reviewed_ids = {
            row[0] for row in self.db.query(ResultReview.result_value_id).all()
        }
        forged = [row.id for row in moved if row.id not in reviewed_ids]
        pending = self.db.query(ResultValue).filter(
            ResultValue.provenance == "legacy_unreviewed",
            ResultValue.review_state == "pending",
        ).count()
        lines.append(
            {
                "key": "legacy_review", "label": "历史结果未被补造审核", "ok": not forged,
                "detail": (
                    f"历史结果 {pending} 条待复核、{len(moved)} 条已由审核人处理，无补造"
                    if not forged
                    else f"{len(forged)} 条历史结果状态已不是待复核却查不到审核记录："
                         + "、".join(forged[:5])
                ),
            }
        )

        # 旧锁定方案不得被当成已审批
        auto_approved = self.db.query(Plan).filter(
            Plan.state == "locked", Plan.approval_state == "approved"
        ).count()
        approved_versions = self.db.execute(
            text("SELECT COUNT(*) FROM plan_versions WHERE state = 'approved'")
        ).scalar() or 0
        lines.append(
            {
                "key": "plan_approval", "label": "锁定方案未被当成已审批",
                "ok": auto_approved <= approved_versions,
                "detail": f"{auto_approved} 个已锁定方案标为已批准，其中 {approved_versions} 个有批准版本记录",
            }
        )

        # 执行前置主数据：人员档案与资产档案。迁移不编造这两样——资质与校准是签字背书的
        # 事实，猜出来的比没有更危险。但缺了它们，开跑检查会在「执行人资质」与「设备校准」
        # 两项上阻塞下发，所以必须在核对里说出来，而不是让责任人在第一次下发时才发现。
        people = self.db.query(Person).count()
        accounts = self.db.query(User).filter(User.state == "active").count()
        linked_users = {
            row.user_id for row in self.db.query(Person).all() if row.user_id
        }
        unlinked = accounts - len(linked_users)
        assets = self.db.query(Asset).count()
        # 只看能承接工步的工位：开跑检查是按配方步骤所落的工位去查资产档案的。
        # 转运车只实现 cap.transfer，不是工步落点、也没有校准档案；退役工位不再参与匹配。
        # 把这两类算进缺口，会报出一个永远修不完的假账。
        stations = [
            row for row in self.db.query(Station).all()
            if not row.retired and set(row.limits or {}) - {TRANSPORT_CAPABILITY}
        ]
        station_gap = [row.id for row in stations if not row.asset_id]
        ready = not unlinked and not station_gap
        lines.append(
            {
                "key": "execution_master_data",
                "label": "执行前置主数据（人员档案 / 资产档案）",
                "ok": ready,
                "detail": (
                    f"人员档案 {people} 份、资产档案 {assets} 份"
                    if ready
                    else (
                        f"人员档案 {people} 份（{unlinked} 个在用账号尚未关联）、"
                        f"资产档案 {assets} 份（{len(station_gap)}/{len(stations)} 个工位未关联"
                        + (
                            "：" + "、".join(station_gap[:6])
                            + (f" 等 {len(station_gap)} 个" if len(station_gap) > 6 else "")
                            if station_gap else ""
                        )
                        + "）；补录前「执行人资质」与「设备校准」两项开跑检查会阻塞下发，"
                        "迁移不代为编造"
                    )
                ),
            }
        )

        # 待人工处理项：在途批次、结果未知指令
        in_flight = [
            row.id for row in self.db.query(Batch).filter(
                Batch.state.notin_(["done", "aborted"])
            ).all()
        ]
        unknown = [
            row.id for row in self.db.query(Command).filter(Command.state == "unknown").all()
        ]
        lines.append(
            {
                "key": "pending", "label": "待人工核查项", "ok": True,
                "detail": (
                    f"在途批次 {len(in_flight)} 个"
                    + (f"（{'、'.join(in_flight[:5])}）" if in_flight else "")
                    + f"；结果未知指令 {len(unknown)} 条"
                    + "；这些不由迁移自动改状态，需责任人确认"
                ),
            }
        )

        return {
            "ok": all(row["ok"] for row in lines),
            "lines": lines,
            "counts": counts,
        }
