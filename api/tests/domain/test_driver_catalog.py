"""驱动目录的嵌套字段说明：界面按它出表单，检查按它提醒嵌套结构里拼错的键。"""
import json
from pathlib import Path

import pytest

from app.adapters.catalog import DRIVERS, check_fields

LIMITS = {"cap.heat": {"temp": [20, 200], "time": [1, 600]}, "cap.transfer": {}}
PRESETS = Path(__file__).resolve().parents[3] / "devices" / "simulators" / "pilot-devices.json"


@pytest.mark.parametrize("driver", sorted(DRIVERS))
def test_driver_templates_match_their_own_field_specs(driver):
    """起步模板里的每个键（含嵌套的）都在字段说明里：说明漏了键，表单就编辑不到它。"""
    check = check_fields(driver, DRIVERS[driver].template(LIMITS))
    assert check.ok and check.warnings == [], check.as_dict()


def test_pilot_presets_pass_the_field_specs():
    """示例工位的预设是真在用的配置：按字段说明不该有问题，也不该有「拼错的键」提醒。"""
    stations = json.loads(PRESETS.read_text(encoding="utf-8"))["stations"]
    checked = 0
    for station, preset in stations.items():
        if preset.get("driver") not in DRIVERS:
            continue
        check = check_fields(preset["driver"], preset["config"], template=True)
        assert check.ok and check.warnings == [], (station, check.as_dict())
        checked += 1
    assert checked >= 5


def test_misspelled_nested_keys_are_flagged_but_not_refused():
    config = DRIVERS["opcua_map_v1"].template(LIMITS)
    config["hold"] = {"point": "cmd_hold", "value": True, "pulse-ms": 300}
    config["capabilities"]["cap.heat"]["start"]["pules_ms"] = 300
    config["status"]["state"] = config["status"].pop("states")
    check = check_fields("opcua_map_v1", config)
    assert check.ok, "拼错的键只提醒，不拒绝（驱动构造时另有校验）"
    assert "hold.pulse-ms 不是登记的配置项：拼错的键会被驱动忽略，请核对" in check.warnings
    assert any(item.startswith("capabilities.cap.heat.start.pules_ms ") for item in check.warnings)
    assert any(item.startswith("status.state ") for item in check.warnings)


def test_shorthand_and_single_item_forms_are_accepted():
    config = DRIVERS["modbus_map_v1"].template(LIMITS)
    config["heartbeat"] = "heartbeat"  # 就是 {"point": "heartbeat"}
    config["capabilities"]["cap.heat"]["write"]["temp"] = {"point": "sp_temp", "map": {"快": 1}}
    check = check_fields("modbus_map_v1", config)
    assert check.ok and check.warnings == [], check.as_dict()

    line = DRIVERS["line_command_v1"].template(LIMITS)
    line["identity"] = [{"send": "*IDN?", "pattern": "^(?P<model>.+)$"}, {"send": "SN?", "pattern": "^(?P<serial>.+)$"}]
    line["actuals"] = {"send": "PV?", "pattern": "^(?P<temp>[-0-9.]+)$"}
    check = check_fields("line_command_v1", line)
    assert check.ok and check.warnings == [], "身份命令、实测命令写一条或一列都行"
    line["identity"][1]["patern"] = "x"
    assert check_fields("line_command_v1", line).warnings == [
        "identity[1].patern 不是登记的配置项：拼错的键会被驱动忽略，请核对",
    ]


def test_catalog_describes_nested_structures_for_the_form():
    fields = {item["name"]: item for item in DRIVERS["modbus_map_v1"].as_dict(LIMITS)["fields"]}
    points = fields["points"]
    assert points["key_label"] == "点名" and points["entries"]["type"] == "object"
    register = {item["name"]: item for item in points["entries"]["fields"]}
    assert register["address"]["required"] and register["table"]["options"] == ["holding", "input", "coil", "discrete"]
    capability = fields["capabilities"]
    assert capability["key_ref"] == "capabilities" and capability["scope"] == "capability"
    write = next(item for item in capability["entries"]["fields"] if item["name"] == "write")
    assert write["key_ref"] == "params" and write["entries"]["shorthand"] == "point"
    assert {item["name"] for item in write["entries"]["fields"]} == {"point", "map"}
    status = {item["name"]: item for item in fields["status"]["fields"]}
    assert status["point"]["ref"] == "points" and status["states"]["entries"]["options"] == [
        "idle", "running", "held", "done", "failed",
    ]
    # 平铺的字段不带多余的键：老界面照旧能读
    assert set(fields["host"]) == {"name", "label", "type", "type_label", "required", "connection", "hint"}
