"""审计表数据库级只追加；出厂角色补齐

Revision ID: 0027_audit_append_only
Revises: 0026_environment_people

- audit_events 加行级触发器：任何 UPDATE / DELETE 都报错（应用、脚本、手工 SQL 一视同仁）；
  同时 REVOKE UPDATE, DELETE FROM PUBLIC。清库（TRUNCATE / DROP）属于运维操作，不在拦截范围。
- 新增出厂角色（自动化工程师、实验室经理、审计员）只是代码里的默认矩阵，不需要改表。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027_audit_append_only"
down_revision: Union[str, None] = "0026_environment_people"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION ilcs_audit_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events 只追加：禁止 % 审计记录（id=%）', TG_OP, OLD.id
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events"))
    op.execute(sa.text("""
        CREATE TRIGGER audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION ilcs_audit_append_only()
    """))
    op.execute(sa.text("REVOKE UPDATE, DELETE ON audit_events FROM PUBLIC"))


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS ilcs_audit_append_only()"))
