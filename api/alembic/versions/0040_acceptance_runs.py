"""设备接入验收记录与配置变更后的验收闸门

Revision ID: 0040_acceptance_runs
Revises: 0039_dataset_snapshots

- acceptance_runs：一次接入验收的申请、执行与报告。排队中、执行中可以改（执行器领取、写结论）；
  出了结论（完成、出错、取消）就由触发器拒绝再改，任何时候都不许删——验收报告是上线证据；
- adapters.acceptance_required：配置变更后还欠的验收级别（'' 不欠 / readonly / physical），
  欠着的工位按「待接入验收」挡住下发；adapters.accepted_config_version / accepted_run_id：
  最近一次满足要求的验收对应的配置版本与记录。

已有适配器不追溯：迁移前在用的配置 acceptance_required 为空，照常可用，下次改配置时才进闸门。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0040_acceptance_runs"
down_revision: Union[str, None] = "0039_dataset_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "acceptance_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("station_id", sa.String(), nullable=False),
        # readonly | physical
        sa.Column("level", sa.String(), nullable=False, server_default="readonly"),
        sa.Column("faults", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("capability", sa.String(), nullable=False, server_default=""),
        sa.Column("params", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        # manual | config_change | device_online | restart | waiver
        sa.Column("trigger", sa.String(), nullable=False, server_default="manual"),
        # 现场批准：动作级验收由谁批准、依据（DEC-02）
        sa.Column("approval", sa.Text(), nullable=False, server_default=""),
        sa.Column("signature_id", sa.String(), nullable=False, server_default=""),
        sa.Column("requested_by", sa.String(), nullable=False, server_default=""),
        sa.Column("requested_by_id", sa.String(), nullable=False, server_default=""),
        # queued | running | done | error | cancelled
        sa.Column("state", sa.String(), nullable=False, server_default="queued"),
        # 验收的对象：执行时刻的驱动与配置
        sa.Column("kind", sa.String(), nullable=False, server_default=""),
        sa.Column("driver", sa.String(), nullable=False, server_default=""),
        sa.Column("protocol", sa.String(), nullable=False, server_default=""),
        sa.Column("adapter_version", sa.String(), nullable=False, server_default=""),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("config_digest", sa.String(), nullable=False, server_default=""),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("simulator", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("identity", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("checks", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("report_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_acceptance_runs_org_id", "acceptance_runs", ["org_id"])
    op.create_index("ix_acceptance_runs_station_id", "acceptance_runs", ["station_id"])
    op.create_index("ix_acceptance_runs_state", "acceptance_runs", ["state"])
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION ilcs_acceptance_run_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'acceptance_runs 是上线证据：禁止删除（id=%）', OLD.id
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF OLD.state NOT IN ('queued', 'running') THEN
                RAISE EXCEPTION 'acceptance_runs 已出结论（%）：禁止修改（id=%）', OLD.state, OLD.id
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER acceptance_runs_guard
        BEFORE UPDATE OR DELETE ON acceptance_runs
        FOR EACH ROW EXECUTE FUNCTION ilcs_acceptance_run_guard()
    """))
    op.execute(sa.text("REVOKE DELETE ON acceptance_runs FROM PUBLIC"))
    op.add_column("adapters", sa.Column("acceptance_required", sa.String(), nullable=False, server_default=""))
    op.add_column("adapters", sa.Column("accepted_config_version", sa.Integer(), nullable=True))
    op.add_column("adapters", sa.Column("accepted_run_id", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("adapters", "accepted_run_id")
    op.drop_column("adapters", "accepted_config_version")
    op.drop_column("adapters", "acceptance_required")
    op.execute(sa.text("DROP TRIGGER IF EXISTS acceptance_runs_guard ON acceptance_runs"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS ilcs_acceptance_run_guard()"))
    op.drop_index("ix_acceptance_runs_state", table_name="acceptance_runs")
    op.drop_index("ix_acceptance_runs_station_id", table_name="acceptance_runs")
    op.drop_index("ix_acceptance_runs_org_id", table_name="acceptance_runs")
    op.drop_table("acceptance_runs")
