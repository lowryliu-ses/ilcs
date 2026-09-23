"""设备回执契约：`http_json_v1` 与 `sila2_v1` 共用同一份解读规则。

回执已经到达时，字段不合规只说明「无法确认」，不能说明「设备没动」——所以一律按
`AdapterIndeterminate`（结果未知）处理，而不是明确失败。
"""
from __future__ import annotations

from datetime import datetime, timezone, tzinfo

from .base import AdapterIndeterminate, CommandResult

STATES = {"accepted", "running", "done", "failed", "unknown"}
QUALITIES = {"good", "bad", "uncertain"}


def device_time(value, device_timezone: tzinfo = timezone.utc) -> datetime:
    """设备时间统一换算为库内的无时区 UTC。带偏移的按偏移换算，不带的按 device_timezone。"""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdapterIndeterminate("设备回执 device_ts 不是 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=device_timezone)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def parse_receipt(
    response: dict, command_id: str, origin: str, device_timezone: tzinfo = timezone.utc,
) -> CommandResult:
    if not isinstance(response, dict):
        raise AdapterIndeterminate("设备回执必须是 JSON 对象")
    returned_id = str(response.get("command_id") or "")
    if returned_id != command_id:
        raise AdapterIndeterminate(
            f"设备回执 command_id 不匹配：期望 {command_id}，实际 {returned_id or '缺失'}"
        )
    state = str(response.get("state") or "")
    if state not in STATES:
        raise AdapterIndeterminate(f"设备回执状态 {state or '缺失'} 不受支持")
    timestamp = response.get("device_ts")
    if not timestamp:
        raise AdapterIndeterminate("设备回执缺少 device_ts")
    quality = str(response.get("quality") or "")
    if quality not in QUALITIES:
        raise AdapterIndeterminate("设备回执 quality 只能是 good、bad 或 uncertain")
    telemetry = []
    for point in response.get("telemetry") or []:
        try:
            telemetry.append((str(point["metric"]), float(point["value"]), point.get("setpoint")))
        except (KeyError, TypeError, ValueError) as exc:
            raise AdapterIndeterminate("设备回执 telemetry 格式无效") from exc
    delivered = response.get("delivered") or {}
    if not isinstance(delivered, dict):
        raise AdapterIndeterminate("设备回执 delivered 必须是对象")
    return CommandResult(
        command_id=returned_id,
        state=state,
        device_ts=device_time(timestamp, device_timezone),
        quality=quality,
        delivered=delivered,
        telemetry=tuple(telemetry),
        error=str(response.get("error") or ""),
        origin=origin,
    )
