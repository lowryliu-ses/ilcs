"""适配器配置变更的规则：设备可能仍在动作时放行什么；配置变更后欠什么级别的接入验收。"""
from app.domain.adapter_rules import (
    PHYSICAL, READONLY, acceptance_requirement, acceptance_satisfies, busy_blocked_changes,
)

CURRENT = {
    "kind": "real", "driver": "sila2_v1", "protocol": "SiLA 2（驱动宿主）", "note": "",
    "config": {"host": "driver-host", "port": 50211, "request_timeout_sec": 3, "expected_device_id": "OVEN-07"},
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
    moved = {**CURRENT["config"], "port": 50212}
    assert busy_blocked_changes(CURRENT, {"config": moved}) == ["连接配置 port"], "换成驱动宿主上的另一台设备"
    renamed = {**CURRENT["config"], "expected_device_id": "OVEN-08"}
    assert busy_blocked_changes(CURRENT, {"config": renamed}) == ["连接配置 expected_device_id"]
    assert busy_blocked_changes(CURRENT, {"driver": "http_json_v1", "kind": "real"}) == ["驱动"]
    assert busy_blocked_changes(CURRENT, {"template_id": "TPL-2"}) == ["设备接入模板"]
    assert busy_blocked_changes(CURRENT, {"supports_query": False}) == ["按指令查询支持"]


def test_acceptance_requirement_after_a_change():
    simulated = {"kind": "simulation", "driver": "simulation"}
    real = {"kind": "real", "driver": "sila2_v1"}
    assert acceptance_requirement(real, simulated) == "", "模拟适配器不设闸门"
    assert acceptance_requirement(simulated, real) == PHYSICAL, "第一次接成真实设备"
    assert acceptance_requirement(real, {"kind": "real", "driver": "http_json_v1"}) == PHYSICAL, "换了驱动"
    assert acceptance_requirement(real, real) == READONLY
    assert acceptance_requirement(real, real, pending=PHYSICAL) == PHYSICAL, "还欠的动作级不降级"


def test_which_reports_clear_the_gate():
    assert acceptance_satisfies(READONLY, READONLY, ok=True, simulator=False)
    assert not acceptance_satisfies(READONLY, PHYSICAL, ok=False, simulator=False), "不通过的报告不放行"
    assert not acceptance_satisfies(PHYSICAL, READONLY, ok=True, simulator=False), "真实设备欠动作级"
    assert acceptance_satisfies(PHYSICAL, READONLY, ok=True, simulator=True), "模拟设备只读级就够"
    assert acceptance_satisfies(PHYSICAL, PHYSICAL, ok=True, simulator=False)


def test_point_only_devices_owe_read_only_acceptance():
    new = {"kind": "", "driver": ""}
    assert acceptance_requirement(new, {"kind": "real", "driver": "sila2_v1", "tasks": False}) == READONLY
    assert acceptance_requirement(new, {"kind": "real", "driver": "sila2_v1", "tasks": True}) == PHYSICAL
    points = {"kind": "real", "driver": "sila2_v1", "tasks": False}
    tasks = {"kind": "real", "driver": "sila2_v1", "tasks": True}
    assert acceptance_requirement(points, tasks) == PHYSICAL, "从只读写点位改成参与自动流程：下发方式是新的"
    assert acceptance_requirement(tasks, points, pending=PHYSICAL) == READONLY, "不接指令了：动作级验收无从做起"
    assert acceptance_requirement(tasks, tasks) == READONLY
