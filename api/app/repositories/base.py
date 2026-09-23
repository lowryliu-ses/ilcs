from __future__ import annotations

from typing import Generic, TypeVar

from sqlalchemy.orm import Query, Session

from ..core.context import AccessContext
from ..core.errors import NotFound, VersionConflict
from ..models import Base

ModelT = TypeVar("ModelT", bound=Base)


class Repository(Generic[ModelT]):
    """不带组织归属的字典类对象（能力、工位岛、适配器）用这个。"""

    model: type[ModelT]

    def __init__(self, db: Session, ctx: AccessContext | None = None):
        self.db = db
        self.ctx = ctx

    def query(self) -> Query:
        return self.db.query(self.model)

    def get(self, identifier) -> ModelT | None:
        return self.db.get(self.model, identifier)

    def require(self, identifier, message: str | None = None) -> ModelT:
        found = self.get(identifier)
        if not found:
            raise NotFound(message or f"{self.model.__name__} {identifier} 不存在")
        return found

    def add(self, entity: ModelT) -> ModelT:
        self.db.add(entity)
        self.db.flush()
        return entity

    def list(self) -> list[ModelT]:
        return list(self.query().all())

    @staticmethod
    def check_version(entity, expected: int | None, label: str = "对象") -> None:
        """乐观并发。预期版本过期返回 409，不静默覆盖别人的编辑。"""
        if expected is None:
            return
        actual = getattr(entity, "row_version", None)
        if actual is None:
            return
        if int(expected) != int(actual):
            raise VersionConflict(
                f"{label}已被他人修改（当前版本 {actual}，提交版本 {expected}），请刷新后重试",
                {"current_version": actual, "submitted_version": int(expected)},
            )

    @staticmethod
    def bump(entity) -> None:
        if hasattr(entity, "row_version"):
            entity.row_version = int(entity.row_version or 0) + 1


class ScopedRepository(Repository[ModelT]):
    """带组织归属的业务对象。

    `query()` 一律先按 org_id 过滤，`get()` 拿到跨组织对象当作不存在——
    返回 404 而不是 403，否则「不存在」和「无权限」的区别本身就泄漏了对象存在。
    """

    def __init__(self, db: Session, ctx: AccessContext | None = None):
        super().__init__(db, ctx)

    @property
    def org_id(self) -> str:
        if not self.ctx:
            raise NotFound("缺少访问上下文，无法确定组织范围")
        return self.ctx.org_id

    def query(self) -> Query:
        query = self.db.query(self.model)
        if self.ctx is None:
            return query
        return query.filter(self.model.org_id == self.ctx.org_id)

    def get(self, identifier) -> ModelT | None:
        found = self.db.get(self.model, identifier)
        if found is None:
            return None
        if self.ctx is not None and getattr(found, "org_id", "") != self.ctx.org_id:
            return None
        return found

    def stamp(self, entity: ModelT) -> ModelT:
        """新建对象时补上组织归属，调用方不需要自己塞 org_id。"""
        if self.ctx is not None and hasattr(entity, "org_id") and not getattr(entity, "org_id", ""):
            entity.org_id = self.ctx.org_id
        return entity

    def add(self, entity: ModelT) -> ModelT:
        return super().add(self.stamp(entity))
