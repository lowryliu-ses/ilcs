from __future__ import annotations

from sqlalchemy import or_

from ..models import ExperimentTask, Plan, PlanBatchLink, PlanVersion, Recipe, TaskAssignment
from .base import Repository, ScopedRepository


class RecipeRepository(ScopedRepository[Recipe]):
    model = Recipe

    def list(self) -> list[Recipe]:
        return list(self.query().order_by(Recipe.id).all())

    def released(self) -> list[Recipe]:
        return list(self.query().filter(Recipe.state == "released").all())

    def reviewable_states(self) -> list[Recipe]:
        return list(self.query().filter(Recipe.state.in_(["review", "approved", "released"])).all())

    def revision_count(self, recipe_id: str) -> int:
        return self.query().filter(Recipe.parent == recipe_id).count()

    def using_sop_version(self, sop_version_id: str) -> list[Recipe]:
        return list(self.query().filter(Recipe.sop_version_id == sop_version_id).all())


class PlanRepository(ScopedRepository[Plan]):
    model = Plan

    def list(self) -> list[Plan]:
        return list(self.query().order_by(Plan.id).all())

    def page(self, offset: int, limit: int, plan_type: str | None = None, keyword: str = ""):
        query = self.query()
        if plan_type:
            query = query.filter(Plan.plan_type == plan_type)
        if keyword:
            like = f"%{keyword}%"
            query = query.filter(or_(Plan.id.like(like), Plan.name.like(like)))
        total = query.count()
        rows = query.order_by(Plan.id).offset(offset).limit(limit).all()
        return list(rows), total

    def for_recipe(self, recipe_id: str) -> list[Plan]:
        return list(self.query().filter(Plan.recipe_id == recipe_id).all())

    def bound_batch_ids(self, plan_id: str) -> list[str]:
        rows = self.db.query(PlanBatchLink).filter(PlanBatchLink.plan_id == plan_id).all()
        return [r.batch_id for r in rows]

    def link_batch(self, plan_id: str, batch_id: str) -> None:
        self.db.add(PlanBatchLink(plan_id=plan_id, batch_id=batch_id))


class PlanVersionRepository(ScopedRepository[PlanVersion]):
    model = PlanVersion

    def for_plan(self, plan_id: str) -> list[PlanVersion]:
        return list(
            self.query().filter(PlanVersion.plan_id == plan_id).order_by(PlanVersion.version).all()
        )

    def find(self, plan_id: str, version: int) -> PlanVersion | None:
        return (
            self.query()
            .filter(PlanVersion.plan_id == plan_id, PlanVersion.version == version)
            .first()
        )

    def latest_approved(self, plan_id: str) -> PlanVersion | None:
        return (
            self.query()
            .filter(PlanVersion.plan_id == plan_id, PlanVersion.state == "approved")
            .order_by(PlanVersion.version.desc())
            .first()
        )


class ExperimentTaskRepository(ScopedRepository[ExperimentTask]):
    model = ExperimentTask

    def page(
        self, offset: int, limit: int, state: str | None = None, assignee: str = "",
        plan_id: str = "",
    ):
        query = self.query()
        if state:
            query = query.filter(ExperimentTask.state == state)
        if assignee:
            query = query.filter(ExperimentTask.assignee_user_id == assignee)
        if plan_id:
            query = query.filter(ExperimentTask.plan_id == plan_id)
        total = query.count()
        rows = (
            query.order_by(ExperimentTask.created_at.desc()).offset(offset).limit(limit).all()
        )
        return list(rows), total

    def for_plan(self, plan_id: str) -> list[ExperimentTask]:
        return list(self.query().filter(ExperimentTask.plan_id == plan_id).all())

    def by_batch(self, batch_id: str) -> ExperimentTask | None:
        return self.query().filter(ExperimentTask.batch_id == batch_id).first()

    def open_for_assignee(self, user_id: str) -> list[ExperimentTask]:
        return list(
            self.query()
            .filter(
                ExperimentTask.assignee_user_id == user_id,
                ExperimentTask.state.notin_(["done", "cancelled"]),
            )
            .order_by(ExperimentTask.due_at)
            .all()
        )

    def pending_accept(self) -> list[ExperimentTask]:
        return list(self.query().filter(ExperimentTask.state == "pending_accept").all())

    def next_id(self) -> str:
        count = self.db.query(ExperimentTask).count()
        return f"ET-{count + 1:05d}"


class TaskAssignmentRepository(Repository[TaskAssignment]):
    model = TaskAssignment

    def for_task(self, task_id: str) -> list[TaskAssignment]:
        return list(
            self.db.query(TaskAssignment)
            .filter(TaskAssignment.task_id == task_id)
            .order_by(TaskAssignment.created_at)
            .all()
        )
