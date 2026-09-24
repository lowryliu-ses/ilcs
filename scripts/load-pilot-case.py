#!/usr/bin/env python
"""把「试点操作案例」（docs/试点操作案例.md）的数据导进演示库。

在 api 容器里运行，走和界面相同的 HTTP 接口；批次由执行器真实投递到两台外部 SiLA 2 模拟设备：

    docker compose exec api python ../scripts/load-pilot-case.py --prune-demo

流程：修订 R-205 加放电容量质检关卡 → 矩阵方案（注液量下发到设备、设计空间）→ 任务 →
批次 → 排程 → 签名下发 → 设备执行 → 关卡返工仍不合格 → QA 放行 → 检测录入与复核 →
报告发布 → 导出训练数据 → 外部提案（接受一份、越界拒绝一份）。

它代替演示账号操作，但不用口令：
- 会话令牌由脚本直接签发；
- 电子签名票据由脚本写入，签名含义照常填写，备注与审计都注明「案例导入脚本代签」，
  审计记录里能看出这批签名不是本人输入口令签署的。

`--prune-demo` 先删掉种子里的演示流程与方案（R-205 除外）和演示报警，只在库里还没有批次时允许。
正式环境（ILCS_ENVIRONMENT=production）一律拒绝运行。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.core.security import issue_token  # noqa: E402
from app.models import Alarm, Batch, ESignature, Plan, PlanVersion, Recipe, User  # noqa: E402
from app.services.audit_service import AuditService  # noqa: E402

BASE = os.environ.get("ILCS_API", "http://127.0.0.1:8000").rstrip("/") + "/api"
ORG = os.environ.get("ILCS_MIGRATION_ORG_ID", "ORG-001")
SOURCE_RECIPE = "R-205"
SIGN_NOTE = "案例导入脚本代签：演示数据，非本人输入口令签署"
METRICS = {"areal_density": "METRIC-areal_density-v1", "discharge_capacity": "METRIC-discharge_capacity-v1"}
RUN = uuid.uuid4().hex[:6]


class Failed(SystemExit):
    pass


def step(title: str) -> None:
    print(f"\n== {title}")


def ok(label: str, detail: str = "") -> None:
    print(f"  ✓ {label}{('：' + detail) if detail else ''}")


class Actor:
    """一个演示账号的会话。令牌直接签发，签名票据直接写库并留审计。"""

    def __init__(self, username: str) -> None:
        with SessionLocal() as db:
            user = db.query(User).filter(User.username == username).one()
            self.id, self.role, self.name = user.id, user.role, user.display_name
        self.username = username
        self.token = issue_token(self.id, self.role, ORG)

    def call(self, method: str, path: str, body: dict | None = None, expect: tuple[int, ...] = (200, 201)):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(BASE + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {self.token}")
        if method != "GET":
            request.add_header("Idempotency-Key", f"case-{RUN}-{uuid.uuid4().hex[:10]}")
        try:
            with urllib.request.urlopen(request) as response:
                status, payload = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, payload = error.code, error.read()
        content = json.loads(payload) if payload and payload[:1] in b"{[" else payload
        if status not in expect:
            raise Failed(f"{self.username} {method} {path} → {status}：{content}")
        return content

    def get(self, path: str):
        return self.call("GET", path)

    def post(self, path: str, body: dict | None = None, expect: tuple[int, ...] = (200, 201)):
        return self.call("POST", path, body or {}, expect)

    def patch(self, path: str, body: dict):
        return self.call("PATCH", path, body)

    def sign(self, meaning: str, target: str, version: int = 0) -> str:
        with SessionLocal() as db:
            user = db.get(User, self.id)
            signature = ESignature(
                user_id=self.id, meaning=meaning, note=SIGN_NOTE, object_ref=target, object_version=version,
            )
            db.add(signature)
            db.flush()
            AuditService(db).record(
                user, "电子签名", target, sign=True, meaning=meaning, detail=SIGN_NOTE,
                signature_id=signature.id, object_version=version, org_id=ORG,
            )
            db.commit()
            return signature.id


def wait_for(describe: str, probe, timeout: float = 300.0, every: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = probe()
        if found:
            return found
        time.sleep(every)
    raise Failed(f"{describe}：{timeout:.0f}s 内未达成")


def prune_demo() -> None:
    step("清理演示设计数据")
    with SessionLocal() as db:
        batches = db.query(Batch).count()
        if batches:
            raise Failed(f"库里已有 {batches} 个批次；--prune-demo 只用于刚播种的空库，请先重置演示库")
        versions = db.query(PlanVersion).delete()
        plans = db.query(Plan).delete()
        recipes = db.query(Recipe).filter(Recipe.id != SOURCE_RECIPE).delete()
        alarms = db.query(Alarm).delete()
        db.commit()
    ok("已删除", f"方案 {plans}（版本 {versions}）、流程 {recipes}（保留 {SOURCE_RECIPE}）、演示报警 {alarms}")


def capacity(fec: float, volume: float) -> float:
    """演示用的放电比容量：FEC 适量提升、过量回落，注液充分略高。"""
    return round(196.0 + 2.6 * fec - 0.24 * fec * fec + (2.4 if volume >= 60 else 0.0), 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prune-demo", action="store_true", help="先删种子里的演示流程、方案与报警")
    args = parser.parse_args()
    if settings.environment == "production":
        raise Failed("正式环境不导入演示案例")

    if args.prune_demo:
        prune_demo()

    researcher, qa, operator = Actor("researcher"), Actor("qa"), Actor("operator")

    gate = researcher.get("/gate")
    blocked = {k: v for k, v in (gate.get("blocked_stations") or {}).items() if k in {"ST-05", "ST-06", "ST-07"}}
    if not gate["open"] or blocked:
        raise Failed(f"执行门未就绪：{gate['reasons']} {blocked}（SiLA 模拟设备在线了吗？）")

    # ---------- 1 流程修订 ----------
    step("1 修订 R-205，加放电容量质检关卡")
    revision = researcher.post(f"/recipes/{SOURCE_RECIPE}/revision")
    recipe_id = revision["id"]
    current = researcher.get(f"/recipes/{recipe_id}")
    steps = [row for row in current["steps"]]
    steps.append({
        "step_id": "s05", "kind": "gate", "name": "放电容量质检", "cap": "", "params": {}, "dur": 0,
        "gate": {"source_step_id": "s04", "field": "discharge_capacity_mAh", "min": 3.25,
                 "scope": "batch", "on_fail": "rework", "rework_to": "s04", "max_rework": 1},
    })
    researcher.patch(f"/recipes/{recipe_id}", {
        "steps": steps, "design": "试点：首次充放电后按放电容量质检，不合格返工一次，仍不合格转 QA 判定",
        "row_version": current["row_version"],
    })
    researcher.post(f"/recipes/{recipe_id}/submit")
    for target, meaning in (("approved", "批准流程"), ("released", "发布流程")):
        fresh = qa.get(f"/recipes/{recipe_id}")
        qa.post(f"/recipes/{recipe_id}/transition", {
            "target_state": target, "signature_id": qa.sign(meaning, recipe_id, fresh["row_version"]),
        })
    ok("流程已发布", recipe_id)

    # ---------- 2 方案 ----------
    step("2 矩阵方案：注液量下发到设备，设定设计空间")
    plan = researcher.post("/plans", {
        "name": "FEC × 注液量 试点（SiLA 2）", "recipe_id": recipe_id, "plan_type": "matrix",
        "goal": "筛选 FEC 添加量与注液量对首次放电比容量的影响；注液在 SiLA 2 配液站按孔位执行",
        "repeats": 1, "layout": "sequential", "seed": 1,
        "factors": [
            {"name": "FEC 含量", "unit": "%", "levels": [0, 2, 5, 10]},
            {"name": "注液量", "unit": "μL", "levels": [50, 60],
             "target": {"step_id": "s03", "param": "electrolyte"},
             "material": {"name": "电解液 LP57", "unit": "mL", "per": 0.001}},
        ],
        "design_space": {
            "bounds": {"FEC 含量": {"min": 0, "max": 10}, "注液量": {"min": 40, "max": 80}},
            "forbidden": [{"FEC 含量": 10, "注液量": 40}], "max_points": 8,
        },
        "required_metrics": list(METRICS.values()),
    })
    plan_id = plan["id"]
    researcher.post(f"/plans/{plan_id}/lock")
    researcher.post(f"/plans/{plan_id}/submit")
    fresh = qa.get(f"/plans/{plan_id}")
    qa.post(f"/plans/{plan_id}/decision", {
        "conclusion": "approved", "signature_id": qa.sign("批准实验方案", plan_id, fresh["row_version"]),
    })
    ok("方案已批准", f"{plan_id}，{fresh.get('condition_count', '?')} 个条件")

    # ---------- 3 任务、批次、排程、下发 ----------
    step("3 任务 → 批次 → 排程 → 签名下发")
    task = researcher.post("/experiment-tasks", {"plan_id": plan_id, "priority": 1})
    researcher.post(f"/experiment-tasks/{task['id']}/assign", {"assignee_user_id": operator.id})
    operator.post(f"/experiment-tasks/{task['id']}/accept")
    batch = operator.post("/batches", {"plan_id": plan_id, "task_id": task["id"], "note": "试点案例：SiLA 2 设备执行"})
    batch_id = batch["id"]
    operator.post(f"/batches/{batch_id}/schedule", {})
    preflight = operator.get(f"/batches/{batch_id}/preflight?manual_review=true")
    if not preflight["ok"]:
        raise Failed("开跑检查未通过：" + "；".join(row["detail"] for row in preflight["blocked"]))
    fresh = operator.get(f"/batches/{batch_id}")
    operator.post(f"/batches/{batch_id}/dispatch", {
        "manual_review": True, "reason": "试点案例",
        "signature_id": operator.sign("批准执行", batch_id, fresh["row_version"]),
    })
    ok("已下发", f"{batch_id}（任务 {task['id']}）")

    # ---------- 4 执行 ----------
    step("4 执行器投递到 SiLA 2 设备（配液约 20 s，充放电约 60 s × 2）")

    def gate_pending():
        detail = operator.get(f"/batches/{batch_id}")
        if detail["state"] in {"fault", "aborted"}:
            raise Failed(f"批次异常：{detail['state']} {detail['failure_reason']}")
        pending = [r for r in detail["step_runs"] if r["kind"] == "gate" and r["state"] == "ready"]
        return (detail, pending[0]) if detail["state"] == "paused" and pending else None

    detail, pending = wait_for("质检关卡转 QA 判定", gate_pending, timeout=420)
    for checkpoint in detail["checkpoints"]:
        payload = checkpoint["payload"]
        delivered = payload.get("delivered") or {}
        brief = delivered.get("discharge_capacity_mAh") or len(delivered.get("wells") or {}) or ""
        ok(f"第 {checkpoint['step_index'] + 1} 步检查点", f"{payload['station_id']} · {payload['origin']} · {brief}")
    reworked = [r for r in detail["step_runs"] if r["state"] == "superseded"]
    ok("关卡不合格返工后仍不合格，批次保持", f"作废保留 {len(reworked)} 次执行")

    # ---------- 5 QA 判定 ----------
    step("5 QA 判定质检关卡")
    qa.post(f"/step-runs/{pending['id']}/gate-decision", {
        "conclusion": "approved",
        "reason": "放电容量 3.2 mAh 左右，低于试点下限 3.25 系下限设定偏严；曲线形态正常，按工艺偏差放行",
        "signature_id": qa.sign("质检判定属实", pending["id"]),
    })
    wait_for("批次完成", lambda: operator.get(f"/batches/{batch_id}")["state"] == "done" or None, timeout=60)
    ok("批次已完成")

    # ---------- 6 检测与复核 ----------
    step("6 检测录入与数据复核")
    detail = operator.get(f"/batches/{batch_id}")
    values = []
    invalid_sample = ""
    for index, sample in enumerate(sorted(detail["samples"], key=lambda s: s["position"])):
        fec, volume = (sample.get("levels") or [0, 60])[:2]
        analysis = researcher.post("/analysis-tasks", {
            "sample_id": sample["id"], "physical_sample_id": sample["physical_sample_id"],
            "method": "电性能测试", "method_version": "EC-02 v2", "required_metrics": list(METRICS.values()),
        })
        entered = researcher.post(f"/analysis-tasks/{analysis['id']}/results", {
            "event_id": f"case-{batch_id}-{sample['id']}",
            "metrics": [
                {"metric_version_id": METRICS["areal_density"], "value": round(22.0 + 0.05 * index, 2), "unit": "mg/cm2"},
                {"metric_version_id": METRICS["discharge_capacity"], "value": capacity(float(fec), float(volume)),
                 "unit": "mAh/g"},
            ],
        })
        if float(fec) == 10 and float(volume) == 50:
            invalid_sample = sample["id"]
        values.extend((sample["id"], row) for row in entered["results"])
    for sample_id, value in values:
        invalid = sample_id == invalid_sample
        qa.post(f"/result-values/{value['id']}/review", {
            "conclusion": "approved", "quality": "invalid" if invalid else "valid",
            "reason": "封口处漏液，数据无效" if invalid else "曲线正常",
            "result_version": value["result_version"],
            "signature_id": qa.sign("数据复核通过", value["id"], value["result_version"]),
        })
    ok("已录入并复核", f"{len(values)} 条，其中 {invalid_sample or '无'} 判定无效")

    # ---------- 7 报告 ----------
    step("7 报告审签与发布")
    report = researcher.post("/reports", {
        "batch_id": batch_id,
        "conclusion": "FEC 2–5% 时首次放电比容量最高，10% 回落；60 μL 注液普遍优于 50 μL。建议下一轮在 FEC 1–8%、注液 55–70 μL 内加密。",
    })
    researcher.post(f"/reports/{report['id']}/submit")
    fresh = qa.get(f"/reports/{report['id']}")
    approved = qa.post(f"/reports/{report['id']}/approve", {
        "conclusion": "approved", "signature_id": qa.sign("批准报告", report["id"], fresh["row_version"]),
    })
    published = qa.post(f"/reports/{report['id']}/publish", {
        "signature_id": qa.sign("发布报告", report["id"], approved["row_version"]),
    })
    ok("报告已发布", f"{report['id']} · {published['state']}")

    # ---------- 8 闭环 ----------
    step("8 训练数据与外部提案")
    dataset = researcher.get(f"/plans/{plan_id}/dataset.csv")
    rows = [line for line in (dataset.decode("utf-8-sig") if isinstance(dataset, bytes) else str(dataset)).splitlines() if line]
    ok("训练数据", f"{max(0, len(rows) - 1)} 行（只含复核通过且质量有效的结果）")
    accepted = researcher.post(f"/plans/{plan_id}/proposals", {
        "proposal_id": "bo-round2-001", "source": "BO-GP", "model_version": "gp-2026.09",
        "rationale": "第 1 轮 7 条有效数据拟合 GP，按 EI 取 3 个点",
        "points": [{"FEC 含量": 1.5, "注液量": 55}, {"FEC 含量": 3, "注液量": 62}, {"FEC 含量": 7.5, "注液量": 70}],
    })
    ok("提案已接受", f"生成第 2 轮方案草稿 {accepted['created_plan_id']}（仍需锁定、提交、QA 批准）")
    rejected = researcher.post(f"/plans/{plan_id}/proposals", {
        "proposal_id": "bo-round2-002", "source": "BO-GP", "model_version": "gp-2026.09",
        "rationale": "探索边界",
        "points": [{"FEC 含量": 12, "注液量": 60}, {"FEC 含量": 10, "注液量": 40}],
    }, expect=(422,))
    ok("越界提案被拒并留档", "；".join(rejected["detail"]["issues"])[:120])

    print(f"\n完成：流程 {recipe_id} · 方案 {plan_id} · 批次 {batch_id} · 报告 {report['id']} · 第 2 轮 {accepted['created_plan_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
