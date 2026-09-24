"""出向事件集成：Webhook 订阅管理与投递。

- 发件箱在业务事务里写（`core/events.py`），这里负责事务外的投递：按到期时间取待投递记录，
  POST JSON，`X-ILCS-Signature: sha256=<HMAC(secret, timestamp + "." + body)>`，2xx 算送达，
  其余按指数退避重试，超过次数判死信；接收方按 `event_id` 去重，重投不会让它处理两遍。
- 只向允许清单里的主机投递；正式环境只允许 https；不跟随重定向。
- 载荷只有「发生了什么」与定位字段，详情由接收方带服务凭据回来取——与界面推送同一原则。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import timedelta
from urllib.parse import quote_plus, urlsplit

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.config import settings
from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..core.events import WEBHOOK_TOPICS, publish
from ..models import User, WebhookDelivery, WebhookSubscription
from .audit_service import AuditService


def url_issues(url: str) -> list[str]:
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return ["地址格式不正确"]
    issues: list[str] = []
    if parts.scheme not in {"http", "https"}:
        issues.append("只支持 http / https 地址")
    if settings.environment == "production" and parts.scheme != "https":
        issues.append("正式环境只允许 https 地址")
    if parts.username or parts.password:
        issues.append("地址里不能带用户名或口令：凭据请走接收方自己的鉴权，签名已经能证明来源")
    host = (parts.hostname or "").lower()
    if not host:
        issues.append("地址缺少主机名")
    elif host not in settings.webhook_allowed_host_set:
        issues.append(f"主机 {host} 不在 ILCS_WEBHOOK_ALLOWED_HOSTS 允许清单里")
    return issues


# 群机器人的官方地址：主机固定，默认放行（其余主机仍要进 ILCS_WEBHOOK_ALLOWED_HOSTS）
BOT_HOSTS = {"wecom": ("qyapi.weixin.qq.com", "/cgi-bin/webhook/send", "key"),
             "dingtalk": ("oapi.dingtalk.com", "/robot/send", "access_token")}
CHANNEL_LABEL = {"webhook": "Webhook", "wecom": "企业微信机器人", "dingtalk": "钉钉机器人", "email": "邮件"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def channel_issues(channel: str, url: str, config: dict) -> list[str]:
    if channel == "webhook":
        return url_issues(url)
    if channel == "email":
        issues = []
        recipients = [str(value).strip() for value in (config or {}).get("to") or [] if str(value).strip()]
        if not recipients:
            issues.append("邮件渠道至少要一个收件人")
        bad = [value for value in recipients if not EMAIL.match(value)]
        if bad:
            issues.append(f"收件人格式不对：{'、'.join(bad)}")
        if not settings.smtp_host:
            issues.append("没有配置 SMTP（ILCS_SMTP_HOST），不能发邮件")
        return issues
    host, path, token = BOT_HOSTS[channel]
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return ["地址格式不正确"]
    issues = []
    if parts.scheme != "https" or (parts.hostname or "").lower() != host or parts.path != path:
        issues.append(f"{CHANNEL_LABEL[channel]}地址应为 https://{host}{path}?{token}=…（在群设置里复制机器人地址）")
    if f"{token}=" not in (parts.query or ""):
        issues.append(f"{CHANNEL_LABEL[channel]}地址缺少 {token}")
    return issues


def masked(channel: str, url: str) -> str:
    """机器人地址里的 key / access_token 就是凭据：列表里不回显。"""
    if channel in BOT_HOSTS and "?" in (url or ""):
        return url.split("?", 1)[0] + "?" + BOT_HOSTS[channel][2] + "=****"
    return url


def signature(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


class IntegrationService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.audit = AuditService(db, ctx)

    def _require_admin(self) -> None:
        if not self.ctx.has("integration.manage"):
            raise PermissionDenied("当前角色不能管理出向事件订阅（integration.manage）")

    def _subscription(self, subscription_id: str) -> WebhookSubscription:
        row = self.db.query(WebhookSubscription).filter(
            WebhookSubscription.id == subscription_id, WebhookSubscription.org_id == self.ctx.org_id,
        ).first()
        if row is None:
            raise NotFound("订阅不存在")
        return row

    @staticmethod
    def out(row: WebhookSubscription, secret: str | None = None) -> dict:
        channel = row.channel or "webhook"
        payload = {
            "id": row.id, "name": row.name, "url": masked(channel, row.url), "topics": row.topics or [],
            "enabled": row.enabled, "channel": channel, "channel_label": CHANNEL_LABEL.get(channel, channel),
            "config": row.config or {}, "bot_signed": channel == "dingtalk" and bool(row.secret),
            "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
            "last_success_at": row.last_success_at.isoformat(timespec="seconds") if row.last_success_at else None,
            "last_error": row.last_error, "consecutive_failures": row.consecutive_failures,
            "row_version": row.row_version,
        }
        if secret is not None:
            # 签名密钥只在签发 / 轮换的这一次响应里出现
            payload["secret"] = secret
        return payload

    def topics(self) -> list[dict]:
        return [{"topic": key, "label": label} for key, label in WEBHOOK_TOPICS.items()]

    def list(self) -> list[dict]:
        self._require_admin()
        rows = self.db.query(WebhookSubscription).filter(WebhookSubscription.org_id == self.ctx.org_id).order_by(
            WebhookSubscription.created_at,
        ).all()
        return [self.out(row) for row in rows]

    def _validate(self, payload: dict) -> list[str]:
        issues = channel_issues(payload.get("channel") or "webhook", payload.get("url", ""), payload.get("config") or {})
        topics = payload.get("topics") or []
        if not topics:
            issues.append("至少订阅一个主题")
        unknown = sorted(set(topics) - set(WEBHOOK_TOPICS) - {"*"})
        if unknown:
            issues.append(f"未知主题：{'、'.join(unknown)}")
        if not str(payload.get("name") or "").strip():
            issues.append("订阅必须有名称")
        return issues

    def create(self, payload: dict, user: User) -> dict:
        self._require_admin()
        issues = self._validate(payload)
        if issues:
            raise ValidationFailed("订阅配置不成立", {"blocked": [{"key": "webhook", "label": text} for text in issues]})
        channel = payload.get("channel") or "webhook"
        # webhook 用系统生成的签名密钥（只显示一次）；钉钉用管理员给的加签密钥；其余渠道不需要
        secret = secrets.token_urlsafe(32) if channel == "webhook" else (payload.get("bot_secret") or "")
        config = {key: value for key, value in (payload.get("config") or {}).items() if value not in (None, [], "")}
        row = WebhookSubscription(
            org_id=self.ctx.org_id, name=payload["name"].strip(), url=(payload.get("url") or "").strip(),
            topics=sorted(set(payload["topics"])), secret=secret, enabled=bool(payload.get("enabled", True)),
            created_by=user.id, channel=channel, config=config,
        )
        self.db.add(row)
        self.db.flush()
        self.audit.record(
            user, "新建出向事件订阅", row.id, after=masked(channel, row.url) or "、".join(config.get("to") or []),
            detail=f"{row.name}（{CHANNEL_LABEL[channel]}）；主题 {'、'.join(row.topics)}"
            + ("；签名密钥只显示一次" if channel == "webhook" else ""),
        )
        self.db.commit()
        return self.out(row, secret if channel == "webhook" else None)

    def update(self, subscription_id: str, payload: dict, user: User) -> dict:
        self._require_admin()
        row = self._subscription(subscription_id)
        expected = payload.get("row_version")
        if expected is not None and int(expected) != int(row.row_version or 1):
            raise StateConflict("订阅已被别人修改，请刷新后再改", code="version_conflict")
        merged = {"name": row.name, "url": row.url, "topics": row.topics, "channel": row.channel or "webhook",
                  "config": row.config or {}, **{k: v for k, v in payload.items() if v is not None}}
        issues = self._validate(merged)
        if issues:
            raise ValidationFailed("订阅配置不成立", {"blocked": [{"key": "webhook", "label": text} for text in issues]})
        row.name, row.url, row.topics = merged["name"].strip(), merged["url"].strip(), sorted(set(merged["topics"]))
        if payload.get("config") is not None:
            row.config = {key: value for key, value in payload["config"].items() if value not in (None, [], "")}
        if payload.get("bot_secret") is not None and (row.channel or "webhook") == "dingtalk":
            row.secret = payload["bot_secret"]
        if "enabled" in payload and payload["enabled"] is not None:
            row.enabled = bool(payload["enabled"])
        row.row_version = int(row.row_version or 1) + 1
        self.audit.record(user, "修改出向事件订阅", row.id, after=masked(row.channel or "webhook", row.url),
                          detail=f"{row.name}；{'启用' if row.enabled else '停用'}；主题 {'、'.join(row.topics)}")
        self.db.commit()
        return self.out(row)

    def rotate(self, subscription_id: str, user: User) -> dict:
        self._require_admin()
        row = self._subscription(subscription_id)
        if (row.channel or "webhook") != "webhook":
            raise StateConflict("只有 Webhook 订阅有系统签名密钥；钉钉加签密钥在编辑里改")
        secret = secrets.token_urlsafe(32)
        row.secret = secret
        row.row_version = int(row.row_version or 1) + 1
        self.audit.record(user, "轮换出向事件签名密钥", row.id, detail="新密钥只显示一次；旧密钥立即失效")
        self.db.commit()
        return self.out(row, secret)

    def ping(self, subscription_id: str, user: User) -> dict:
        """给这个订阅排一条测试事件，验证地址、签名与接收方处理。"""
        self._require_admin()
        row = self._subscription(subscription_id)
        from ..models.base import uid

        event_id = uid()
        delivery = WebhookDelivery(
            org_id=row.org_id, subscription_id=row.id, event_id=event_id, topic="ping",
            payload={"event_id": event_id, "topic": "ping", "org_id": row.org_id,
                     "occurred_at": now().isoformat(timespec="seconds") + "Z",
                     "object": {"type": "webhook_subscription", "id": row.id}, "data": {"by": user.display_name}},
        )
        self.db.add(delivery)
        self.db.commit()
        return self.delivery_out(delivery)

    def deliveries(self, subscription_id: str, limit: int = 100) -> list[dict]:
        self._require_admin()
        self._subscription(subscription_id)
        rows = self.db.query(WebhookDelivery).filter(
            WebhookDelivery.subscription_id == subscription_id, WebhookDelivery.org_id == self.ctx.org_id,
        ).order_by(WebhookDelivery.created_at.desc()).limit(limit).all()
        return [self.delivery_out(row) for row in rows]

    def redeliver(self, delivery_id: str, user: User) -> dict:
        self._require_admin()
        row = self.db.query(WebhookDelivery).filter(
            WebhookDelivery.id == delivery_id, WebhookDelivery.org_id == self.ctx.org_id,
        ).first()
        if row is None:
            raise NotFound("投递记录不存在")
        row.state = "pending"
        row.next_attempt_at = now()
        row.attempts = 0
        self.audit.record(user, "重投出向事件", row.id, detail=f"{row.topic}；事件 {row.event_id}（接收方按它去重）")
        self.db.commit()
        return self.delivery_out(row)

    @staticmethod
    def delivery_out(row: WebhookDelivery) -> dict:
        return {
            "id": row.id, "subscription_id": row.subscription_id, "event_id": row.event_id, "topic": row.topic,
            "payload": row.payload or {}, "state": row.state, "attempts": row.attempts,
            "next_attempt_at": row.next_attempt_at.isoformat(timespec="seconds") if row.next_attempt_at else None,
            "last_error": row.last_error, "response_status": row.response_status,
            "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
            "delivered_at": row.delivered_at.isoformat(timespec="seconds") if row.delivered_at else None,
        }


def deliver_due(db: Session, limit: int = 50, client=None) -> dict:
    """执行器调用：投递到期的记录。每条单独提交，一个接收方卡住不连累别的记录。"""
    import httpx

    rows = (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.state == "pending", WebhookDelivery.next_attempt_at <= now())
        .order_by(WebhookDelivery.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .all()
    )
    work = [(row.id, row.subscription_id) for row in rows]
    # 租约：领取时把下次时刻推到这次投递必然结束之后，提交后释放行锁也不会被另一个进程再领一遍
    lease = now() + timedelta(seconds=settings.webhook_timeout_sec * 2 + 30)
    for row in rows:
        row.next_attempt_at = lease
    db.commit()
    sent = failed = 0
    owned = client is None
    http = client or httpx.Client(timeout=settings.webhook_timeout_sec, follow_redirects=False)
    try:
        for delivery_id, subscription_id in work:
            delivery = db.get(WebhookDelivery, delivery_id)
            subscription = db.get(WebhookSubscription, subscription_id)
            if delivery is None or delivery.state != "pending":
                continue
            if subscription is None or not subscription.enabled:
                delivery.state = "dead"
                delivery.last_error = "订阅已停用或删除"
                db.commit()
                continue
            problems = channel_issues(subscription.channel or "webhook", subscription.url, subscription.config or {})
            status, error = 0, ""
            if problems:
                error = "；".join(problems)
            else:
                try:
                    status, error = _send(db, subscription, delivery, http)
                except Exception as exc:  # noqa: BLE001 —— 网络层的任何失败都只影响这一条投递
                    error = f"{exc.__class__.__name__}: {exc}"[:500]
            delivery.attempts = int(delivery.attempts or 0) + 1
            delivery.response_status = status
            if not error:
                delivery.state = "delivered"
                delivery.delivered_at = now()
                delivery.last_error = ""
                subscription.last_success_at = now()
                subscription.consecutive_failures = 0
                subscription.last_error = ""
                sent += 1
            else:
                delivery.last_error = error
                subscription.last_error = error
                subscription.consecutive_failures = int(subscription.consecutive_failures or 0) + 1
                if delivery.attempts >= settings.webhook_max_attempts or problems:
                    delivery.state = "dead"
                else:
                    backoff = min(3600.0, settings.webhook_retry_base_sec * (2 ** (delivery.attempts - 1)))
                    delivery.next_attempt_at = now() + timedelta(seconds=backoff)
                failed += 1
            db.commit()
    finally:
        if owned:
            http.close()
    return {"sent": sent, "failed": failed}


def _send(db: Session, subscription: WebhookSubscription, delivery: WebhookDelivery, http) -> tuple[int, str]:
    """按渠道投递一条。返回（HTTP 状态, 失败原因）；原因为空表示送达。"""
    channel = subscription.channel or "webhook"
    payload = delivery.payload or {}
    if channel == "webhook":
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        response = http.post(
            subscription.url, content=body,
            headers={
                "Content-Type": "application/json", "User-Agent": "ILCS-Webhook/1",
                "X-ILCS-Event": delivery.topic, "X-ILCS-Event-Id": delivery.event_id,
                "X-ILCS-Delivery": delivery.id, "X-ILCS-Timestamp": timestamp,
                "X-ILCS-Signature": signature(subscription.secret, timestamp, body),
            },
        )
        return response.status_code, "" if 200 <= response.status_code < 300 else f"接收方返回 {response.status_code}"
    from ..domain.notify_text import link_for, render

    title, text = render(delivery.topic, payload, _extra(db, payload), link_for(settings.public_url, payload))
    if channel == "email":
        _send_mail(subscription.config.get("to") or [], title, text)
        return 250, ""
    url = subscription.url
    if channel == "wecom":
        body = {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n{text}"}}
    else:
        body = {"msgtype": "markdown", "markdown": {"title": title, "text": f"### {title}\n{text}"}}
        if subscription.secret:
            # 钉钉加签：timestamp（毫秒）+ 换行 + 密钥，HMAC-SHA256 后 Base64，再 URL 编码
            stamp = str(int(time.time() * 1000))
            digest = hmac.new(subscription.secret.encode(), f"{stamp}\n{subscription.secret}".encode(), hashlib.sha256).digest()
            url += f"&timestamp={stamp}&sign={quote_plus(base64.b64encode(digest).decode())}"
    response = http.post(url, json=body, headers={"User-Agent": "ILCS-Notify/1"})
    if not 200 <= response.status_code < 300:
        return response.status_code, f"机器人接口返回 {response.status_code}"
    try:
        answer = response.json()
    except ValueError:
        return response.status_code, "机器人接口回执不是 JSON"
    if int(answer.get("errcode", 0) or 0) != 0:
        return response.status_code, f"机器人拒收：{answer.get('errcode')} {answer.get('errmsg', '')}"[:300]
    return response.status_code, ""


def _extra(db: Session, payload: dict) -> dict:
    """给人看的消息要一句话说明：发送时回查报警文字、批次失败原因（载荷本身只带定位字段）。"""
    from ..models import Alarm, Batch

    obj = payload.get("object") or {}
    if obj.get("type") == "alarm":
        alarm = db.get(Alarm, obj.get("id"))
        return {"message": alarm.message, "response": alarm.response} if alarm is not None else {}
    if obj.get("type") == "batch":
        batch = db.get(Batch, obj.get("id"))
        return {"reason": batch.failure_reason} if batch is not None and batch.failure_reason else {}
    return {}


def _send_mail(recipients: list[str], subject: str, text: str) -> None:
    import smtplib
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from or settings.smtp_user or "ilcs@localhost"
    message["To"] = ", ".join(recipients)
    message.set_content(text)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.webhook_timeout_sec) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)


def notify(db: Session, org_id: str, batch_id: str, step: dict, step_id: str, message: str) -> None:
    """流程里的消息通知节点：发一条 flow.notify 对外事件（随本事务提交）。"""
    publish(db, org_id, "flow.notify", "batch", batch_id, {
        "step_id": step_id, "name": step.get("name") or "", "message": message,
        "channel": (step.get("notify") or {}).get("channel") or "",
    })
