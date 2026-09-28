"""界面发来的时间都带偏移（`Date.toISOString()` 以 Z 结尾）：入口统一换算成库内的无时区 UTC。

没换算时，带时区的值留在对象上，服务里拿它和 `now()` 或库里已有的时间一比较（是否逾期、到期是否晚于生效、
区间是否重叠）就是 TypeError，接口返回纯文本的 500，界面报「Internal Server Error is not valid JSON」。
"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _to_iso_string(moment: datetime) -> str:
    """和浏览器 `new Date(...).toISOString()` 同一格式：毫秒 + Z。"""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_request_models_convert_offsets_to_naive_utc():
    from app.schemas import TaskCreateIn

    shanghai = TaskCreateIn(plan_id="EP-205-01", due_at="2026-10-01T10:00:00+08:00").due_at
    zulu = TaskCreateIn(plan_id="EP-205-01", due_at="2026-10-01T02:00:00.000Z").due_at
    naive = TaskCreateIn(plan_id="EP-205-01", due_at="2026-10-01T02:00:00").due_at
    assert shanghai == zulu == naive == datetime(2026, 10, 1, 2, 0)
    assert shanghai.tzinfo is None and zulu.tzinfo is None


def test_request_models_never_take_a_bare_datetime():
    source = (Path(__file__).parents[2] / "app" / "schemas" / "__init__.py").read_text()
    bare = [line.strip() for line in source.splitlines() if re.search(r":\s*datetime\b", line)]
    assert not bare, f"请求模型的时间字段要用 UtcDatetime（入口换算成无时区 UTC）：{bare}"


def test_task_with_a_due_date_from_the_browser_is_created(researcher, reset_runtime):
    due = datetime.utcnow().replace(second=0, microsecond=0) + timedelta(days=3)
    created = researcher.post("/api/experiment-tasks", {"plan_id": "EP-205-01", "due_at": _to_iso_string(due)})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["due_at"] == due.isoformat(timespec="minutes") and body["overdue"] is False

    # 带 +08:00 的按偏移换算，存的是同一个 UTC 时刻
    local = (due + timedelta(hours=8)).replace(tzinfo=timezone(timedelta(hours=8))).isoformat()
    other = researcher.post("/api/experiment-tasks", {"plan_id": "EP-205-01", "due_at": local})
    assert other.status_code == 201, other.text
    assert other.json()["due_at"] == body["due_at"]

    # 已经过了的截止时间：照样能建，标为逾期
    late = researcher.post(
        "/api/experiment-tasks", {"plan_id": "EP-205-01", "due_at": _to_iso_string(due - timedelta(days=10))},
    )
    assert late.status_code == 201, late.text
    assert late.json()["overdue"] is True


def test_qualification_expiry_from_the_browser_without_an_effective_date(admin, reset_runtime):
    person = admin.post("/api/people", {"code": "P-UTC-1", "name": "时区验证人员"}).json()
    granted = admin.post(f"/api/people/{person['id']}/qualifications", {
        "scope_kind": "safety", "scope_ref": "general",
        "expires_at": _to_iso_string(datetime.utcnow() + timedelta(days=365)),
    })
    assert granted.status_code == 201, granted.text


def test_second_booking_from_the_browser_is_judged_not_crashed(admin, operator, reset_runtime):
    asset = admin.post("/api/assets", {"asset_no": "AS-UTC-1", "name": "时区验证资产", "capacity": 1}).json()
    start = datetime.utcnow().replace(second=0, microsecond=0) + timedelta(days=2)

    def booking(begin: datetime, end: datetime) -> dict:
        return {"asset_id": asset["id"], "kind": "manual", "reason": "时区验证",
                "starts_at": _to_iso_string(begin), "ends_at": _to_iso_string(end)}

    first = operator.post("/api/resource-bookings", booking(start, start + timedelta(hours=2)))
    assert first.status_code == 201, first.text
    assert first.json()["starts_at"] == start.isoformat(timespec="minutes")
    overlapping = operator.post(
        "/api/resource-bookings", booking(start + timedelta(minutes=30), start + timedelta(hours=3)),
    )
    assert overlapping.status_code == 409, overlapping.text
    assert overlapping.json()["detail"]["code"] == "booking_conflict"


def test_unexpected_errors_still_answer_in_json(client, monkeypatch, reset_runtime):
    """没料到的异常回统一格式的 JSON 500，界面能显示出一句话，而不是 JSON 解析错误。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.task_service import TaskService
    from conftest import Session

    def boom(self, payload, user):
        raise RuntimeError("模拟未处理的异常")

    monkeypatch.setattr(TaskService, "create", boom)
    lenient = Session(TestClient(app, raise_server_exceptions=False), "researcher")
    response = lenient.post("/api/experiment-tasks", {"plan_id": "EP-205-01"})
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"]["code"] == "internal_error"
