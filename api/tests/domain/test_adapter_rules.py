"""适配器配置变更的规则：设备可能仍在动作时放行什么；配置变更后欠什么级别的接入验收。"""
from app.domain.adapter_rules import (
    PHYSICAL, READONLY, acceptance_requirement, acceptance_satisfies, busy_blocked_changes,
)

CURRENT = {
    "kind": "real", "driver": "line_command_v1", "protocol": "串口 / TCP 命令", "note": "",
    "config": {"transport": {"kind": "tcp", "host": "10.20.1.5", "port": 4001}, "request_timeout_sec": 3,
               "status": {"send": "STAT?"}},
    "template_id": "TPL-1", "credential_ref": "",
}


def test_free_changes_while_the_device_may_be_acting():
    # 界面保存时把没改的字段一并发回来：不算改
    assert busy_blocked_changes(CURRENT, {**CURRENT, "note": "换班", "enabled": False}) == []
    tuned = {**CURRENT["config"], "request_timeout_sec": 10, "probe_interval_sec": 5}
    assert busy_blocked_changes(CURRENT, {"config": tuned}) == []
    # 不再按模板管理：配置原样保留
    assert busy_blocked_changes(CURRENT, {"template_id": ""}) == []


def test_changes_that_would_lose_track_of_acting_commands():
    moved = {**CURRENT["config"], "transport": {"kind": "tcp", "host": "10.20.1.6", "port": 4001}}
    assert busy_blocked_changes(CURRENT, {"config": moved}) == ["连接配置 transport"]
    remapped = {**CURRENT["config"], "status": {"send": "STATE?"}}
    assert busy_blocked_changes(CURRENT, {"config": remapped}) == ["连接配置 status"]
    assert busy_blocked_changes(CURRENT, {"driver": "rest_map_v1", "kind": "real"}) == ["驱动"]
    assert busy_blocked_changes(CURRENT, {"template_id": "TPL-2"}) == ["设备接入模板"]
    assert busy_blocked_changes(CURRENT, {"supports_query": False}) == ["按指令查询支持"]


def test_acceptance_requirement_after_a_change():
    simulated = {"kind": "simulation", "driver": "simulation"}
    real = {"kind": "real", "driver": "line_command_v1"}
    assert acceptance_requirement(real, simulated) == "", "模拟适配器不设闸门"
    assert acceptance_requirement(simulated, real) == PHYSICAL, "第一次接成真实设备"
    assert acceptance_requirement(real, {"kind": "real", "driver": "rest_map_v1"}) == PHYSICAL, "换了驱动"
    assert acceptance_requirement(real, real) == READONLY
    assert acceptance_requirement(real, real, pending=PHYSICAL) == PHYSICAL, "还欠的动作级不降级"


def test_which_reports_clear_the_gate():
    assert acceptance_satisfies(READONLY, READONLY, ok=True, simulator=False)
    assert not acceptance_satisfies(READONLY, PHYSICAL, ok=False, simulator=False), "不通过的报告不放行"
    assert not acceptance_satisfies(PHYSICAL, READONLY, ok=True, simulator=False), "真实设备欠动作级"
    assert acceptance_satisfies(PHYSICAL, READONLY, ok=True, simulator=True), "模拟设备只读级就够"
    assert acceptance_satisfies(PHYSICAL, PHYSICAL, ok=True, simulator=False)
