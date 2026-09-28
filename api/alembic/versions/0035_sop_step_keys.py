"""SOP 结构化步骤的稳定标识

Revision ID: 0035_sop_step_keys
Revises: 0034_backfill_command_outcome

流程节点原来只按 `sop_step`（数组序号）引用 SOP 步骤：新版本在前面插入一步，旧流程节点就会显示
另一步的说明。之后每一步带稳定标识 `key`，节点按标识引用，修订时从生效版本复制步骤会连同标识带过去。

回填：已有 SOP 版本的每一步补一个版本内唯一的标识。流程与在途批次快照不改（受控内容不因迁移变动）：
没有标识的旧节点按序号、经流程关联版本的标识、按类型与能力唯一匹配依次解析，对不上时明示映射失效。
"""
import json
from typing import Sequence, Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0035_sop_step_keys"
down_revision: Union[str, None] = "0034_backfill_command_outcome"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    for row in bind.execute(sa.text("SELECT id, steps FROM sop_versions")).fetchall():
        steps = row.steps if isinstance(row.steps, list) else json.loads(row.steps or "[]")
        if not steps:
            continue
        seen: set[str] = set()
        keyed = []
        changed = False
        for step in steps:
            item = dict(step)
            key = str(item.get("key") or "").strip()
            while not key or key in seen:
                key = uuid4().hex[:8]
                changed = True
            seen.add(key)
            item["key"] = key
            keyed.append(item)
        if changed:
            bind.execute(
                sa.text("UPDATE sop_versions SET steps = CAST(:steps AS json) WHERE id = :id"),
                {"steps": json.dumps(keyed, ensure_ascii=False), "id": row.id},
            )


def downgrade() -> None:
    # 标识留在步骤里对旧代码无害（旧代码不读它）
    pass
