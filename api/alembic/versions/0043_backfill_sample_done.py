"""已跑完批次的运行分配完成状态回填

Revision ID: 0043_backfill_sample_done
Revises: 0042_formulation_templates

以前只有历史三指标回传会把运行分配记为完成（`samples.state = done`）；检测任务写入的类型化结果（结果回传、
人工录入、设备回报）从不改它，批次跑完、结果都到齐了，运行分配仍是 running，结果分析「运行分配」与看板
样品完成数一直是 0。新代码按「批次跑完 + 名下未取消的检测任务都采集齐」记完成（`AnalysisService.settle_assignment`）。

按同一条规则回填：只动已完成批次里仍是 running、至少有一个未取消检测任务且都已采集的运行分配。
在途批次、还有任务没采集齐的、没有检测任务的保持原样；已失败、已拆分的不动。数据回填不回退。
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0043_backfill_sample_done"
down_revision: Union[str, None] = "0042_formulation_templates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE samples AS s
        SET state = 'done'
        FROM batches AS b
        WHERE b.id = s.batch_id
          AND b.state = 'done'
          AND s.state = 'running'
          AND EXISTS (
            SELECT 1 FROM analysis_tasks AS t WHERE t.sample_id = s.id AND t.state <> 'cancelled'
          )
          AND NOT EXISTS (
            SELECT 1 FROM analysis_tasks AS t WHERE t.sample_id = s.id AND t.state NOT IN ('collected', 'cancelled')
          )
        """
    )


def downgrade() -> None:
    # 回填的是运行分配的完成状态，回到 0042 时保留：0042 的代码同样把 done 当作完成
    pass
