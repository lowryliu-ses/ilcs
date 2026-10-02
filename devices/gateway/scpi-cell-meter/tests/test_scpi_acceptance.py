"""对三台假仪表跑 ILCS 的接入验收清单（`app.adapters.acceptance.run_acceptance`）：只读 + 动作 + 故障注入，
故障项目经统一控制口注入。驱动按 profile 的映射配置建，和工位套用模板之后是同一份配置。CI 里必须全过。"""
from __future__ import annotations

import pytest

from scpi_harness import acceptance, bench, record

# 测量是即时动作：保持、终止按契约声明判跳过；Hioki 没有联锁输入，联锁项目注入不了也判跳过
EXPECTED_SKIPS = {
    "keithley-2450": {"hold", "abort"},
    "keithley-2400": {"hold", "abort"},
    "hioki-bt3562": {"hold", "abort", "interlock"},
}


@pytest.mark.parametrize("name", sorted(EXPECTED_SKIPS))
def test_profile_passes_the_ilcs_acceptance_checklist(name):
    with bench(name) as rig:
        report = acceptance(name, record(rig))
        motions = rig.meter.motions
    states = {check.key: check.state for check in report.checks}
    assert report.ok, report.markdown()
    assert {key for key, state in states.items() if state == "skip"} == EXPECTED_SKIPS[name], states
    assert all(state == "pass" for key, state in states.items() if key not in EXPECTED_SKIPS[name]), states
    assert {"identity", "health", "complete", "duplicate", "restart_query", "lost_receipt", "busy", "offline"} <= set(states)
    assert report.simulator and not report.leftovers
    details = {check.key: check.detail for check in report.checks}
    assert "总动作次数 1 → 1" in details["duplicate"], "仪表不认 ILCS 指令号：按总测量次数判重投没有再测"
    assert "1 → 2" in details["lost_receipt"] and "done" in details["lost_receipt"]
    assert "契约声明不支持" in details["hold"] and "契约声明不支持" in details["abort"]
    assert motions == 2, "正常完成测一次、回执丢失测一次；重投、忙、联锁、失联都没让仪表多测"


@pytest.mark.parametrize("name", sorted(EXPECTED_SKIPS))
def test_read_only_acceptance_never_measures(name):
    """只读级验收（保存工位配置后自动跑的那一级）：身份、健康检查、查询不存在的指令号；不触发测量。"""
    with bench(name, control=False) as rig:
        report = acceptance(name, record(rig), physical=False, faults=False)
        motions = rig.meter.motions
    states = {check.key: check.state for check in report.checks}
    assert report.ok and motions == 0
    assert states["identity"] == states["health"] == states["query_unknown"] == "pass"
    assert states["complete"] == states["lost_receipt"] == "skip"
    assert report.identity["reported_model"] in {"2450", "2400", "BT3562A"}
