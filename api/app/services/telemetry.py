"""遥测点的归属：哪条指令、哪一步、谁在值守、哪个样本。

样本只在能确定时填：设备按孔位报（well → 本批次该孔位的样本）或点上直接带了样本号（须属于本批次），
或者批次只有一个样本。一板多样的整板遥测（箱温、真空度）不属于任何单个样本，留空，按批次追溯。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..domain.steps import normalize, step_id_of
from ..models import Batch, Command, Sample


def context(db: Session, batch: Batch | None, command: Command | None, *, well: str = "", sample_id: str = "") -> dict:
    if batch is None or command is None:
        return {"command_id": command.id if command else "", "step_id": "", "step_index": None, "operator": "", "sample_id": ""}
    steps = normalize((batch.recipe_snapshot or {}).get("steps") or [])
    index = command.step_index
    samples = db.query(Sample).filter(Sample.batch_id == batch.id).all()
    chosen = ""
    if sample_id:
        chosen = next((row.physical_sample_id or row.id for row in samples
                       if sample_id in {row.id, row.physical_sample_id}), "")
    elif well:
        chosen = next((row.physical_sample_id or row.id for row in samples if row.well == well), "")
    elif len(samples) == 1:
        chosen = samples[0].physical_sample_id or samples[0].id
    return {
        "command_id": command.id,
        "step_id": step_id_of(steps[index], index) if 0 <= index < len(steps) else "",
        "step_index": index,
        "operator": batch.operator or "",
        "sample_id": chosen,
    }
