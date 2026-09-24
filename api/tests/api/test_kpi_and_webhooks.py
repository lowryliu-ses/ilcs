"""运行指标与出向事件。

- 指标：窗口内完成的批次、自动化成功率、设备实际利用率。
- Webhook：业务事件在同一事务里进发件箱；投递带 HMAC 签名、按事件编号去重；保存点回滚的事件不发；
  只向允许清单里的主机投递；失败指数退避，超过次数判死信；流程里的消息通知节点发 flow.notify。
"""
import hashlib
import hmac
import json

import httpx

from test_graph_workflow import _dispatch, _graph_batch


def _run(operator, batch_id, executor, rounds=30):
    import time

    for _ in range(rounds):
        executor()
        detail = operator.get(f"/api/batches/{batch_id}").json()
        if detail["state"] in {"done", "fault", "aborted"}:
            return detail
        time.sleep(0.05)
    return operator.get(f"/api/batches/{batch_id}").json()


def _subscribe(admin, topics, name="LIMS 事件"):
    created = admin.post("/api/webhooks", {"name": name, "url": "http://127.0.0.1:9/ilcs-hook", "topics": topics})
    assert created.status_code == 201, created.text
    return created.json()


def _disable_subscriptions(db):
    from app.models import WebhookSubscription

    db.query(WebhookSubscription).update({"enabled": False}, synchronize_session=False)
    db.commit()


class _Recorder:
    def __init__(self, status=200):
        self.status = status
        self.requests: list[httpx.Request] = []

    def client(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self.status)

        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def _deliver(recorder):
    from app.core.db import SessionLocal
    from app.services.integration_service import deliver_due

    with SessionLocal() as session, recorder.client() as client:
        return deliver_due(session, limit=500, client=client)


def test_kpi_counts_completed_batches_success_rate_and_utilization(operator, reset_runtime, db, executor):
    def linear(steps):
        return [{k: v for k, v in step.items() if k != "hard"} for step in steps]

    batch_id = _graph_batch(operator, db, linear)
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    kpi = operator.get("/api/dashboard/kpi?window_hours=24").json()
    assert kpi["experiments"]["completed"] >= 1
    assert kpi["automation"]["success_rate"] is not None and 0 <= kpi["automation"]["success_rate"] <= 1
    stations = {row["station_id"]: row for row in kpi["utilization"]["stations"]}
    used = {row["station_id"] for row in detail["allocations"] if row["kind"] == "work"}
    assert all(stations[station]["busy_min"] >= 0 for station in used)
    assert kpi["utilization"]["overall"] is not None


def test_business_events_are_delivered_signed_and_deduplicable(admin, operator, reset_runtime, db, executor):
    _disable_subscriptions(db)
    subscription = _subscribe(admin, ["batch.created", "batch.state_changed"])
    secret = subscription["secret"]
    assert "secret" not in admin.get("/api/webhooks").json()[0], "签名密钥只在签发时显示一次"

    batch_id = _graph_batch(operator, db, lambda steps: [{k: v for k, v in s.items() if k != "hard"} for s in steps])
    _dispatch(operator, batch_id)
    recorder = _Recorder()
    result = _deliver(recorder)
    assert result["sent"] >= 2
    bodies = [json.loads(request.content) for request in recorder.requests]
    topics = [(body["topic"], body["object"]["id"], body["data"].get("to")) for body in bodies]
    assert ("batch.created", batch_id, None) in topics
    assert ("batch.state_changed", batch_id, "running") in topics
    request = recorder.requests[0]
    expected = "sha256=" + hmac.new(
        secret.encode(), request.headers["X-ILCS-Timestamp"].encode() + b"." + request.content, hashlib.sha256,
    ).hexdigest()
    assert request.headers["X-ILCS-Signature"] == expected, "接收方用签发时的密钥能验证签名"
    assert request.headers["X-ILCS-Event-Id"] == json.loads(request.content)["event_id"]

    deliveries = admin.get(f"/api/webhooks/{subscription['id']}/deliveries").json()
    delivered = next(row for row in deliveries if row["state"] == "delivered")
    again = admin.post(f"/api/webhook-deliveries/{delivered['id']}/redeliver")
    assert again.status_code == 200 and again.json()["event_id"] == delivered["event_id"], "重投事件编号不变"
    _disable_subscriptions(db)


