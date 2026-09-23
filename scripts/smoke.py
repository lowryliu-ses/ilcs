"""端到端冒烟：走一遍首期完整链路。

方案审批 → 实验任务分配与接单 → 批次与排程 → 开跑检查与签名下发 →
人工记录 → 设备执行 → 定时等待（后台推进）→ 流程审核 → 检测任务与回传 →
数据复核 → 报告审签与发布 PDF。

依赖 API 与执行器都在运行；只用 HTTP，不碰数据库，因此也能跑在部署环境上。
用法：api/.venv/bin/python scripts/smoke.py [http://127.0.0.1:8011]

它刻意断言几条容易被做反的规则：
- 矩阵锁定不等于已审批，未审批的方案建不出批次；
- 人工记录缺项不推进；
- 等待到期由后台唤醒，不靠界面刷新；
- 提交人不能审核自己提交的记录；
- 采集完成不等于质量有效，未复核的结果进不了正式报告；
- 审核通过但判定无效的结果进排除说明，不进统计。

`ILCS_BATCH_NOTE` 可改批次备注。在演示环境里跑完整流程时用它，免得那条批次
在界面上顶着「冒烟」二字。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011").rstrip("/") + "/api"
PASSWORD = "ilcs1234"
MARKER = os.environ.get("ILCS_BATCH_NOTE", "端到端冒烟")
PLAN_ID = os.environ.get("ILCS_SMOKE_PLAN", "EP-210-01")
LIMS = ("lims-ec", os.environ.get("ILCS_SMOKE_LIMS_SECRET", "ilcs-lims-dev-secret"))
METRICS = ["METRIC-areal_density-v1", "METRIC-discharge_capacity-v1"]
RUN = uuid.uuid4().hex[:8]

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}{('：' + detail) if detail else ''}")
    if not ok:
        failures.append(f"{label}{('：' + detail) if detail else ''}")
    return ok


class Client:
    """一个登录会话。关键写接口一律带幂等键——服务端缺键会拒。"""

    def __init__(self, username: str | None = None) -> None:
        self.token = ""
        self.user: dict = {}
        if username:
            self.login(username)

    def call(
        self, method: str, path: str, body: dict | None = None, key: str | None = None,
        service: tuple[str, str] | None = None, raw: bool = False,
    ):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(BASE + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if self.token and not service:
            request.add_header("Authorization", f"Bearer {self.token}")
        if key:
            request.add_header("Idempotency-Key", key)
        if service:
            request.add_header("X-Service-Source", service[0])
            request.add_header("X-Service-Secret", service[1])
        try:
            with urllib.request.urlopen(request) as response:
                payload = response.read()
                if raw:
                    return response.status, payload, dict(response.headers)
                return response.status, json.loads(payload or b"null")
        except urllib.error.HTTPError as error:
            payload = error.read()
            if raw:
                return error.code, payload, dict(error.headers)
            return error.code, json.loads(payload or b"null")

    def get(self, path: str):
        return self.call("GET", path)[1]

    def post(self, path: str, body: dict | None = None, key: str | None = None):
        return self.call("POST", path, body or {}, key=key or f"{RUN}-{uuid.uuid4().hex[:8]}")

    def login(self, username: str) -> None:
        status, body = self.call("POST", "/auth/login", {"username": username, "password": PASSWORD})
        if status != 200:
            raise SystemExit(f"登录 {username} 失败：{body}")
        self.token = body["access_token"]
        self.user = body["user"]

    def sign(self, meaning: str, target: str, version: int = 0) -> str:
        status, body = self.call(
            "POST", "/signatures",
            {"password": PASSWORD, "meaning": meaning, "target": target, "object_version": version},
        )
        if status != 200:
            raise SystemExit(f"签名失败：{body}")
        return body["signature_id"]


def wait_for(describe: str, probe, timeout: float = 45.0) -> object | None:
    """等后台推进器把事情做完。它是独立进程，所以这里只能轮询。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = probe()
        if found:
            return found
        time.sleep(1.5)
    check(describe, False, f"{timeout:.0f}s 内未达成")
    return None


