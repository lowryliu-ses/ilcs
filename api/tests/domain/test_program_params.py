"""程序表参数（充放电工步、升温程序）：列定义、逐格校验、引用本步参数、工位的列极限、下发前代入。"""
from app.domain import matrix, program
from app.domain.capability import StationSpec, out_of_range, station_fits
from app.domain.methods import MethodSpec, apply, definition_issues
from app.domain.params import clean_specs, limit_issues, spec_issues, spec_of
from app.domain.recipe_rules import device_issues

COLUMNS = [
    {"key": "mode", "label": "工步", "type": "enum", "options": ["恒流充电", "恒压充电", "恒流放电", "静置"], "required": True},
    {"key": "current", "label": "电流", "unit": "C"},
    {"key": "voltage", "label": "电压", "unit": "V"},
    {"key": "time", "label": "时长", "unit": "min"},
    {"key": "cycles", "label": "循环", "type": "integer"},
]
CYCLER = {
    "name": "充放电测试", "params": {"protocol": "工步", "rate": "倍率", "vmax": "充电截止电压", "label": "协议名"},
    "param_specs": {
        "protocol": {"type": "program", "columns": COLUMNS, "max_rows": 10},
        "rate": {"unit": "C"}, "vmax": {"unit": "V"},
        "label": {"type": "enum", "options": ["化成", "倍率"]},
    },
}
CAPS = {"cap.cycle": CYCLER}
FORMATION = [
    {"mode": "恒流充电", "current": {"param": "rate"}, "voltage": {"param": "vmax"}},
    {"mode": "恒压充电", "voltage": {"param": "vmax"}, "time": 30},
    {"mode": "静置", "time": 10},
    {"mode": "恒流放电", "current": {"param": "rate"}, "voltage": 2.8},
]
STEP = {"step_id": "s01", "name": "化成", "cap": "cap.cycle", "dur": 600,
        "params": {"protocol": FORMATION, "rate": 0.1, "vmax": 4.2, "label": "化成"}}


def test_column_definitions_are_checked_and_cleaned():
    params = {"protocol": "工步"}
    assert spec_issues(params, {"protocol": {"type": "program", "columns": COLUMNS}}) == []
    assert "至少要有一列" in spec_issues(params, {"protocol": {"type": "program"}})[0]
    dup = spec_issues(params, {"protocol": {"type": "program", "columns": [{"key": "a"}, {"key": "a"}]}})
    assert any("列 a 重复" in issue for issue in dup)
    bad = spec_issues(params, {"protocol": {"type": "program", "columns": [{"key": "x y"}, {"key": "m", "type": "enum"}]}})
    assert any("要有标识" in issue for issue in bad) and any("至少要有一个选项" in issue for issue in bad)
    assert "不是程序表，不写列定义" in spec_issues({"rate": "倍率"}, {"rate": {"columns": [{"key": "a"}]}})[0]
    cleaned = clean_specs(params, {"protocol": {"type": "program", "max_rows": 10, "columns": [
        {"key": "current", "label": "电流", "unit": "c", "required": False, "options": []},
        {"key": "mode", "label": "工步", "type": "enum", "options": [" 静置 ", "恒流充电"], "required": True}]}})
    assert cleaned == {"protocol": {"type": "program", "max_rows": 10, "columns": [
        {"key": "current", "label": "电流", "unit": "c"},
        {"key": "mode", "label": "工步", "type": "enum", "options": ["静置", "恒流充电"], "required": True}]}}


def test_rows_are_validated_cell_by_cell():
    spec = spec_of(CYCLER, "protocol")
    assert program.value_issues(spec, FORMATION, "工步") == []
    issues = program.value_issues(spec, [{"current": 0.1}, {"mode": "快充"}, {"mode": "静置", "time": "十分钟"},
                                         {"mode": "静置", "cycles": 1.5}, {"mode": "静置", "power": 3}], "工步")
    assert "工步第 1 行的工步必填" in issues
    assert any("第 2 行的工步只能是" in issue for issue in issues)
    assert "工步第 3 行的时长必须是数值" in issues
    assert "工步第 4 行的循环必须是整数" in issues
    assert "工步第 5 行的列 power 不在程序表的列定义里" in issues
    assert "最多 10 行" in program.value_issues(spec, [{"mode": "静置"}] * 11, "工步")[0]
    assert program.value_issues(spec, [], "工步") == ["工步 至少要有一行"]
    assert program.summary(FORMATION, spec) == "4 步：恒流充电 → 恒压充电 → 静置 → 恒流放电"


