"""站外通知的消息文本。Webhook 发机器可读的 JSON；企业微信 / 钉钉机器人与邮件给人看，按主题写成一句中文。

文本只写「发生了什么」与编号，不带配方参数、结果数值这类业务明细——详情回到系统里看（带链接）。
"""
from __future__ import annotations

from typing import Any

from ..core.events import WEBHOOK_TOPICS

STATE_LABEL = {
    "planned": "计划", "scheduled": "已排程", "running": "运行中", "held": "已保持", "fault": "故障挂起",
    "done": "已完成", "aborted": "已终止", "aborting": "终止中", "failed": "失败", "unknown": "结果未知",
    "completed": "已完成", "waiting": "等待中", "ready": "待处理", "skipped": "已跳过",
}
SEVERITY = {1: "紧急", 2: "高", 3: "中", 4: "低"}


def render(topic: str, payload: dict[str, Any], extra: dict[str, Any] | None = None,
           link: str = "") -> tuple[str, str]:
    """返回（标题, 正文 markdown）。extra 是发送时回查到的补充信息（如报警文字）。"""
    data = payload.get("data") or {}
    obj = payload.get("object") or {}
    extra = extra or {}
    ident = obj.get("id") or ""
    label = WEBHOOK_TOPICS.get(topic, "测试消息" if topic == "ping" else topic)
    if topic == "alarm.raised":
        level = SEVERITY.get(int(data.get("severity") or 3), str(data.get("severity")))
        title = f"【ILCS 报警 · {level}】{extra.get('message') or ident}"
        body = f"{extra.get('message') or '新报警'}\n\n- 报警 {ident}\n- 来源 {data.get('source_type', '')} {data.get('source_id', '')}"
        if extra.get("response"):
            body += f"\n- 处置建议：{extra['response']}"
    elif topic == "batch.state_changed":
        title = f"【ILCS】批次 {ident} {STATE_LABEL.get(data.get('to'), data.get('to'))}"
        body = f"批次 {ident}：{STATE_LABEL.get(data.get('from'), data.get('from') or '—')} → {STATE_LABEL.get(data.get('to'), data.get('to'))}"
        if extra.get("reason"):
            body += f"\n\n原因：{extra['reason']}"
    elif topic == "step.state_changed":
        title = f"【ILCS】{data.get('batch_id', '')} 步骤 {data.get('step_id', '')} {STATE_LABEL.get(data.get('to'), data.get('to'))}"
        body = f"批次 {data.get('batch_id', '')} 步骤 {data.get('step_id', '')}（第 {data.get('attempt', 1)} 次）：{STATE_LABEL.get(data.get('to'), data.get('to'))}"
    elif topic == "exception.opened":
        title = f"【ILCS 异常】{data.get('batch_id') or data.get('station_id') or ident}"
        body = f"登记异常 {ident}：类别 {data.get('category', '')}；批次 {data.get('batch_id') or '—'}；工位 {data.get('station_id') or '—'}"
    elif topic == "exception.resolved":
        title = f"【ILCS】异常 {ident} 已收尾"
        body = f"异常 {ident}（{data.get('category', '')}）：{STATE_LABEL.get(data.get('state'), data.get('state'))}"
    elif topic == "flow.notify":
        title = f"【ILCS】{data.get('name') or '流程通知'}"
        body = f"{data.get('message') or ''}\n\n- 批次 {ident}\n- 步骤 {data.get('step_id', '')}"
    elif topic == "report.published":
        title = f"【ILCS】报告已发布 {ident}"
        body = f"报告版本 {ident} 已发布"
    elif topic == "schedule.proposal_created":
        title = "【ILCS】生成重排建议"
        body = f"触发：{data.get('trigger', '')}；涉及批次 {'、'.join(data.get('batch_ids') or []) or '—'}；请调度确认"
    else:
        title = f"【ILCS】{label}"
        body = f"{label}：{obj.get('type', '')} {ident}"
    if link:
        body += f"\n\n[在 ILCS 中查看]({link})"
    return title[:120], body


def link_for(base_url: str, payload: dict[str, Any]) -> str:
    if not base_url:
        return ""
    obj = payload.get("object") or {}
    data = payload.get("data") or {}
    kind, ident = obj.get("type"), obj.get("id") or ""
    base = base_url.rstrip("/")
    if kind == "batch":
        return f"{base}/batches/{ident}"
    if kind == "step_run" and data.get("batch_id"):
        return f"{base}/batches/{data['batch_id']}"
    if kind == "alarm":
        return f"{base}/alarms"
    if kind == "exception":
        return f"{base}/exceptions"
    if kind == "report_version":
        return f"{base}/reports"
    if kind == "schedule_proposal":
        return f"{base}/schedule"
    return base
