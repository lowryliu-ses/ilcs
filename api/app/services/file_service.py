"""文件与原始数据。

存储键由服务端生成；上传中 / 可用 / 失败分状态，写盘失败不留下「可下载」记录。
下载一律经过对象访问校验并记审计。服务器不抓取调用方传入的外部 URI。
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.config import settings
from ..core.context import AccessContext
from ..core.errors import NotFound, StateConflict, ValidationFailed
from ..models import FileObject, User
from ..repositories.files import FileRepository
from .audit_service import AuditService


class FileStore:
    """本地受控目录实现。换对象存储只替换这个类，接口不变。"""

    def __init__(self, root: str | None = None):
        self.root = Path(root or settings.file_root)

    def path_for(self, storage_key: str) -> Path:
        return self.root / storage_key

    def write(self, storage_key: str, stream) -> tuple[int, str]:
        target = self.path_for(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        with target.open("wb") as handle:
            while True:
                chunk = stream.read(1024 * 256)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.file_max_bytes:
                    handle.close()
                    target.unlink(missing_ok=True)
                    raise ValidationFailed(
                        f"文件超过上限 {settings.file_max_bytes // (1024 * 1024)} MiB",
                        code="file_too_large",
                    )
                digest.update(chunk)
                handle.write(chunk)
        return size, digest.hexdigest()

    def remove(self, storage_key: str) -> None:
        self.path_for(storage_key).unlink(missing_ok=True)

    def exists(self, storage_key: str) -> bool:
        return self.path_for(storage_key).is_file()

    def checksum(self, storage_key: str) -> str:
        digest = hashlib.sha256()
        with self.path_for(storage_key).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 256), b""):
                digest.update(chunk)
        return digest.hexdigest()


class FileService:
    def __init__(self, db: Session, ctx: AccessContext, store: FileStore | None = None):
        self.db = db
        self.ctx = ctx
        self.files = FileRepository(db, ctx)
        self.store = store or FileStore()
        self.audit = AuditService(db, ctx)

    # ---------- 写 ----------

    def upload(
        self, filename: str, media_type: str, stream, user: User,
        ref_type: str = "", ref_id: str = "", note: str = "",
    ) -> dict:
        if media_type not in settings.allowed_media_types:
            raise ValidationFailed(
                f"不支持的文件类型 {media_type}；允许 "
                f"{'、'.join(sorted(settings.allowed_media_types))}。"
                f"真实仪器的其他格式请先列入试点配置，不做静默丢弃",
                code="media_type_not_allowed",
            )
        suffix = Path(filename).suffix[:16]
        storage_key = f"{self.ctx.org_id}/{now():%Y/%m}/{uuid4().hex}{suffix}"
        record = FileObject(
            org_id=self.ctx.org_id, filename=filename[:255], media_type=media_type,
            storage_key=storage_key, uploaded_by=user.id, state="uploading",
            ref_type=ref_type, ref_id=ref_id, note=note, origin="upload",
        )
        self.files.add(record)
        self.db.flush()
        try:
            size, checksum = self.store.write(storage_key, stream)
        except Exception as exc:
            # 写盘失败：记录留在 failed 状态，永远不会出现在可下载列表里
            record.state = "failed"
            record.note = f"写入失败：{exc}"[:500]
            self.audit.record(
                user, "文件上传失败", record.id, before="uploading", after="failed",
                detail=f"{filename}；{exc}",
            )
            self.db.commit()
            raise
        record.byte_size = size
        record.checksum = checksum
        record.state = "available"
        self.audit.record(
            user, "上传文件", record.id, before="uploading", after="available",
            detail=f"{filename}；{size} 字节；sha256 {checksum[:16]}…；关联 {ref_type or '—'} {ref_id or '—'}",
        )
        self.db.commit()
        return self.out(record)

    def attach(self, file_id: str, ref_type: str, ref_id: str, user: User) -> dict:
        record = self.files.get(file_id)
        if not record:
            raise NotFound("文件不存在")
        if record.state != "available":
            raise StateConflict(f"文件状态为 {record.state}，不能关联")
        record.ref_type = ref_type
        record.ref_id = ref_id
        self.audit.record(user, "关联文件", record.id, detail=f"{ref_type} {ref_id}")
        self.db.commit()
        return self.out(record)

    def register_external(
        self, filename: str, media_type: str, source_ref: str, origin: str, note: str,
    ) -> FileObject:
        """登记「有引用但取不到原件」的记录。

        用于历史模拟数据：保留 simulation 标签与原 URI，但状态是 missing，
        不会被当成真实采集原件下载。
        """
        record = FileObject(
            org_id=self.ctx.org_id, filename=filename, media_type=media_type,
            storage_key=f"external/{uuid4().hex}", state="missing", origin=origin,
            note=f"{note}；原始引用 {source_ref}"[:500],
        )
        self.files.add(record)
        return record

    def cleanup_orphans(self, older_than_hours: int = 24, limit: int = 100) -> dict:
        """受控清理未关联的临时文件。正式引用由业务表反查，不能只信关联标签。"""
        from datetime import timedelta

        cutoff = now() - timedelta(hours=older_than_hours)
        removed = []
        for record in self.files.orphans(cutoff, limit):
            self.store.remove(record.storage_key)
            self.audit.record(
                None, "清理未关联文件", record.id,
                before=record.state, after="已物理删除",
                detail=(
                    f"{record.filename}；创建于 {record.created_at.isoformat(timespec='seconds')}；"
                    f"关联元数据 {record.ref_type or '—'} / {record.ref_id or '—'}"
                ),
            )
            self.db.delete(record)
            removed.append(record.id)
        self.db.commit()
        return {"removed": removed, "count": len(removed)}

    # ---------- 读 ----------

    def out(self, record: FileObject) -> dict:
        return {
            "id": record.id,
            "filename": record.filename,
            "media_type": record.media_type,
            "byte_size": record.byte_size,
            "checksum": record.checksum,
            "state": record.state,
            "origin": record.origin,
            "ref_type": record.ref_type,
            "ref_id": record.ref_id,
            "note": record.note,
            "uploaded_by": record.uploaded_by,
            "created_at": record.created_at.isoformat(timespec="seconds"),
            "downloadable": record.state == "available",
        }

    def list_for(self, ref_type: str, ref_id: str) -> list[dict]:
        return [self.out(row) for row in self.files.for_ref(ref_type, ref_id)]

    def page(self, offset: int, limit: int, ref_type: str = ""):
        rows, total = self.files.page(offset, limit, ref_type)
        return [self.out(row) for row in rows], total

    def open_for_download(self, file_id: str, user: User) -> tuple[FileObject, Path]:
        """下载。跨组织拿不到（get 已按范围过滤），损坏的明确报错而不是冒充原件。"""
        record = self.files.get(file_id)
        if not record:
            raise NotFound("文件不存在")
        if record.state == "missing":
            raise StateConflict(
                f"该记录只有引用没有原件：{record.note or '原件缺失'}；不提供替代内容",
                code="file_missing",
            )
        if record.state != "available":
            raise StateConflict(f"文件状态为 {record.state}，不可下载", code="file_not_available")
        path = self.store.path_for(record.storage_key)
        if not path.is_file():
            raise StateConflict(
                "文件在存储中缺失，已记录异常；不提供替代内容", code="file_missing_on_disk"
            )
        actual = self.store.checksum(record.storage_key)
        if record.checksum and actual != record.checksum:
            self.audit.record(
                user, "文件摘要不一致", record.id,
                detail=f"登记 {record.checksum[:16]}…，实际 {actual[:16]}…；已阻止下载",
            )
            self.db.commit()
            raise StateConflict(
                "文件内容摘要与登记值不一致，可能已损坏；已阻止下载并记录异常",
                code="file_checksum_mismatch",
            )
        self.audit.record(
            user, "下载文件", record.id, detail=f"{record.filename}；{record.byte_size} 字节"
        )
        self.db.commit()
        return record, path