def test_references_must_point_at_numeric_params_with_the_same_unit():
    assert device_issues(STEP, CAPS) == []
    wrong = [{"mode": "恒流充电", "current": {"param": "vmax"}}, {"mode": "静置", "time": {"param": "label"}},
             {"mode": {"param": "rate"}}, {"mode": "静置", "time": {"param": "nope"}}]
    issues = device_issues({**STEP, "params": {**STEP["params"], "protocol": wrong}}, CAPS)
    assert any("电流的单位是 C，引用的 充电截止电压 是 V" in issue for issue in issues)
    assert any("引用的 协议名 不是数值参数" in issue for issue in issues)
    assert any("第 3 行的工步只能是" in issue for issue in issues), "选项列不能写引用"
    assert any("引用的参数 nope 不是本能力的另一个参数" in issue for issue in issues)
    missing = device_issues({**STEP, "params": {"protocol": FORMATION, "vmax": 4.2, "label": "化成"}}, CAPS)
    assert "倍率 未填写" in missing


def test_station_column_limits():
    spec = spec_of(CYCLER, "protocol")
    window = {"voltage": [2.5, 4.4], "time": [0, 120], "mode": ["恒流充电", "恒压充电", "静置", "恒流放电"]}
    assert limit_issues(spec, window, "protocol") == []
    assert "没有列 power" in limit_issues(spec, {"power": [0, 1]}, "protocol")[0]
    assert "要按列写" in limit_issues(spec, [0, 1], "protocol")[0]
    station = StationSpec(id="CY-1", limits={"cap.cycle": {"protocol": window, "rate": [0.05, 2], "vmax": [3, 4.4],
                                                           "label": ["化成", "倍率"]}})
    assert station_fits(station, STEP), "引用的格子不按列极限查，查被引用参数自己的极限"
    too_long = {**STEP, "params": {**STEP["params"], "protocol": [*FORMATION, {"mode": "静置", "time": 600}]}}
    assert not station_fits(station, too_long)
    assert out_of_range(station, too_long) == ["CY-1 protocol 第 5 行 time=600 超出 [0, 120]"]
    assert station_fits(StationSpec(id="CY-2", limits={"cap.cycle": {**station.limits["cap.cycle"], "protocol": {}}}), too_long)


def test_method_default_program_and_factor_rules():
    rules = {"protocol": {"default": FORMATION}, "rate": {"min": 0.05, "max": 1, "default": 0.1}}
    assert definition_issues("cap.cycle", rules, [], CAPS, name="标准化成") == []
    bad = definition_issues("cap.cycle", {"protocol": {"default": [{"mode": "快充"}], "min": 1}}, [], CAPS, name="x")
    assert any("不写 min" in issue for issue in bad) and any("缺省程序表" in issue for issue in bad)
    spec = MethodSpec(id="M1", code="M-FORM", version=1, name="标准化成", capability_id="cap.cycle", state="released",
                      params=rules)
    resolved, _ = apply([{**STEP, "params": {"vmax": 4.2, "label": "化成"}, "method": {"id": "M1"}}], lambda ref: spec)
    assert resolved[0]["params"]["protocol"] == FORMATION and resolved[0]["params"]["protocol"] is not FORMATION

    factor = {"name": "工步", "levels": ["a", "b"], "target": {"step_id": "s01", "param": "protocol"}}
    assert "不能作用于程序表参数" in matrix.target_issues([factor], [STEP], [], CAPS)[0]


def test_resolve_per_well_when_a_factor_varies_the_referenced_param():
    params = {**STEP["params"], "wells": {"A1": {"rate": 0.1}, "A2": {"rate": 0.5}, "A3": {"label": "倍率"}}}
    out, problems = program.resolve_command(params)
    assert problems == []
    assert out["protocol"][0] == {"mode": "恒流充电", "current": 0.1, "voltage": 4.2}
    assert out["wells"]["A2"]["protocol"][3] == {"mode": "恒流放电", "current": 0.5, "voltage": 2.8}
    assert out["wells"]["A1"]["protocol"][0]["current"] == 0.1
    assert "protocol" not in out["wells"]["A3"], "这个孔位没改被引用的参数，用顶层那份"
    assert not program.refs(out["protocol"]), "下发的程序表里不留引用"

    _, problems = program.resolve_command({"protocol": FORMATION, "vmax": 4.2})
    assert problems == ["程序表 protocol 引用的参数 rate 没有值"]
