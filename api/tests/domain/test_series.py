"""曲线型检测值的纯规则：写法统一、校验、抽稀、派生、设备输出核对、方法关联、模拟曲线、报告曲线一节。"""
import math

from app.domain import dataquality, metrics, series
from app.domain.methods import definition_issues


def test_three_ways_of_writing_a_curve_normalize_to_traces():
    expected = {"traces": [{"name": "", "x": [0.0, 1.0, 2.0], "y": [4.2, 3.9, 3.5]}]}
    assert series.normalize({"x": [0, 1, 2], "y": [4.2, 3.9, 3.5]}) == (expected, [])
    assert series.normalize([[0, 4.2], [1, 3.9], [2, 3.5]]) == (expected, [])
    named = series.normalize({"traces": [{"name": 1, "x": [0, 1], "y": [1, 2]}, {"name": "第 2 圈", "x": [0, 1], "y": [1, 1.5]}]})
    assert named[1] == [] and [trace["name"] for trace in named[0]["traces"]] == ["1", "第 2 圈"]


def test_invalid_curves_are_explained_not_coerced():
    assert series.issues({"x": [0, 1, 2], "y": [1, 2]})[0].endswith("长短不一")
    assert "不是有限数值" in series.issues({"x": [0, 1], "y": [1, float("nan")]})[0]
    assert "不是有限数值" in series.issues({"x": [0, None], "y": [1, 2]})[0], "缺测的点不能写 null"
    assert "至少要 2 个点" in series.issues({"x": [0], "y": [1]})[0]
    assert series.issues(3.2) and series.issues("0,1") and series.issues({"traces": []})
    assert "超过指标允许的 5 点" in series.issues({"x": list(range(6)), "y": list(range(6))}, {"max_points": 5})[0]
    too_many = {"traces": [{"x": [0, 1], "y": [0, 1]} for _ in range(series.MAX_TRACES + 1)]}
    assert any("最多" in item for item in series.issues(too_many))


def test_metric_rules_and_value_checks_for_series():
    assert metrics.validate_rules("series", {"x_label": "比容量", "x_unit": "mAh/g", "max_points": 5000}) == []
    assert metrics.validate_rules("series", {"max_points": 1}) == ["规则 max_points 要是 2–100000 的整数"]
    problems = metrics.validate_rules("series", {"derived": [{"metric": "cap", "of": "median"}, {"of": "last_x"}]})
    assert any("取法 of 只能是" in item for item in problems) and any("要写数值指标代码" in item for item in problems)
    assert metrics.check_value("series", "V", {}, {"x": [0, 1], "y": [4, 3]}, "V") == []
    assert metrics.check_value("series", "V", {}, {"x": [0, 1], "y": [4, 3]}, "mV") == ["单位 mV 与指标标准单位 V 不一致"]


def test_summary_display_and_derivation():
    curve = series.normalize({"x": [0, 50, 100, 180], "y": [4.2, 3.9, 3.6, 2.8]})[0]
    assert series.summary(curve) == {"trace_count": 1, "points": 4, "x_range": [0.0, 180.0], "y_range": [2.8, 4.2]}
    assert series.display(curve, "V", "mAh/g") == "曲线 4 点（x 0–180 mAh/g，y 2.8–4.2 V）"
    assert series.derive(curve, "last_x") == 180 and series.derive(curve, "max_y") == 4.2
    assert series.derive(curve, "min_y") == 2.8 and series.derive(curve, "first_y") == 4.2
    triangle = series.normalize({"x": [2, 0, 1], "y": [0, 0, 2]})[0]
    assert series.derive(triangle, "area") == 2.0, "面积按 x 排序后梯形积分"
    assert series.derive({"traces": []}, "last_y") is None


def test_lttb_keeps_shape_and_endpoints():
    x = [float(i) for i in range(1000)]
    y = [math.sin(i / 50) for i in range(1000)]
    y[617] = 9.0  # 一个尖峰：等间隔抽样大概率丢掉它，LTTB 必须留住
    picked_x, picked_y = series.downsample(x, y, 100)
    assert len(picked_x) == 100 and picked_x[0] == 0 and picked_x[-1] == 999
    assert 9.0 in picked_y and picked_x == sorted(picked_x)
    assert series.downsample([0, 1, 2], [1, 2, 3], 100) == ([0, 1, 2], [1, 2, 3]), "点数不超过上限原样返回"
    preview = series.preview({"traces": [{"name": "a", "x": x, "y": y}, {"name": "b", "x": x, "y": y}]}, 100)
    assert [len(trace["x"]) for trace in preview] == [50, 50], "几条曲线分摊缩略点数"


