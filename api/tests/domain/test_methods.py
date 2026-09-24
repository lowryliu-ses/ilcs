"""设备方法的纯规则：定义完整性、流程步骤引用时的解析与冻结、按型号与程序筛工位。"""
from app.domain import methods
from app.domain.capability import StationSpec, out_of_range, station_fits

CAPS = {"cap.vacuum_dry": {"name": "极片真空干燥", "params": {"temp": "箱温", "vacuum": "真空度"}, "retired": False}}


def _spec(**extra):
    base = dict(
        id="m1", code="DM-001", version=1, name="120℃ 干燥", capability_id="cap.vacuum_dry", state="released",
        program="VD-120", instrument_models=("VAC-WEIGH-12",),
        params={"temp": {"default": 120, "min": 100, "max": 130}, "vacuum": {"default": 1, "min": 0.5, "max": 5}},
        outputs=({"key": "moisture_ppm", "hi": 200},), dur_min=60,
    )
    return methods.MethodSpec(**{**base, **extra})


def test_definition_rejects_unknown_params_and_inverted_ranges():
    issues = methods.definition_issues(
        "cap.vacuum_dry", {"speed": {"min": 1}, "temp": {"min": 130, "max": 100}},
        [{"key": "x"}, {"key": "x"}], CAPS, name="干燥",
    )
    assert any("speed 不是能力" in issue for issue in issues)
    assert any("下限 130 大于上限 100" in issue for issue in issues)
    assert any("重复" in issue for issue in issues)
    assert methods.definition_issues("cap.vacuum_dry", _spec().params, list(_spec().outputs), CAPS, name="干燥") == []


def test_applying_a_method_fills_defaults_and_freezes_the_snapshot():
    step = {"step_id": "s01", "name": "干燥", "cap": "cap.vacuum_dry", "params": {"temp": 110}, "method": {"id": "m1"}}
    applied, problems = methods.apply([step], lambda ref: _spec() if ref == "m1" else None)
    assert problems == {}
    row = applied[0]
    assert row["params"] == {"temp": 110, "vacuum": 1.0}, "没写的参数取方法缺省值，写了的保留"
    assert row["dur"] == 60 and row["method"]["program"] == "VD-120" and row["method"]["version"] == 1
    assert methods.command_method(row) == {"id": "m1", "code": "DM-001", "version": 1, "name": "120℃ 干燥", "program": "VD-120"}


def test_bad_references_are_reported_per_step():
    step = {"step_id": "s01", "name": "干燥", "cap": "cap.coat", "params": {"temp": 150}, "method": {"id": "m1"}}
    _, problems = methods.apply([step], lambda ref: _spec(state="retired", latest_version=2))
    text = "；".join(problems["s01"])
    assert "已退役，请改引用 v2" in text and "能力 cap.coat 与设备方法的能力" in text and "temp=150 超出" in text
    _, missing = methods.apply([{**step, "method": {"id": "nope"}}], lambda ref: None)
    assert "不存在" in missing["s01"][0]


def test_stations_are_filtered_by_model_and_reported_programs():
    applied, _ = methods.apply(
        [{"step_id": "s01", "name": "干燥", "cap": "cap.vacuum_dry", "params": {}, "method": {"id": "m1"}}],
        lambda ref: _spec(),
    )
    step = applied[0]
    limits = {"cap.vacuum_dry": {"temp": [40, 160], "vacuum": [0.1, 100]}}
    right = StationSpec(id="ST-05", model="VAC-WEIGH-12", limits=limits)
    other_model = StationSpec(id="ST-X", model="OTHER", limits=limits)
    unknown_catalog = StationSpec(id="ST-Y", model="VAC-WEIGH-12", limits=limits, programs=())
    wrong_catalog = StationSpec(id="ST-Z", model="VAC-WEIGH-12", limits=limits, programs=("VD-90",))
    wildcard = StationSpec(id="ST-W", model="VAC-WEIGH-12", limits=limits, programs=("*",))
    assert station_fits(right, step) and station_fits(unknown_catalog, step) and station_fits(wildcard, step)
    assert not station_fits(other_model, step) and "不在方法适用型号" in out_of_range(other_model, step)[0]
    assert not station_fits(wrong_catalog, step) and "未报告支持程序 VD-120" in out_of_range(wrong_catalog, step)[0]