def main() -> int:
    health = Client().get("/health")
    print(f"ILCS {health['version']}｜库版本 {health['schema_revision']}")
    if not check("库版本与应用一致", health["status"] == "ok", health.get("detail", "")):
        return 1

    researcher, operator, qa = Client("researcher"), Client("operator"), Client("qa")
    print(f"\n1 方案审批（{PLAN_ID}）")
    plan = researcher.get(f"/plans/{PLAN_ID}")
    if plan["state"] != "locked":
        researcher.post(f"/plans/{PLAN_ID}/lock")
        plan = researcher.get(f"/plans/{PLAN_ID}")
    if plan["approval_state"] == "draft":
        # 锁定之后、审批之前：正式批次必须建不出来
        status, rejected = operator.post("/batches", {"plan_id": PLAN_ID, "note": MARKER})
        check(
            "锁定但未审批的方案建不出批次", status == 409
            and rejected.get("detail", {}).get("code") == "plan_not_approved",
            str(rejected.get("detail", {}).get("code")),
        )
        researcher.post(f"/plans/{PLAN_ID}/submit")
        plan = researcher.get(f"/plans/{PLAN_ID}")
    if plan["approval_state"] == "review":
        status, _ = qa.post(
            f"/plans/{PLAN_ID}/decision",
            {"conclusion": "approved",
             "signature_id": qa.sign("批准实验方案", PLAN_ID, plan["row_version"])},
        )
        check("QA 批准方案版本", status == 200)
    plan = researcher.get(f"/plans/{PLAN_ID}")
    check("方案已批准", plan["approval_state"] == "approved", f"v{plan['approved_version']}")

    print("\n2 实验任务")
    status, task = researcher.post("/experiment-tasks", {"plan_id": PLAN_ID, "priority": 1})
    if not check("建立任务", status == 201, task.get("id", str(task))):
        return 1
    status, assigned = researcher.post(
        f"/experiment-tasks/{task['id']}/assign", {"assignee_user_id": operator.user["id"]}
    )
    check("分配给操作员（按预计执行时间校验资质）", status == 200, str(assigned.get("state")))
    status, accepted = operator.post(f"/experiment-tasks/{task['id']}/accept")
    check("操作员接单", status == 200 and accepted["state"] == "accepted")

    print("\n3 批次、排程与下发")
    status, batch = operator.post(
        "/batches", {"plan_id": PLAN_ID, "task_id": task["id"], "note": MARKER}
    )
    if not check("创建批次并与任务原子绑定", status == 201, batch.get("id", str(batch))):
        return 1
    batch_id = batch["id"]
    status, _ = operator.post(f"/batches/{batch_id}/schedule", {})
    check("排程（只对需要占用的步骤预约）", status == 200)

    preflight = operator.get(f"/batches/{batch_id}/preflight?manual_review=true")
    check(
        "开跑检查按适用性给结论", preflight["ok"],
        f"通过 {preflight['summary']['passed']}、不适用 {preflight['summary']['not_applicable']}、"
        f"阻塞 {preflight['summary']['blocked']}"
        + ("；" + "；".join(row["detail"] for row in preflight["blocked"]) if preflight["blocked"] else ""),
    )
    status, running = operator.post(
        f"/batches/{batch_id}/dispatch",
        {"manual_review": True, "reason": MARKER,
         "signature_id": operator.sign("批准执行", batch_id, batch["row_version"])},
    )
    if not check(
        "签名下发", status == 200 and running["state"] == "running",
        running.get("state", str(running)),
    ):
        return 1

    print("\n4 四类节点")
    detail = operator.get(f"/batches/{batch_id}")
    demand = detail["resource_demand"]
    print(
        f"  节点构成：设备 {demand['device']}、人工 {demand['manual']}、"
        f"等待 {demand['wait']}、审核 {demand['review']}；需占工位 {demand['needs_station']}"
    )
    for run in detail["step_runs"]:
        if run["kind"] != "manual" or run["state"] not in {"ready", "running"}:
            continue
        # 缺项不推进
        status, rejected = operator.post(
            f"/step-runs/{run['id']}/submit",
            {"form_data": {}, "checks": {}, "row_version": run["row_version"]},
        )
        check(
            "人工记录缺项不推进", status == 409,
            "；".join(row["label"] for row in rejected.get("detail", {}).get("blocked", []))[:90],
        )
        values = {}
        for field in run["form"]:
            kind = field.get("type") or "text"
            values[field["key"]] = 20.1 if kind == "number" else True if kind == "bool" else "BAL-01"
        body = {
            "form_data": values,
            "checks": {"samples": True, "materials": bool(detail["reservations"])},
            "note": MARKER,
            "row_version": run["row_version"],
        }
        if run["requires_signature"]:
            body["signature_id"] = operator.sign("人工步骤记录确认", run["id"], run["row_version"])
        status, submitted = operator.post(f"/step-runs/{run['id']}/submit", body)
        check("人工记录补齐后推进一步", status == 200, str(submitted.get("advance", {}).get("processed")))
        break

    def device_done():
        rows = operator.get(f"/batches/{batch_id}")["step_runs"]
        return [r for r in rows if r["kind"] == "device" and r["state"] == "completed"] or None

    if wait_for("设备步骤由执行器完成", device_done):
        check("设备步骤完成并写检查点", True, f"{len(operator.get(f'/batches/{batch_id}')['checkpoints'])} 个检查点")

    waiting = [r for r in operator.get(f"/batches/{batch_id}")["step_runs"]
               if r["kind"] == "wait" and r["state"] == "waiting"]
    if waiting:
        check("等待节点已在计时", True, f"到期 {waiting[0]['due_at']}")

        def wait_completed():
            rows = operator.get(f"/batches/{batch_id}")["step_runs"]
            return [r for r in rows if r["kind"] == "wait" and r["state"] == "completed"] or None

        completed_wait = wait_for("等待节点由后台推进器到期唤醒", wait_completed)
        check("等待到期不依赖浏览器刷新", bool(completed_wait))

    def review_ready():
        rows = operator.get(f"/batches/{batch_id}")["step_runs"]
        return [r for r in rows if r["kind"] == "review" and r["state"] in {"ready", "running"}] or None

    review = wait_for("审核节点就绪", review_ready)
    if review:
        run = review[0]
        status, denied = operator.post(
            f"/step-runs/{run['id']}/review",
            {"conclusion": "approved",
             "signature_id": operator.sign("流程审核通过", run["id"], run["row_version"]),
             "row_version": run["row_version"]},
        )
        check("提交人不能审核自己的记录", status in {403, 409}, str(denied.get("detail", {}).get("code")))
        status, _ = qa.post(
            f"/step-runs/{run['id']}/review",
            {"conclusion": "approved",
             "signature_id": qa.sign("流程审核通过", run["id"], run["row_version"]),
             "row_version": run["row_version"]},
        )
        check("QA 批准审核节点", status == 200)

    wait_for("批次运行结束", lambda: operator.get(f"/batches/{batch_id}")["state"] == "done" or None)
    return report(batch_id, operator)


