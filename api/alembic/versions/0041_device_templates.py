"""设备接入模板：一类设备怎么接，存成有版本、要发布的模板；工位套用模板 + 自己的连接参数

Revision ID: 0041_device_templates
Revises: 0040_acceptance_runs

- device_templates：驱动、映射配置（不含每台设备的连接参数）、连接参数示例、支持标志、验收缺省，
  (组织, 编号, 修订号) 唯一；起草与改过草稿的人记在 editors（他们都不能发布它）。草稿可改可删；
  发布后内容与审批证据（起草人、编辑人、发布人、发布时间）由触发器冻结，状态只能从已发布改为退役，不许删除——
  套用过它的工位与验收报告都指回这一版；
- adapters.template_id / template_connection：工位套用的模板版本与工位自己的连接参数。工位上的 config 仍是
  合并后的完整配置，驱动照旧只读它；换模板版本时拿同一份连接参数重新合并；
- acceptance_runs.template_id / template_code / template_revision：验收时工位用的是哪一版模板。

已有工位不套模板（template_id 为空），照旧按自己的配置运行。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0041_device_templates"
down_revision: Union[str, None] = "0040_acceptance_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FROZEN = ("code", "revision", "driver", "protocol", "version", "config", "connection", "supports", "acceptance",
          "digest", "org_id", "created_by", "editors", "released_by", "released_at")
JSON_COLUMNS = {"config", "connection", "supports", "acceptance", "editors"}


def upgrade() -> None:
    op.create_table(
        "device_templates",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False, server_default=""),
        sa.Column("vendor", sa.String(), nullable=False, server_default=""),
        sa.Column("driver", sa.String(), nullable=False),
        sa.Column("protocol", sa.String(), nullable=False, server_default=""),
        sa.Column("version", sa.String(), nullable=False, server_default=""),
        sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("connection", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("supports", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("acceptance", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        # draft | released | retired
        sa.Column("state", sa.String(), nullable=False, server_default="draft"),
        sa.Column("digest", sa.String(), nullable=False, server_default=""),
        sa.Column("source", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_by_name", sa.String(), nullable=False, server_default=""),
        sa.Column("editors", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("released_by", sa.String(), nullable=False, server_default=""),
        sa.Column("released_by_name", sa.String(), nullable=False, server_default=""),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("org_id", "code", "revision", name="uq_device_template_revision"),
    )
    op.create_index("ix_device_templates_org_id", "device_templates", ["org_id"])
    changed = " OR ".join(
        f"NEW.{column}::jsonb IS DISTINCT FROM OLD.{column}::jsonb"
        if column in JSON_COLUMNS else f"NEW.{column} IS DISTINCT FROM OLD.{column}"
        for column in FROZEN
    )
    op.execute(sa.text(f"""
        CREATE OR REPLACE FUNCTION ilcs_device_template_guard() RETURNS trigger AS $$
        BEGIN
            IF OLD.state = 'draft' THEN
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'device_templates 发布过的模板不许删除（id=%）', OLD.id
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF {changed} THEN
                RAISE EXCEPTION 'device_templates 发布过的模板内容冻结：要改请新建修订（id=%）', OLD.id
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF NEW.state IS DISTINCT FROM OLD.state AND NOT (OLD.state = 'released' AND NEW.state = 'retired') THEN
                RAISE EXCEPTION 'device_templates 状态只能从已发布改为退役（id=%，% → %）', OLD.id, OLD.state, NEW.state
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """))
    op.execute(sa.text("""
        CREATE TRIGGER device_templates_guard
        BEFORE UPDATE OR DELETE ON device_templates
        FOR EACH ROW EXECUTE FUNCTION ilcs_device_template_guard()
    """))
    op.add_column("adapters", sa.Column("template_id", sa.String(), nullable=False, server_default=""))
    op.add_column("adapters", sa.Column("template_connection", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column("acceptance_runs", sa.Column("template_id", sa.String(), nullable=False, server_default=""))
    op.add_column("acceptance_runs", sa.Column("template_code", sa.String(), nullable=False, server_default=""))
    op.add_column("acceptance_runs", sa.Column("template_revision", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("acceptance_runs", "template_revision")
    op.drop_column("acceptance_runs", "template_code")
    op.drop_column("acceptance_runs", "template_id")
    op.drop_column("adapters", "template_connection")
    op.drop_column("adapters", "template_id")
    op.execute(sa.text("DROP TRIGGER IF EXISTS device_templates_guard ON device_templates"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS ilcs_device_template_guard()"))
    op.drop_index("ix_device_templates_org_id", table_name="device_templates")
    op.drop_table("device_templates")