def test_device_curve_outputs_are_checked_as_curves():
    rule = {"key": "curve", "label": "放电曲线", "kind": "series", "lo": 2.5, "hi": 4.3, "unit": "V", "required": True}
    good = {"wells": {"A1": {"curve": {"x": [0, 1, 2], "y": [4.2, 3.8, 3.0]}}}}
    assert dataquality.output_flags([rule], good) == []
    low = {"wells": {"A1": {"curve": {"x": [0, 1, 2], "y": [4.2, 2.4, 2.1]}}}}
    flags = dataquality.output_flags([rule], low)
    assert flags[0]["code"] == "out_of_range" and "2 个点低于方法下限 2.5" in flags[0]["message"]
    wrong = {"wells": {"A1": {"curve": 3.4}}}
    assert dataquality.output_flags([rule], wrong)[0]["code"] == "output_invalid"
    assert dataquality.output_flags([rule], {"wells": {"A1": {}}})[0]["code"] == "output_missing"


def test_method_outputs_must_declare_curve_kind_when_linked_to_a_curve_metric():
    capabilities = {"cap.test": {"params": {"rate": "倍率"}, "param_specs": {}}}
    known = {"M-curve": {"code": "dis_curve", "unit": "V", "value_type": "series", "state": "active"},
             "M-text": {"code": "look", "unit": "", "value_type": "text", "state": "active"}}
    plain = [{"key": "curve", "unit": "V", "metric_id": "M-curve"}]
    assert any("输出类型要选曲线" in item for item in
               definition_issues("cap.test", {}, plain, capabilities, name="充放电", metrics=known))
    declared = [{"key": "curve", "unit": "V", "metric_id": "M-curve", "kind": "series"}]
    assert definition_issues("cap.test", {}, declared, capabilities, name="充放电", metrics=known) == []
    text = [{"key": "look", "unit": "", "metric_id": "M-text"}]
    assert any("不是数值或曲线型" in item for item in
               definition_issues("cap.test", {}, text, capabilities, name="充放电", metrics=known))


def test_simulator_curve_stays_inside_the_rule_range():
    from app.adapters.drivers.simulation import sample_curve

    curve = sample_curve({"lo": 2.5, "hi": 4.3}, "cmd:curve:A1")
    assert series.issues(curve) == [] and min(curve["y"]) >= 2.5 and max(curve["y"]) <= 4.3
    assert curve["y"][0] > curve["y"][-1], "像放电曲线：从高往低"
    assert sample_curve({"lo": 2.5, "hi": 4.3}, "cmd:curve:A1") == curve, "同一指令同一孔位确定性"
    assert sample_curve({"lo": 2.5, "hi": 4.3}, "cmd:curve:A2") != curve


def test_closed_loop_dataset_leaves_curves_out():
    from app.models import ResultValue
    from app.services.proposal_service import _exclusion

    curve = ResultValue(value_series={"traces": [{"name": "", "x": [0, 1], "y": [1, 2]}]}, review_state="approved",
                        quality="valid", flags=[], superseded_by_id="")
    assert _exclusion(curve) == "series"


def test_report_pdf_draws_curves_and_skips_the_section_without_them():
    from app.adapters.drivers.simulation import sample_curve
    from app.domain.report_templates import template
    from app.services.report_pdf import render

    snapshot = {**template("standard"), "titles": {}, "texts": {}}
    assert "curves" in snapshot["sections"] and snapshot["version"] == "2.2"
    traces = [{"label": f"S-{index}", "group": group, **sample_curve({"lo": 2.5, "hi": 4.3}, f"s{index}")}
              for index, group in enumerate(["A", "A", "B"])]
    with_curves = render({"title": "曲线", "template": snapshot, "curves": [
        {"metric_name": "放电曲线", "unit": "V", "x_label": "放电深度", "x_unit": "%", "total": 3, "shown": 3,
         "excluded": 0, "traces": traces},
    ]})
    without = render({"title": "曲线", "template": snapshot})
    assert with_curves.startswith(b"%PDF") and without.startswith(b"%PDF")
    assert len(with_curves) > len(without)