def test_events_inside_a_rolled_back_savepoint_are_not_published(admin, db):
    from app.core.db import SessionLocal
    from app.core.events import publish
    from app.models import WebhookDelivery

    _disable_subscriptions(db)
    subscription = _subscribe(admin, ["flow.notify"])
    with SessionLocal() as session:
        try:
            with session.begin_nested():
                publish(session, "ORG-001", "flow.notify", "batch", "B-ROLLED-BACK", {"message": "不该发出"})
                raise RuntimeError("保存点里的动作失败")
        except RuntimeError:
            pass
        publish(session, "ORG-001", "flow.notify", "batch", "B-KEPT", {"message": "应当发出"})
        session.commit()
    db.expire_all()
    rows = db.query(WebhookDelivery).filter(WebhookDelivery.subscription_id == subscription["id"]).all()
    assert [row.payload["object"]["id"] for row in rows] == ["B-KEPT"]
    _disable_subscriptions(db)


def test_only_allowed_hosts_and_failures_back_off_then_dead_letter(admin, operator, db):
    from app.core.config import settings
    from app.models import WebhookDelivery

    outside = admin.post("/api/webhooks", {"name": "外网", "url": "https://example.org/hook", "topics": ["batch.created"]})
    assert outside.status_code == 422, "不在允许清单里的主机拒绝"
    assert operator.get("/api/webhooks").status_code == 403

    _disable_subscriptions(db)
    subscription = _subscribe(admin, ["flow.notify"], name="会失败的接收方")
    ping = admin.post(f"/api/webhooks/{subscription['id']}/ping")
    assert ping.status_code == 200
    failing = _Recorder(status=503)
    _deliver(failing)
    db.expire_all()
    row = db.get(WebhookDelivery, ping.json()["id"])
    assert row.state == "pending" and row.attempts == 1 and row.response_status == 503
    original = settings.webhook_max_attempts
    settings.webhook_max_attempts = 2
    try:
        row.next_attempt_at = row.created_at
        db.commit()
        _deliver(failing)
        db.expire_all()
        assert db.get(WebhookDelivery, row.id).state == "dead", "超过次数判死信"
    finally:
        settings.webhook_max_attempts = original
        _disable_subscriptions(db)


def test_notify_node_publishes_flow_notify(admin, operator, reset_runtime, db, executor):
    from app.models import WebhookDelivery

    _disable_subscriptions(db)
    subscription = _subscribe(admin, ["flow.notify"], name="通知")

    def shape(steps):
        dry, weigh, assemble, test = steps
        return [
            {k: v for k, v in dry.items() if k != "hard"},
            {"step_id": "n1", "name": "通知 QA", "kind": "notify", "notify": {"message": "干燥完成，请准备称重"}},
            {k: v for k, v in weigh.items() if k != "hard"},
        ]

    batch_id = _graph_batch(operator, db, shape)
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    db.expire_all()
    rows = db.query(WebhookDelivery).filter(WebhookDelivery.subscription_id == subscription["id"]).all()
    assert any(row.payload["data"].get("message") == "干燥完成，请准备称重" for row in rows)
    _disable_subscriptions(db)


def test_a_rolled_back_savepoint_keeps_events_registered_before_it(admin, db):
    """保存点回滚也会触发 after_rollback：外层事务里先登记的对外事件不能被一起清掉。"""
    from app.core.db import SessionLocal
    from app.core.events import publish
    from app.models import WebhookDelivery

    _disable_subscriptions(db)
    subscription = _subscribe(admin, ["flow.notify"])
    with SessionLocal() as session:
        publish(session, "ORG-001", "flow.notify", "batch", "B-BEFORE", {})
        for attempt in range(3):
            try:
                with session.begin_nested():
                    publish(session, "ORG-001", "flow.notify", "batch", f"B-ROLLED-{attempt}", {})
                    raise RuntimeError("保存点失败")
            except RuntimeError:
                pass
            with session.begin_nested():
                publish(session, "ORG-001", "flow.notify", "batch", f"B-RELEASED-{attempt}", {})
        session.commit()
    db.expire_all()
    ids = sorted(row.payload["object"]["id"] for row in db.query(WebhookDelivery).filter(
        WebhookDelivery.subscription_id == subscription["id"]).all())
    assert ids == ["B-BEFORE", "B-RELEASED-0", "B-RELEASED-1", "B-RELEASED-2"]
    _disable_subscriptions(db)
