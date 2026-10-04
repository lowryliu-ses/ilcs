#!/usr/bin/env python3
"""登记一台 Neware 充放电柜（模拟网关），跑一个单独的实验任务：扣电上柜 → 恒流恒压循环 → QA 复核。

只走 HTTP，和界面调同一组接口；签名用演示账号口令逐次签署（`POST /signatures`）。设备侧是设备模块
ilcs-devices/gateway/neware-bts 的模拟网关（假 BTS，8 通道，`deploy/compose.yml` 起的 neware-sim），ILCS 经
`http_json_v1` 走 HTTPS + 令牌接它，和接真机是同一条路，只是网关自报为模拟器：

    docker compose -f ../ilcs-devices/gateway/neware-bts/deploy/compose.yml up -d --build   # 先起模拟网关
    python3 scripts/load-neware-cycler.py register [--base http://127.0.0.1:8090] [--acceptance]
    python3 scripts/load-neware-cycler.py run [--cells 4] [--first-channel 1]
    python3 scripts/load-neware-cycler.py resume --batch B-xxxxxx-xxx       # 中途失败后接着跑已建好的批次

- register：能力 cap.test（参数 channel，单位「号」）、实验区、资产 AS-NW-01、工位 ST-NW-01（8 通道，按样本计）；
  接入模板 TPL-NEWARE-BTS（工程师导入 profile.json、QA 发布），工位套用模板连到 neware-sim（签名），重连，
  等执行器跑完只读级验收；`--acceptance` 再申请一次动作级 + 故障项目验收（签名 + 批准说明）并等它出结论。
  另有检测指标、设备方法（工程师起草、QA 发布）、操作员资质。已有的先查后用，重复运行不会多建。
- run：先照 register 补齐，再 流程（研究员起草、QA 批准发布；同名已发布的沿用）→ 方案（N 颗扣电同一工步，
  QA 批准）→ 实验任务（研究员建、分配给操作员、操作员接受）→ 批次 → 排程 → 开跑检查 → 签名下发 →
  上柜人工节点按样本记通道 → 执行器把通道前馈给循环测试，网关在 BTS 上逐颗启动 → QA 审核节点 →
  设备回报的结果 QA 逐条复核 → 报告发布。要求执行器在跑。

模拟网关的数是假 BTS 给的：结果带「模拟」标记、仪器注明模拟设备，不进闭环训练数据。
正式环境（ILCS_ENVIRONMENT=production）拒绝运行：它会登记占位资产与模拟设备的连接。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
# 设备仓库：环境变量 ILCS_DEVICES，缺省是 ILCS 旁边的 ../ilcs-devices
DEVICES_REPO = Path(os.environ.get("ILCS_DEVICES") or ROOT.parent / "ilcs-devices")
PROFILE = DEVICES_REPO / "gateway" / "neware-bts" / "profile.json"
PASSWORD = "ilcs1234"
RUN = uuid.uuid4().hex[:6]

STATION = "ST-NW-01"
ASSET_NO = "AS-NW-01"
ISLAND = {"id": 10, "name": "电池测试区"}
MODEL = "CT-4008T-5V6A"
DEVICE_ID = "SIM-NW-BTS-01"
CHANNELS = 8
OPERATOR = "P-003"

CAPABILITY = {
    "id": "cap.test", "name": "电池充放电测试", "params": {"channel": "通道"},
    "param_specs": {"channel": {"type": "integer", "unit": "号"}},
    # BTS 接口不做保持；重跑等于对同一颗电芯再充放一遍，不自动重试
    "recovery": {"maxHoldMin": 0, "pausable": False, "retryable": False, "hold": "BTS 接口不做保持：要停只能终止",
                 "sideEffect": "重跑会对同一颗电芯再充放一遍，改变它的状态", "verify": ["通道状态", "电芯电压"]},
}
METRICS = [
    {"code": "nw_capacity", "name": "BTS 回报容量", "unit": "Ah", "rules": {"min": 0, "max": 1}},
    {"code": "nw_cycles", "name": "循环圈数", "unit": "圈", "rules": {"min": 0, "max": 5000}},
]
METHOD = {
    "name": "Neware 恒流恒压循环（CC-CV）", "capability_id": "cap.test", "program": "CC-CV", "params": {},
    "outputs": [
        {"key": "capacity", "label": "BTS 回报容量", "unit": "Ah", "lo": 0.001, "required": True, "metric": "nw_capacity"},
        {"key": "cycle", "label": "循环圈数", "unit": "圈", "lo": 1, "required": True, "metric": "nw_cycles"},
        {"key": "energy", "label": "BTS 回报能量", "unit": "Wh", "required": False},
    ],
    "dur_min": 30,
    "note": "工步在 BTS 工步文件 CC-CV 里定；指令只带通道（上柜时按样本记、前馈）。容量是 BTS inquire 报的原值，单位以 BTS 设置为准",
}
RECIPE_NAME = "Neware 扣电恒流恒压循环"
# 开跑检查要求流程有风险评估：模拟阶段写明占位，接真机前按实物与现场做
RISK = "RA-NW-01 v1（模拟阶段占位：模拟网关、无真实电芯；接真机前按实物补做）"
RECIPE_DESIGN = "扣电上柜并按样本记下所在通道；Neware 柜按工步文件 CC-CV 逐颗充放电，回报 BTS 容量与循环圈数；QA 复核"

Transport = Callable[..., tuple[int, Any]]


class Failed(Exception):
    pass


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


def ok(label: str, detail: str = "") -> None:
    print(f"  ✓ {label}{('：' + detail) if detail else ''}", flush=True)


def note(label: str) -> None:
    print(f"  ! {label}", flush=True)


def _items(payload: Any) -> list:
    return payload.get("items", []) if isinstance(payload, dict) else payload


def http_transport(base: str) -> Transport:
    root = base.rstrip("/") + "/api"

    def call(method: str, path: str, body: Any = None, headers: dict | None = None):
        headers = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(root + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else None
        except urllib.error.HTTPError as error:
            payload = error.read()
            try:
                return error.code, json.loads(payload)
            except ValueError:
                return error.code, payload.decode(errors="replace")

    return call


class Actor:
    """一个演示账号：登录拿令牌，签名时再验一次口令。写请求一律带幂等键（和前端一样）。"""

    def __init__(self, username: str, transport: Transport, password: str = PASSWORD) -> None:
        self.username = username
        self.transport = transport
        self.password = password
        self.token = ""
        body = self.call("POST", "/auth/login", {"username": username, "password": password})
        self.token = body["access_token"]
        self.id = body["user"]["id"]

    def call(self, method: str, path: str, body: Any = None, expect: tuple[int, ...] = (200, 201)):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if method != "GET":
            headers["Idempotency-Key"] = f"nw-{RUN}-{uuid.uuid4().hex[:10]}"
        status, content = self.transport(method, path, body, headers)
        if status not in expect:
            raise Failed(f"{self.username} {method} {path} → {status}：{content}")
        return content

    def get(self, path: str):
        return self.call("GET", path)

    def post(self, path: str, body: dict | None = None):
        return self.call("POST", path, body or {})

    def patch(self, path: str, body: dict):
        return self.call("PATCH", path, body)

    def put(self, path: str, body: dict):
        return self.call("PUT", path, body)

    def sign(self, meaning: str, target: str = "", version: int = 0) -> str:
        return self.post("/signatures", {"password": self.password, "meaning": meaning, "target": target,
                                         "object_version": version})["signature_id"]


def actors(transport: Transport) -> dict[str, Actor]:
    return {name: Actor(name, transport) for name in ("engineer", "qa", "operator", "researcher", "admin")}


def wait_for(describe: str, probe: Callable[[], Any], timeout: float, every: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = probe()
        if found:
            return found
        time.sleep(every)
    raise Failed(f"{describe}：{timeout:.0f} 秒内没有达成")


def refuse_production() -> None:
    if os.environ.get("ILCS_ENVIRONMENT", "").strip().lower() == "production":
        raise Failed("正式环境拒绝运行：本脚本登记占位资产与模拟设备的连接")


# ---------------------------------------------------------------- register


def register(team: dict[str, Actor], args: argparse.Namespace) -> dict:
    engineer, qa, operator, researcher, admin = (team[k] for k in ("engineer", "qa", "operator", "researcher", "admin"))
    step(f"登记 Neware 充放电柜 {STATION}（模拟网关 {args.device_id}）")
    register_capability(engineer)
    register_station(engineer)
    template = register_template(engineer, qa)
    connect(engineer, operator, template, args)
    if args.acceptance:
        physical_acceptance(engineer)
    metrics = register_metrics(researcher)
    method = register_method(engineer, qa, metrics)
    grant_qualification(admin)
    return {"metrics": metrics, "method": method}


def register_capability(engineer: Actor) -> None:
    existing = {row["id"]: row for row in engineer.get("/capabilities")}
    current = existing.get(CAPABILITY["id"])
    if current is None:
        engineer.post("/capabilities", {**CAPABILITY, "signature_id": engineer.sign("能力模型变更批准", CAPABILITY["id"])})
        ok("能力", f"{CAPABILITY['id']} {CAPABILITY['name']}（新登记，参数 channel 通道 / 号）")
        return
    if "channel" not in (current.get("params") or {}):
        raise Failed(f"能力 {CAPABILITY['id']} 已存在但没有参数 channel：先在能力字典里加上（整数，单位「号」）")
    ok("能力", f"{CAPABILITY['id']} {current['name']}（沿用）")


def _asset(engineer: Actor) -> str:
    found = [row for row in _items(engineer.get(f"/assets?keyword={ASSET_NO}&page_size=100")) if row["asset_no"] == ASSET_NO]
    if found:
        return found[0]["id"]
    return engineer.post("/assets", {
        "asset_no": ASSET_NO, "name": "Neware 充放电柜 #1", "model": MODEL, "vendor": "Neware", "capacity": CHANNELS,
        "calibration_applicable": False,
        "calibration_exempt_reason": "模拟阶段占位：接的是模拟网关（假 BTS），接真机前按实物登记序列号与校准",
        "note": "模拟阶段占位资产：ilcs-devices/gateway/neware-bts 的模拟网关",
    })["id"]


def register_station(engineer: Actor) -> None:
    islands = {row["id"]: row["name"] for row in engineer.get("/islands")}
    stations = {row["id"]: row for row in engineer.get("/stations")}
    if STATION not in stations:
        # 先按内置模拟登记，再套用接入模板（和界面「工位与接入 → 设备连接」同一条路）
        engineer.post("/stations", {
            "id": STATION, "name": "Neware 充放电柜 #1", "island": ISLAND["id"], "channels": CHANNELS,
            "channel_unit": "sample", "limits": {"cap.test": {"channel": [1, CHANNELS]}}, "asset_id": _asset(engineer),
            "protocol": "sim", "adapter_kind": "simulation", "adapter_driver": "simulation",
            "signature_id": engineer.sign("工程变更批准", STATION),
        })
        ok("工位", f"{STATION}（{CHANNELS} 通道，按样本计：一颗电芯占一个通道；通道 1–{CHANNELS}）")
    else:
        ok("工位", f"{STATION}（沿用）")
    if islands.get(ISLAND["id"]) != ISLAND["name"]:
        engineer.put(f"/islands/{ISLAND['id']}", {"name": ISLAND["name"]})
    ok("实验区", f"#{ISLAND['id']} {ISLAND['name']}")


def register_template(engineer: Actor, qa: Actor) -> dict:
    """工程师导入 profile.json 成草稿，QA 发布（起草人不能发布本人起草的模板）。同编号同修订已发布的沿用。"""
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    rows = [row for row in _items(engineer.get("/device-templates"))
            if row.get("code") == profile["code"] and int(row.get("revision") or 0) == int(profile["revision"])]
    template = next((row for row in rows if row.get("state") == "released"), None) or next(iter(rows), None)
    if template is not None and template.get("digest") != profile["digest"]:
        raise Failed(f"接入模板 {profile['code']} 修订 {profile['revision']} 已登记，但和 {PROFILE.name} 的摘要不同："
                     "改了 profile.json 要升修订号再导入")
    if template is None:
        template = engineer.post("/device-templates/import", {"filename": PROFILE.name, "document": profile})
        if not (template.get("check") or {}).get("ok", True):
            raise Failed(f"接入模板检查没通过：{template['check']}")
        note(f"接入模板 {profile['code']} 已导入（草稿）")
    if template["state"] == "draft":
        template = qa.post(f"/device-templates/{template['id']}/release", {
            "row_version": template["row_version"],
            "signature_id": qa.sign("发布设备接入模板", template["id"], template["row_version"]),
        })
    if template["state"] != "released":
        raise Failed(f"接入模板 {profile['code']} 状态 {template['state']}，没能发布")
    ok("接入模板", f"{profile['code']} 修订 {profile['revision']} {profile['name']}（已发布）")
    return template


def via_driver_host(adapter: dict) -> bool:
    """工位已经改经驱动宿主接（scripts/load-driver-host-devices.py：sila2_v1 连 driver-host）：沿用，不改回直连网关。"""
    return adapter.get("driver") == "sila2_v1" and (adapter.get("config") or {}).get("host") == "driver-host"


def _gate(actor: Actor) -> dict:
    return actor.get(f"/stations/{STATION}/adapter/acceptance")


def connect(engineer: Actor, operator: Actor, template: dict, args: argparse.Namespace) -> None:
    """工位套用接入模板，连接参数指向模拟网关；重连一次，等执行器跑完只读级验收（模拟网关只读级就放行）。"""
    connection = {"base_url": args.gateway_url, "ca_file": f"/run/secrets/ilcs/gateway/{args.device_id}.crt",
                  "expected_device_id": args.device_id}
    credential = f"file:///run/secrets/ilcs/gateway/{args.device_id}.token"
    adapter = engineer.get(f"/stations/{STATION}/adapter")
    if via_driver_host(adapter):
        ok("设备连接", f"经驱动宿主接 driver-host:{adapter['config'].get('port')}（沿用；见 scripts/load-driver-host-devices.py）")
    elif (adapter.get("template") or {}).get("id") != template["id"] or adapter.get("template_connection") != connection \
            or adapter.get("credential_ref") != credential:
        adapter = engineer.patch(f"/stations/{STATION}/adapter", {
            "template_id": template["id"], "template_connection": connection, "credential_ref": credential,
            "row_version": adapter["row_version"],
            "signature_id": engineer.sign("设备集成配置变更批准", STATION, adapter["row_version"]),
        })
        ok("设备连接", f"套用 {template['code']}：{connection['base_url']}，设备编号 {args.device_id}（已签名保存）")
    else:
        ok("设备连接", f"{connection['base_url']}（沿用）")
    if (operator.get("/gate").get("blocked_stations") or {}).get(STATION):
        operator.post(f"/stations/{STATION}/adapter/reconnect")

    def accepted():
        listed = _gate(engineer)
        gate, runs = listed["gate"], listed.get("runs") or []
        if gate["required"] == "" and gate.get("accepted_config_version") == adapter["config_version"]:
            return next((row for row in runs if row["id"] == gate.get("accepted_run_id")), {"id": gate.get("accepted_run_id")})
        latest = next((row for row in runs if row.get("config_version") == adapter["config_version"]), None)
        if latest and latest.get("state") == "done" and not latest.get("ok") and latest.get("level") == "readonly":
            raise Failed(f"只读级验收没通过：{latest.get('error') or latest.get('report_md', '')[:500]}")
        return None

    run = wait_for(f"{STATION} 接入验收放行", accepted, timeout=args.acceptance_timeout)
    level = {"readonly": "只读级", "physical": "动作级"}.get(run.get("level"), run.get("level") or "")
    ok("接入验收", f"配置 v{adapter['config_version']} 已由 {run['id']}（{level}）放行"
       + ("；设备自报为模拟器，只读级即可放行" if run.get("simulator") else ""))
    wait_for(f"{STATION} 适配器在线", lambda: not (operator.get("/gate").get("blocked_stations") or {}).get(STATION),
             timeout=60)


def physical_acceptance(engineer: Actor) -> None:
    """动作级 + 故障项目：设备真的动作（模拟网关上是假 BTS 的通道 1），要写批准说明并签名（DEC-02）。"""
    adapter = engineer.get(f"/stations/{STATION}/adapter")
    requested = engineer.post(f"/stations/{STATION}/adapter/acceptance", {
        "level": "physical", "faults": True, "approval": "模拟网关联调（假 BTS），无真实电芯",
        "signature_id": engineer.sign("批准设备接入验收", STATION, adapter["config_version"]),
    })
    run = wait_for("动作级验收出结论", lambda: (lambda row: row if row["state"] in {"done", "failed"} else None)(
        engineer.get(f"/acceptance-runs/{requested['id']}")), timeout=600, every=5)
    states = {check["key"]: check["state"] for check in run.get("checks") or []}
    if not run.get("ok"):
        raise Failed(f"动作级验收没通过：{states}\n{run.get('report_md', '')[:1500]}")
    passed = sorted(key for key, state in states.items() if state == "pass")
    skipped = sorted(key for key, state in states.items() if state == "skip")
    ok("接入验收（动作级 + 故障项目）", f"{run['id']} 通过：{len(passed)} 项通过"
       + (f"，跳过 {'、'.join(skipped)}" if skipped else ""))


def register_metrics(researcher: Actor) -> dict[str, str]:
    existing = {(row["code"], row.get("version")): row for row in researcher.get("/metrics")}
    ids = {}
    for metric in METRICS:
        row = existing.get((metric["code"], "v1")) or researcher.post("/metrics", {
            "code": metric["code"], "name": metric["name"], "unit": metric["unit"], "value_type": "number",
            "sample_types": ["扣电"], "rules": metric["rules"],
        })
        ids[metric["code"]] = row["id"]
    ok("检测指标", "、".join(f"{m['name']} {m['unit']}" for m in METRICS))
    return ids


def register_method(engineer: Actor, qa: Actor, metrics: dict[str, str]) -> str:
    """工程师起草、QA 发布；适用型号写这台柜子的型号（排程按它和设备自报的程序把步骤落到工位上）。"""
    outputs = []
    for rule in METHOD["outputs"]:
        row = {key: value for key, value in rule.items() if key != "metric"}
        if rule.get("metric"):
            row["metric_id"] = metrics[rule["metric"]]
        outputs.append(row)
    cap = METHOD["capability_id"]
    released = [row for row in engineer.get(f"/device-methods?state=released&capability_id={cap}")
                if row["name"] == METHOD["name"]]
    if released:
        ok("设备方法", f"{released[0]['code']} v{released[0]['version']} {METHOD['name']}（已发布，沿用）")
        return released[0]["id"]
    drafts = [row for row in engineer.get(f"/device-methods?state=draft&capability_id={cap}") if row["name"] == METHOD["name"]]
    draft = drafts[0] if drafts else engineer.post("/device-methods", {
        **{key: value for key, value in METHOD.items() if key != "outputs"}, "outputs": outputs,
        "instrument_models": [MODEL],
    })
    done = qa.post(f"/device-methods/{draft['id']}/release", {"row_version": draft["row_version"]})
    ok("设备方法已发布", f"{done['code']} v{done['version']} {done['name']}：程序 {done['program']}，"
                        "回报 BTS 容量与循环圈数（关联指标，写成检测结果）")
    return done["id"]


def grant_qualification(admin: Actor) -> None:
    person = next((row for row in _items(admin.get(f"/people?keyword={OPERATOR}")) if row.get("code") == OPERATOR), None)
    if person is None:
        raise Failed(f"人员 {OPERATOR} 不存在")
    held = {row["scope_ref"] for row in admin.get(f"/people/{person['id']}/qualifications")
            if row.get("scope_kind") == "capability" and row.get("status") not in {"revoked", "expired"}}
    if CAPABILITY["id"] not in held:
        admin.post(f"/people/{person['id']}/qualifications", {
            "scope_kind": "capability", "scope_ref": CAPABILITY["id"], "label": CAPABILITY["name"],
        })
    ok("操作员资质", f"{OPERATOR} 有 {CAPABILITY['id']} {CAPABILITY['name']}")


# ---------------------------------------------------------------- run


def recipe_steps(method_id: str) -> list[dict]:
    return [
        {"step_id": "s01", "kind": "manual", "name": "扣电上柜", "dur": 10, "requires_signature": False,
         "requires_sample_check": True,
         "form": [
             {"key": "channel", "label": f"所在通道（1–{CHANNELS}）", "type": "number", "required": True, "per_sample": True},
             {"key": "polarity_ok", "label": "已核对极性与夹具", "type": "bool", "required": True},
         ]},
        # 通道不写固定值：取上柜时按样本记的通道（逐样本前馈），一颗电芯一个孔位，网关按孔位逐个启动
        {"step_id": "s02", "kind": "device", "name": "恒流恒压循环", "cap": "cap.test", "params": {},
         "dur": METHOD["dur_min"], "method": {"id": method_id},
         "bindings": {"channel": {"source_step_id": "s01", "field": "channel", "scope": "sample", "unit": "号",
                                  "expect": [1, CHANNELS]}}},
        {"step_id": "s03", "kind": "review", "name": "QA 复核循环数据", "review_role": "qa"},
    ]


def release_recipe(researcher: Actor, qa: Actor, method_id: str) -> str:
    """同名已发布流程的步骤、风险评估都和这里一致就沿用；不一致就出修订版（发布时取代原版）；没有就新建。
    研究员起草、提交，QA 批准并发布。"""
    steps = recipe_steps(method_id)
    released = [row for row in _items(researcher.get("/recipes"))
                if row.get("name") == RECIPE_NAME and row.get("state") == "released"]
    for row in released:
        detail = researcher.get(f"/recipes/{row['id']}")
        same_steps = [(row.get("method") or {}).get("id") for row in detail.get("steps") or []] == \
            [(row.get("method") or {}).get("id") for row in steps]
        if same_steps and detail.get("risk") == RISK:
            ok("流程", f"{row['id']} {RECIPE_NAME}（已发布，沿用）")
            return row["id"]
    if released:
        draft = researcher.post(f"/recipes/{released[0]['id']}/revision")
        note(f"已发布的 {released[0]['id']} 与这里的定义不同（步骤或风险评估）：出修订版 {draft['id']}")
    else:
        draft = researcher.post("/recipes", {"name": RECIPE_NAME, "plate": CHANNELS})
    current = researcher.get(f"/recipes/{draft['id']}")
    researcher.patch(f"/recipes/{draft['id']}", {
        "steps": steps, "bom": [], "design": RECIPE_DESIGN, "risk": RISK, "row_version": current["row_version"],
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
    ok("流程已发布", f"{draft['id']} {RECIPE_NAME}（上柜 → 循环 → QA 复核）"
       + (f"；原版 {released[0]['id']} 随之退役" if released else ""))
    return draft["id"]


def approve_plan(researcher: Actor, qa: Actor, recipe_id: str, metrics: dict[str, str], cells: int, name: str) -> str:
    plan = researcher.post("/plans", {
        "name": name, "recipe_id": recipe_id, "plan_type": "single_condition", "sample_count": cells,
        "goal": f"{cells} 颗扣电在 Neware 柜上按同一工步（CC-CV）充放电，验证接入、逐颗下发、回报与复核链路（模拟网关）",
        "required_metrics": [metrics[m["code"]] for m in METRICS],
    })
    plan_id = plan["id"]
    detail = researcher.get(f"/plans/{plan_id}")
    failing = [f"{row.get('label') or row['key']}：{row.get('detail')}" for row in detail.get("checks") or []
               if not row.get("ok")]
    if failing:
        raise Failed(f"方案 {plan_id} 检查未通过：" + "；".join(failing))
    researcher.post(f"/plans/{plan_id}/lock")
    researcher.post(f"/plans/{plan_id}/submit")
    fresh = qa.get(f"/plans/{plan_id}")
    qa.post(f"/plans/{plan_id}/decision", {
        "conclusion": "approved", "signature_id": qa.sign("批准实验方案", plan_id, fresh["row_version"]),
    })
    ok("方案已批准", f"{plan_id} {name}（{cells} 颗扣电）")
    return plan_id


def create_task(researcher: Actor, operator: Actor, plan_id: str, title: str) -> str:
    task = researcher.post("/experiment-tasks", {"plan_id": plan_id, "title": title, "priority": 2,
                                                  "note": "单独的实验任务：Neware 柜模拟测试"})
    researcher.post(f"/experiment-tasks/{task['id']}/assign", {"assignee_user_id": operator.id})
    operator.post(f"/experiment-tasks/{task['id']}/accept")
    ok("实验任务", f"{task['id']} {title}（研究员建、分配给操作员 {OPERATOR}、已接受）")
    return task["id"]


def launch(operator: Actor, plan_id: str, task_id: str, note_text: str) -> str:
    batch = operator.post("/batches", {"plan_id": plan_id, "task_id": task_id, "note": note_text})
    batch_id = batch["id"]
    operator.post(f"/batches/{batch_id}/schedule", {})
    dispatch(operator, batch_id, note_text)
    return batch_id


def dispatch(operator: Actor, batch_id: str, note_text: str) -> None:
    """开跑检查 → 签名下发。检查没过就退回待排程（归还工位时间窗，别挡后面的批次），处理后用 resume 接着跑。"""
    preflight = operator.get(f"/batches/{batch_id}/preflight?manual_review=true")
    if not preflight["ok"]:
        operator.post(f"/batches/{batch_id}/unschedule", {})
        raise Failed("开跑检查未通过：" + "；".join(f"{row.get('label')}：{row.get('detail')}" for row in preflight["blocked"])
                     + f"\n批次 {batch_id} 已退回待排程；处理后：scripts/load-neware-cycler.py resume --batch {batch_id}")
    fresh = operator.get(f"/batches/{batch_id}")
    operator.post(f"/batches/{batch_id}/dispatch", {
        "manual_review": True, "reason": note_text,
        "signature_id": operator.sign("批准执行", batch_id, fresh["row_version"]),
    })
    stations = sorted({row.get("station_id") for row in fresh.get("allocations") or [] if row.get("station_id")})
    ok("批次已排程、开跑检查通过、签名下发", batch_id + (f"（排到 {'、'.join(stations)}）" if stations else ""))


def drive(operator: Actor, qa: Actor, batch_id: str, first_channel: int, timeout: float) -> dict:
    """推到批次结束：上柜节点按样本位置记通道（first_channel 起），审核节点由 QA 批准；设备步骤由执行器投递。"""
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
                samples = sorted(detail["samples"], key=lambda row: row["position"])
                channels = {sample["id"]: first_channel + index for index, sample in enumerate(samples)}
                body = {"form_data": {"channel": channels, "polarity_ok": True},
                        "checks": {"samples": True, "materials": bool(detail["reservations"])},
                        "note": "Neware 模拟测试脚本", "row_version": run["row_version"]}
                if run["requires_signature"]:
                    body["signature_id"] = operator.sign("人工步骤记录确认", run["id"], run["row_version"])
                operator.post(f"/step-runs/{run['id']}/submit", body)
                handled.add(run["id"])
                ok(f"人工节点「{run['step_name']}」已提交", "、".join(
                    f"{sample['well']}→通道 {channels[sample['id']]}" for sample in samples))
            elif run["kind"] == "review":
                qa.post(f"/step-runs/{run['id']}/review", {
                    "conclusion": "approved", "row_version": run["row_version"],
                    "signature_id": qa.sign("流程审核通过", run["id"], run["row_version"]),
                })
                handled.add(run["id"])
                ok(f"审核节点「{run['step_name']}」QA 已批准")
        return None

    detail = wait_for(f"批次 {batch_id} 完成", advance, timeout=timeout)
    ok("批次已完成", f"{batch_id}，用时 {time.monotonic() - started:.0f} 秒")
    return detail


def show_device_step(detail: dict) -> None:
    for checkpoint in sorted(detail["checkpoints"], key=lambda row: row["step_index"]):
        payload = checkpoint["payload"]
        wells = (payload.get("delivered") or {}).get("wells") or {}
        if not wells:
            continue
        step(f"第 {checkpoint['step_index'] + 1} 步设备回报（{payload.get('station_id')} · {payload.get('origin')}）")
        for well in sorted(wells, key=lambda w: (w[:1], int(w[1:]) if w[1:].isdigit() else 0)):
            row = wells[well]
            ok(f"孔位 {well}", f"通道 {row.get('channel')} · 条码 {row.get('bts_barcode')} · {row.get('workstatus')} · "
                              f"循环 {row.get('cycle')} 圈 · 容量 {row.get('capacity')} Ah · 能量 {row.get('energy')} Wh")
        flags = [flag.get("code") for flag in payload.get("flags") or []]
        if flags:
            note("回执标记：" + "、".join(flags))


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
        simulated = any(flag.get("code") == "simulated" for flag in row.get("flags") or [])
        qa.post(f"/result-values/{row['id']}/review", {
            "conclusion": "approved", "quality": "valid", "result_version": row["result_version"],
            "reason": "模拟网关（假 BTS）回报的值，复核只为验证接入与数据链路" if simulated else "设备回报，数据完整",
            "signature_id": qa.sign("数据复核通过", row["id"], row["result_version"]),
        })
    fresh = device_results(researcher, detail)
    simulated = sum(1 for row in fresh if any(flag.get("code") == "simulated" for flag in row.get("flags") or []))
    ok("设备回报结果已复核", f"{len(fresh)} 条（其中 {simulated} 条带「模拟」标记，不进闭环训练数据）")
    return fresh


def publish_report(researcher: Actor, qa: Actor, batch_id: str, detail: dict, results: list[dict]) -> str:
    cells = len(detail["samples"])
    conclusion = (
        f"批次 {batch_id} 在 {STATION}（Neware 充放电柜，模拟网关 {DEVICE_ID}）上按工步 CC-CV 完成 {cells} 颗扣电的充放电；"
        f"上柜时按样本记录的通道前馈给循环步骤，网关逐颗启动并按孔位回报；BTS 容量与循环圈数 {len(results)} 条结果已复核。"
        "模拟阶段：数值来自假 BTS，不是实测，只用于验证接入、执行、审核与报告链路。"
    )
    report = researcher.post("/reports", {"batch_id": batch_id, "conclusion": conclusion})
    researcher.post(f"/reports/{report['id']}/submit")
    fresh = qa.get(f"/reports/{report['id']}")
    approved = qa.post(f"/reports/{report['id']}/approve", {
        "conclusion": "approved", "signature_id": qa.sign("批准报告", report["id"], fresh["row_version"]),
    })
    published = qa.post(f"/reports/{report['id']}/publish", {
        "signature_id": qa.sign("发布报告", report["id"], approved["row_version"]),
    })
    ok("报告已发布", f"{report['id']} · {published.get('state_label') or published.get('state')}")
    return report["id"]


def run(team: dict[str, Actor], context: dict, args: argparse.Namespace) -> dict:
    researcher, qa, operator = team["researcher"], team["qa"], team["operator"]
    if args.first_channel < 1 or args.first_channel + args.cells - 1 > CHANNELS:
        raise Failed(f"通道 {args.first_channel}–{args.first_channel + args.cells - 1} 超出 1–{CHANNELS}")
    gate = operator.get("/gate")
    if not gate["open"]:
        raise Failed(f"执行门关着（执行器在跑吗？）：{gate['reasons']}")
    step(f"实验任务：{args.cells} 颗扣电在 {STATION} 上恒流恒压循环")
    recipe_id = release_recipe(researcher, qa, context["method"])
    name = args.plan_name or f"Neware 柜模拟测试（{args.cells} 颗扣电，CC-CV）"
    plan_id = approve_plan(researcher, qa, recipe_id, context["metrics"], args.cells, name)
    task_id = create_task(researcher, operator, plan_id, name)
    batch_id = launch(operator, plan_id, task_id, f"Neware 柜模拟测试：{args.cells} 颗扣电")
    return {"recipe": recipe_id, "plan": plan_id, "task": task_id, **finish(team, batch_id, args)}


def resume(team: dict[str, Actor], args: argparse.Namespace) -> dict:
    """接着跑一个已建好的批次：还没下发就排程（需要时）、开跑检查、签名下发，然后推到结束、复核、出报告。"""
    operator = team["operator"]
    detail = operator.get(f"/batches/{args.batch}")
    step(f"接着跑批次 {args.batch}（{detail['state']}）")
    if detail["state"] in {"planned", "draft"}:
        operator.post(f"/batches/{args.batch}/schedule", {})
        detail = operator.get(f"/batches/{args.batch}")
    if detail["state"] == "scheduled":
        dispatch(operator, args.batch, detail.get("note") or "Neware 柜模拟测试")
    return {"recipe": detail.get("recipe_id", ""), "plan": detail.get("plan_id", ""), "task": detail.get("task_id", ""),
            **finish(team, args.batch, args)}


def finish(team: dict[str, Actor], batch_id: str, args: argparse.Namespace) -> dict:
    researcher, qa, operator = team["researcher"], team["qa"], team["operator"]
    step("执行")
    detail = drive(operator, qa, batch_id, args.first_channel, args.timeout)
    show_device_step(detail)
    step("数据复核与报告")
    results = review_results(qa, researcher, detail)
    report_id = publish_report(researcher, qa, batch_id, detail, results)
    return {"batch": batch_id, "report": report_id}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="登记 Neware 充放电柜（模拟网关），跑一个单独的实验任务")
    parser.add_argument("command", choices=("register", "run", "resume"))
    parser.add_argument("--base", default=os.environ.get("ILCS_BASE_URL", "http://127.0.0.1:8090"))
    parser.add_argument("--gateway-url", default="https://neware-sim:8443/api/v1", help="ILCS 容器里看到的网关地址")
    parser.add_argument("--device-id", default=DEVICE_ID, help="网关的设备编号（证书、令牌按它命名）")
    parser.add_argument("--acceptance", action="store_true", help="register 时再跑一次动作级 + 故障项目验收")
    parser.add_argument("--acceptance-timeout", type=float, default=180)
    parser.add_argument("--cells", type=int, default=4, help="扣电颗数（1–8）")
    parser.add_argument("--first-channel", type=int, default=1, help="上柜时从第几号通道放起")
    parser.add_argument("--plan-name", default="")
    parser.add_argument("--batch", default="", help="resume：接着跑的批次号")
    parser.add_argument("--timeout", type=float, default=900, help="等批次完成的秒数")
    args = parser.parse_args(argv)
    try:
        refuse_production()
        team = actors(http_transport(args.base))
        if args.command == "resume":
            if not args.batch:
                raise Failed("resume 要给 --batch <批次号>")
            outcome = resume(team, args)
            print(f"\n完成：批次 {outcome['batch']} · 报告 {outcome['report']}")
            return 0
        context = register(team, args)
        if args.command == "register":
            print(f"\n完成：{STATION} 已登记并接上模拟网关。跑实验任务：scripts/load-neware-cycler.py run")
            return 0
        outcome = run(team, context, args)
        print(f"\n完成：流程 {outcome['recipe']} · 方案 {outcome['plan']} · 实验任务 {outcome['task']} · "
              f"批次 {outcome['batch']} · 报告 {outcome['report']}")
        return 0
    except Failed as exc:
        print(f"\n失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
