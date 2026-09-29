"""前馈参数：称重结果驱动注液量。在批次快照上给注液步骤声明绑定，用模拟设备回执控制称重读数。

R-205：s01 真空干燥 → s02 称重选片（cap.weigh，mass 单位 g）→ s03 注液封口（cap.assemble，electrolyte 单位 μL，
ST-06 极限 10–200）→ s04 电性能测试。
"""
from dataclasses import replace

import pytest

from test_failure_paths import running_batch  # noqa: F401  （复用 fixture）

BINDING = {
    "source_step_id": "s02", "field": "mass", "scope": "sample", "unit": "g",
    "coefficient": {"value": 3900, "unit": "μL/g"}, "expect": [40, 80],
}


@pytest.fixture()
def readings(monkeypatch):
    """按步骤注入设备回执：{step_id: {"delivered": {...}, "quality": "good"}}。"""
    from app.adapters.simulation import SimulationAdapter

    plan: dict[str, dict] = {}
    original = SimulationAdapter.submit

    def submit(self, request):
        result = original(self, request)
        extra = plan.get(request.step_id)
        if not extra:
            return result
        patched = replace(
            result, delivered={**result.delivered, **extra.get("delivered", {})},
            quality=extra.get("quality", result.quality),
        )
        self._ledger[request.command_id] = patched
        return patched

    monkeypatch.setattr(SimulationAdapter, "submit", submit)
    return plan


def _rewrite(batch_id: str, **steps: dict) -> None:
    """改批次快照里的步骤：键是 step_id，值是要合并进去的字段（值为 None 的键删除）。"""
    from app.core.db import SessionLocal
    from app.models import Batch

    with SessionLocal() as db:
        batch = db.get(Batch, batch_id)
        snapshot = dict(batch.recipe_snapshot)
        rows = []
        for step in snapshot["steps"]:
            row = dict(step)
            for key, value in (steps.get(row.get("step_id")) or {}).items():
                if value is None:
                    row.pop(key, None)
                else:
                    row[key] = value
            rows.append(row)
        snapshot["steps"] = rows
        batch.recipe_snapshot = snapshot
        db.commit()


def _bind(batch_id: str, binding: dict | None = None) -> None:
    _rewrite(batch_id, s03={"params": {}, "bindings": {"electrolyte": binding or BINDING}})


def _samples(operator, batch_id: str) -> list[dict]:
    detail = operator.get(f"/api/batches/{batch_id}").json()
    return [row for row in detail["samples"] if row["state"] not in {"failed", "split"}]


def _run(operator, batch_id, executor, rounds=16):
    for _ in range(rounds):
        executor()
    return operator.get(f"/api/batches/{batch_id}").json()


def _fill_command(detail: dict) -> dict:
    rows = [row for row in detail["commands"] if row["step_index"] == 2 and row["type"] == "dispatch"]
    assert rows, "注液步骤应当已经生成指令"
    return rows[-1]


def _params(command_id: str) -> dict:
    from app.core.db import SessionLocal
    from app.models import Command

    with SessionLocal() as db:
        return dict(db.get(Command, command_id).params or {})


def test_per_sample_weights_drive_fill_volume(operator, running_batch, executor, readings):
    samples = _samples(operator, running_batch)
    masses = {row["well"]: round(0.0150 + 0.0001 * index, 4) for index, row in enumerate(samples)}
    readings["s02"] = {"delivered": {"wells": {well: {"mass": mass} for well, mass in masses.items()}}}
    _bind(running_batch)

    detail = _run(operator, running_batch, executor)
    assert detail["state"] == "done", detail.get("failure_reason")
    command = _fill_command(detail)
    wells = _params(command["id"])["wells"]
    for well, mass in masses.items():
        assert wells[well]["electrolyte"] == pytest.approx(mass * 3900), "注液量 = 该样本极片质量 × 系数"
    records = command["bindings"]
    assert len(records) == len(samples)
    first = next(row for row in records if row["well"] == samples[0]["well"])
    assert first["source_step_id"] == "s02" and first["source_kind"] == "device" and first["source_ref"]
    assert first["unit"] == "g" and first["coefficient"] == "3900" and first["target_unit"] == "μL"
    assert first["raw"] == str(masses[samples[0]["well"]]) and first["coefficient_source"] == "flow"


def test_missing_sample_weight_blocks_the_whole_command(operator, running_batch, executor, readings):
    samples = _samples(operator, running_batch)
    wells = {row["well"]: {"mass": 0.0152} for row in samples[1:]}
    readings["s02"] = {"delivered": {"wells": wells}}
    _bind(running_batch)

    detail = _run(operator, running_batch, executor)
    command = _fill_command(detail)
    assert command["delivery_state"] == "unreachable", "指令没离开系统"
    assert "前馈参数不能下发" in command["error"] and samples[0]["well"] in command["error"]
    assert detail["state"] == "fault"
    assert "wells" not in _params(command["id"]), "缺一个样本就整条不下发，不拿缺省值顶上"


