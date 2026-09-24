"""数据质量的纯规则：越界打标不拒收、设备输出核对、前后逻辑校验。"""
from app.domain import dataquality
from app.domain.metrics import check_value


def test_out_of_range_is_a_flag_not_a_format_error():
    assert check_value("number", "mAh/g", {"min": 0, "max": 400}, 450.0, "mAh/g") == []
    flags = dataquality.range_flags("number", {"min": 0, "max": 400}, 450.0, "discharge_capacity")
    assert flags[0]["code"] == "out_of_range" and "上限 400" in flags[0]["message"]
    assert check_value("number", "mAh/g", {}, "abc", "mAh/g"), "不是数值仍是格式错，整次拒收"


def test_device_outputs_are_checked_per_well_and_required_items():
    outputs = [{"key": "moisture", "label": "水分", "hi": 200, "required": True}, {"key": "temp", "lo": 100}]
    flags = dataquality.output_flags(outputs, {"wells": {"A1": {"moisture": 150, "temp": 90}, "A2": {"temp": 120}}})
    messages = [row["message"] for row in flags]
    assert any("孔位 A1 温度" in m or "孔位 A1 temp" in m for m in messages)
    assert any("孔位 A2 必报输出 水分" in m for m in messages)
    assert dataquality.output_flags(outputs, {"moisture": 100, "temp": 110}) == []


def test_logic_rules_compare_metrics_with_scale_and_skip_missing_sides():
    rule = dataquality.LogicRule(id="r1", name="放电不超过充电", left="discharge", op="<=", right="charge", factor=1.0)
    constant = dataquality.LogicRule(id="r2", name="效率不超过 100%", left="ce", op="<=", value=100)
    found = dataquality.violations([rule, constant], {"discharge": 210, "charge": 200, "ce": 101})
    assert [row.id for row, _ in found] == ["r1", "r2"]
    assert dataquality.violations([rule], {"discharge": 210}) == [], "缺一侧的值不判"
    assert dataquality.rule_issues("a", "<=", "a", None, "flag") == ["左右两侧是同一个指标"]