def report(batch_id: str, operator: Client) -> int:
    """检测任务 → 回传 → 复核 → 报告。"""
    researcher, qa = Client("researcher"), Client("qa")
    print("\n5 检测与数据复核")
    detail = operator.get(f"/batches/{batch_id}")
    values: list[dict] = []
    for index, assignment in enumerate(detail["samples"]):
        status, task = researcher.post(
            "/analysis-tasks",
            {"sample_id": assignment["id"], "physical_sample_id": assignment["physical_sample_id"],
             "method": "电性能测试", "method_version": "EC-02 v2", "required_metrics": METRICS},
        )
        if status != 201:
            check("建立检测任务", False, str(task))
            return 1
        payload = {
            "event_id": f"smoke-{RUN}-{index}", "task_id": task["id"],
            "parser_version": "ec-parser 2.1",
            "metrics": [
                {"metric_version_id": METRICS[0], "value": 22.0 + index * 0.1, "unit": "mg/cm2"},
                {"metric_version_id": METRICS[1], "value": 205.0 + index, "unit": "mAh/g"},
            ],
        }
        status, ingested = Client().call("POST", "/integrations/results", payload, service=LIMS)
        if index == 0:
            check("服务认证回传，多指标原子入账", status == 200, str(ingested.get("task_state")))
            status, replayed = Client().call("POST", "/integrations/results", payload, service=LIMS)
            check("同一事件重传回放原结果", status == 200 and replayed.get("replayed") is True)
            status, anonymous = Client().call("POST", "/integrations/results", payload)
            check("无服务凭据的回传被拒", status == 401)
        values.extend(ingested["results"])

    pending = [row for row in values if row["review_state"] == "pending"]
    check("采集完成后仍全部待复核", len(pending) == len(values), f"{len(pending)}/{len(values)}")

    for index, value in enumerate(values):
        quality = "invalid" if index == 0 else "valid"
        status, _ = qa.post(
            f"/result-values/{value['id']}/review",
            {"conclusion": "approved", "quality": quality,
             "reason": "首片边缘缺陷，判定无效" if quality == "invalid" else "曲线正常",
             "result_version": value["result_version"],
             "signature_id": qa.sign("数据复核通过", value["id"], value["result_version"])},
        )
        if status != 200:
            check("数据复核", False, f"{value['id']} → {status}")
            return 1
    check("逐条复核并绑定结果版本", True, f"{len(values)} 条，其中 1 条判定无效")

    official = operator.get(f"/results/{batch_id}")
    numeric = [row for row in official["metrics"]]
    excluded = sum(row["summary"]["excluded"] for row in numeric)
    check(
        "正式统计排除已审核但无效的结果", excluded >= 1,
        "；".join(f"{row['metric_name']} 纳入 {row['summary']['included']} / 排除 {row['summary']['excluded']}"
                  for row in numeric),
    )

    print("\n6 报告审签与发布")
    status, draft = researcher.post(
        "/reports", {"batch_id": batch_id, "conclusion": f"{MARKER}：链路贯通，容量符合预期。"}
    )
    if not check("生成报告草稿", status == 201, draft.get("code", str(draft))):
        return 1
    check(
        "排除说明进入报告内容", bool(draft["content"]["exclusions"]),
        "；".join(f"{row['metric_name']} {row['reason_label']}" for row in draft["content"]["exclusions"]),
    )
    status, _ = researcher.call("POST", f"/reports/{draft['id']}/submit", {}, key=f"{RUN}-submit")
    check("提交报告审核", status == 200)
    fresh = qa.get(f"/reports/{draft['id']}")
    status, self_denied = researcher.call(
        "POST", f"/reports/{draft['id']}/approve",
        {"conclusion": "approved",
         "signature_id": researcher.sign("批准报告", draft["id"], fresh["row_version"])},
        key=f"{RUN}-selfapprove",
    )
    check("作者不能批准自己的报告", status == 403, str(self_denied.get("detail", {}).get("code")))
    status, approved = qa.call(
        "POST", f"/reports/{draft['id']}/approve",
        {"conclusion": "approved",
         "signature_id": qa.sign("批准报告", draft["id"], fresh["row_version"])},
        key=f"{RUN}-approve",
    )
    if not check("QA 批准报告", status == 200, approved.get("state", str(approved))):
        return 1
    status, published = qa.post(
        f"/reports/{draft['id']}/publish",
        {"signature_id": qa.sign("发布报告", draft["id"], approved["row_version"])},
    )
    if not check(
        "发布报告", status == 200 and published["state"] == "published",
        published.get("state", str(published)),
    ):
        return 1
    snapshot = published["publish_snapshot"]
    check(
        "发布固化结果版本、算法、模板与签名",
        bool(snapshot["result_versions"]) and bool(snapshot["pdf_checksum"]),
        f"{len(snapshot['result_versions'])} 条结果版本｜模板 {snapshot['template_version']}"
        f"｜PDF 摘要 {snapshot['pdf_checksum'][:12]}…",
    )
    status, content, headers = qa.call("GET", f"/reports/{draft['id']}/download", raw=True)
    response_checksum = next(
        (value for name, value in headers.items() if name.lower() == "x-checksum-sha256"), ""
    )
    check(
        "下载固定模板 PDF 且摘要一致",
        status == 200 and content[:4] == b"%PDF"
        and response_checksum == snapshot["pdf_checksum"],
        f"{len(content)} 字节",
    )
    check("已发布报告只读", researcher.call("PATCH", f"/reports/{draft['id']}", {"conclusion": "改一下"})[0] == 409)

    print("\n7 审计链")
    audit = operator.get(f"/audit?target={batch_id}")
    signed = [row for row in audit if row["sign"]]
    check("批次审计含签名动作", bool(signed), "；".join(row["action"] for row in signed[:4]))
    return 0


if __name__ == "__main__":
    code = main()
    print()
    if failures:
        print(f"冒烟失败 {len(failures)} 项：")
        for row in failures:
            print(f"  - {row}")
        raise SystemExit(1)
    print("冒烟通过：首期链路贯通。")
    raise SystemExit(code)
