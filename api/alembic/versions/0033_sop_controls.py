"""SOP 受控元数据与版本取代

Revision ID: 0033_sop_controls
Revises: 0032_outcome_and_labware_wells

- sops.category / sops.owner_id：受控文件的分类与负责人，属于文件本身而不是某个版本；
- sop_versions.effective_to：失效时间。到点后不再是「生效版本」，新批次不再按它执行；
- sop_versions.review_due：下次复审日期。过期只提醒，不自动失效；
- sop_versions.superseded_by / superseded_at：同编号新版本发布时，旧的已发布版本被它取代，
  失效时间取新版本的生效时间。

回填：同一 SOP 下有多个已发布版本的，按生效时间排序，除最后一个外都记为被下一个取代——
升级前它们同时「生效」，新任务可以随意挑旧版本，这正是要修掉的问题。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0033_sop_controls"
down_revision: Union[str, None] = "0032_outcome_and_labware_wells"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sops", sa.Column("category", sa.String(), nullable=False, server_default=""))
    op.add_column("sops", sa.Column("owner_id", sa.String(), nullable=False, server_default=""))
    op.add_column("sop_versions", sa.Column("effective_to", sa.DateTime(), nullable=True))
    op.add_column("sop_versions", sa.Column("review_due", sa.Date(), nullable=True))
    op.add_column("sop_versions", sa.Column("superseded_by", sa.String(), nullable=False, server_default=""))
    op.add_column("sop_versions", sa.Column("superseded_at", sa.DateTime(), nullable=True))
    op.execute(
        """
        UPDATE sop_versions AS old
        SET superseded_by = ranked.next_id,
            superseded_at = ranked.next_from,
            effective_to = COALESCE(old.effective_to, ranked.next_from)
        FROM (
            SELECT id,
                   LEAD(id) OVER (PARTITION BY sop_id ORDER BY effective_from, created_at, id) AS next_id,
                   LEAD(effective_from) OVER (PARTITION BY sop_id ORDER BY effective_from, created_at, id) AS next_from
            FROM sop_versions
            WHERE state = 'published' AND effective_from IS NOT NULL
        ) AS ranked
        WHERE old.id = ranked.id AND ranked.next_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("sop_versions", "superseded_at")
    op.drop_column("sop_versions", "superseded_by")
    op.drop_column("sop_versions", "review_due")
    op.drop_column("sop_versions", "effective_to")
    op.drop_column("sops", "owner_id")
    op.drop_column("sops", "category")
