"""历史指令的设备结论回填

Revision ID: 0034_backfill_command_outcome
Revises: 0033_sop_controls

0032 给 commands 加 outcome 列时，已有指令一律记为空串。上一版回执路径已经形成的「设备收到、结论未知」
（指令 unknown / manual、投递 delivered、台账回执 state=unknown）因此不满足未知占用规则：升级后工位被
当作空闲，下一条动作可以压上一台可能仍在动作的设备。

按幂等台账回填：
- 台账明确为 unknown 的记 unknown：继续占用工位，直到现场核查给出结论；
- 台账明确为 failed 的记 failed：设备明确报了失败、已停下，与新回执路径一致；
- 其余判断不了的保持空串，不猜，由迁移核对（「结论待核查的指令」）列出，交现场核查。

只动还没有结论的指令（unknown / manual），已经核查过的记录不受影响。数据回填不回退。
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0034_backfill_command_outcome"
down_revision: Union[str, None] = "0033_sop_controls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backfill(outcome: str) -> None:
    op.execute(
        f"""
        UPDATE commands AS c
        SET outcome = '{outcome}'
        FROM adapter_executions AS ae
        WHERE ae.command_id = c.id
          AND c.state IN ('unknown', 'manual')
          AND c.delivery_state = 'delivered'
          AND c.outcome = ''
          AND (ae.state = '{outcome}' OR (ae.result::jsonb ->> 'state') = '{outcome}')
        """
    )


def upgrade() -> None:
    _backfill("unknown")
    _backfill("failed")


def downgrade() -> None:
    # 回填的是设备侧结论，回到 0033 时保留它们：0033 的代码同样读 outcome
    pass
