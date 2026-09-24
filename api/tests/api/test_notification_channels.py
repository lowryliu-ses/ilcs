"""站外通知渠道：企业微信 / 钉钉机器人（加签）、邮件；报警严重度门槛；机器人地址里的凭据不回显。"""
import json
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx

from tests.conftest import ORG

WECOM = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wecom-secret-key"
DINGTALK = "https://oapi.dingtalk.com/robot/send?access_token=ding-token"


class _Bot:
    def __init__(self, answer=None):
        self.answer = answer or {"errcode": 0, "errmsg": "ok"}
        self.requests: list[httpx.Request] = []

    def client(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json=self.answer)

        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def _deliver(bot):
    from app.core.db import SessionLocal
    from app.services.integration_service import deliver_due

    with SessionLocal() as session, bot.client() as client:
        return deliver_due(session, limit=500, client=client)


def _only(db, keep_id):
    from app.models import WebhookDelivery, WebhookSubscription

    db.query(WebhookSubscription).filter(WebhookSubscription.id != keep_id).update({"enabled": False}, synchronize_session=False)
    db.query(WebhookDelivery).filter(WebhookDelivery.state == "pending").update({"state": "dead"}, synchronize_session=False)
    db.commit()


def _alarm(db, severity, message):
    from app.models import Alarm

    db.add(Alarm(id=f"AL-T-{uuid.uuid4().hex[:8]}", org_id=ORG, severity=severity, source_type="batch", source_id="B-NOTIFY-TEST",
                 message=message, response="到现场确认", origin="system"))
    db.commit()


def test_wecom_bot_gets_markdown_only_for_severe_alarms(admin, db):
    created = admin.post("/api/webhooks", {"name": "产线群", "channel": "wecom", "url": WECOM, "topics": ["alarm.raised"],
                                           "config": {"max_severity": 2}})
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["url"].endswith("key=****") and "secret" not in row, "机器人地址里的 key 是凭据，不回显"
    _only(db, row["id"])
    _alarm(db, 1, "ST-05 真空度失控")
    _alarm(db, 3, "ST-05 心跳偏慢")
    bot = _Bot()
    assert _deliver(bot)["sent"] == 1, "严重度 3 的报警低于门槛，不推到群里"
    body = json.loads(bot.requests[0].content)
    assert body["msgtype"] == "markdown" and "ST-05 真空度失控" in body["markdown"]["content"]
    assert "到现场确认" in body["markdown"]["content"]
    assert admin.patch(f"/api/webhooks/{row['id']}", {"enabled": False, "row_version": row["row_version"]}).status_code == 200


def test_dingtalk_signs_and_records_rejections(admin, db):
    created = admin.post("/api/webhooks", {"name": "钉钉群", "channel": "dingtalk", "url": DINGTALK,
                                           "topics": ["alarm.raised"], "bot_secret": "SEC-demo"})
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["bot_signed"] is True
    _only(db, row["id"])
    _alarm(db, 2, "手套箱水含量超标")
    bot = _Bot({"errcode": 310000, "errmsg": "sign not match"})
    assert _deliver(bot)["failed"] == 1
    query = parse_qs(urlsplit(str(bot.requests[0].url)).query)
    assert query["access_token"] == ["ding-token"] and query["timestamp"] and query["sign"]
    hooks = {hook["id"]: hook for hook in admin.get("/api/webhooks").json()}
    assert "机器人拒收：310000" in hooks[row["id"]]["last_error"]
    admin.patch(f"/api/webhooks/{row['id']}", {"enabled": False, "row_version": hooks[row["id"]]["row_version"]})


def test_bot_urls_must_be_the_official_endpoints(admin):
    wrong = admin.post("/api/webhooks", {"name": "错", "channel": "wecom", "url": "https://evil.example/send?key=1",
                                         "topics": ["alarm.raised"]})
    assert wrong.status_code == 422
    assert "qyapi.weixin.qq.com" in wrong.text


def test_email_channel_sends_through_smtp(admin, db, monkeypatch):
    from app.core.config import settings

    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append({"host": host, "port": port})

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self):
            sent[-1]["tls"] = True

        def login(self, user, password):
            sent[-1]["login"] = user

        def send_message(self, message):
            sent[-1]["to"] = message["To"]
            sent[-1]["subject"] = message["Subject"]

    no_smtp = admin.post("/api/webhooks", {"name": "邮件", "channel": "email", "topics": ["alarm.raised"],
                                           "config": {"to": ["qa@lab.example"]}})
    assert no_smtp.status_code == 422 and "SMTP" in no_smtp.text
    monkeypatch.setattr(settings, "smtp_host", "smtp.lab.example")
    monkeypatch.setattr(settings, "smtp_user", "ilcs")
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    created = admin.post("/api/webhooks", {"name": "QA 邮件", "channel": "email", "topics": ["alarm.raised"],
                                           "config": {"to": ["qa@lab.example", "ehs@lab.example"]}})
    assert created.status_code == 201, created.text
    _only(db, created.json()["id"])
    _alarm(db, 1, "废液桶液位过高")
    assert _deliver(_Bot())["sent"] == 1
    assert sent[0]["host"] == "smtp.lab.example" and sent[0]["tls"] and sent[0]["login"] == "ilcs"
    assert sent[0]["to"] == "qa@lab.example, ehs@lab.example" and "废液桶液位过高" in sent[0]["subject"]
    admin.patch(f"/api/webhooks/{created.json()['id']}", {"enabled": False, "row_version": created.json()["row_version"]})
