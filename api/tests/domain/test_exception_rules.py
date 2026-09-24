"""异常策略的纯规则：安全边界优先于策略。"""
from app.domain.exceptions import Rule, Signal, classify_condition, decide, rule_issues


def _rule(action="reroute", category="communication", **extra):
    return Rule(id="r1", name="失联改派", category=category, action=action, **extra)


def test_driving_actions_need_proof_that_the_device_never_saw_the_command():
    unsure = decide(Signal(category="communication", never_sent=False), [_rule()])
    assert unsure.action == "hold" and "结果未知" in unsure.reason
    sure = decide(Signal(category="communication", never_sent=True), [_rule()])
    assert sure.action == "reroute" and sure.rule is not None


def test_safety_is_always_manual_and_cannot_be_configured_otherwise():
    assert decide(Signal(category="safety", never_sent=True), [_rule(category="safety")]).action == "hold"
    assert rule_issues({"name": "联锁重试", "category": "safety", "action": "retry", "params": {"max_attempts": 1}})


def test_attempt_limits_and_skippable_are_respected():
    retry = _rule("retry", params={"max_attempts": 2, "delay_sec": 0})
    assert decide(Signal(category="communication", never_sent=True, attempts=1), [retry]).action == "retry"
    assert decide(Signal(category="communication", never_sent=True, attempts=2), [retry]).action == "hold"
    skip = _rule("skip")
    assert decide(Signal(category="communication", never_sent=True), [skip]).action == "hold"
    assert decide(Signal(category="communication", never_sent=True, skippable=True), [skip]).action == "skip"


def test_rules_match_by_priority_and_scope():
    narrow = Rule(id="a", name="ST-01-A 改派", category="communication", action="reroute",
                  match={"station_id": "ST-01-A"}, priority=10)
    broad = Rule(id="b", name="其余转人工", category="communication", action="hold", priority=50)
    assert decide(Signal(category="communication", station_id="ST-01-A", never_sent=True), [broad, narrow]).rule.id == "a"
    assert decide(Signal(category="communication", station_id="ST-02", never_sent=True), [broad, narrow]).rule.id == "b"
    disabled = Rule(id="c", name="停用", category="communication", action="reroute", enabled=False)
    assert decide(Signal(category="communication", never_sent=True), [disabled]).rule is None


def test_alarm_condition_keys_map_to_categories():
    assert classify_condition("station:ST-05:heartbeat_stale") == "communication"
    assert classify_condition("station:ST-05:interlock") == "safety"
    assert classify_condition("asset:A-1:calibration_due") == "device_fault"
    assert classify_condition("command:abc:overdue") == "timeout"
    assert classify_condition("gate:run-1") == "sample"


def test_only_commands_never_handed_to_an_adapter_count_as_never_sent():
    """对账时找不到适配器的指令之前已经交给过适配器：即使被判不可达，也不能当作「设备没见过」。"""
    from datetime import datetime

    from app.models import Command
    from app.services.exception_service import never_left_system

    refused = Command(type="dispatch", delivery_state="unreachable", started_at=None)
    assert never_left_system(refused)
    handed_over = Command(type="dispatch", delivery_state="unreachable", started_at=datetime(2026, 9, 24, 8, 0))
    assert not never_left_system(handed_over)
    assert not never_left_system(Command(type="dispatch", delivery_state="maybe_sent", started_at=None))
    assert not never_left_system(Command(type="hold", delivery_state="unreachable", started_at=None))
