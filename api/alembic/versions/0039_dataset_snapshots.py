"""实验活动的数据集快照与分析运行记录

Revision ID: 0039_dataset_snapshots
Revises: 0038_param_specs_bindings

- dataset_snapshots：训练数据的快照，固化当时纳入的结果版本清单、排除清单、数据行与原始文件摘要。
  之后结果被更正也不改快照，按快照导出的内容不变；
- analysis_runs：一次分析 / 模型运行的输入快照、程序与模型版本、参数、随机种子与输出摘要，
  (组织, 方案, 运行编号) 唯一；
- plan_proposals.analysis_run_id：提案由哪次分析运行生成。已有提案为空：它们提交时没有登记运行。

两张新表与审计表一样由数据库触发器拒绝 UPDATE / DELETE：快照改一行，「当时用了哪批数据」就说不清了。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039_dataset_snapshots"
down_revision: Union[str, None] = "0038_param_specs_bindings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dataset_snapshots",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("plan_id", sa.String(), nullable=False),
        sa.Column("root_plan_id", sa.String(), nullable=False),
        sa.Column("snapshot_key", sa.String(), nullable=False, server_default=""),
        sa.Column("filters", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("result_versions", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("exclusions", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("rows", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("files", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("digest", sa.String(), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_dataset_snapshots_org_id", "dataset_snapshots", ["org_id"])
    op.create_index("ix_dataset_snapshots_plan_id", "dataset_snapshots", ["plan_id"])
    op.create_index("ix_dataset_snapshots_root_plan_id", "dataset_snapshots", ["root_plan_id"])
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("plan_id", sa.String(), nullable=False),
        sa.Column("root_plan_id", sa.String(), nullable=False),
        sa.Column("run_key", sa.String(), nullable=False),
        sa.Column("snapshot_id", sa.String(), nullable=False),
        sa.Column("program", sa.String(), nullable=False, server_default=""),
        sa.Column("program_version", sa.String(), nullable=False, server_default=""),
        sa.Column("model_version", sa.String(), nullable=False, server_default=""),
        sa.Column("params", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("seed", sa.String(), nullable=False, server_default=""),
        sa.Column("outputs", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("digest", sa.String(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("org_id", "plan_id", "run_key", name="uq_analysis_run_key"),
    )
    op.create_index("ix_analysis_runs_org_id", "analysis_runs", ["org_id"])
    op.create_index("ix_analysis_runs_plan_id", "analysis_runs", ["plan_id"])
    op.create_index("ix_analysis_runs_root_plan_id", "analysis_runs", ["root_plan_id"])
    op.create_index("ix_analysis_runs_snapshot_id", "analysis_runs", ["snapshot_id"])
    op.add_column(
        "plan_proposals", sa.Column("analysis_run_id", sa.String(), nullable=False, server_default=""),
    )
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION ilcs_frozen_record() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% 只追加：禁止 % 记录（id=%）', TG_TABLE_NAME, TG_OP, OLD.id
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$ LANGUAGE plpgsql
    """))
    for table in ("dataset_snapshots", "analysis_runs"):
        op.execute(sa.text(f"""
            CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION ilcs_frozen_record()
        """))
        op.execute(sa.text(f"REVOKE UPDATE, DELETE ON {table} FROM PUBLIC"))


def downgrade() -> None:
    for table in ("dataset_snapshots", "analysis_runs"):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS ilcs_frozen_record()"))
    op.drop_column("plan_proposals", "analysis_run_id")
    op.drop_index("ix_analysis_runs_snapshot_id", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_root_plan_id", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_plan_id", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_org_id", table_name="analysis_runs")
    op.drop_table("analysis_runs")
    op.drop_index("ix_dataset_snapshots_root_plan_id", table_name="dataset_snapshots")
    op.drop_index("ix_dataset_snapshots_plan_id", table_name="dataset_snapshots")
    op.drop_index("ix_dataset_snapshots_org_id", table_name="dataset_snapshots")
    op.drop_table("dataset_snapshots")
