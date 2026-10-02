"""手动写设备点位：point_writes

Revision ID: 0049_point_writes
Revises: 0048_sample_merge

映射驱动（Modbus / OPC UA 点表、REST、串口命令）分成两层：只配点表就能读点、手动写声明了可写的点；参与自动流程
才要能力映射与状态。手动写由人签名申请、执行器执行（执行器是唯一驱动设备的进程），先读、写、再回读，记录永不删除。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0049_point_writes"
down_revision: Union[str, None] = "0048_sample_merge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "point_writes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("station_id", sa.String(), nullable=False),
        sa.Column("point", sa.String(), nullable=False),
        # {"value": 要写的值}（JSON 列装标量要包一层）
        sa.Column("value", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("signature_id", sa.String(), nullable=False, server_default=""),
        sa.Column("requested_by", sa.String(), nullable=False, server_default=""),
        sa.Column("requested_by_id", sa.String(), nullable=False, server_default=""),
        # 申请时的配置版本：执行前配置变了就不执行
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="0"),
        # queued | running | done | failed | unknown | cancelled
        sa.Column("state", sa.String(), nullable=False, server_default="queued"),
        sa.Column("before", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("after", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("matches", sa.Boolean(), nullable=True),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_point_writes_org_id", "point_writes", ["org_id"])
    op.create_index("ix_point_writes_station_id", "point_writes", ["station_id"])
    op.create_index("ix_point_writes_state", "point_writes", ["state"])
    # 写过设备的记录是证据：出了结论不许改、任何时候不许删（和 acceptance_runs 一样）
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION ilcs_point_write_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'point_writes 是写设备的记录：禁止删除（id=%）', OLD.id
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF OLD.state NOT IN ('queued', 'running') THEN
                RAISE EXCEPTION 'point_writes 已出结论（%）：禁止修改（id=%）', OLD.state, OLD.id
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER point_writes_guard
        BEFORE UPDATE OR DELETE ON point_writes
        FOR EACH ROW EXECUTE FUNCTION ilcs_point_write_guard()
    """))
    op.execute(sa.text("REVOKE DELETE ON point_writes FROM PUBLIC"))


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS point_writes_guard ON point_writes"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS ilcs_point_write_guard()"))
    op.drop_index("ix_point_writes_state", table_name="point_writes")
    op.drop_index("ix_point_writes_station_id", table_name="point_writes")
    op.drop_index("ix_point_writes_org_id", table_name="point_writes")
    op.drop_table("point_writes")
