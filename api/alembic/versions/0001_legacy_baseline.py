"""现有结构基线

已有库用 `alembic stamp 0001_legacy_baseline` 打标，再 `upgrade head`；
空库直接 `upgrade head` 会先由本迁移建出这份基线。

Revision ID: 0001_legacy_baseline
Revises:
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_legacy_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("username", sa.String(), nullable=False, unique=True),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
    )
    op.create_table(
        "esignatures",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("meaning", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "capabilities",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("recovery", sa.JSON(), nullable=False),
        sa.Column("retired", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "islands",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
    )
    op.create_table(
        "stations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("island", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("cal_due", sa.String(), nullable=False),
        sa.Column("positions", sa.Integer(), nullable=False),
        sa.Column("clean", sa.Boolean(), nullable=False),
        sa.Column("limits", sa.JSON(), nullable=False),
        sa.Column("retired", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "adapters",
        sa.Column("station_id", sa.String(), sa.ForeignKey("stations.id"), primary_key=True),
        sa.Column("protocol", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("connected", sa.Boolean(), nullable=False),
        sa.Column("accepts_commands", sa.Boolean(), nullable=False),
        sa.Column("site_interlock", sa.Boolean(), nullable=False),
        sa.Column("dedup_count", sa.Integer(), nullable=False),
        sa.Column("last_heartbeat", sa.DateTime(), nullable=False),
        sa.Column("current_command_id", sa.String(), nullable=False),
    )
    op.create_table(
        "recipes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("updated", sa.String(), nullable=False),
        sa.Column("plate", sa.Integer(), nullable=False),
        sa.Column("risk", sa.String(), nullable=False),
        sa.Column("design", sa.String(), nullable=False),
        sa.Column("golden_batch_id", sa.String(), nullable=False),
        sa.Column("parent", sa.String(), nullable=False),
        sa.Column("needs_revision", sa.Boolean(), nullable=False),
        sa.Column("bom", sa.JSON(), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("diff", sa.JSON(), nullable=False),
    )
    op.create_table(
        "plans",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("recipe_id", sa.String(), sa.ForeignKey("recipes.id"), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("created", sa.String(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("repeats", sa.Integer(), nullable=False),
        sa.Column("layout", sa.String(), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("factors", sa.JSON(), nullable=False),
        sa.Column("control", sa.JSON(), nullable=True),
    )
    op.create_table(
        "batches",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("plan_id", sa.String(), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("recipe_id", sa.String(), sa.ForeignKey("recipes.id"), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("operator", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("recipe_snapshot", sa.JSON(), nullable=False),
        sa.Column("plan_snapshot", sa.JSON(), nullable=False),
        sa.Column("current_step", sa.Integer(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("held_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "allocations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.String(), sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("station_id", sa.String(), sa.ForeignKey("stations.id"), nullable=False),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
    )
    op.create_table(
        "samples",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("batch_id", sa.String(), sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("well", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("condition_group", sa.String(), nullable=False),
        sa.Column("condition_label", sa.String(), nullable=False),
        sa.Column("repeat", sa.Integer(), nullable=False),
        sa.Column("levels", sa.JSON(), nullable=True),
        sa.Column("is_control", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("quality", sa.String(), nullable=True),
        sa.Column("flag_note", sa.Text(), nullable=False),
        sa.Column("station_id", sa.String(), nullable=False),
    )
    op.create_table(
        "analysis_tasks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("sample_id", sa.String(), sa.ForeignKey("samples.id"), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("external_ref", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "results",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("sample_id", sa.String(), sa.ForeignKey("samples.id"), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("areal_density", sa.Float(), nullable=True),
        sa.Column("discharge_capacity", sa.Float(), nullable=True),
        sa.Column("retention", sa.Float(), nullable=True),
        sa.Column("raw_uri", sa.String(), nullable=False),
        sa.Column("checksum", sa.String(), nullable=False),
        sa.Column("parser_version", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "commands",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("batch_id", sa.String(), sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("station_id", sa.String(), sa.ForeignKey("stations.id"), nullable=False),
        sa.Column("capability", sa.String(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("checkpoint_id", sa.String(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "checkpoints",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("batch_id", sa.String(), sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("command_id", sa.String(), sa.ForeignKey("commands.id"), nullable=False, unique=True),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "adapter_executions",
        sa.Column("command_id", sa.String(), sa.ForeignKey("commands.id"), primary_key=True),
        sa.Column("station_id", sa.String(), sa.ForeignKey("stations.id"), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "telemetry",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("station_id", sa.String(), nullable=False),
        sa.Column("batch_id", sa.String(), nullable=False),
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column("setpoint", sa.Float(), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("quality", sa.String(), nullable=False),
        sa.Column("device_ts", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "lots",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("material", sa.String(), nullable=False),
        sa.Column("cas", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("qty", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("release", sa.String(), nullable=False),
        sa.Column("sds", sa.String(), nullable=False),
        sa.Column("compat", sa.String(), nullable=False),
        sa.Column("expiry", sa.String(), nullable=False),
        sa.Column("opened", sa.String(), nullable=False),
        sa.Column("storage", sa.String(), nullable=False),
        sa.Column("ghs", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="active"),
        sa.Column("scrap_reason", sa.String(), nullable=False, server_default=""),
    )
    op.create_table(
        "reservations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.String(), nullable=False),
        sa.Column("lot_id", sa.String(), sa.ForeignKey("lots.id"), nullable=False),
        sa.Column("qty", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("delivered_qty", sa.Float(), nullable=False),
    )
    op.create_table(
        "waste_tanks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("level_pct", sa.Float(), nullable=False),
        sa.Column("capacity_l", sa.Float(), nullable=False),
    )
    op.create_table(
        "alarms",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("severity", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("condition_active", sa.Boolean(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("response", sa.Text(), nullable=False),
        sa.Column("shelved_until", sa.String(), nullable=False),
        sa.Column("raised_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("time", sa.DateTime(), nullable=False),
        sa.Column("user", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("sign", sa.Boolean(), nullable=False),
        sa.Column("meaning", sa.String(), nullable=False),
        sa.Column("before", sa.String(), nullable=False),
        sa.Column("after", sa.String(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("signature_id", sa.String(), nullable=False),
        sa.Column("command_id", sa.String(), nullable=False),
        sa.Column("checkpoint_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "plan_batches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.String(), nullable=False),
        sa.Column("batch_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("ref", sa.String(), nullable=False),
    )


def downgrade() -> None:
    for table in [
        "plan_batches", "idempotency_keys", "audit_events", "alarms", "waste_tanks", "reservations",
        "lots", "telemetry", "adapter_executions", "checkpoints", "commands", "results",
        "analysis_tasks", "samples", "allocations", "batches", "plans", "recipes", "adapters",
        "stations", "islands", "capabilities", "esignatures", "users",
    ]:
        op.drop_table(table)
