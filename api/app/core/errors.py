"""领域异常。服务层只抛这些，由 API 层翻译成 HTTP 状态码。

响应保留原来的 `detail` 包装，并补上稳定的 `code`（给客户端分支用）、
可读的 `message`（给人看）和可选的 `blocked`（对象、步骤、字段、原因）。
"""
from typing import Any


class DomainError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, detail: Any = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail
        if code:
            self.code = code

    def as_payload(self) -> Any:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if isinstance(self.detail, dict):
            payload.update(self.detail)
        elif self.detail is not None:
            payload["detail"] = self.detail
        return payload


class Unauthenticated(DomainError):
    status_code = 401
    code = "unauthenticated"


class NotFound(DomainError):
    """对象不存在，或在当前访问范围内不可见。两者一律 404，不泄漏跨组织对象的存在。"""

    status_code = 404
    code = "not_found"


class PermissionDenied(DomainError):
    """对象可见但当前主体不允许这个动作。"""

    status_code = 403
    code = "permission_denied"


class StateConflict(DomainError):
    """对象当前状态不允许该操作，或业务校验未通过。"""

    status_code = 409
    code = "state_conflict"


class VersionConflict(StateConflict):
    """携带的预期对象版本已过期，别人改过了。客户端取最新数据后重新提交。"""

    code = "version_conflict"


class IdempotencyConflict(StateConflict):
    code = "idempotency_conflict"


class ValidationFailed(DomainError):
    status_code = 422
    code = "validation_failed"


class SignatureInvalid(DomainError):
    status_code = 400
    code = "signature_invalid"


class ExecutionGateClosed(DomainError):
    """执行门关闭或执行门禁止该动作：控制类写操作一律拒绝。"""

    status_code = 423
    code = "execution_gate_closed"


def blockers(items: list[dict[str, Any]]) -> dict[str, Any]:
    """把阻塞项包成 detail。key 是对象/步骤/字段，label 是给人看的原因。"""
    return {"blocked": items}


def blocked_labels(labels: list[str], key: str = "rule") -> dict[str, Any]:
    return {"blocked": [{"key": key, "label": label} for label in labels]}
