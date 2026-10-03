#!/usr/bin/env python3
"""ProtoForge 联调线全流程：三台 ProtoForge 模拟设备经驱动宿主接入（只走 SiLA 2），按 SOP → 能力 / 设备方法 → 流程 →
方案 → 实验任务 → 排程 → 批次执行 → 数据复核 → 报告跑一遍。

    python3 scripts/load-driver-host-pilot.py register            # 先把三台设备经驱动宿主接进来（见 devices/host/README.md）
    python3 scripts/load-protoforge-flow.py register [--base http://127.0.0.1:8090]
    python3 scripts/load-protoforge-flow.py run [--samples 3] [--temp 60]

三台设备各管一段：
- ST-PF-HTTP（HTTP REST 温湿度传感器，只读写点位）：环境采集，把温度、相对湿度记成「设备模拟联调区」的环境读数；
- ST-PF-OPCUA（OPC UA 压力传感器，只读写点位）：环境采集，把压力记成 ST-PF-MB 工位的读数；
- ST-PF-MB（Modbus TCP 握手 PLC）：执行「PLC 控温运行」（cap.plc_run，写设定温度、置启动，约 5 s 后回报实测温度）。

流程的步骤写了环境要求（实验区温湿度、PLC 工位压力）：开跑检查、PLC 步骤下发前都按两台传感器的最新读数核对，
没有读数、过期、超限都不放行。

- register：两台传感器的环境采集（连接配置 environment，签名保存，不用重新握手）、能力 cap.plc_run 与工位范围、检测指标
  「PLC 实测温度」、设备方法（工程师起草、QA 发布）、操作员资质、SOP-PF-01（scripts/lines/protoforge/sop.json，研究员起草、
  QA 批准发布、操作员阅读确认）、流程（研究员起草、关联 SOP、QA 批准发布）。已有的先查后用，重复运行不会多建。
- run：先照 register 补齐，再 方案（单条件、N 个联调样品，QA 批准）→ 实验任务（研究员建、分配给操作员、操作员接受）→
  批次 → 排程 → 开跑检查（含环境核对）→ 签名下发 → 两个人工节点 → PLC 控温运行（执行器经驱动宿主下发）→ QA 审核节点 →
  设备回报的实测温度 QA 逐条复核 → 报告发布。要求执行器在跑、三台设备在线。

数据来自模拟设备：结果带「模拟」标记，不进闭环训练数据。正式环境（ILCS_ENVIRONMENT=production）拒绝运行。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("load_electrolyte_line", HERE / "load-electrolyte-line.py")
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)  # 复用它的 HTTP 传输（含上传）、演示账号与签名、SOP 登记、输出
Actor, Failed, actors, http_transport = common.Actor, common.Failed, common.actors, common.http_transport
step, ok, note, _items = common.step, common.ok, common.note, common._items

SOP = HERE / "lines" / "protoforge" / "sop.json"
PLC = "ST-PF-MB"
AMBIENT = "设备模拟联调区"
OPERATOR = "P-003"
SAMPLE_TYPE = "联调样品"
CAPABILITY = "cap.plc_run"
TEMP_RANGE = (0, 100)
# 两台只读写点位的传感器：环境采集（连接配置 environment）
SENSORS = {
    "ST-PF-HTTP": {"zone": AMBIENT, "interval_sec": 30, "points": {"temperature": "temperature", "humidity": "humidity"}},
    "ST-PF-OPCUA": {"zone": PLC, "interval_sec": 30, "points": {"pressure": "pressure"}},
}
# 步骤的环境要求：两台传感器给的读数
AMBIENT_REQUIREMENTS = [
    {"metric": "temperature", "min": 10, "max": 45, "zone": AMBIENT},
    {"metric": "humidity", "max": 80, "zone": AMBIENT},
]
PROCESS_REQUIREMENTS = [{"metric": "pressure", "min": 0.5, "max": 5, "zone": PLC}]
METRIC = {"code": "pf_plc_temp", "name": "PLC 实测温度", "unit": "℃", "rules": {"min": 0, "max": 150}}
METHOD = {
    "name": "ProtoForge PLC 控温运行", "capability_id": CAPABILITY, "program": "", "params": {},
    "dur_min": 1, "note": "握手 PLC：写设定温度 sp、置启动 cmd_start，状态字 1 运行 → 3 完成，取实测温度 pv，复位 cmd_ack",
}
RECIPE_NAME = "ProtoForge PLC 控温运行联调"
RISK = "RA-PF-01 v1（联调占位：ProtoForge 模拟设备，无真实样品、加热体与压力容器）"
RECIPE_DESIGN = ("三台 ProtoForge 设备经驱动宿主接入：HTTP 传感器给实验区温湿度、OPC UA 传感器给 PLC 工位压力（步骤环境要求核对），"
                 "Modbus 握手 PLC 按设定温度运行并回报实测温度；QA 复核")


def refuse_production() -> None:
    if os.environ.get("ILCS_ENVIRONMENT", "").strip().lower() == "production":
        raise Failed("正式环境拒绝运行：本脚本把模拟设备接进流程")


def wait_for(describe: str, probe: Callable[[], Any], timeout: float, every: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = probe()
        if found:
            return found
        time.sleep(every)
    raise Failed(f"{describe}：{timeout:.0f} 秒内没有达成")


# ---------------------------------------------------------------- register


def register_environment(engineer: Actor, timeout: float) -> None:
    """两台传感器的连接配置加环境采集（签名保存；改它不用重新握手），等执行器记下第一批读数。"""
    for station_id, spec in SENSORS.items():
        adapter = engineer.get(f"/stations/{station_id}/adapter")
        if adapter.get("kind") != "real":
            raise Failed(f"{station_id} 还没接成真实设备：先跑 scripts/load-driver-host-pilot.py register")
        config = adapter.get("config") or {}
        if config.get("environment") != spec:
            engineer.patch(f"/stations/{station_id}/adapter", {
                "config": {**config, "environment": spec}, "row_version": adapter["row_version"],
                "signature_id": engineer.sign("设备集成配置变更批准", station_id, adapter["row_version"]),
            })
            ok("环境采集", f"{station_id} → {spec['zone']}：{'、'.join(spec['points'])}（每 {spec['interval_sec']} s，已签名保存）")
        else:
            ok("环境采集", f"{station_id} → {spec['zone']}（沿用）")

    wanted = {(spec["zone"], metric) for spec in SENSORS.values() for metric in spec["points"]}

    def sampled():
        latest = {(row["zone"], row["metric"]): row for row in engineer.get("/environment/readings")
                  if str(row.get("source") or "").startswith("device:") and not row.get("stale")}
        return latest if wanted <= set(latest) else None

    latest = wait_for("执行器记下传感器的环境读数", sampled, timeout=timeout)
    ok("环境读数", "；".join(f"{zone} {row['metric_label']} {row['value']:g}{row['unit']}"
                            for (zone, _), row in sorted(latest.items()) if (zone, row["metric"]) in wanted))


def register_capability(engineer: Actor) -> None:
    current = next((row for row in engineer.get("/capabilities") if row["id"] == CAPABILITY), None)
    if current is None or "temp" not in (current.get("params") or {}):
        raise Failed(f"能力 {CAPABILITY} 不存在或没有参数 temp：先跑 scripts/load-driver-host-pilot.py register")
    station = next(row for row in engineer.get("/stations") if row["id"] == PLC)
    limits = ((station.get("limits") or {}).get(CAPABILITY) or {}).get("temp")
    if not limits or float(limits[0]) > TEMP_RANGE[0] or float(limits[1]) < TEMP_RANGE[1]:
        raise Failed(f"{PLC} 的 {CAPABILITY} 温度范围是 {limits}，要覆盖 {TEMP_RANGE}")
    ok("能力", f"{CAPABILITY} {current['name']}（参数 temp ℃；{PLC} 可做 {limits[0]:g}–{limits[1]:g} ℃）")


def register_metric(researcher: Actor) -> str:
    existing = {(row["code"], row.get("version")): row for row in _items(researcher.get("/metrics?page_size=200"))}
    row = existing.get((METRIC["code"], "v1")) or researcher.post("/metrics", {
        "code": METRIC["code"], "name": METRIC["name"], "unit": METRIC["unit"], "value_type": "number",
        "sample_types": [SAMPLE_TYPE], "rules": METRIC["rules"],
    })
    ok("检测指标", f"{METRIC['name']} {METRIC['unit']}（{row['id']}）")
    return row["id"]


def register_method(engineer: Actor, qa: Actor, metric_id: str) -> str:
    """工程师起草、QA 发布：输出 temp（PLC 回报的实测温度）写成「PLC 实测温度」检测结果。"""
    released = [row for row in engineer.get(f"/device-methods?state=released&capability_id={CAPABILITY}")
                if row["name"] == METHOD["name"]]
    if released:
        ok("设备方法", f"{released[0]['code']} v{released[0]['version']} {METHOD['name']}（已发布，沿用）")
        return released[0]["id"]
    drafts = [row for row in engineer.get(f"/device-methods?state=draft&capability_id={CAPABILITY}")
              if row["name"] == METHOD["name"]]
    draft = drafts[0] if drafts else engineer.post("/device-methods", {
        **METHOD, "outputs": [{"key": "temp", "label": METRIC["name"], "unit": METRIC["unit"], "lo": 0,
                               "required": True, "metric_id": metric_id}],
    })
    done = qa.post(f"/device-methods/{draft['id']}/release", {"row_version": draft["row_version"]})
    ok("设备方法已发布", f"{done['code']} v{done['version']} {done['name']}：回报实测温度（关联指标，写成检测结果）")
    return done["id"]


def grant_qualification(admin: Actor) -> None:
    person = next((row for row in _items(admin.get(f"/people?keyword={OPERATOR}")) if row.get("code") == OPERATOR), None)
    if person is None:
        raise Failed(f"人员 {OPERATOR} 不存在")
    held = {row["scope_ref"] for row in admin.get(f"/people/{person['id']}/qualifications")
            if row.get("scope_kind") == "capability" and row.get("status") not in {"revoked", "expired"}}
    if CAPABILITY not in held:
        admin.post(f"/people/{person['id']}/qualifications", {
            "scope_kind": "capability", "scope_ref": CAPABILITY, "label": "PLC 控温运行"})
    ok("操作员资质", f"{OPERATOR} 有 {CAPABILITY}")


def recipe_steps(method_id: str, temp: float) -> list[dict]:
    return [
        {"step_id": "s01", "kind": "manual", "name": "核对环境与设备在线", "dur": 2, "requires_signature": False,
         "environment": AMBIENT_REQUIREMENTS,
         "form": [{"key": "devices_online", "label": "ST-PF-HTTP、ST-PF-OPCUA、ST-PF-MB 在线", "type": "bool",
                   "required": True}]},
        {"step_id": "s02", "kind": "manual", "name": "装样并核对设定温度", "dur": 2, "requires_signature": False,
         "requires_sample_check": True,
         "form": [{"key": "setpoint_ok", "label": f"已核对设定温度 {temp:g} ℃", "type": "bool", "required": True}]},
        {"step_id": "s03", "kind": "device", "name": "PLC 控温运行", "cap": CAPABILITY, "params": {"temp": temp},
         "dur": METHOD["dur_min"], "method": {"id": method_id},
         "environment": [*PROCESS_REQUIREMENTS, *AMBIENT_REQUIREMENTS]},
        {"step_id": "s04", "kind": "review", "name": "QA 复核运行数据", "review_role": "qa"},
    ]


def _sop_steps(sop_id: str, researcher: Actor) -> dict[str, str]:
    """SOP 结构化步骤：标题 → 稳定标识（流程节点按它引用 SOP 的那一步）。"""
    detail = researcher.get(f"/sops/{sop_id}")
    return {row["title"]: row["key"] for row in detail.get("steps") or [] if row.get("key")}


def release_recipe(researcher: Actor, qa: Actor, method_id: str, sop: dict, temp: float) -> str:
    """同名已发布的流程（步骤、风险评估、SOP 都一致）沿用；不一致出修订版；没有就新建。研究员起草，QA 批准发布。"""
    steps = recipe_steps(method_id, temp)
    keys = _sop_steps(sop["id"], researcher)
    titles = ["核对环境与设备在线", "装样并核对设定温度", "PLC 控温运行", "QA 复核运行数据"]
    for index, (row, title) in enumerate(zip(steps, titles), start=1):
        if keys.get(title):  # 节点记 SOP 步骤的稳定标识（批次页据此把 SOP 说明带到执行人面前）与序号
            row["sop_step_key"], row["sop_step"] = keys[title], index
    released = [row for row in _items(researcher.get("/recipes")) if row.get("name") == RECIPE_NAME and row.get("state") == "released"]
    for row in released:
        detail = researcher.get(f"/recipes/{row['id']}")
        same = ([(s.get("step_id"), s.get("kind"), (s.get("params") or {}).get("temp")) for s in detail.get("steps") or []]
                == [(s["step_id"], s["kind"], (s.get("params") or {}).get("temp")) for s in steps])
        if same and detail.get("risk") == RISK and (detail.get("sop_version_id") or "") == sop["id"]:
            ok("流程", f"{row['id']} {RECIPE_NAME}（已发布，沿用）")
            return row["id"]
    if released:
        draft = researcher.post(f"/recipes/{released[0]['id']}/revision")
        note(f"已发布的 {released[0]['id']} 和这里的定义不同：出修订版 {draft['id']}")
    else:
        draft = researcher.post("/recipes", {"name": RECIPE_NAME, "plate": 8})
    current = researcher.get(f"/recipes/{draft['id']}")
    researcher.patch(f"/recipes/{draft['id']}", {
        "steps": steps, "bom": [], "design": RECIPE_DESIGN, "risk": RISK, "sop_version_id": sop["id"],
        "row_version": current["row_version"],
    })
    detail = researcher.get(f"/recipes/{draft['id']}")
    bad = [row for row in detail.get("validation") or [] if not row["ok"]]
    if bad:
        raise Failed(f"流程 {draft['id']} 校验不通过：" + "；".join(
            f"第 {r['index'] + 1} 步 {r.get('issues') or r.get('blockers')}" for r in bad))
    researcher.post(f"/recipes/{draft['id']}/submit")
    for target, meaning in (("approved", "批准流程"), ("released", "发布流程")):
        fresh = qa.get(f"/recipes/{draft['id']}")
        qa.post(f"/recipes/{draft['id']}/transition", {
            "target_state": target, "signature_id": qa.sign(meaning, draft["id"], fresh["row_version"]),
        })
    ok("流程已发布", f"{draft['id']} {RECIPE_NAME}（核对环境 → 装样 → PLC 控温运行 → QA 复核，关联 {sop['code']} {sop['version']}）")
    return draft["id"]


def register(team: dict[str, Actor], args: argparse.Namespace) -> dict:
    engineer, qa, operator, researcher, admin = (team[k] for k in ("engineer", "qa", "operator", "researcher", "admin"))
    step("设备与环境采集")
    register_environment(engineer, args.timeout)
    register_capability(engineer)
    step("指标、设备方法、资质")
    metric_id = register_metric(researcher)
    method_id = register_method(engineer, qa, metric_id)
    grant_qualification(admin)
    step("SOP 与流程")
    sop = common.register_sop(researcher, qa, operator, SOP)
    recipe_id = release_recipe(researcher, qa, method_id, sop, args.temp)
    return {"metric": metric_id, "method": method_id, "sop": sop, "recipe": recipe_id}


# ---------------------------------------------------------------- run


def approve_plan(researcher: Actor, qa: Actor, recipe_id: str, metric_id: str, samples: int, temp: float, name: str) -> str:
    plan = researcher.post("/plans", {
        "name": name, "recipe_id": recipe_id, "plan_type": "single_condition", "sample_count": samples,
        "goal": (f"{samples} 个联调样品在 {PLC} 上按 {temp:g} ℃ 控温运行：验证经驱动宿主下发、环境核对（两台传感器的读数）、"
                 "设备回报、复核与报告全链路（模拟设备）"),
        "required_metrics": [metric_id],
    })
    plan_id = plan["id"]
    detail = researcher.get(f"/plans/{plan_id}")
    failing = [f"{row.get('label') or row['key']}：{row.get('detail')}" for row in detail.get("checks") or [] if not row.get("ok")]
    if failing:
        raise Failed(f"方案 {plan_id} 检查未通过：" + "；".join(failing))
    researcher.post(f"/plans/{plan_id}/lock")
    researcher.post(f"/plans/{plan_id}/submit")
    fresh = qa.get(f"/plans/{plan_id}")
    qa.post(f"/plans/{plan_id}/decision", {
        "conclusion": "approved", "signature_id": qa.sign("批准实验方案", plan_id, fresh["row_version"])})
    ok("方案已批准", f"{plan_id} {name}")
    return plan_id


def create_task(researcher: Actor, operator: Actor, plan_id: str, title: str) -> str:
    task = researcher.post("/experiment-tasks", {"plan_id": plan_id, "title": title, "priority": 2,
                                                  "note": "ProtoForge 联调线全流程"})
    researcher.post(f"/experiment-tasks/{task['id']}/assign", {"assignee_user_id": operator.id})
    operator.post(f"/experiment-tasks/{task['id']}/accept")
    ok("实验任务", f"{task['id']} {title}（研究员建、分配给操作员 {OPERATOR}、已接受）")
    return task["id"]


def launch(operator: Actor, plan_id: str, task_id: str, note_text: str) -> str:
    """建批次 → 排程 → 开跑检查（含环境核对）→ 签名下发。检查没过就退回待排程并说明原因。"""
    batch = operator.post("/batches", {"plan_id": plan_id, "task_id": task_id, "note": note_text})
    batch_id = batch["id"]
    operator.post(f"/batches/{batch_id}/schedule", {})
    scheduled = operator.get(f"/batches/{batch_id}")
    kinds = {"work": "", "clean": "清洁 "}
    windows = [f"第 {row['step_index'] + 1} 步 {row.get('station_id')} {kinds.get(row.get('kind'), row.get('kind') + ' ')}"
               f"{str(row.get('starts_at', ''))[11:16]}–{str(row.get('ends_at', ''))[11:16]}"
               for row in scheduled.get("allocations") or [] if row.get("kind") != "assist"]
    ok("已排程", f"{batch_id}：" + "；".join(windows) + "（UTC；人工步骤不占工位）")
    preflight = operator.get(f"/batches/{batch_id}/preflight?manual_review=true")
    if not preflight["ok"]:
        operator.post(f"/batches/{batch_id}/unschedule", {})
        raise Failed("开跑检查未通过：" + "；".join(f"{row.get('label')}：{row.get('detail')}" for row in preflight["blocked"])
                     + f"\n批次 {batch_id} 已退回待排程")
    environment = [row for row in preflight.get("checks") or [] if "环境" in str(row.get("label") or row.get("key"))]
    ok("开跑检查通过", "；".join(f"{row.get('label')}：{row.get('detail') or '通过'}" for row in environment) or "全部通过")
    fresh = operator.get(f"/batches/{batch_id}")
    operator.post(f"/batches/{batch_id}/dispatch", {
        "manual_review": True, "reason": note_text,
        "signature_id": operator.sign("批准执行", batch_id, fresh["row_version"]),
    })
    ok("签名下发", batch_id)
    return batch_id


def drive(operator: Actor, qa: Actor, batch_id: str, timeout: float) -> dict:
    """推到批次结束：两个人工节点由操作员提交，审核节点由 QA 批准；PLC 步骤由执行器经驱动宿主下发。"""
    handled: set[str] = set()
    started = time.monotonic()

    def advance():
        detail = operator.get(f"/batches/{batch_id}")
        if detail["state"] == "done":
            return detail
        if detail["state"] in {"fault", "aborted", "paused", "hold"}:
            alarms = "；".join(a["message"] for a in detail.get("alarms") or [] if a["state"] != "closed")
            raise Failed(f"批次 {batch_id} 进入 {detail['state']}：{detail.get('failure_reason')} {alarms}".strip())
        for run in detail["step_runs"]:
            if run["id"] in handled or run["state"] not in {"ready", "running"}:
                continue
            if run["kind"] == "manual":
                form = {"devices_online": True} if run.get("step_id") == "s01" or "核对环境" in run["step_name"] \
                    else {"setpoint_ok": True}
                body = {"form_data": form, "checks": {"samples": True, "materials": bool(detail["reservations"])},
                        "note": "ProtoForge 联调脚本", "row_version": run["row_version"]}
                if run["requires_signature"]:
                    body["signature_id"] = operator.sign("人工步骤记录确认", run["id"], run["row_version"])
                operator.post(f"/step-runs/{run['id']}/submit", body)
                handled.add(run["id"])
                ok(f"人工节点「{run['step_name']}」已提交")
            elif run["kind"] == "review":
                qa.post(f"/step-runs/{run['id']}/review", {
                    "conclusion": "approved", "row_version": run["row_version"],
                    "signature_id": qa.sign("流程审核通过", run["id"], run["row_version"])})
                handled.add(run["id"])
                ok(f"审核节点「{run['step_name']}」QA 已批准")
        return None

    detail = wait_for(f"批次 {batch_id} 完成", advance, timeout=timeout, every=2)
    ok("批次已完成", f"{batch_id}，用时 {time.monotonic() - started:.0f} 秒")
    for checkpoint in sorted(detail["checkpoints"], key=lambda row: row["step_index"]):
        payload = checkpoint.get("payload") or {}
        if payload.get("origin", "").startswith("real:"):
            ok(f"第 {checkpoint['step_index'] + 1} 步设备回报", f"{payload.get('station_id')} · {payload.get('origin')} · "
                                                         f"回报 {payload.get('delivered')}")
    return detail


def device_results(actor: Actor, detail: dict) -> list[dict]:
    rows = []
    for sample in sorted(detail["samples"], key=lambda row: row["position"]):
        for task in _items(actor.get(f"/analysis-tasks?sample_id={sample['id']}&page_size=100")):
            if task.get("method") != "设备回报":
                continue
            for value in actor.get(f"/analysis-tasks/{task['id']}").get("values") or []:
                if not value.get("superseded_by_id"):
                    rows.append({**value, "sample_id": sample["id"]})
    return rows


def review_results(qa: Actor, researcher: Actor, detail: dict) -> list[dict]:
    rows = device_results(researcher, detail)
    if not rows:
        raise Failed("批次跑完了，但没有设备写入的检测结果：检查设备方法的输出项有没有关联指标")
    for row in rows:
        if row.get("review_state") != "pending":
            continue
        qa.post(f"/result-values/{row['id']}/review", {
            "conclusion": "approved", "quality": "valid", "result_version": row["result_version"],
            "reason": "ProtoForge 模拟 PLC 回报的值，复核只为验证接入与数据链路",
            "signature_id": qa.sign("数据复核通过", row["id"], row["result_version"])})
    fresh = device_results(researcher, detail)
    ok("设备回报结果已复核", f"{len(fresh)} 条：" + "、".join(f"{row.get('value')}{row.get('unit') or ''}" for row in fresh))
    return fresh


def publish_report(researcher: Actor, qa: Actor, batch_id: str, detail: dict, results: list[dict], temp: float) -> str:
    conclusion = (
        f"批次 {batch_id} 按 {RECIPE_NAME} 在 {PLC}（ProtoForge 握手 PLC，经驱动宿主以 SiLA 2 接入）上完成 {len(detail['samples'])} 个"
        f"联调样品的 {temp:g} ℃ 控温运行；开跑检查与下发前按 ST-PF-HTTP、ST-PF-OPCUA 的环境读数核对了实验区温湿度与工位压力；"
        f"PLC 回报的实测温度 {len(results)} 条已复核。模拟设备：数值不是实测，只用于验证 SOP、流程、方案、任务、排程、执行、复核与"
        "报告链路。")
    report = researcher.post("/reports", {"batch_id": batch_id, "conclusion": conclusion})
    researcher.post(f"/reports/{report['id']}/submit")
    fresh = qa.get(f"/reports/{report['id']}")
    approved = qa.post(f"/reports/{report['id']}/approve", {
        "conclusion": "approved", "signature_id": qa.sign("批准报告", report["id"], fresh["row_version"])})
    published = qa.post(f"/reports/{report['id']}/publish", {
        "signature_id": qa.sign("发布报告", report["id"], approved["row_version"])})
    ok("报告已发布", f"{report['id']} · {published.get('state_label') or published.get('state')}")
    return report["id"]


def run(team: dict[str, Actor], context: dict, args: argparse.Namespace) -> dict:
    researcher, qa, operator = team["researcher"], team["qa"], team["operator"]
    gate = operator.get("/gate")
    if not gate["open"] or PLC in (gate.get("blocked_stations") or {}):
        raise Failed(f"执行门没开或 {PLC} 被挡：{gate.get('reasons')} {gate.get('blocked_stations')}")
    name = args.plan_name or f"ProtoForge 联调：{args.samples} 个样品 {args.temp:g} ℃ 控温运行"
    step("方案与实验任务")
    plan_id = approve_plan(researcher, qa, context["recipe"], context["metric"], args.samples, args.temp, name)
    task_id = create_task(researcher, operator, plan_id, name)
    step("排程与下发")
    batch_id = launch(operator, plan_id, task_id, f"ProtoForge 联调：{args.samples} 个样品")
    step("执行")
    detail = drive(operator, qa, batch_id, args.timeout)
    step("数据复核与报告")
    results = review_results(qa, researcher, detail)
    report_id = publish_report(researcher, qa, batch_id, detail, results, args.temp)
    return {"plan": plan_id, "task": task_id, "batch": batch_id, "report": report_id}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ProtoForge 联调线全流程（经驱动宿主、只走 SiLA 2）")
    parser.add_argument("command", choices=("register", "run"))
    parser.add_argument("--base", default=os.environ.get("ILCS_BASE_URL", "http://127.0.0.1:8090"))
    parser.add_argument("--samples", type=int, default=3, help="联调样品数（1–8）")
    parser.add_argument("--temp", type=float, default=60, help="设定温度 ℃（0–100）")
    parser.add_argument("--plan-name", default="")
    parser.add_argument("--timeout", type=float, default=600, help="等读数、等批次完成的秒数")
    args = parser.parse_args(argv)
    try:
        refuse_production()
        if not 1 <= args.samples <= 8:
            raise Failed("样品数 1–8")
        if not TEMP_RANGE[0] <= args.temp <= TEMP_RANGE[1]:
            raise Failed(f"设定温度要在 {TEMP_RANGE[0]}–{TEMP_RANGE[1]} ℃")
        team = actors(http_transport(args.base))
        context = register(team, args)
        if args.command == "register":
            print(f"\n完成：SOP {context['sop']['code']} {context['sop']['version']} · 流程 {context['recipe']} · 设备方法 {context['method']}")
            return 0
        outcome = run(team, context, args)
        print(f"\n完成：方案 {outcome['plan']} · 任务 {outcome['task']} · 批次 {outcome['batch']} · 报告 {outcome['report']}")
        return 0
    except Failed as exc:
        print(f"\n失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
