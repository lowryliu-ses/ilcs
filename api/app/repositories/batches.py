from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_

from ..domain.scheduling import WORK, Interval
from ..models import (
    Allocation, AnalysisTask, Batch, PlanBatchLink, Reservation, Sample,
)
from .base import Repository, ScopedRepository

TERMINAL_STATES = {"done", "aborted"}


# 不再是处理对象的运行分配状态：已拆分（母样）与失败（剔除、报废、返工作废）
INACTIVE_SAMPLE_STATES = frozenset({"split", "merged", "failed"})


class BatchRepository(ScopedRepository[Batch]):
    model = Batch

    def list(self) -> list[Batch]:
        return list(self.query().order_by(Batch.created_at.desc()).all())

    def page(self, offset: int, limit: int, state: str | None = None, keyword: str = ""):
        query = self.query()
        if state:
            query = query.filter(Batch.state == state)
        if keyword:
            like = f"%{keyword}%"
            query = query.filter(or_(Batch.id.like(like), Batch.note.like(like)))
        total = query.count()
        rows = query.order_by(Batch.created_at.desc()).offset(offset).limit(limit).all()
        return list(rows), total

    def active(self) -> list[Batch]:
        return list(self.query().filter(Batch.state.notin_(TERMINAL_STATES)).all())

    def lock(self, batch_id: str, *, skip_locked: bool = False) -> Batch | None:
        """取批次行锁并刷新内存中的旧值。

        保持 / 终止与执行器投递指令都先拿这把锁：否则「批次已保持」与「指令已发出」
        可以各自读到对方提交前的状态，排在队里的动作指令会在保持之后照发。
        `skip_locked`：别人正锁着就不等，返回 None（执行器兜底扫描用，控制回路不能卡在锁上）。
        """
        query = self.query().filter(Batch.id == batch_id).populate_existing()
        query = query.with_for_update(skip_locked=skip_locked)
        return query.first()

    def ids_for_recipe(self, recipe_id: str) -> list[str]:
        rows = self.query().with_entities(Batch.id).filter(Batch.recipe_id == recipe_id).all()
        return [r[0] for r in rows]

    def recipe_pairs(self) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self.query().with_entities(Batch.id, Batch.recipe_id).all()]

    def plan_pairs(self) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self.query().with_entities(Batch.id, Batch.plan_id).all()]

    def ids_for_plan(self, plan_id: str) -> list[str]:
        rows = self.query().with_entities(Batch.id).filter(Batch.plan_id == plan_id).all()
        return [r[0] for r in rows]

    def by_task(self, task_id: str) -> Batch | None:
        return self.query().filter(Batch.task_id == task_id).first()

    def purge(self, batch_id: str) -> None:
        """物理删除未下发的批次及其附属行。

        只对 planned / scheduled 调用（服务层已守过）。样本的运行分配可以删，
        物理样本不删——它可能已独立登记、流转或被别的运行引用。
        """
        self.db.query(AnalysisTask).filter(
            AnalysisTask.sample_id.in_(
                self.db.query(Sample.id).filter(Sample.batch_id == batch_id)
            )
        ).delete(synchronize_session=False)
        self.db.query(Sample).filter(Sample.batch_id == batch_id).delete(synchronize_session=False)
        self.db.query(Reservation).filter(Reservation.batch_id == batch_id).delete(synchronize_session=False)
        self.db.query(Allocation).filter(Allocation.batch_id == batch_id).delete(synchronize_session=False)
        self.db.query(PlanBatchLink).filter(PlanBatchLink.batch_id == batch_id).delete(synchronize_session=False)
        self.db.query(Batch).filter(Batch.id == batch_id).delete(synchronize_session=False)

    def by_state(self, *states: str) -> list[Batch]:
        return list(self.query().filter(Batch.state.in_(states)).all())

    def next_id(self, today: datetime) -> str:
        """当天序号。

        不能用「现有条数 + 1」：删掉一个草稿批次后条数会回退，新批次就会拿到一个
        已经出现在审计链和任务记录里的编号。这里取已用过的最大序号再 +1，并跳过
        任何仍被引用的编号。
        """
        from ..models import AuditEvent, ExperimentTask

        prefix = f"B-{today:%y%m%d}-"
        used = {
            row[0] for row in
            self.db.query(Batch.id).filter(Batch.id.startswith(prefix)).all()
        }
        used |= {
            row[0] for row in
            self.db.query(ExperimentTask.batch_id)
            .filter(ExperimentTask.batch_id.startswith(prefix)).all()
        }
        used |= {
            row[0] for row in
            self.db.query(AuditEvent.target).filter(AuditEvent.target.startswith(prefix)).all()
        }
        sequence = 0
        for value in used:
            suffix = value[len(prefix):]
            if suffix.isdigit():
                sequence = max(sequence, int(suffix))
        candidate = sequence + 1
        while f"{prefix}{candidate:03d}" in used:
            candidate += 1
        return f"{prefix}{candidate:03d}"


