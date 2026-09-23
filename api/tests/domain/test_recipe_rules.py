"""配方校验规则。图形化编辑器在前端复算同一套判据，两边必须给出同一结论。"""
from app.domain.capability import StationSpec
from app.domain.recipe_rules import is_valid, recipe_checks, step_issues, validate_steps

CAPABILITIES = {
    "cap.mix": {"name": "控温匀浆", "params": {"temp": "浆料温度 ℃", "rpm": "匀浆转速 rpm"}},
    "cap.degas": {"name": "真空脱泡", "params": {"vacuum": "真空度 mbar"}},
    "cap.transfer": {"name": "托盘转运", "params": {}},
}
STATIONS = [
    StationSpec(id="ST-01-A", limits={"cap.mix": {"temp": [15, 80], "rpm": [0, 3000]}, "cap.degas": {"vacuum": [5, 900]}}),
    StationSpec(id="ST-02", limits={"cap.mix": {"temp": [15, 60], "rpm": [0, 1200]}}),
]


def mix_step(**overrides) -> dict:
    return {"name": "控温匀浆", "cap": "cap.mix", "params": {"temp": 25, "rpm": 2000}, "dur": 60, **overrides}


def test_a_complete_step_has_no_issues():
    assert step_issues(mix_step(), CAPABILITIES) == []


def test_missing_parameter_is_reported_by_its_display_label():
    issues = step_issues(mix_step(params={"temp": 25}), CAPABILITIES)

    assert issues == ["匀浆转速 rpm 未填写"]


def test_blank_name_and_non_positive_duration_are_issues():
    issues = step_issues(mix_step(name="  ", dur=0), CAPABILITIES)

    assert issues == ["步骤名称为空", "计划时长必须大于 0"]


def test_hard_window_without_a_start_event_is_an_issue():
    issues = step_issues(mix_step(hard={"from": "", "maxGapMin": 30}), CAPABILITIES)

    assert issues == ["硬时限缺少起算事件"]


def test_unregistered_capability_is_an_issue():
    issues = step_issues(mix_step(cap="cap.sonicate"), CAPABILITIES)

    assert "能力 cap.sonicate 未登记" in issues


def test_capability_without_parameters_is_complete():
    assert step_issues({"name": "转运", "cap": "cap.transfer", "params": {}, "dur": 10}, CAPABILITIES) == []


def test_validation_reports_fitting_stations_and_stays_valid():
    """2000 rpm 超出中试罐的 1200 上限，所以只有高通量匀浆站能承接。"""
    rows = validate_steps([mix_step()], STATIONS, CAPABILITIES)

    assert rows[0]["fits"] == ["ST-01-A"]
    assert rows[0]["cap_name"] == "控温匀浆"
    assert is_valid(rows)


def test_parameters_beyond_every_station_limit_invalidate_the_step():
    rows = validate_steps([mix_step(params={"temp": 25, "rpm": 5000})], STATIONS, CAPABILITIES)

    assert rows[0]["fits"] == []
    assert not is_valid(rows)
    assert any("rpm=5000" in blocker for blocker in rows[0]["blockers"])


def test_an_incomplete_step_is_invalid_even_with_a_fitting_station():
    """时长缺失与参数越限是两类问题：有工位能承接，不代表这一步可以提交。"""
    rows = validate_steps([mix_step(dur=0)], STATIONS, CAPABILITIES)

    assert rows[0]["fits"] == ["ST-01-A"]
    assert rows[0]["issues"] == ["计划时长必须大于 0"]
    assert not is_valid(rows)


def test_recipe_checks_summarise_by_step_number():
    # 声明消耗物料的步骤 + 空 BOM 才算缺失；没声明的步骤显示「无需物料」
    steps = [
        mix_step(consumes_materials=True),
        mix_step(params={"temp": 25, "rpm": 5000}),
        mix_step(dur=0),
    ]
    checks = {c["key"]: c for c in recipe_checks(steps, validate_steps(steps, STATIONS, CAPABILITIES), [], "")}

    assert checks["stations"]["ok"] is False
    assert "第 2 步" in checks["stations"]["detail"]
    assert checks["complete"]["ok"] is False
    assert "第 3 步" in checks["complete"]["detail"]
    assert checks["bom"]["ok"] is False
    assert checks["risk"]["ok"] is True  # 风险评估编号不阻断草稿保存


def test_empty_bom_is_fine_when_no_step_consumes_materials():
    """DEV-07.3：合法空 BOM 的方法显示「无需物料」，不被物料项拦住。"""
    steps = [mix_step()]
    checks = {c["key"]: c for c in recipe_checks(steps, validate_steps(steps, STATIONS, CAPABILITIES), [], "")}

    assert checks["bom"]["ok"] is True
    assert "无需物料" in checks["bom"]["detail"]


def test_recipe_checks_pass_on_a_ready_draft():
    steps = [mix_step()]
    checks = recipe_checks(
        steps, validate_steps(steps, STATIONS, CAPABILITIES),
        [{"material": "NMP 溶剂", "qty": 0.6, "unit": "L"}], "RA-201 v3",
    )

    assert all(check["ok"] for check in checks)
