"""界面推送：Server-Sent Events。

只推「什么变了」（主题 + 对象 ID），不推数据；客户端据此带着自己的令牌重取。
鉴权在建流时做一次，然后立即归还数据库会话——长连接不能一直占着连接池里的连接。
连接最长 `stream_max_sec` 秒，到点由服务端结束，客户端重连时重新鉴权：令牌撤销、
成员关系撤销在一个周期内生效。

浏览器原生 `EventSource` 不能带 `Authorization` 头，前端用 fetch 读流；
`X-Accel-Buffering: no` 让 nginx（本容器与公共网关）不缓冲这条响应。
"""
from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from ...core.config import settings
from ...core.db import SessionLocal
from ...core.errors import Unauthenticated
from ...core.security import decode_token
from ...core.stream_hub import hub
from ...services.identity_service import IdentityService
from ..deps import user_context

router = APIRouter(tags=["stream"])


def _authenticate(request: Request, authorization: str, organization: str | None):
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthenticated("缺少访问令牌")
    with SessionLocal() as db:
        user = IdentityService(db).user_from_token(decode_token(token))
        return user_context(request, db, user, organization)


def _frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/stream")
async def stream(
    request: Request,
    authorization: Annotated[str, Header()] = "",
    organization: Annotated[str | None, Header(alias="X-Organization-Id")] = None,
):
    """变更推送流。事件：`hello`（建流）、`change`（主题变更）、`resync`（请整体重取）。"""
    ctx = await run_in_threadpool(_authenticate, request, authorization, organization)
    subscriber = hub.subscribe(ctx.org_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1.0, settings.stream_max_sec)

    async def frames():
        try:
            yield "retry: 3000\n\n"
            yield _frame("hello", {"org": ctx.org_id, "max_sec": settings.stream_max_sec})
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    yield _frame("bye", {"reason": "max_age"})
                    return
                if await request.is_disconnected():
                    return
                try:
                    message = await asyncio.wait_for(
                        subscriber.queue.get(), timeout=min(settings.stream_keepalive_sec, remaining)
                    )
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                kind = "resync" if message.get("topic") == "resync" else "change"
                yield _frame(kind, {"topic": message.get("topic"), "ids": message.get("ids") or []})
        finally:
            hub.unsubscribe(subscriber)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