def test_value_outside_expected_range_is_not_dispatched(operator, running_batch, executor, readings):
    samples = _samples(operator, running_batch)
    readings["s02"] = {"delivered": {"wells": {row["well"]: {"mass": 0.0300} for row in samples}}}
    _bind(running_batch)

    detail = _run(operator, running_batch, executor)
    command = _fill_command(detail)
    assert command["delivery_state"] == "unreachable"
    assert "预期范围" in command["error"] and "117" in command["error"]


def test_uncertain_checkpoint_is_not_used_as_setpoint(operator, running_batch, executor, readings):
    samples = _samples(operator, running_batch)
    readings["s02"] = {
        "delivered": {"wells": {row["well"]: {"mass": 0.0152} for row in samples}}, "quality": "uncertain",
    }
    _bind(running_batch)

    detail = _run(operator, running_batch, executor)
    command = _fill_command(detail)
    assert command["delivery_state"] == "unreachable"
    assert "uncertain" in command["error"] and "人工确认" in command["error"]


def test_batch_scope_binding_sets_one_value(operator, running_batch, executor, readings):
    """整批一个值：称重步骤回执里的 mass（模拟设备回显设定值 0.0152 g）× 3900 μL/g。"""
    _bind(running_batch, {**BINDING, "scope": "batch"})

    detail = _run(operator, running_batch, executor)
    assert detail["state"] == "done", detail.get("failure_reason")
    command = _fill_command(detail)
    assert _params(command["id"])["electrolyte"] == pytest.approx(59.28)
    assert [row["sample_id"] for row in command["bindings"]] == [""]


def test_manual_per_sample_weights_drive_fill_volume(operator, running_batch, executor):
    """人工逐片称重：记录表单按样本录入，缺样本不能提交；提交后注液量按各自的质量算。"""
    # 硬时限按上一步的设备检查点起算，人工步骤没有检查点：改成人工称重时一并去掉注液的硬时限
    _rewrite(
        running_batch,
        s02={"kind": "manual", "cap": None, "params": None,
             "form": [{"key": "mass", "label": "极片质量", "type": "number", "per_sample": True}]},
        s03={"hard": None},
    )
    _bind(running_batch, {**BINDING, "unit": "mg", "coefficient": {"value": 3.9, "unit": "μL/mg"}})
    detail = _run(operator, running_batch, executor, rounds=8)
    run = next(row for row in detail["step_runs"] if row["step_id"] == "s02")
    assert run["state"] == "ready"
    samples = _samples(operator, running_batch)
    masses = {row["id"]: 15.0 + 0.5 * index for index, row in enumerate(samples)}

    partial = operator.post(f"/api/step-runs/{run['id']}/submit", {
        "form_data": {"mass": {sid: value for sid, value in list(masses.items())[1:]}},
        "checks": {"samples": True, "materials": True},
    })
    assert partial.status_code == 409
    assert any(samples[0]["id"] in row["label"] for row in partial.json()["detail"]["blocked"])

    submitted = operator.post(f"/api/step-runs/{run['id']}/submit", {
        "form_data": {"mass": masses}, "checks": {"samples": True, "materials": True},
    })
    assert submitted.status_code == 200, submitted.text
    detail = _run(operator, running_batch, executor)
    assert detail["state"] == "done", detail.get("failure_reason")
    command = _fill_command(detail)
    wells = _params(command["id"])["wells"]
    for row in samples:
        assert wells[row["well"]]["electrolyte"] == pytest.approx(masses[row["id"]] * 3.9)
    assert {record["source_kind"] for record in command["bindings"]} == {"manual"}


def test_capability_param_specs_are_validated_and_normalized(admin):
    signature = admin.sign("修改能力定义", target="cap.assemble")
    updated = admin.patch("/api/capabilities/cap.assemble", {
        "param_specs": {"electrolyte": {"type": "number", "unit": "uL", "required": True}},
        "signature_id": signature,
    })
    assert updated.status_code == 200, updated.text
    row = next(item for item in admin.get("/api/capabilities").json() if item["id"] == "cap.assemble")
    assert row["param_specs"] == {"electrolyte": {"unit": "μL"}}, "单位收成规范写法，缺省值不存"

    unknown = admin.patch("/api/capabilities/cap.assemble", {
        "param_specs": {"volume": {"unit": "mL"}}, "signature_id": admin.sign("修改能力定义", target="cap.assemble"),
    })
    assert unknown.status_code == 422
    assert "不是本能力的参数" in unknown.text
