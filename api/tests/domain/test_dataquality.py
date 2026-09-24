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


def test_nan_readings_and_values_never_pass():
    from datetime import datetime

    from app.domain import environment

    moment = datetime(2026, 9, 24, 12, 0)
    reason = environment.check({"metric": "h2o_ppm", "max": 0.1}, "GB-01", environment.Reading(float("nan"), moment), moment, 30)
    assert reason and "无效" in reason
    assert check_value("number", "", {}, float("nan"), "")
    flags = dataquality.output_flags([{"key": "temp", "hi": 100}], {"temp": float("nan")})
    assert flags[0]["code"] == "output_invalid"


def test_saved_matrices_pick_up_permissions_added_later():
    from app.domain.permissions import merge_saved_matrix

    saved = {"operator": ["batch.control"], "qa": ["plan.approve"]}
    merged = merge_saved_matrix(saved, None)
    assert "environment.record" in merged["operator"] and "batch.create" not in merged["operator"], "只补新增的键，不回填管理员撤掉的"
    assert "audit.read" in merged["qa"] and "method.release" in merged["qa"]
    assert merged["auditor"] == ["audit.read"], "矩阵里没有的新角色取出厂默认"
    known = merge_saved_matrix(saved, sorted(__import__("app.domain.permissions", fromlist=["PERMISSIONS"]).PERMISSIONS))
    assert "environment.record" not in known["operator"], "管理员保存时已经有这个键：按他的选择"
