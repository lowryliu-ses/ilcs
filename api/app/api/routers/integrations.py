"""出向事件（Webhook）订阅与投递。"""
from fastapi import APIRouter

from ...schemas import WebhookIn, WebhookPatchIn
from ...services.integration_service import IntegrationService
from ..deps import Ctx, CurrentUser, DbSession

router = APIRouter(tags=["integration"])


@router.get("/webhooks/topics")
def webhook_topics(db: DbSession, ctx: Ctx):
    """可订阅的主题。载荷只有「发生了什么」与定位字段，详情由接收方带服务凭据回来取。"""
    return IntegrationService(db, ctx).topics()


@router.get("/webhooks")
def list_webhooks(db: DbSession, ctx: Ctx):
    return IntegrationService(db, ctx).list()


@router.post("/webhooks", status_code=201)
def create_webhook(payload: WebhookIn, db: DbSession, user: CurrentUser, ctx: Ctx):
    """新建订阅。响应里的 secret 只出现这一次：接收方用它校验 X-ILCS-Signature。"""
    return IntegrationService(db, ctx).create(payload.model_dump(), user)


@router.patch("/webhooks/{subscription_id}")
def update_webhook(subscription_id: str, payload: WebhookPatchIn, db: DbSession, user: CurrentUser, ctx: Ctx):
    return IntegrationService(db, ctx).update(subscription_id, payload.model_dump(), user)


@router.post("/webhooks/{subscription_id}/rotate")
def rotate_webhook(subscription_id: str, db: DbSession, user: CurrentUser, ctx: Ctx):
    return IntegrationService(db, ctx).rotate(subscription_id, user)


@router.post("/webhooks/{subscription_id}/ping")
def ping_webhook(subscription_id: str, db: DbSession, user: CurrentUser, ctx: Ctx):
    return IntegrationService(db, ctx).ping(subscription_id, user)


@router.get("/webhooks/{subscription_id}/deliveries")
def webhook_deliveries(subscription_id: str, db: DbSession, ctx: Ctx):
    return IntegrationService(db, ctx).deliveries(subscription_id)


@router.post("/webhook-deliveries/{delivery_id}/redeliver")
def redeliver(delivery_id: str, db: DbSession, user: CurrentUser, ctx: Ctx):
    """重投。事件编号不变，接收方按它去重。"""
    return IntegrationService(db, ctx).redeliver(delivery_id, user)
