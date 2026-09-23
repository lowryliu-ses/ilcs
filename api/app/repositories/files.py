from __future__ import annotations

from datetime import datetime

from sqlalchemy import exists, or_

from ..models import (
    CalibrationRecord, FileObject, Qualification, ReportVersion, ResultValue, SopVersion,
)
from .base import ScopedRepository


class FileRepository(ScopedRepository[FileObject]):
    model = FileObject

    def for_ref(self, ref_type: str, ref_id: str) -> list[FileObject]:
        return list(
            self.query()
            .filter(
                FileObject.ref_type == ref_type,
                FileObject.ref_id == ref_id,
                FileObject.state == "available",
            )
            .order_by(FileObject.created_at)
            .all()
        )

    def available(self, file_id: str) -> FileObject | None:
        found = self.get(file_id)
        if found and found.state == "available":
            return found
        return None

    def orphans(self, cutoff: datetime, limit: int = 100) -> list[FileObject]:
        """领取过期且未被任何受控业务对象引用的文件。

        完整的 ``ref_type/ref_id`` 表示调用方已显式关联；只有一半通常是表单上传后尚未
        保存业务对象。反过来，早期数据可能只在业务表保存 file_id，辅助元数据未补齐，
        因此还必须从所有正式引用列反查后再删。
        """
        referenced = or_(
            exists().where(SopVersion.file_id == FileObject.id),
            exists().where(Qualification.evidence_file_id == FileObject.id),
            exists().where(CalibrationRecord.certificate_file_id == FileObject.id),
            exists().where(ResultValue.raw_file_id == FileObject.id),
            exists().where(ReportVersion.pdf_file_id == FileObject.id),
        )
        incomplete_link = or_(
            FileObject.ref_type == "",
            FileObject.ref_id == "",
            FileObject.state != "available",
        )
        query = (
            self.query()
            .filter(FileObject.created_at <= cutoff, incomplete_link, ~referenced)
            .order_by(FileObject.created_at, FileObject.id)
            .limit(max(1, limit))
        )
        query = query.with_for_update(skip_locked=True)
        return list(query.all())

    def page(self, offset: int, limit: int, ref_type: str = ""):
        query = self.query()
        if ref_type:
            query = query.filter(FileObject.ref_type == ref_type)
        total = query.count()
        rows = query.order_by(FileObject.created_at.desc()).offset(offset).limit(limit).all()
        return list(rows), total
