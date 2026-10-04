#!/usr/bin/env python3
"""把四个参考案例（docs/操作案例.md）真实跑一遍导进演示库。

只走 HTTP，和界面调同一组接口；签名用演示账号口令逐次签署（`POST /signatures`），
与在界面上签的一样。批次由执行器真实投递到外部模拟设备（reset-demo-cases.sh 已按
ilcs-devices/simulators/pilot-devices.json 把示例工位接好）：ST-05 用内置模拟适配器，ST-06 走 SiLA 2，
ST-07 走 HTTPS 网关（厂家 SDK 接口服务），AGV 走车队 REST 接口；机械臂 ARM-01 登记时就接 UR 仪表盘服务。

    python3 scripts/load-demo-cases.py http://127.0.0.1:8090
    python3 scripts/load-demo-cases.py http://127.0.0.1:8090 --pilot-devices=../ilcs-devices/simulators/pilot-devices.json

    案例 A 注液：        真空干燥 → 称重 → 按孔位注液封口（SiLA 2）→ 逐孔注液量质检 → 人工封口检查 → QA 复核
    案例 B 循环测试：    上柜检查 → 化成 → 静置 → 循环测试（8 通道，引用设备方法）→ 放电容量质检 → QA 复核
    案例 C 串行：        托盘绑定批次，AGV 在 板库 → ST-05 → ST-06 → ST-07 之间自动转运，
                         注液时手套箱机械臂协同上下料；注液与循环测试在一个批次里串起来
    案例 D 分批：        20 个扣电用案例 B 的流程（每批 8 位）：建任务时自动拆成 3 个子任务（7/7/6），
                         一次建 3 个批次、多批次优化一起排程，逐批执行；父任务汇总进度，出一份合并报告

每个案例都走完：流程发布 → 方案审批 → 任务 → 批次 → 排程 → 开跑检查 → 签名下发 → 执行 →
检测录入 → 数据复核 → 报告发布。

前提：刚用 reset-demo-cases.sh 重置过（种子主数据、没有演示流程与方案），外部模拟设备在线。
不带 `--pilot-devices` 时机械臂 ARM-01 登记为内置模拟适配器。
正式环境不要运行：它会登记演示用的机械臂工位、能力与指标。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

ARGS = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
BASE = (ARGS[0] if ARGS else "http://127.0.0.1:8090").rstrip("/") + "/api"
# 试点设备预设：ARM-01 按它登记真实驱动（UR 仪表盘服务）；没给就用内置模拟适配器
PILOT_DEVICES = next((arg.split("=", 1)[1] for arg in sys.argv[1:] if arg.startswith("--pilot-devices=")),
                     os.environ.get("ILCS_PILOT_DEVICES", ""))
PASSWORD = "ilcs1234"
RUN = uuid.uuid4().hex[:6]

ELECTROLYTE = {"name": "电解液 LP57", "unit": "mL", "per": 0.001}
METRIC_IDS: dict[str, str] = {}

# 案例 B 的循环测试按这版设备方法执行：流程只写倍率（方案因子按孔位覆盖），截止电压与圈数取方法缺省；
# 方法规定设备端程序与应回报的数据，排程只把它落到型号适用、设备报告支持该程序的工位上
CYCLING_METHOD = {
    "name": "扣电 50 圈循环测试", "capability_id": "cap.test", "program": "CYC-50",
    "params": {
        "rate": {"default": 0.5, "min": 0.1, "max": 2, "unit": "C"},
        "vmax": {"default": 4.3, "min": 4.2, "max": 4.4, "unit": "V"},
        "cycles": {"default": 50, "min": 50, "max": 50, "unit": "圈"},
    },
    "outputs": [
        {"key": "cycles_completed", "label": "完成圈数", "unit": "圈", "lo": 50, "required": True},
        {"key": "discharge_capacity_mAh", "label": "放电容量", "unit": "mAh", "lo": 2.5, "hi": 4.0, "required": True},
    ],
    "dur_min": 120,
    "note": "参考案例 B：倍率可在 0.1–2C 内由流程或方案因子调整，截止电压 4.2–4.4 V，圈数固定 50",
}


class Failed(SystemExit):
    pass


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


def ok(label: str, detail: str = "") -> None:
    print(f"  ✓ {label}{('：' + detail) if detail else ''}", flush=True)


class Actor:
    """一个演示账号：登录拿令牌，签名时再验一次口令。"""

    def __init__(self, username: str) -> None:
        self.username = username
        self.token = ""
        body = self.call("POST", "/auth/login", {"username": username, "password": PASSWORD})
        self.token = body["access_token"]
        self.id = body["user"]["id"]

    def call(self, method: str, path: str, body: dict | None = None, expect: tuple[int, ...] = (200, 201)):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(BASE + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        if method != "GET":
            request.add_header("Idempotency-Key", f"case-{RUN}-{uuid.uuid4().hex[:10]}")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
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
        body = self.post("/signatures", {
            "password": PASSWORD, "meaning": meaning, "target": target, "object_version": version,
        })
        return body["signature_id"]


def wait_for(describe: str, probe, timeout: float = 300.0, every: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = probe()
        if found:
            return found
        time.sleep(every)
    raise Failed(f"{describe}：{timeout:.0f}s 内未达成")


# ---------------------------------------------------------------- 主数据补充


def prepare(admin: Actor, operator: Actor) -> None:
    step("0 主数据补充：机械臂工位、循环圈数参数、检测指标、托盘")
    capabilities = {row["id"]: row for row in admin.get("/capabilities")}
    stations = {row["id"]: row for row in admin.get("/stations")}

    # 循环测试要把圈数下发给充放电柜：cap.test 加一个 cycles 参数，ST-07 登记它的范围
    test = capabilities["cap.test"]
    if "cycles" not in (test.get("params") or {}):
        admin.patch("/capabilities/cap.test", {
            "params": {**test["params"], "cycles": "循环圈数"},
            "signature_id": admin.sign("修改能力定义", "cap.test"),
        })
    st07 = stations["ST-07"]
    if "cycles" not in ((st07.get("limits") or {}).get("cap.test") or {}):
        admin.patch("/stations/ST-07/limits", {
            "limits": {"cap.test": {"cycles": [1, 2000]}}, "row_version": st07.get("row_version"),
            "signature_id": admin.sign("修改能力极限", "ST-07"),
        })
    ok("充放电柜 ST-07", "cap.test 增加 cycles（1–2000 圈）")

    # 手套箱里的上下料机械臂：注液时与 ST-06 同起同止（协同资源），不当承运工位用
    if "cap.robot_load" not in capabilities:
        admin.post("/capabilities", {
            "id": "cap.robot_load", "name": "机械臂上下料", "params": {},
            "recovery": {"maxHoldMin": 30, "pausable": True, "hold": "机械臂回安全位，夹爪保持", "retryable": True,
                         "sideEffect": "重试前确认夹爪上没有电芯", "verify": ["夹爪状态", "托盘孔位"]},
            "signature_id": admin.sign("登记新能力", "cap.robot_load"),
        })
    arm_driver = "内置模拟适配器"
    if "ARM-01" not in stations:
        adapter = {"protocol": "UR 仪表盘服务（TCP 命令）", "adapter_version": "5.12"}
        preset = None
        if PILOT_DEVICES:  # 预设里登记了 ARM-01 接哪台设备才接；没有（串口命令驱动已移出 ILCS）就用内置模拟
            with open(PILOT_DEVICES, encoding="utf-8") as handle:
                preset = json.load(handle)["stations"].get("ARM-01")
        if preset:
            adapter = {
                "protocol": preset["protocol"], "adapter_version": "5.12", "adapter_kind": "real",
                "adapter_driver": preset["driver"], "adapter_config": preset["config"],
                "credential_ref": preset.get("credential_ref", ""),
            }
            arm_driver = f"{preset['driver']}（{preset['protocol']}）"
        admin.post("/stations", {
            "id": "ARM-01", "name": "手套箱上下料机械臂", "island": 5, "model": "UR5e-GB",
            "limits": {"cap.robot_load": {}}, **adapter, "signature_id": admin.sign("登记新工位", "ARM-01"),
        })
    if (admin.get("/gate").get("blocked_stations") or {}).get("ARM-01"):
        # 新登记的适配器是离线的：在「现场监控」该工位卡片上点「重连」做一次握手，之后由心跳维持在线
        admin.post("/stations/ARM-01/adapter/reconnect")
    wait_for("ARM-01 适配器在线", lambda: not (admin.get("/gate").get("blocked_stations") or {}).get("ARM-01"),
             timeout=60)
    ok("机械臂 ARM-01", f"能力 cap.robot_load，{arm_driver}")

    # 操作员要有新能力的资质，否则任务分配按资质挡住；管理员默认拥有全部能力资质（和种子一致）
    people = admin.get("/people?limit=200")
    people = people.get("items", people) if isinstance(people, dict) else people
    for code in ("P-003", "P-005"):
        person = next(row for row in people if row.get("code") == code)
        quals = admin.get(f"/people/{person['id']}/qualifications")
        if not any(row["scope_ref"] == "cap.robot_load" and row.get("state") != "revoked" for row in quals):
            admin.post(f"/people/{person['id']}/qualifications", {
                "scope_kind": "capability", "scope_ref": "cap.robot_load", "label": "机械臂上下料",
            })
    ok("操作员与管理员资质", "cap.robot_load")

    metrics = {row["code"]: row for row in admin.get("/metrics")}
    for code, name, unit, rules in (
        ("electrolyte_volume", "实际注液量", "μL", {"min": 0, "max": 200}),
        ("ocv", "开路电压", "V", {"min": 0, "max": 5}),
    ):
        if code not in metrics:
            metrics[code] = admin.post("/metrics", {
                "code": code, "name": name, "unit": unit, "value_type": "number",
                "method_version": "EC-02 v2", "sample_types": ["扣电"], "rules": rules,
            })
    METRIC_IDS.update({code: row["id"] for code, row in metrics.items()})
    ok("检测指标", "实际注液量 μL、开路电压 V（另用种子里的放电比容量、容量保持率、外观判定）")

    existing = {row["barcode"] for row in operator.get("/labware")}
    for barcode, slot in (("TRAY-C01", "HOTEL-01/S01"), ("TRAY-C02", "HOTEL-01/S02")):
        if barcode not in existing:
            operator.post("/labware", {"barcode": barcode, "type_id": "LT-TRAY-8", "location_id": slot,
                                       "note": "案例 C 用 8 位扣电托盘"})
    ok("托盘", "TRAY-C01 → 板库 S01（案例导入用）、TRAY-C02 → 板库 S02（留给你自己操作）")


def release_cycling_method(engineer: Actor, qa: Actor) -> str:
    """自动化工程师起草、QA 发布「扣电 50 圈循环测试」。适用型号取 ST-07 关联资产登记的型号。"""
    for row in engineer.get("/device-methods?state=released&capability_id=cap.test"):
        if row["name"] == CYCLING_METHOD["name"]:
            ok("设备方法", f"{row['code']} v{row['version']} {row['name']}（已发布，沿用）")
            return row["id"]
    st07 = next(row for row in engineer.get("/stations") if row["id"] == "ST-07")
    if not st07.get("model"):
        raise Failed("ST-07 关联的资产没有登记型号：先到「仪器设备」补上，设备方法按它匹配工位")
    draft = engineer.post("/device-methods", {**CYCLING_METHOD, "instrument_models": [st07["model"]]})
    released = qa.post(f"/device-methods/{draft['id']}/release", {"row_version": draft["row_version"]})
    ok("设备方法已发布", f"{released['code']} v{released['version']} {released['name']}：适用型号 {st07['model']}，"
                        f"程序 {released['program']}，回报完成圈数与放电容量")
    return released["id"]


# ---------------------------------------------------------------- 通用链路


def release_recipe(researcher: Actor, qa: Actor, name: str, design: str, steps: list[dict], bom: list[dict]) -> str:
    recipe = researcher.post("/recipes", {"name": name, "plate": 8})
    recipe_id = recipe["id"]
    current = researcher.get(f"/recipes/{recipe_id}")
    # 关联 SOP-EC-02 当前生效的版本：它的适用能力覆盖干燥、称重、组装、测试，执行人（操作员）已有阅读确认
    sops = researcher.get("/sops?page_size=100")
    sops = sops.get("items", sops) if isinstance(sops, dict) else sops
    sop = next((row for row in sops if row.get("code") == "SOP-EC-02" and row.get("effective")), None)
    if sop is None:
        raise Failed("SOP-EC-02 没有生效版本：案例流程要关联它")
    # 节点按 SOP 步骤的稳定标识引用（编辑器里选「对应 SOP 步骤」也是这样存的）：新版本插入步骤时仍对得上
    sop_steps = sop.get("steps") or []
    for row in steps:
        position = row.get("sop_step")
        if position and 1 <= position <= len(sop_steps) and sop_steps[position - 1].get("key"):
            row["sop_step_key"] = sop_steps[position - 1]["key"]
    researcher.patch(f"/recipes/{recipe_id}", {
        "steps": steps, "bom": bom, "design": design, "risk": "RA-205 v2",
        **({"sop_version_id": sop["id"]} if sop else {}),
        "row_version": current["row_version"],
    })
    detail = researcher.get(f"/recipes/{recipe_id}")
    bad = [row for row in detail.get("validation") or [] if not row["ok"]]
    if bad:
        raise Failed(f"流程 {recipe_id} 校验不通过：" + "；".join(f"第 {r['index'] + 1} 步 {r['issues'] or r['blockers']}" for r in bad))
    researcher.post(f"/recipes/{recipe_id}/submit")
    for target, meaning in (("approved", "批准流程"), ("released", "发布流程")):
        fresh = qa.get(f"/recipes/{recipe_id}")
        qa.post(f"/recipes/{recipe_id}/transition", {
            "target_state": target, "signature_id": qa.sign(meaning, recipe_id, fresh["row_version"]),
        })
    ok("流程已发布", f"{recipe_id} {name}")
    return recipe_id


def approve_plan(researcher: Actor, qa: Actor, body: dict) -> str:
    plan = researcher.post("/plans", body)
    plan_id = plan["id"]
    researcher.post(f"/plans/{plan_id}/lock")
    researcher.post(f"/plans/{plan_id}/submit")
    fresh = qa.get(f"/plans/{plan_id}")
    qa.post(f"/plans/{plan_id}/decision", {
        "conclusion": "approved", "signature_id": qa.sign("批准实验方案", plan_id, fresh["row_version"]),
    })
    fresh = qa.get(f"/plans/{plan_id}")
    ok("方案已批准", f"{plan_id} {body['name']}，{fresh.get('condition_count', '?')} 个条件")
    return plan_id


def launch(researcher: Actor, operator: Actor, plan_id: str, note: str, tray: str = "") -> str:
    task = researcher.post("/experiment-tasks", {"plan_id": plan_id, "priority": 2})
    researcher.post(f"/experiment-tasks/{task['id']}/assign", {"assignee_user_id": operator.id})
    operator.post(f"/experiment-tasks/{task['id']}/accept")
    batch = operator.post("/batches", {"plan_id": plan_id, "task_id": task["id"], "note": note})
    batch_id = batch["id"]
    if tray:
        labware = next(row for row in operator.get(f"/labware?keyword={tray}") if row["barcode"] == tray)
        operator.post(f"/batches/{batch_id}/labware", {"labware_id": labware["id"], "role": ""})
        ok("托盘已绑定批次", f"{tray} @ {labware.get('location_id')}")
    operator.post(f"/batches/{batch_id}/schedule", {})
    preflight = operator.get(f"/batches/{batch_id}/preflight?manual_review=true")
    if not preflight["ok"]:
        raise Failed("开跑检查未通过：" + "；".join(row["detail"] for row in preflight["blocked"]))
    sop_check = next((row for row in preflight.get("checks") or [] if row.get("key") == "sop"), None)
    if sop_check:
        ok("开跑检查 · 受控 SOP", f"{sop_check.get('state_label') or sop_check.get('state')}：{sop_check.get('detail')}")
    fresh = operator.get(f"/batches/{batch_id}")
    operator.post(f"/batches/{batch_id}/dispatch", {
        "manual_review": True, "reason": note,
        "signature_id": operator.sign("批准执行", batch_id, fresh["row_version"]),
    })
    ok("已排程、开跑检查通过、签名下发", f"{batch_id}（任务 {task['id']}）")
    return batch_id


def drive(operator: Actor, qa: Actor, batch_id: str, manual_values: dict, timeout: float = 600) -> dict:
    """推到批次结束：人工节点按表单填，审核节点由 QA 批准；设备、等待、关卡由系统自己走。"""
    return drive_many(operator, qa, [batch_id], manual_values, timeout)[0]


def drive_many(operator: Actor, qa: Actor, batch_ids: list[str], manual_values: dict, timeout: float = 600) -> list[dict]:
    """几个批次一起推：哪个批次走到人工或审核节点就先处理哪个，全部结束才返回（按传入顺序）。"""
    handled: set[str] = set()
    finished: dict[str, dict] = {}

    def advance():
        for batch_id in batch_ids:
            if batch_id in finished:
                continue
            detail = operator.get(f"/batches/{batch_id}")
            if detail["state"] in {"fault", "aborted", "paused"}:
                raise Failed(f"批次 {batch_id} 进入 {detail['state']}：{detail.get('failure_reason')}")
            if detail["state"] == "done":
                finished[batch_id] = detail
                continue
            for run in detail["step_runs"]:
                if run["id"] in handled or run["state"] not in {"ready", "running"}:
                    continue
                label = f"{batch_id} " if len(batch_ids) > 1 else ""
                if run["kind"] == "manual":
                    values = {field["key"]: manual_values[field["key"]] for field in run["form"]}
                    body = {"form_data": values, "checks": {"samples": True, "materials": bool(detail["reservations"])},
                            "note": "案例导入", "row_version": run["row_version"]}
                    if run["requires_signature"]:
                        body["signature_id"] = operator.sign("人工步骤记录确认", run["id"], run["row_version"])
                    operator.post(f"/step-runs/{run['id']}/submit", body)
                    handled.add(run["id"])
                    ok(f"{label}人工节点「{run['step_name']}」已提交")
                elif run["kind"] == "review":
                    qa.post(f"/step-runs/{run['id']}/review", {
                        "conclusion": "approved", "row_version": run["row_version"],
                        "signature_id": qa.sign("流程审核通过", run["id"], run["row_version"]),
                    })
                    handled.add(run["id"])
                    ok(f"{label}审核节点「{run['step_name']}」QA 已批准")
        return len(finished) == len(batch_ids)

    wait_for(f"批次 {'、'.join(batch_ids)} 完成", advance, timeout=timeout)
    return [report_batch(finished[batch_id]) for batch_id in batch_ids]


def report_batch(detail: dict) -> dict:
    batch_id = detail["id"]
    for checkpoint in sorted(detail["checkpoints"], key=lambda c: c["step_index"]):
        payload = checkpoint["payload"]
        delivered = payload.get("delivered") or {}
        brief = (
            f"{len(delivered['wells'])} 孔实测注液量" if delivered.get("wells")
            else f"通道 {delivered.get('channel')} · {delivered.get('cycles_completed')} 圈 · "
                 f"{delivered.get('discharge_capacity_mAh')} mAh" if "discharge_capacity_mAh" in delivered
            else ""
        )
        ok(f"第 {checkpoint['step_index'] + 1} 步检查点", f"{payload['station_id']} · {payload['origin']} {brief}".strip())
    transfers = [c for c in detail["commands"] if c["type"] == "transfer"]
    if transfers:
        ok("转运指令", f"{len(transfers)} 条，" + "、".join(f"第 {c['step_index'] + 1} 步前由 {c['station_id']}（{c['state']}）"
                                                     for c in transfers))
    assisted = sorted({s for c in detail["commands"] for s in c.get("assist_station_ids") or []})
    if assisted:
        ok("协同工位", "、".join(assisted))
    ok("批次已完成", batch_id)
    return detail


def delivered_volumes(detail: dict, station: str) -> dict:
    """样本 → 设备回报的实际注液量。设备按实体孔位回报（绑定托盘时与布局孔位不同），对不上时按孔位顺序对应。"""
    wells: dict = {}
    for checkpoint in detail["checkpoints"]:
        payload = checkpoint["payload"]
        if payload["station_id"] == station and (payload.get("delivered") or {}).get("wells"):
            wells = payload["delivered"]["wells"]
    samples = sorted(detail["samples"], key=lambda s: s["position"])
    if all(sample["well"] in wells for sample in samples):
        return {sample["id"]: wells[sample["well"]].get("electrolyte") for sample in samples}
    ordered = sorted(wells, key=lambda w: (w[0], int(w[1:]) if w[1:].isdigit() else 0))
    return {sample["id"]: wells[well].get("electrolyte") for sample, well in zip(samples, ordered)}


def record_results(researcher: Actor, qa: Actor, detail: dict, metrics_for, invalid: str = "") -> int:
    """每个样本建检测任务、录入结果，QA 逐条复核；`invalid` 样本判质量无效（进报告的排除说明）。"""
    values = []
    samples = sorted(detail["samples"], key=lambda s: s["position"])
    for index, sample in enumerate(samples):
        rows = metrics_for(index, sample)
        analysis = researcher.post("/analysis-tasks", {
            "sample_id": sample["id"], "physical_sample_id": sample["physical_sample_id"],
            "method": "电性能测试", "method_version": "EC-02 v2",
            "required_metrics": [row["metric_version_id"] for row in rows],
        })
        entered = researcher.post(f"/analysis-tasks/{analysis['id']}/results", {
            "event_id": f"case-{detail['id']}-{sample['id']}", "metrics": rows,
        })
        values.extend((sample, row) for row in entered["results"])
    invalid_sample = samples[int(invalid)]["id"] if invalid != "" else ""
    for sample, value in values:
        bad = sample["id"] == invalid_sample
        qa.post(f"/result-values/{value['id']}/review", {
            "conclusion": "approved", "quality": "invalid" if bad else "valid",
            "reason": "封口处漏液，数据无效" if bad else "数据完整、曲线正常",
            "result_version": value["result_version"],
            "signature_id": qa.sign("数据复核通过", value["id"], value["result_version"]),
        })
    ok("检测录入并逐条复核", f"{len(values)} 条" + (f"，样本 {invalid_sample} 判质量无效" if invalid_sample else ""))
    return len(values)


def publish_report(researcher: Actor, qa: Actor, batch_id: str, conclusion: str, task_id: str = "") -> str:
    """发布报告。给了 task_id（已拆分的父任务）就出一份多批合并报告。"""
    body = {"task_id": task_id} if task_id else {"batch_id": batch_id}
    report = researcher.post("/reports", {**body, "conclusion": conclusion})
    researcher.post(f"/reports/{report['id']}/submit")
    fresh = qa.get(f"/reports/{report['id']}")
    approved = qa.post(f"/reports/{report['id']}/approve", {
        "conclusion": "approved", "signature_id": qa.sign("批准报告", report["id"], fresh["row_version"]),
    })
    published = qa.post(f"/reports/{report['id']}/publish", {
        "signature_id": qa.sign("发布报告", report["id"], approved["row_version"]),
    })
    ok("报告已发布", f"{report['id']} · {published['state']}")
    return report["id"]


# ---------------------------------------------------------------- 流程定义


def drying_steps() -> list[dict]:
    return [
        {"step_id": "s01", "kind": "device", "name": "极片真空干燥", "cap": "cap.vacuum_dry",
         "params": {"temp": 120, "vacuum": 1}, "dur": 60, "sop_step": 1},
        {"step_id": "s02", "kind": "device", "name": "称重选片", "cap": "cap.weigh",
         "params": {"mass": 0.0152}, "dur": 15, "hard": {"from": "真空干燥结束", "maxGapMin": 15}, "sop_step": 2},
    ]


def filling_step(step_id: str, assist: bool = False) -> dict:
    row = {"step_id": step_id, "kind": "device", "name": "注液封口", "cap": "cap.assemble",
           "consumes_materials": True, "params": {"electrolyte": 60}, "dur": 30,
           "hard": {"from": "称重结束", "maxGapMin": 20}, "sop_step": 4}
    if assist:
        row["assist"] = ["cap.robot_load"]
    return row


def formation_step(step_id: str) -> dict:
    return {"step_id": step_id, "kind": "device", "name": "化成（0.1C 首次充放电）", "cap": "cap.test",
            "params": {"rate": 0.1, "vmax": 4.3, "cycles": 1}, "dur": 45, "sop_step": 7}


def cycling_step(step_id: str, method_id: str = "") -> dict:
    if method_id:
        # 引用设备方法：参数与时长按方法缺省写全（与编辑器里选方法时自动带出的一样，复制这个流程也能直接用），
        # 倍率由方案因子按孔位覆盖；程序与输出规则随方法冻结进批次
        defaults = {key: rule["default"] for key, rule in CYCLING_METHOD["params"].items()}
        return {"step_id": step_id, "kind": "device", "name": "循环测试", "cap": "cap.test",
                "params": defaults, "dur": CYCLING_METHOD["dur_min"], "method": {"id": method_id}}
    return {"step_id": step_id, "kind": "device", "name": "循环测试", "cap": "cap.test",
            "params": {"rate": 0.5, "vmax": 4.3, "cycles": 50}, "dur": 120}


def rest_step(step_id: str, name: str) -> dict:
    # 演示把静置压到 6 秒；正式流程按工艺填（如 720 min），等待到期由执行器唤醒
    return {"step_id": step_id, "kind": "wait", "name": name, "dur": 0.1, "wait_for": {"mode": "duration"},
            "sop_step": 6}


def capacity_gate(step_id: str, source: str) -> dict:
    return {"step_id": step_id, "kind": "gate", "name": "放电容量质检", "cap": "", "params": {}, "dur": 0,
            "gate": {"source_step_id": source, "field": "discharge_capacity_mAh", "min": 3.0,
                     "scope": "batch", "on_fail": "rework", "rework_to": source, "max_rework": 1}}


def review_step(step_id: str, name: str) -> dict:
    return {"step_id": step_id, "kind": "review", "name": name, "review_role": "qa", "sop_step": 8}


# ---------------------------------------------------------------- 四个案例


def case_filling(researcher: Actor, qa: Actor, operator: Actor) -> dict:
    step("A 注液：流程 → 方案 → 批次 → ST-06 按孔位注液 → 质检 → 人工检查 → QA 复核 → 数据 → 报告")
    recipe_id = release_recipe(
        researcher, qa, "案例A 扣电注液封口",
        "极片干燥称重后在手套箱配液站按孔位注液封口；逐孔读设备回报的实际注液量做质检，人工检查封口与开路电压",
        [
            *drying_steps(),
            filling_step("s03"),
            {"step_id": "s04", "kind": "gate", "name": "逐孔注液量质检", "cap": "", "params": {}, "dur": 0,
             "gate": {"source_step_id": "s03", "field": "electrolyte", "min": 35, "max": 75,
                      "scope": "sample", "on_fail": "hold"}},
            {"step_id": "s05", "kind": "manual", "name": "封口外观与开路电压检查", "dur": 15, "sop_step": 5,
             "requires_signature": True, "requires_sample_check": True,
             "form": [
                 {"key": "no_leak", "label": "逐个目检封口无漏液", "type": "bool", "required": True},
                 {"key": "ocv_min", "label": "最低开路电压 V", "type": "number", "required": True},
                 {"key": "meter_id", "label": "万用表编号", "type": "text", "required": True},
             ]},
            review_step("s06", "QA 复核注液记录"),
        ],
        [{"material": "电解液 LP57", "qty": 0.45, "unit": "mL"}],
    )
    plan_id = approve_plan(researcher, qa, {
        "name": "案例A 注液量梯度", "recipe_id": recipe_id, "plan_type": "matrix",
        "goal": "4 个注液量 × 2 次重复，确认配液站按孔位执行设定注液量、偏差在 ±1% 内",
        "repeats": 2, "layout": "sequential", "seed": 1,
        "factors": [{"name": "注液量", "unit": "μL", "levels": [40, 50, 60, 70],
                     "target": {"step_id": "s03", "param": "electrolyte"}, "material": ELECTROLYTE}],
        "required_metrics": [METRIC_IDS["electrolyte_volume"], METRIC_IDS["ocv"], METRIC_IDS["appearance"]],
    })
    batch_id = launch(researcher, operator, plan_id, "案例A 注液")
    detail = drive(operator, qa, batch_id, {"no_leak": True, "ocv_min": 2.98, "meter_id": "DMM-03"})
    volumes = delivered_volumes(detail, "ST-06")

    def metrics(index, sample):
        actual = volumes.get(sample["id"])
        level = float((sample.get("levels") or [60])[0])
        return [
            {"metric_version_id": METRIC_IDS["electrolyte_volume"], "value": round(actual or level, 2), "unit": "μL"},
            {"metric_version_id": METRIC_IDS["ocv"], "value": round(3.02 + 0.005 * index, 3), "unit": "V"},
            {"metric_version_id": METRIC_IDS["appearance"], "value": "合格"},
        ]

    record_results(researcher, qa, detail, metrics)
    report_id = publish_report(researcher, qa, batch_id,
                               "8 个样本实际注液量与设定值偏差均在 ±0.5% 内，封口无漏液，开路电压 3.0 V 左右，注液工序可用。")
    return {"流程": recipe_id, "方案": plan_id, "批次": batch_id, "报告": report_id}


def case_cycling(researcher: Actor, qa: Actor, operator: Actor, method_id: str) -> dict:
    step("B 循环测试：上柜检查 → 化成 → 静置 → 循环测试（ST-07，按设备方法）→ 放电容量质检 → QA 复核 → 数据 → 报告")
    recipe_id = release_recipe(
        researcher, qa, "案例B 扣电循环测试",
        "已组装扣电上柜：化成一圈、静置后按倍率循环 50 圈；放电容量低于下限返工重测一次，仍不合格转 QA",
        [
            {"step_id": "s01", "kind": "manual", "name": "扣电上柜与夹具检查", "dur": 10,
             "requires_signature": False, "requires_sample_check": True,
             "form": [
                 {"key": "channel_checked", "label": "已按孔位核对通道与夹具极性", "type": "bool", "required": True},
                 {"key": "ocv_min", "label": "上柜前最低开路电压 V", "type": "number", "required": True},
             ]},
            formation_step("s02"),
            rest_step("s03", "化成后静置"),
            cycling_step("s04", method_id),
            capacity_gate("s05", "s04"),
            review_step("s06", "QA 复核循环数据"),
        ],
        [],
    )
    plan_id = approve_plan(researcher, qa, {
        "name": "案例B 循环倍率对比", "recipe_id": recipe_id, "plan_type": "matrix",
        "goal": "0.5C 与 1C 各 4 个扣电循环 50 圈，比较容量保持率",
        "repeats": 4, "layout": "sequential", "seed": 1,
        "factors": [{"name": "循环倍率", "unit": "C", "levels": [0.5, 1.0],
                     "target": {"step_id": "s04", "param": "rate"}}],
        "required_metrics": [METRIC_IDS["discharge_capacity"], METRIC_IDS["retention"]],
    })
    batch_id = launch(researcher, operator, plan_id, "案例B 循环测试")
    detail = drive(operator, qa, batch_id, {"channel_checked": True, "ocv_min": 3.01})

    def metrics(index, sample):
        rate = float((sample.get("levels") or [0.5])[0])
        return [
            {"metric_version_id": METRIC_IDS["discharge_capacity"],
             "value": round(203.5 - (4.0 if rate >= 1 else 0) + 0.3 * index, 1), "unit": "mAh/g"},
            {"metric_version_id": METRIC_IDS["retention"],
             "value": round((93.5 if rate >= 1 else 97.2) - 0.1 * index, 1), "unit": "%"},
        ]

    record_results(researcher, qa, detail, metrics, invalid="7")
    report_id = publish_report(researcher, qa, batch_id,
                               "50 圈后 0.5C 容量保持率约 97%，1C 约 93%；1 个样本因封口漏液判无效，已在排除说明中列出。")
    return {"流程": recipe_id, "方案": plan_id, "批次": batch_id, "报告": report_id}


def case_serial(researcher: Actor, qa: Actor, operator: Actor, method_id: str) -> dict:
    step("C 串行：托盘绑定批次，AGV 板库 → ST-05 → ST-06（机械臂协同）→ ST-07，注液接循环测试")
    recipe_id = release_recipe(
        researcher, qa, "案例C 注液—循环测试串行",
        "托盘装 8 个样本：干燥称重 → 注液封口（机械臂协同上下料）→ 静置浸润 → 化成 → 循环测试；"
        "工位之间由 AGV 按载具位置自动转运",
        [
            *drying_steps(),
            filling_step("s03", assist=True),
            rest_step("s04", "注液后静置浸润"),
            formation_step("s05"),
            cycling_step("s06", method_id),
            capacity_gate("s07", "s06"),
            review_step("s08", "QA 复核注液与循环记录"),
        ],
        [{"material": "电解液 LP57", "qty": 0.45, "unit": "mL"}],
    )
    plan_id = approve_plan(researcher, qa, {
        "name": "案例C 注液量 × 循环倍率", "recipe_id": recipe_id, "plan_type": "matrix",
        "goal": "注液量 50 / 60 μL × 循环倍率 0.5 / 1C，各 2 个扣电，注液后直接接循环测试",
        "repeats": 2, "layout": "sequential", "seed": 1,
        "factors": [
            {"name": "注液量", "unit": "μL", "levels": [50, 60],
             "target": {"step_id": "s03", "param": "electrolyte"}, "material": ELECTROLYTE},
            {"name": "循环倍率", "unit": "C", "levels": [0.5, 1.0],
             "target": {"step_id": "s06", "param": "rate"}},
        ],
        "required_metrics": [METRIC_IDS["electrolyte_volume"], METRIC_IDS["discharge_capacity"], METRIC_IDS["retention"]],
    })
    batch_id = launch(researcher, operator, plan_id, "案例C 串行", tray="TRAY-C01")
    detail = drive(operator, qa, batch_id, {}, timeout=900)
    volumes = delivered_volumes(detail, "ST-06")

    def metrics(index, sample):
        volume, rate = (float(v) for v in (sample.get("levels") or [60, 0.5])[:2])
        actual = volumes.get(sample["id"])
        return [
            {"metric_version_id": METRIC_IDS["electrolyte_volume"], "value": round(actual or volume, 2), "unit": "μL"},
            {"metric_version_id": METRIC_IDS["discharge_capacity"],
             "value": round(201.0 + (2.5 if volume >= 60 else 0) - (4.0 if rate >= 1 else 0), 1), "unit": "mAh/g"},
            {"metric_version_id": METRIC_IDS["retention"],
             "value": round(96.8 + (0.8 if volume >= 60 else 0) - (3.5 if rate >= 1 else 0), 1), "unit": "%"},
        ]

    record_results(researcher, qa, detail, metrics)
    report_id = publish_report(researcher, qa, batch_id,
                               "注液到循环测试在一个批次内串行完成，托盘由 AGV 自动转运 3 次；60 μL 注液与 0.5C 循环组合保持率最高。")
    tray = next(row for row in operator.get("/labware?keyword=TRAY-C01") if row["barcode"] == "TRAY-C01")
    operator.post(f"/labware/{tray['id']}/move", {"barcode": "TRAY-C01", "to_location_id": "HOTEL-01/S01",
                                                  "reason": "循环测试结束，下柜放回板库"})
    ok("托盘下柜", "TRAY-C01 扫码放回 HOTEL-01/S01")
    return {"流程": recipe_id, "方案": plan_id, "批次": batch_id, "报告": report_id}


def case_split(researcher: Actor, qa: Actor, operator: Actor, recipe_id: str) -> dict:
    step("D 分批：20 个扣电、流程每批 8 位 → 建任务时拆成 3 个子任务 → 3 个批次一起排程 → 合并统计与一份报告")
    plan_id = approve_plan(researcher, qa, {
        "name": "案例D 20 个扣电循环测试", "recipe_id": recipe_id, "plan_type": "single_condition",
        "goal": "同一配方 20 个扣电 0.5C 循环 50 圈；流程每批 8 个，分 3 批执行，合并统计容量保持率并检查批次差异",
        "sample_count": 20,
        "required_metrics": [METRIC_IDS["discharge_capacity"], METRIC_IDS["retention"]],
    })
    capacity = next(row for row in researcher.get(f"/plans/{plan_id}")["checks"] if row["key"] == "capacity")
    ok("方案校验 · 容量", capacity["detail"])
    preview = researcher.post("/experiment-tasks/split-preview", {"plan_id": plan_id})
    ok("拆分预览", preview["detail"] + "：" + "、".join(row["label"] for row in preview["parts"]))
    parent = researcher.post("/experiment-tasks", {
        "plan_id": plan_id, "priority": 2, "title": "案例D 20 个扣电循环测试", "split": {"mode": "parallel"},
    })
    ok("建任务时自动拆分", f"{parent['id']} → " + "、".join(
        f"{row['id']}（{row['portion_label']}）" for row in parent["children"]))
    # 分配、接单父任务就是分配、接单整件事：3 个子任务一并分配给操作员、一并接单
    researcher.post(f"/experiment-tasks/{parent['id']}/assign", {"assignee_user_id": operator.id})
    operator.post(f"/experiment-tasks/{parent['id']}/accept")
    made = operator.post(f"/experiment-tasks/{parent['id']}/batches", {"note": "案例D 分批"})
    batch_ids = [row["id"] for row in made["batches"]]
    ok("为 3 个子任务建批次", "、".join(f"{row['id']}（{row['sample_count']} 个样本）" for row in made["batches"]))
    # 一起排程：多批次优化给出顺序，写入时间线（ST-07 按批计 8 个通道，三批可以同时上柜）
    optimized = operator.post("/schedule/optimize", {"batch_ids": batch_ids})
    operator.post("/schedule/optimize/apply", {"order": optimized["best"]["order"], "start_from": optimized["start_from"]})
    ok("多批次优化并写入时间线", f"顺序 {'→'.join(optimized['best']['order'])}，跨度 {optimized['best']['span_min']} min")
    for batch_id in batch_ids:
        preflight = operator.get(f"/batches/{batch_id}/preflight?manual_review=true")
        if not preflight["ok"]:
            raise Failed(f"{batch_id} 开跑检查未通过：" + "；".join(row["detail"] for row in preflight["blocked"]))
        fresh = operator.get(f"/batches/{batch_id}")
        operator.post(f"/batches/{batch_id}/dispatch", {
            "manual_review": True, "reason": "案例D 分批",
            "signature_id": operator.sign("批准执行", batch_id, fresh["row_version"]),
        })
    ok("三批开跑检查通过、逐批签名下发", "、".join(batch_ids))
    details = drive_many(operator, qa, batch_ids, {"channel_checked": True, "ocv_min": 3.01}, timeout=900)

    def metrics(index, sample):
        # 同一配方：数值只有小的随机波动（按全局样本序号取），三批之间没有系统差异
        number = int(sample.get("repeat") or index + 1)
        return [
            {"metric_version_id": METRIC_IDS["discharge_capacity"],
             "value": round(203.2 + 0.1 * ((number * 7) % 5), 1), "unit": "mAh/g"},
            {"metric_version_id": METRIC_IDS["retention"],
             "value": round(96.9 + 0.1 * ((number * 3) % 4), 1), "unit": "%"},
        ]

    for number, detail in enumerate(details):
        # 第 2 批的第 6 个样本（全局第 13 号）判无效：合并报告的排除说明里列出
        record_results(researcher, qa, detail, metrics, invalid="5" if number == 1 else "")
    view = researcher.get(f"/experiment-tasks/{parent['id']}/results")
    for block in view["metrics"]:
        effect = block.get("batch_effect") or {}
        ok(f"合并统计 · {block['metric_name']}",
           f"纳入 {block['summary']['included']}、排除 {block['summary']['excluded']}；"
           f"按批 {'/'.join(str(row['n_included']) for row in block.get('by_batch') or [])}；{effect.get('note', '')}")
    report_id = publish_report(
        researcher, qa, "", "20 个扣电分 3 批（7/7/6）完成 0.5C 循环 50 圈，容量保持率约 97%；三批之间没有显著差异，"
        "1 个样本因封口漏液判无效，已在排除说明中列出。", task_id=parent["id"],
    )
    progress = researcher.get(f"/experiment-tasks/{parent['id']}")
    ok("父任务", f"{progress['state_label']}；计划 {progress['progress']['target']}，有效 {progress['progress']['valid']}")
    return {"方案": plan_id, "任务": parent["id"], "批次": "、".join(batch_ids), "报告": report_id}


def main() -> int:
    researcher, qa, operator, admin = Actor("researcher"), Actor("qa"), Actor("operator"), Actor("admin")
    engineer = Actor("engineer")
    gate = operator.get("/gate")
    blocked = {k: v for k, v in (gate.get("blocked_stations") or {}).items() if k in {"ST-05", "ST-06", "ST-07"}}
    if not gate["open"] or blocked:
        raise Failed(f"执行门未就绪：{gate['reasons']} {blocked}（外部模拟设备在线了吗？）")
    if researcher.get("/recipes"):
        print("  注意：库里已有流程，本次会再建一套案例（同名不同编号）")
    prepare(admin, operator)
    method_id = release_cycling_method(engineer, qa)
    if "--only=D" in sys.argv[1:]:
        # 只补导入案例 D：库里已有三个参考案例，沿用案例 B 已发布的流程
        recipe = next((row for row in researcher.get("/recipes")
                       if row["name"] == "案例B 扣电循环测试" and row["state"] == "released"), None)
        if recipe is None:
            raise Failed("没找到已发布的「案例B 扣电循环测试」：先导入三个参考案例")
        ids = case_split(researcher, qa, operator, recipe["id"])
        print("\n完成：\n  案例D 分批：" + " · ".join(f"{key} {value}" for key, value in ids.items()))
        return 0
    summary = {
        "案例A 注液": case_filling(researcher, qa, operator),
        "案例B 循环测试": case_cycling(researcher, qa, operator, method_id),
        "案例C 串行": case_serial(researcher, qa, operator, method_id),
    }
    summary["案例D 分批"] = case_split(researcher, qa, operator, summary["案例B 循环测试"]["流程"])
    # 参考案例应当干净地跑完：案例批次上不该留下报警（消耗被拒、偏差、数据越界都说明案例数据有问题）
    batches = {batch_id for ids in summary.values() for batch_id in ids["批次"].split("、")}
    raised = [row for row in operator.get("/alarms") if row.get("source_id") in batches]
    for alarm in raised:
        print(f"  ! 报警 {alarm['id']} {alarm['source_id']}：{alarm['message']}")
    print("\n完成：")
    for name, ids in summary.items():
        print(f"  {name}：" + " · ".join(f"{key} {value}" for key, value in ids.items()))
    if raised:
        raise Failed(f"案例批次上有 {len(raised)} 条报警，见上")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
