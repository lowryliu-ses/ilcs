"""驱动目录的字段说明：界面按它出表单，检查按它提醒嵌套结构里拼错的键。

ILCS 只登记 sila2_v1 与 http_json_v1；协议驱动（PLC 点表、串口命令、REST……）的映射在驱动宿主的设备文件里，不在这里。
"""
import json

import pytest

from app.adapters.catalog import DRIVERS, check_fields
from sim_harness import require_devices

LIMITS = {"cap.heat": {"temp": [20, 200], "time": [1, 600]}, "cap.transfer": {}}


def test_only_the_two_contract_drivers_are_registered():
    assert set(DRIVERS) == {"sila2_v1", "http_json_v1"}


@pytest.mark.parametrize("driver", sorted(DRIVERS))
def test_driver_templates_match_their_own_field_specs(driver):
    """起步模板里的每个键（含嵌套的）都在字段说明里：说明漏了键，表单就编辑不到它。"""
    check = check_fields(driver, DRIVERS[driver].template(LIMITS))
    assert check.ok and check.warnings == [], check.as_dict()


def test_pilot_presets_pass_the_field_specs():
    """示例工位的预设（设备仓库的 simulators/pilot-devices.json）是真在用的配置：按字段说明不该有问题，
    也不该有「拼错的键」提醒。"""
    presets = require_devices() / "simulators" / "pilot-devices.json"
    stations = json.loads(presets.read_text(encoding="utf-8"))["stations"]
    checked = 0
    for station, preset in stations.items():
        assert preset.get("driver") in DRIVERS, f"{station} 用的驱动 {preset.get('driver')} 不在 ILCS 里"
        check = check_fields(preset["driver"], preset["config"], template=True)
        assert check.ok and check.warnings == [], (station, check.as_dict())
        checked += 1
    assert checked >= 3


def test_misspelled_nested_keys_are_flagged_but_not_refused():
    config = DRIVERS["http_json_v1"].template(LIMITS)
    config["paths"]["helth"] = "/health"
    config["simulator_control"] = {"url": "http://gw-sim:9900", "tokn_ref": "file:///run/secrets/x.token"}
    config["environment"] = {"zone": "实验区 A", "points": {"humidity": "rh"}, "interval": 30}
    check = check_fields("http_json_v1", config)
    assert check.ok, "拼错的键只提醒，不拒绝（驱动构造时另有校验）"
    assert "paths.helth 不是登记的配置项：拼错的键会被驱动忽略，请核对" in check.warnings
    assert any(item.startswith("simulator_control.tokn_ref ") for item in check.warnings)
    assert any(item.startswith("environment.interval ") for item in check.warnings)
    assert check_fields("sila2_v1", {"host": "driver-host", "port": 50201, "taks": False}).warnings == [
        "taks 不是 SiLA 2 登记的配置项：拼错的键会被驱动忽略，请核对",
    ]


def test_shorthand_forms_are_accepted():
    """方法目录写程序名或 {program, name, capability} 都行；对象里拼错的键照样提醒。"""
    config = {"host": "driver-host", "port": 50211, "methods": ["OCV", {"program": "ACIR-OCV", "name": "内阻 + 电压"}]}
    check = check_fields("sila2_v1", config)
    assert check.ok and check.warnings == [], check.as_dict()
    config["methods"][1]["nmae"] = "x"
    assert check_fields("sila2_v1", config).warnings == [
        "methods[1].nmae 不是登记的配置项：拼错的键会被驱动忽略，请核对",
    ]


def test_missing_and_mistyped_fields_are_problems():
    assert any("缺少「主机」" in item for item in check_fields("sila2_v1", {"port": 50201}).problems)
    assert any("（port）应是整数" in item for item in check_fields("sila2_v1", {"host": "h", "port": "50201"}).problems)
    template = check_fields("sila2_v1", {"tasks": False}, template=True)
    assert template.ok, "模板里连接参数可以不填，套用时由工位填"
    assert "驱动 modbus_map_v1 没有登记" in check_fields("modbus_map_v1", {}).problems[0]


def test_catalog_describes_nested_structures_for_the_form():
    fields = {item["name"]: item for item in DRIVERS["http_json_v1"].as_dict(LIMITS)["fields"]}
    paths = {item["name"] for item in fields["paths"]["fields"]}
    assert paths == {"health", "submit", "query", "hold", "abort"}
    control = fields["simulator_control"]
    assert control["connection"] and {item["name"] for item in control["fields"]} >= {"url", "token_ref", "unit"}
    environment = {item["name"]: item for item in fields["environment"]["fields"]}
    assert environment["points"]["key_options"][:2] == ["temperature", "humidity"]
    assert environment["points"]["entries"]["ref"] == "points"
    methods = fields["methods"]
    assert methods["type"] == "array" and methods["items"]["shorthand"] == "program"
    sila = {item["name"]: item for item in DRIVERS["sila2_v1"].as_dict(LIMITS)["fields"]}
    # 平铺的字段不带多余的键：老界面照旧能读
    assert set(sila["host"]) == {"name", "label", "type", "type_label", "required", "connection", "hint"}
    assert sila["tasks"]["type"] == "boolean"


def test_wells_per_command_must_be_at_least_one():
    """一条指令最多几瓶：设备一次只能处理一瓶时填 1；填 0 或负数没有意义，保存前就拒绝。"""
    base = {"base_url": "https://gw.lab.internal/api/v1"}
    assert check_fields("http_json_v1", {**base, "wells_per_command": 1}).ok
    check = check_fields("http_json_v1", {**base, "wells_per_command": 0})
    assert not check.ok and "不能小于 1" in check.problems[0]
    assert not check_fields("sila2_v1", {"host": "h", "port": 1, "wells_per_command": True}).ok, "布尔值不是整数"
