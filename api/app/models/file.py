"""文件对象。存储键由服务端生成，用户不能拼物理路径。"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.clock import now
from .base import Base, uid


class FileObject(Base):
    __tablename__ = "file_objects"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    filename: Mapped[str] = mapped_column(String)
    media_type: Mapped[str] = mapped_column(String)
    byte_size: Mapped[int] = mapped_column(BigInteger, default=0)
    checksum: Mapped[str] = mapped_column(String, default="")
    storage_key: Mapped[str] = mapped_column(String, unique=True)
    uploaded_by: Mapped[str] = mapped_column(String, default="")
    # uploading | available | failed：写入失败不留下可下载记录
    state: Mapped[str] = mapped_column(String, default="uploading")
    ref_type: Mapped[str] = mapped_column(String, default="")
    ref_id: Mapped[str] = mapped_column(String, default="")
    # simulation 标签不能被包装成真实采集原件
    origin: Mapped[str] = mapped_column(String, default="upload")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (Index("ix_file_ref", "ref_type", "ref_id"),)