class AllocationRepository(Repository[Allocation]):
    """工步预约。没有自己的 org_id，一律经由已鉴权的批次访问。"""

    model = Allocation

    def for_batch(self, batch_id: str) -> list[Allocation]:
        return list(
            self.db.query(Allocation)
            .filter(Allocation.batch_id == batch_id)
            .order_by(Allocation.step_index, Allocation.starts_at)
            .all()
        )

    def work_step(self, batch_id: str, step_index: int) -> Allocation | None:
        return (
            self.db.query(Allocation)
            .filter(
                Allocation.batch_id == batch_id,
                Allocation.step_index == step_index,
                Allocation.kind == WORK,
            )
            .first()
        )

    def delete_for_batch(self, batch_id: str) -> None:
        self.db.query(Allocation).filter(Allocation.batch_id == batch_id).delete()

    def delete_steps(self, batch_id: str, step_indices) -> int:
        """只删要重排的那些步骤的时间窗。已开出、已判定的步骤不在其中，原地保留。"""
        indices = sorted(set(step_indices))
        if not indices:
            return 0
        return (
            self.db.query(Allocation)
            .filter(Allocation.batch_id == batch_id, Allocation.step_index.in_(indices))
            .delete(synchronize_session="fetch")
        )

    def open_count_for_station(self, station_id: str) -> int:
        return (
            self.db.query(Allocation)
            .join(Batch, Batch.id == Allocation.batch_id)
            .filter(Allocation.station_id == station_id, Batch.state.notin_(TERMINAL_STATES))
            .count()
        )

    def open_for_station(self, station_id: str) -> list[tuple[Allocation, Batch]]:
        """工位上未结束批次的时间窗，连同所属批次（要按批次快照看这一步用的是哪项能力）。"""
        return list(
            self.db.query(Allocation, Batch)
            .join(Batch, Batch.id == Allocation.batch_id)
            .filter(Allocation.station_id == station_id, Batch.state.notin_(TERMINAL_STATES))
            .all()
        )

    def busy_timeline(self, exclude_batch_ids: set[str] | None = None) -> dict[str, list[Interval]]:
        query = self.db.query(Allocation).join(Batch, Batch.id == Allocation.batch_id).filter(
            Batch.state.notin_(TERMINAL_STATES)
        )
        if exclude_batch_ids:
            query = query.filter(Allocation.batch_id.notin_(list(exclude_batch_ids)))
        timeline: dict[str, list[Interval]] = {}
        for allocation in query.all():
            timeline.setdefault(allocation.station_id, []).append(
                Interval(allocation.starts_at, allocation.ends_at, max(1, int(allocation.units or 1)))
            )
        return timeline

    def lane_mates(self, station_id: str, exclude_batch_id: str) -> list[str]:
        rows = (
            self.db.query(Allocation.batch_id)
            .join(Batch, Batch.id == Allocation.batch_id)
            .filter(
                Allocation.station_id == station_id,
                Allocation.batch_id != exclude_batch_id,
                Batch.state == "scheduled",
            )
            .distinct()
            .all()
        )
        return [row[0] for row in rows]

    def shift_steps(self, batch_id: str, shifts: dict) -> int:
        """按步骤各自平移时间窗（含转运、清洗、协同）：{步骤下标: 平移量}。不在表里的步骤不动。"""
        moved = 0
        for allocation in self.for_batch(batch_id):
            delta = shifts.get(allocation.step_index)
            if not delta:
                continue
            allocation.starts_at = allocation.starts_at + delta
            allocation.ends_at = allocation.ends_at + delta
            moved += 1
        return moved


class SampleRepository(ScopedRepository[Sample]):
    """运行分配。物理样本在 repositories/samples.py。"""

    model = Sample

    def for_batch(self, batch_id: str) -> list[Sample]:
        return list(self.query().filter(Sample.batch_id == batch_id).order_by(Sample.position).all())

    def active_for_batch(self, batch_id: str) -> list[Sample]:
        """在用样本：当前步骤的有效输入。已拆分的母样与判为失败的样本即使仍占着孔位，也不再是处理对象。"""
        return [sample for sample in self.for_batch(batch_id) if sample.state not in INACTIVE_SAMPLE_STATES]

    def for_physical(self, physical_sample_id: str) -> list[Sample]:
        return list(self.query().filter(Sample.physical_sample_id == physical_sample_id).all())

    def unfinished_count(self, batch_id: str) -> int:
        return self.query().filter(Sample.batch_id == batch_id, Sample.state != "done").count()

    def mark_all(self, batch_id: str, state: str, only_if_not: str | None = None) -> None:
        for sample in self.for_batch(batch_id):
            if only_if_not and sample.state == only_if_not:
                continue
            sample.state = state


class AnalysisTaskRepository(ScopedRepository[AnalysisTask]):
    model = AnalysisTask

    def for_sample(self, sample_id: str) -> list[AnalysisTask]:
        return list(self.query().filter(AnalysisTask.sample_id == sample_id).all())

    def for_physical(self, physical_sample_id: str) -> list[AnalysisTask]:
        return list(
            self.query()
            .filter(AnalysisTask.physical_sample_id == physical_sample_id)
            .order_by(AnalysisTask.round_no)
            .all()
        )

    def for_batch(self, batch_id: str) -> list[AnalysisTask]:
        return list(
            self.query()
            .join(Sample, Sample.id == AnalysisTask.sample_id)
            .filter(Sample.batch_id == batch_id)
            .all()
        )

    def max_round(self, physical_sample_id: str) -> int:
        rows = self.for_physical(physical_sample_id)
        return max((row.round_no for row in rows), default=0)

    def page(self, offset: int, limit: int, state: str | None = None, sample_id: str = ""):
        query = self.query()
        if state:
            query = query.filter(AnalysisTask.state == state)
        if sample_id:
            query = query.filter(
                or_(
                    AnalysisTask.sample_id == sample_id,
                    AnalysisTask.physical_sample_id == sample_id,
                )
            )
        total = query.count()
        rows = query.order_by(AnalysisTask.created_at.desc()).offset(offset).limit(limit).all()
        return list(rows), total
