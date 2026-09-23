"""数据库版本校验。

应用启动不再建表、不再补列、不再自动播种：迁移是部署的独立步骤。
启动时只做一件事——确认库的 Alembic 版本等于本代码期待的版本。不一致就拒绝
进入可用状态，而不是自行改结构：多副本发布时后者会让两个版本各改一半。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, inspect, text

# 本代码要求的迁移版本。新增迁移时同步改这里，否则升级后的库会被判为「超前」。
EXPECTED_REVISION = "0010_operations_b"
MIGRATION_HINT = (
    "请先执行迁移：cd api && ILCS_DATABASE_URL=... .venv/bin/alembic upgrade head"
    "（已有旧库先 alembic stamp 0001_legacy_baseline）"
)


class SchemaMismatch(RuntimeError):
    pass


def current_revision(engine: Engine) -> str | None:
    inspector = inspect(engine)
    if "alembic_version" not in inspector.get_table_names():
        return None
    with engine.connect() as connection:
        row = connection.execute(text("SELECT version_num FROM alembic_version")).fetchone()
    return row[0] if row else None


def known_revisions() -> list[str]:
    versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    if not versions.exists():
        return []
    return sorted(path.stem for path in versions.glob("*.py"))


def verify(engine: Engine) -> str:
    """返回当前版本；不匹配时抛 SchemaMismatch，调用方据此拒绝启动。"""
    revision = current_revision(engine)
    if revision is None:
        raise SchemaMismatch(f"数据库没有迁移版本记录。{MIGRATION_HINT}")
    if revision != EXPECTED_REVISION:
        direction = "落后" if revision < EXPECTED_REVISION else "超前于"
        raise SchemaMismatch(
            f"数据库版本 {revision} {direction}应用期待的 {EXPECTED_REVISION}，"
            f"拒绝以不兼容结构启动。{MIGRATION_HINT}"
        )
    return revision
