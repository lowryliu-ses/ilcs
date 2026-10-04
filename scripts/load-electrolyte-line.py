#!/usr/bin/env python3
"""登记 C 公司 A-Lab 电解液产线（scripts/lines/c-electrolyte/line.json），按配方表跑一批。

只走 HTTP，和界面调同一组接口；签名用演示账号口令逐次签署（`POST /signatures`），与在界面上签的一样。
产线在模拟阶段：16 个工位都接内置模拟适配器（工位配置 `simulate_outputs: true`，检测步骤回报示意值），
资产按「校准豁免」占位、原料用模拟阶段测试批号——接真机前要替换的东西见 docs/电解液配液线.md。

    python3 scripts/load-electrolyte-line.py register [--base http://127.0.0.1:8090]
    python3 scripts/load-electrolyte-line.py run [--table 配方表.xlsx] [--bottles 2 --volume 30] [--plan-name 名称]
    python3 scripts/load-electrolyte-line.py connect      # gateways.json 里的工位改接设备网关（真实接入链路）
    python3 scripts/load-electrolyte-line.py disconnect   # 这几个工位切回内置模拟

- register：能力、资产、工位（接好后重连一次）、设备方法（工程师起草、QA 发布）、物料主数据与测试批号
  （QA 复验放行）、检测指标、操作员资质、配液模板 FT-ELY-01。已有的先查后用，重复运行不会多建。
- run：先照 register 补齐，再导入配方表（缺省用参考配方 formula-20260929.csv）→ 研究员提交流程 → QA 批准、发布
  （同结构的配方表沿用已发布流程，这几步跳过）→ 研究员锁定、提交方案 → QA 批准 → 操作员建批次、排程、开跑检查、
  签名下发 → 人工节点按表单填写，直到批次完成 → 打印每步检查点、消耗入账、检测值与报警。
  run 要求执行器在跑（本机部署的 executor），它负责投递与推进。
  一瓶一配方：配过液的瓶子不能再导入，同一张表（含缺省参考配方）run 过一次后再 run 会在导入时被拒，换新序列号的表。
- connect：gateways.json 列出的工位（现在是配液天平、配粉天平、拉曼）套用设备接入模板、连到设备网关（模拟阶段是设备仓库
  ilcs-devices 的 deploy/compose.yml 里 electrolyte profile 起的模拟站），等执行器跑完只读级验收放行；之后 run 的这几步就走
  http_json_v1：网关核对加的料、回报天平称出来的实际量，消耗按实际量入账。disconnect 把它们切回内置模拟。
  模拟站的主机名要先加进 ILCS 的 ILCS_ADAPTER_ALLOWED_HOSTS（deploy/.env）。
- 资产只给新登记的工位建 AS-<工位> 占位；已登记的工位沿用它现在关联的资产，不补建、不改。

正式环境（ILCS_ENVIRONMENT=production）拒绝运行：它会登记模拟工位与测试批号。服务端在正式环境同样拒绝
模拟适配器下发，这里先挡一道，免得把占位主数据登记进正式库。

测试里用 `client_transport(TestClient)` 注入传输，驱动与正式部署完全相同的调用序列
（api/tests/api/test_electrolyte_line.py）。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
LINE = HERE / "lines" / "c-electrolyte" / "line.json"
GATEWAYS = HERE / "lines" / "c-electrolyte" / "gateways.json"
# 设备仓库（设备模块的 profile.json 在它的 gateway/<模块>/ 下）：环境变量 ILCS_DEVICES，缺省是 ILCS 旁边的 ../ilcs-devices
DEVICES_REPO = Path(os.environ.get("ILCS_DEVICES") or HERE.parent.parent / "ilcs-devices")
FORMULA = HERE / "lines" / "c-electrolyte" / "formula-20260929.csv"
SOP = HERE / "lines" / "c-electrolyte" / "sop.json"
PASSWORD = "ilcs1234"
RUN = uuid.uuid4().hex[:6]

# 传输：(method, path, body, files, headers) → (状态码, 内容)。path 不含 /api 前缀
Transport = Callable[..., tuple[int, Any]]


class Failed(Exception):
    pass


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


def ok(label: str, detail: str = "") -> None:
    print(f"  ✓ {label}{('：' + detail) if detail else ''}", flush=True)


def note(label: str) -> None:
    print(f"  ! {label}", flush=True)


def _decode(payload: bytes) -> Any:
    return json.loads(payload) if payload and payload[:1] in b"{[" else payload


def http_transport(base: str) -> Transport:
    """正式部署：urllib 直连服务。上传走 multipart（与浏览器上传同一个接口）。"""
    root = base.rstrip("/") + "/api"

    def call(method: str, path: str, body: Any = None, files: dict | None = None, headers: dict | None = None):
        headers = dict(headers or {})
        data = None
        if files:
            boundary = uuid.uuid4().hex
            chunks = []
            for field, (filename, content, media_type) in files.items():
                chunks.append(
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                    f"Content-Type: {media_type}\r\n\r\n".encode()
                )
                chunks.append(content + b"\r\n")
            chunks.append(f"--{boundary}--\r\n".encode())
            data = b"".join(chunks)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(root + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as error:
            return error.code, _decode(error.read())

    return call


def client_transport(client) -> Transport:
    """测试：Starlette TestClient（或任何有 .request 的客户端），调用序列与正式部署一样。"""

    def call(method: str, path: str, body: Any = None, files: dict | None = None, headers: dict | None = None):
        kwargs: dict[str, Any] = {"headers": dict(headers or {})}
        if files:
            kwargs["files"] = files
        elif body is not None:
            kwargs["json"] = body
        response = client.request(method, "/api" + path, **kwargs)
        return response.status_code, _decode(response.content)

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

    def call(self, method: str, path: str, body: Any = None, *, files: dict | None = None,
             expect: tuple[int, ...] = (200, 201)):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if method != "GET":
            headers["Idempotency-Key"] = f"ely-{RUN}-{uuid.uuid4().hex[:10]}"
        status, content = self.transport(method, path, body, files, headers)
        if status not in expect:
            raise Failed(f"{self.username} {method} {path} → {status}：{content}")
        return content

    def get(self, path: str):
        return self.call("GET", path)

    def post(self, path: str, body: dict | None = None, expect: tuple[int, ...] = (200, 201)):
        return self.call("POST", path, body or {}, expect=expect)

    def patch(self, path: str, body: dict):
        return self.call("PATCH", path, body)

    def put(self, path: str, body: dict):
        return self.call("PUT", path, body)

    def upload(self, path: str, filename: str, content: bytes, media_type: str):
        return self.call("POST", path, files={"file": (filename, content, media_type)})

    def sign(self, meaning: str, target: str = "", version: int = 0) -> str:
        body = self.post("/signatures", {
            "password": self.password, "meaning": meaning, "target": target, "object_version": version,
        })
        return body["signature_id"]


def actors(transport: Transport) -> dict[str, Actor]:
    """链路上的分工：工程师管能力 / 工位 / 方法，QA 发布与放行，操作员管物料与执行，研究员管流程与方案。"""
    return {name: Actor(name, transport) for name in ("engineer", "qa", "operator", "researcher", "admin")}


def load_line(path: Path = LINE) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# 管理员默认拥有全部能力资质（和种子一致）：产线登记的能力也给它发一份
ADMIN_PERSON = "P-005"


def _items(payload: Any) -> list:
    return payload.get("items", []) if isinstance(payload, dict) else payload


def refuse_production() -> None:
    if os.environ.get("ILCS_ENVIRONMENT", "").strip().lower() == "production":
        raise Failed("正式环境拒绝运行：本脚本登记模拟工位、占位资产与测试批号")


# ---------------------------------------------------------------- register


def register(team: dict[str, Actor], line: dict) -> dict:
    """登记整条线。返回后续步骤要用的标识：方法 key → id、指标 code → id、模板。"""
    engineer, qa, operator, researcher, admin = (team[k] for k in ("engineer", "qa", "operator", "researcher", "admin"))
    step(f"登记产线：{line['name']}")
    register_capabilities(engineer, line)
    register_islands(engineer, line)
    register_stations(engineer, operator, line)
    # 检测方法的输出项关联指标：先有指标，再登记方法
    metrics = register_metrics(researcher, line)
    methods = register_methods(engineer, qa, line, metrics)
    register_materials(operator, qa, line)
    grant_qualifications(admin, line)
    # 模板生成的流程关联这份 SOP 的生效版本：先有 SOP，再建模板
    register_sop(researcher, qa, operator, SOP)
    template = register_template(researcher, line, methods, metrics)
    return {"methods": methods, "metrics": metrics, "template": template,
            "stations": [station["id"] for station in line["stations"]]}


def register_sop(researcher: Actor, qa: Actor, operator: Actor, path: Path) -> dict:
    """配液线 SOP（sop.json + 同目录生成好的附件 PDF）：研究员起草、上传附件、写结构化步骤、提交评审，
    QA 批准发布（起草人不能批准本人的版本），要求阅读确认的由执行批次的操作员确认。同编号同版本已有就接着走完。"""
    spec = json.loads(path.read_text(encoding="utf-8"))
    rows = researcher.get("/sops?page_size=100")
    rows = rows.get("items", rows) if isinstance(rows, dict) else rows
    current = next((row for row in rows if row["code"] == spec["code"] and row["version"] == spec["version"]), None)
    label = f"{spec['code']} {spec['version']}"
    if current is None:
        pdf = path.with_name(f"{spec['code']}-{spec['version']}.pdf")
        attachment = researcher.upload("/files", pdf.name, pdf.read_bytes(), "application/pdf")
        current = researcher.post("/sops", {
            "code": spec["code"], "title": spec["title"], "version": spec["version"], "file_id": attachment["id"],
            "capability_scope": spec["capability_scope"], "sample_types": spec["sample_types"],
            "requires_training_ack": spec["requires_training_ack"], "category": spec["category"], "owner_id": qa.id,
        })
        note(f"SOP {label} 已起草（附件 {pdf.name}）")
    if current["state"] == "draft":
        steps = [{key: row[key] for key in ("title", "kind", "capability", "duration_min", "instructions", "checks")
                  if key in row} for row in spec["steps"]]
        current = researcher.put(f"/sops/{current['id']}/steps", {"steps": steps, "row_version": current["row_version"]})
        researcher.post(f"/sops/{current['id']}/submit")
        current = qa.get(f"/sops/{current['id']}")
    if current["state"] == "review":
        current = qa.post(f"/sops/{current['id']}/decision", {
            "conclusion": "approved",
            "signature_id": qa.sign("批准并发布 SOP", current["id"], current["row_version"]),
        })
    if current["state"] != "published":
        raise Failed(f"SOP {label} 没能发布：当前状态 {current.get('state_label') or current['state']}")
    if spec.get("requires_training_ack"):
        operator.post(f"/sops/{current['id']}/acknowledge")
    ok("SOP", f"{label} {spec['title']}（已发布，{len(spec['steps'])} 步"
              + ("，操作员已阅读确认" if spec.get("requires_training_ack") else "") + "）")
    return current


def register_islands(engineer: Actor, line: dict) -> None:
    """三段各是一个实验区：工位上写岛号，这里给岛号起名（看板、现场监控按名称显示）。名称一致就不动。"""
    current = {row["id"]: row["name"] for row in engineer.get("/islands")}
    changed = 0
    for island in line.get("islands") or []:
        if current.get(island["id"]) != island["name"]:
            engineer.put(f"/islands/{island['id']}", {"name": island["name"]})
            changed += 1
    ok("实验区", "、".join(f"#{row['id']} {row['name']}" for row in line.get("islands") or [])
                 + (f"（登记 / 改名 {changed}）" if changed else ""))


def register_capabilities(engineer: Actor, line: dict) -> None:
    existing = {row["id"]: row for row in engineer.get("/capabilities")}
    created = []
    for cap in line["capabilities"]:
        if cap["id"] in existing:
            if set(existing[cap["id"]].get("params") or {}) != set(cap["params"]):
                note(f"能力 {cap['id']} 已存在但参数与 line.json 不同，沿用现有定义")
            continue
        # 能力极限只在工位上登记（不带 stations），否则每个参数先被写成 [0,100]
        engineer.post("/capabilities", {
            "id": cap["id"], "name": cap["name"], "params": cap["params"], "param_specs": cap["param_specs"],
            "recovery": cap["recovery"], "signature_id": engineer.sign("能力模型变更批准", cap["id"]),
        })
        created.append(cap["id"])
    ok("能力", f"{len(line['capabilities'])} 项（新登记 {len(created)}）")


def _asset(engineer: Actor, line: dict, station: dict) -> str:
    asset_no = f"AS-{station['id']}"
    found = [row for row in _items(engineer.get(f"/assets?keyword={asset_no}&page_size=100"))
             if row["asset_no"] == asset_no]
    if found:
        return found[0]["id"]
    defaults = line.get("asset") or {}
    # 设备没到场，校准无从谈起：按豁免登记并写明理由，接真机前按实物补型号、序列号与校准
    created = engineer.post("/assets", {
        "asset_no": asset_no, "name": station["name"], "model": station["model"], "vendor": defaults.get("vendor", ""),
        "capacity": station.get("channels", 1), "calibration_applicable": False,
        "calibration_exempt_reason": defaults["calibration_exempt_reason"], "note": defaults.get("note", ""),
    })
    return created["id"]


def register_stations(engineer: Actor, operator: Actor, line: dict) -> None:
    existing = {row["id"]: row for row in engineer.get("/stations")}
    adapter = line.get("adapter") or {}
    created = []
    for station in line["stations"]:
        if station["id"] not in existing:
            # 只给要新建的工位找或建占位资产；已登记的工位沿用它现在关联的资产（可能已换成实物资产），
            # 否则被有意删掉的 AS-<工位> 会在重跑时又被建成一台没人用的豁免资产
            asset_id = _asset(engineer, line, station)
            engineer.post("/stations", {
                "id": station["id"], "name": station["name"], "island": station["island"],
                "channels": station.get("channels", 1), "channel_unit": "batch", "limits": station["limits"],
                "asset_id": asset_id, **adapter, "signature_id": engineer.sign("工程变更批准", station["id"]),
            })
            created.append(station["id"])
            continue
        # 已登记的工位：内置模拟要打开示意检测值，否则检测步骤都是「缺必报项」
        current = engineer.get(f"/stations/{station['id']}/adapter")
        wanted = adapter.get("adapter_config") or {}
        config = current.get("config") or {}
        if current.get("kind") == "simulation" and any(config.get(k) != v for k, v in wanted.items()):
            engineer.patch(f"/stations/{station['id']}/adapter", {
                "config": {**config, **wanted}, "row_version": current["row_version"],
                "signature_id": engineer.sign("设备集成配置变更批准", station["id"], current["row_version"]),
            })
            ok(f"工位 {station['id']}", "内置模拟打开示意检测值")
    # 新登记的适配器先离线：在「现场监控」工位卡片上点「重连」做一次握手，之后由心跳维持在线
    blocked = operator.get("/gate").get("blocked_stations") or {}
    for station in line["stations"]:
        if station["id"] in created or station["id"] in blocked:
            operator.post(f"/stations/{station['id']}/adapter/reconnect")
    still = operator.get("/gate").get("blocked_stations") or {}
    offline = [sid for sid in (s["id"] for s in line["stations"]) if sid in still]
    if offline:
        raise Failed(f"工位仍不可用：{ {sid: still[sid] for sid in offline} }")
    ok("工位", f"{len(line['stations'])} 个（新登记 {len(created)}），内置模拟适配器在线，资产按校准豁免占位")


def method_outputs(method: dict, metrics: dict[str, str]) -> list[dict]:
    """line.json 里输出项用 metric 写指标编码；换成本库的指标 id（metric_id），设备回报的值才会写成检测结果。"""
    rows = []
    for rule in method.get("outputs") or []:
        row = {key: value for key, value in rule.items() if key != "metric"}
        if rule.get("metric"):
            if rule["metric"] not in metrics:
                raise Failed(f"方法 {method['name']} 的输出 {rule['key']} 关联的指标 {rule['metric']} 没有登记")
            row["metric_id"] = metrics[rule["metric"]]
        rows.append(row)
    return rows


def _same_outputs(current: list[dict], wanted: list[dict]) -> bool:
    """比较输出项时补齐缺省字段：接口返回的规则带齐了 label / lo / hi / required / metric_id。"""
    def norm(rows):
        return [{"key": r.get("key"), "label": r.get("label") or "", "unit": r.get("unit") or "",
                 "lo": r.get("lo"), "hi": r.get("hi"), "required": bool(r.get("required")),
                 "metric_id": r.get("metric_id") or ""} for r in rows or []]
    return norm(current) == norm(wanted)


def register_methods(engineer: Actor, qa: Actor, line: dict, metrics: dict[str, str]) -> dict[str, str]:
    """工程师起草、QA 发布（起草人不能发布自己的方法）。适用型号只写对应工位的型号：同能力多工位靠它分流。

    已发布的方法输出项与 line.json 不一致（例如后来给检测输出关联了指标）：按修订流程出一个新版本——
    工程师修订、改输出项，QA 发布，同编号旧版本随之退役；模板随后按新版本的 id 更新。"""
    models = {row["id"]: row["model"] for row in line["stations"]}
    ids: dict[str, str] = {}
    created = revised = 0
    for method in line["methods"]:
        cap = method["capability"]
        outputs = method_outputs(method, metrics)
        released = [row for row in engineer.get(f"/device-methods?state=released&capability_id={cap}")
                    if row["name"] == method["name"]]
        drafts = [row for row in engineer.get(f"/device-methods?state=draft&capability_id={cap}")
                  if row["name"] == method["name"]]
        if released and _same_outputs(released[0].get("outputs") or [], outputs):
            ids[method["key"]] = released[0]["id"]
            continue
        if released:
            # 上次中途失败留下的修订草稿接着用，不再修订第二份
            draft = drafts[0] if drafts else engineer.post(f"/device-methods/{released[0]['id']}/revise")
            draft = engineer.patch(f"/device-methods/{draft['id']}", {"outputs": outputs, "row_version": draft["row_version"]})
            revised += 1
        else:
            # 上次中途失败留下的草稿接着发布，不再起草第二份
            draft = drafts[0] if drafts else engineer.post("/device-methods", {
                "name": method["name"], "capability_id": cap, "instrument_models": [models[method["station"]]],
                "program": method["program"], "params": method.get("params") or {}, "outputs": outputs,
                "dur_min": method["dur_min"], "note": method.get("note", ""),
            })
            created += 1
        done = qa.post(f"/device-methods/{draft['id']}/release", {"row_version": draft["row_version"]})
        ids[method["key"]] = done["id"]
    ok("设备方法", f"{len(ids)} 个已发布（新发布 {created}" + (f"、修订 {revised}" if revised else "") + "）")
    return ids


def register_materials(operator: Actor, qa: Actor, line: dict) -> None:
    lots = line["lots"]
    materials = {row["code"]: row for row in operator.get("/materials")}
    existing_lots = {row["id"]: row for row in operator.get("/lots")}
    made, received, released = 0, 0, 0
    for item in line["materials"]:
        code = f"ELY-{item['name']}"
        material = materials.get(code)
        if material is None:
            material = operator.post("/materials", {
                "code": code, "name": item["name"], "base_unit": lots["unit"], "category": item["category"],
            })
            made += 1
        lot_id = f"LOT-SIM-{item['name']}-01"
        lot = existing_lots.get(lot_id)
        if lot is None:
            lot = operator.post("/lots", {
                "id": lot_id, "material": item["name"], "material_id": material["id"], "type": item["category"],
                "qty": lots["qty"][item["category"]], "unit": lots["unit"], "expiry": lots["expiry"],
                "storage": lots["storage"],
            })
            received += 1
        if lot["release"] != "已放行":
            qa.post(f"/lots/{lot_id}/release", {"signature_id": qa.sign("复验合格", lot_id)})
            released += 1
    ok("物料与测试批号", f"{len(line['materials'])} 种（新登记物料 {made}、批号 {received}、放行 {released}）")


def register_metrics(researcher: Actor, line: dict) -> dict[str, str]:
    existing = {(row["code"], row.get("version")): row for row in researcher.get("/metrics")}
    ids = {}
    for metric in line["metrics"]:
        row = existing.get((metric["code"], "v1")) or researcher.post("/metrics", {
            "code": metric["code"], "name": metric["name"], "unit": metric["unit"], "value_type": "number",
            "sample_types": ["电解液"], "rules": metric.get("rules") or {},
        })
        ids[metric["code"]] = row["id"]
    ok("检测指标", "、".join(f"{m['name']} {m['unit']}" for m in line["metrics"]))
    return ids


def grant_capabilities(admin: Actor, person_code: str, capabilities: dict[str, str]) -> int:
    """给人员档案补发能力资质（能力 id → 名称）；已有有效资质的跳过。返回新增几项。"""
    person = next((row for row in _items(admin.get(f"/people?keyword={person_code}"))
                   if row.get("code") == person_code), None)
    if person is None:
        raise Failed(f"人员 {person_code} 不存在")
    held = {row["scope_ref"] for row in admin.get(f"/people/{person['id']}/qualifications")
            if row.get("scope_kind") == "capability" and row.get("status") not in {"revoked", "expired"}}
    added = 0
    for capability_id, name in capabilities.items():
        if capability_id not in held:
            admin.post(f"/people/{person['id']}/qualifications", {
                "scope_kind": "capability", "scope_ref": capability_id, "label": name,
            })
            added += 1
    return added


def grant_qualifications(admin: Actor, line: dict) -> None:
    capabilities = {cap["id"]: cap["name"] for cap in line["capabilities"]}
    for label, code in (("操作员资质", line["operator"]), ("管理员资质", ADMIN_PERSON)):
        added = grant_capabilities(admin, code, capabilities)
        ok(label, f"{code} 获得全部 {len(capabilities)} 项产线能力（新增 {added}）")


def template_config(line: dict, methods: dict[str, str], metrics: dict[str, str]) -> dict:
    """line.json 里方法按 key、指标按 code 引用；换成本库登记出来的 id。"""
    config = copy.deepcopy(line["template"]["config"])

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if node.get("kind") == "device" and isinstance(node.get("method"), str):
                node["method"] = {"id": methods[node["method"]]}
            for value in node.values():
                resolve(value)
        elif isinstance(node, list):
            for value in node:
                resolve(value)
        return node

    resolve(config)
    config["required_metrics"] = [metrics.get(code, code) for code in config.get("required_metrics") or []]
    return config


def register_template(researcher: Actor, line: dict, methods: dict[str, str], metrics: dict[str, str]) -> dict:
    spec = line["template"]
    config = template_config(line, methods, metrics)
    found = next((row for row in researcher.get("/formulation-templates") if row["code"] == spec["code"]), None)
    if found is None:
        template = researcher.post("/formulation-templates", {
            "code": spec["code"], "name": spec["name"], "description": spec["description"], "config": config,
        })
        ok("配液模板", f"{spec['code']} {spec['name']}（新建）")
        return template
    if found["state"] != "active":
        raise Failed(f"配液模板 {spec['code']} 已退役：换一个编号或在库里恢复")
    if found["config"] != config or found["name"] != spec["name"] or found["description"] != spec["description"]:
        template = researcher.patch(f"/formulation-templates/{found['id']}", {
            "name": spec["name"], "description": spec["description"], "config": config,
            "row_version": found["row_version"],
        })
        ok("配液模板", f"{spec['code']} 按 line.json 更新")
        return template
    ok("配液模板", f"{spec['code']}（沿用）")
    return found


# ---------------------------------------------------------------- run


def import_table(researcher: Actor, template: dict, filename: str, content: bytes, params: dict,
                 plan_name: str = "") -> dict:
    """上传解析（和界面同一个接口）→ 有问题就停 → 用解析出的表格导入。服务端导入时重新生成，是最终裁决。"""
    step(f"导入配方表 {filename}")
    media = "text/csv" if filename.lower().endswith(".csv") else \
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    preview = researcher.upload(f"/formulation-templates/{template['id']}/parse", filename, content, media)
    if params:
        preview = researcher.post(f"/formulation-templates/{template['id']}/preview", {
            "filename": filename, "table": preview["table"], "params": params,
        })
    if preview["issues"]:
        raise Failed("配方表有问题：" + "；".join(preview["issues"]))
    for warning in preview["warnings"]:
        note(warning)
    plan = preview["plan"]
    ok("预览", f"{len(preview['steps'])} 步；{len(plan['design_points'])} 个配方 × {plan['repeats']} 瓶；"
               f"试剂 {'、'.join(row['name'] for row in preview['reagents'] if row['total'])}")
    result = researcher.post(f"/formulation-templates/{template['id']}/import", {
        "filename": filename, "table": preview["table"], "params": params, "plan_name": plan_name,
    })
    recipe = result["recipe"]
    created = sum(1 for row in result["samples"] if row["created"])
    ok("已导入", f"流程 {recipe['id']}（{'沿用' if recipe['reused'] else '新建草稿'}，{recipe['state']}）；"
                f"方案 {result['plan']['id']}；瓶子 {len(result['samples'])} 个（新登记 {created}）")
    return result


def release_recipe(researcher: Actor, qa: Actor, recipe_id: str) -> None:
    """研究员提交评审 → QA 批准 → QA 发布。沿用的流程已走过的环节跳过。"""
    state = researcher.get(f"/recipes/{recipe_id}")["state"]
    if state == "draft":
        detail = researcher.get(f"/recipes/{recipe_id}")
        bad = [row for row in detail.get("validation") or [] if not row["ok"]]
        if bad:
            raise Failed(f"流程 {recipe_id} 校验不通过：" + "；".join(
                f"第 {r['index'] + 1} 步 {r['issues'] or r.get('blockers')}" for r in bad))
        researcher.post(f"/recipes/{recipe_id}/submit")
        state = "review"
    for target, meaning, before in (("approved", "批准流程", "review"), ("released", "发布流程", "approved")):
        if state != before:
            continue
        fresh = qa.get(f"/recipes/{recipe_id}")
        qa.post(f"/recipes/{recipe_id}/transition", {
            "target_state": target, "signature_id": qa.sign(meaning, recipe_id, fresh["row_version"]),
        })
        state = target
    if state != "released":
        raise Failed(f"流程 {recipe_id} 状态 {state}，没能发布")
    ok("流程已发布", recipe_id)


def approve_plan(researcher: Actor, qa: Actor, plan_id: str) -> None:
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
    ok("方案已锁定并批准", plan_id)


def launch(operator: Actor, plan_id: str, note_text: str) -> str:
    """操作员建批次（没指定任务时自动建一个已接受的任务）→ 排程 → 开跑检查 → 签名下发。"""
    batch = operator.post("/batches", {"plan_id": plan_id, "note": note_text})
    batch_id = batch["id"]
    reserved = operator.get(f"/batches/{batch_id}")["reservations"]
    ok("批次已建", f"{batch_id}；预留 " + "、".join(f"{r['material']} {r['qty']}{r['unit']}" for r in reserved))
    operator.post(f"/batches/{batch_id}/schedule", {})
    preflight = operator.get(f"/batches/{batch_id}/preflight?manual_review=true")
    if not preflight["ok"]:
        raise Failed("开跑检查未通过：" + "；".join(f"{row.get('label')}：{row.get('detail')}" for row in preflight["blocked"]))
    fresh = operator.get(f"/batches/{batch_id}")
    operator.post(f"/batches/{batch_id}/dispatch", {
        "manual_review": True, "reason": note_text,
        "signature_id": operator.sign("批准执行", batch_id, fresh["row_version"]),
    })
    ok("已排程、开跑检查通过、签名下发", batch_id)
    return batch_id


def form_values(form: list[dict], samples: list[dict]) -> dict:
    """人工节点的示例填写：数量类填本批瓶数，确认项勾上，枚举取第一个选项；按样本的字段逐瓶填。"""
    values: dict[str, Any] = {}
    for field in form:
        kind = field.get("type") or "text"
        value: Any = {"number": len(samples), "bool": True, "enum": (field.get("options") or [""])[0]}.get(kind, "脚本导入")
        if field.get("per_sample"):
            value = {sample["id"]: len(samples) for sample in samples}
        values[field["key"]] = value
    return values


def drive(operator: Actor, qa: Actor, batch_id: str, pump: Callable[[], Any], rounds: int) -> dict:
    """推到批次结束：每轮先让执行器走一步（正式部署是等常驻执行器），再处理就绪的人工与审核节点。"""
    handled: set[str] = set()
    for _ in range(rounds):
        pump()
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
                body = {"form_data": form_values(run["form"], detail["samples"]),
                        "checks": {"samples": True, "materials": bool(detail["reservations"])},
                        "note": "电解液产线脚本", "row_version": run["row_version"]}
                if run["requires_signature"]:
                    body["signature_id"] = operator.sign("人工步骤记录确认", run["id"], run["row_version"])
                operator.post(f"/step-runs/{run['id']}/submit", body)
                handled.add(run["id"])
                ok(f"人工节点「{run['step_name']}」已提交")
            elif run["kind"] == "review":
                qa.post(f"/step-runs/{run['id']}/review", {
                    "conclusion": "approved", "row_version": run["row_version"],
                    "signature_id": qa.sign("流程审核通过", run["id"], run["row_version"]),
                })
                handled.add(run["id"])
                ok(f"审核节点「{run['step_name']}」QA 已批准")
    raise Failed(f"批次 {batch_id} 在 {rounds} 轮内没有完成：{operator.get(f'/batches/{batch_id}')['state']}")


def report(detail: dict) -> None:
    """每步检查点（工位、投料、检测值）、消耗入账与预留对账、批次报警。"""
    steps = detail["snapshot"]["steps"]
    step(f"批次 {detail['id']} 已完成：{len(steps)} 步")
    for checkpoint in sorted(detail["checkpoints"], key=lambda c: c["step_index"]):
        payload = checkpoint["payload"]
        delivered = payload.get("delivered") or {}
        spec = steps[checkpoint["step_index"]]
        parts = [payload.get("station_id", "")]
        for row in delivered.get("materials") or []:
            parts.append(f"投 {row['material']} {row['quantity']}{row['unit']}")
        outputs = [rule["key"] for rule in (spec.get("method") or {}).get("outputs") or []]
        parts.extend(f"{key} = {delivered[key]}" for key in outputs if key in delivered)
        flags = [flag.get("code") for flag in payload.get("flags") or []]
        if flags:
            parts.append("标记 " + "、".join(flags))
        ok(f"第 {checkpoint['step_index'] + 1} 步 {spec.get('name')}", " · ".join(p for p in parts if p))
    step("消耗入账")
    for row in detail["reservations"]:
        ok(row["material"], f"预留 {row['qty']}{row['unit']}，消耗 {row['consumed_qty']}{row['unit']}（{row['lot_id']}）")
    alarms = detail.get("alarms") or []
    step(f"报警 {len(alarms)} 条")
    for alarm in alarms:
        note(f"{alarm['id']} [{alarm['severity']}] {alarm['state']}：{alarm['message']}")


def run(team: dict[str, Actor], context: dict, filename: str, content: bytes, params: dict | None = None, *,
        pump: Callable[[], Any], rounds: int, plan_name: str = "") -> dict:
    """导入一张配方表并跑完一批。返回 {recipe, plan, batch, detail}。"""
    researcher, qa, operator = team["researcher"], team["qa"], team["operator"]
    result = import_table(researcher, context["template"], filename, content, params or {}, plan_name)
    step("审批与下发")
    recipe_id, plan_id = result["recipe"]["id"], result["plan"]["id"]
    release_recipe(researcher, qa, recipe_id)
    approve_plan(researcher, qa, plan_id)
    batch_id = launch(operator, plan_id, f"电解液产线脚本：配方表 {filename}")
    step("执行")
    detail = drive(operator, qa, batch_id, pump, rounds)
    report(detail)
    step("数据复核与报告")
    reviewed = review_device_results(researcher, qa, detail)
    report_id = publish_report(researcher, qa, batch_id, detail, reviewed)
    return {"recipe": recipe_id, "plan": plan_id, "batch": batch_id, "import": result, "detail": detail,
            "results": reviewed, "report": report_id}


def device_results(actor: Actor, detail: dict) -> list[dict]:
    """这个批次各样本「设备回报」检测任务里的当前结果（设备方法输出项关联了指标时由系统写入）。"""
    rows = []
    for sample in sorted(detail["samples"], key=lambda row: row["position"]):
        for task in _items(actor.get(f"/analysis-tasks?sample_id={sample['id']}&page_size=100")):
            if task.get("method") != "设备回报":
                continue
            for value in actor.get(f"/analysis-tasks/{task['id']}").get("values") or []:
                if not value.get("superseded_by_id"):
                    rows.append({**value, "sample_id": sample["id"], "physical_sample_id": sample["physical_sample_id"]})
    return rows


def review_device_results(researcher: Actor, qa: Actor, detail: dict) -> list[dict]:
    """QA 逐条复核设备写入的结果。模拟阶段的值是示意值：复核通过、质量判有效，只为走通审核与报告；
    结果上的「模拟设备示意值」标记保留，闭环训练数据照样把它们排除。"""
    rows = device_results(researcher, detail)
    if not rows:
        raise Failed("批次跑完了，但没有设备写入的检测结果：检查检测方法的输出项有没有关联指标")
    for row in rows:
        if row.get("review_state") != "pending":
            continue
        simulated = any(flag.get("code") == "simulated" for flag in row.get("flags") or [])
        qa.post(f"/result-values/{row['id']}/review", {
            "conclusion": "approved", "quality": "valid", "result_version": row["result_version"],
            "reason": "模拟阶段：内置模拟设备的示意值，复核只为验证数据链路" if simulated else "设备回报，数据完整",
            "signature_id": qa.sign("数据复核通过", row["id"], row["result_version"]),
        })
    fresh = device_results(researcher, detail)
    by_metric: dict[str, list] = {}
    for row in fresh:
        by_metric.setdefault(row.get("metric_name") or row.get("metric_code") or "?", []).append(row)
    ok("设备回报结果已复核", "；".join(
        f"{name} {len(items)} 条（{'、'.join(str(item.get('display') or item.get('value')) for item in items)}）"
        for name, items in by_metric.items()))
    return fresh


def publish_report(researcher: Actor, qa: Actor, batch_id: str, detail: dict, results: list[dict]) -> str:
    """出报告：研究员起草并提交，QA 批准、发布。结论写明模拟阶段的数据来历。"""
    simulated = any(any(flag.get("code") == "simulated" for flag in row.get("flags") or []) for row in results)
    bottles = len(detail["samples"])
    metrics = "、".join(dict.fromkeys(row.get("metric_name") or row.get("metric_code") or "?" for row in results))
    conclusion = (
        f"批次 {batch_id} 按 {detail.get('sop_snapshot', {}).get('code') or '配液线 SOP'} 完成 {bottles} 瓶电解液的配制与检测"
        f"（{len(detail['snapshot']['steps'])} 步）；{metrics} {len(results)} 条结果已复核。"
        + ("模拟阶段：检测值为内置模拟设备按方法输出规则给的示意值，不是实测，只用于验证配液、检测、审核与报告链路。"
           if simulated else "")
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


# ---------- 改走真实接入链路：工位接设备网关（gateways.json） ----------

def wait_for(describe: str, probe: Callable[[], Any], timeout: float, every: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = probe()
        if found:
            return found
        time.sleep(every)
    raise Failed(f"等不到：{describe}（{timeout:.0f} s）")


def ensure_template(engineer: Actor, qa: Actor, spec: dict) -> dict:
    """设备模块的接入模板，以设备仓库里模块的 profile.json 为准：这一修订已发布就沿用；没登记就导入、QA 签名发布
    （发布新修订时同编号的旧发布版自动退役，套用旧版的工位照常运行）。找不到设备仓库时退回已发布的最新修订。"""
    path = DEVICES_REPO / "gateway" / spec["module"] / "profile.json"
    rows = [row for row in _items(engineer.get("/device-templates")) if row.get("code") == spec["template"]]
    if not path.is_file():
        released = [row for row in rows if row.get("state") == "released"]
        if not released:
            raise Failed(f"接入模板 {spec['template']} 没有发布，设备仓库里也找不到 {path}（设 ILCS_DEVICES 指向 ilcs-devices）")
        note(f"找不到设备仓库里的 {path}：沿用已发布的 {spec['template']}")
        return max(released, key=lambda row: int(row.get("revision") or 0))
    profile = json.loads(path.read_text(encoding="utf-8"))
    same = [row for row in rows if int(row.get("revision") or 0) == int(profile["revision"])]
    template = next((row for row in same if row.get("state") == "released"), None) or next(iter(same), None)
    if template is not None and template.get("digest") != profile["digest"]:
        raise Failed(f"接入模板 {profile['code']} 修订 {profile['revision']} 已登记，但和 {path} 的摘要不同：改了要升修订号")
    if template is None:
        template = engineer.post("/device-templates/import", {"filename": path.name, "document": profile})
        note(f"接入模板 {profile['code']} 修订 {profile['revision']} 从 {path} 导入")
    if template["state"] == "draft":
        template = qa.post(f"/device-templates/{template['id']}/release", {
            "row_version": template["row_version"],
            "signature_id": qa.sign("发布设备接入模板", template["id"], template["row_version"]),
        })
        note(f"接入模板 {profile['code']} 修订 {profile['revision']} 已发布")
    if template["state"] != "released":
        raise Failed(f"接入模板 {profile['code']} 修订 {profile['revision']} 状态 {template['state']}，不能套用")
    return template


def _accepted(engineer: Actor, station_id: str, config_version: int):
    listed = engineer.get(f"/stations/{station_id}/adapter/acceptance")
    gate, runs = listed["gate"], listed.get("runs") or []
    if gate["required"] == "" and gate.get("accepted_config_version") == config_version:
        return next((row for row in runs if row["id"] == gate.get("accepted_run_id")), {"id": gate.get("accepted_run_id")})
    latest = next((row for row in runs if row.get("config_version") == config_version), None)
    if latest and latest.get("state") == "done" and not latest.get("ok") and latest.get("level") == "readonly":
        raise Failed(f"{station_id} 只读级验收没通过：{latest.get('error') or latest.get('report_md', '')[:500]}")
    return None


def connect(team: dict[str, Actor], gateways: dict, timeout: float) -> None:
    """工位套用接入模板、连接参数指向设备网关；重连一次，等执行器跑完只读级验收（模拟网关只读级就放行）。"""
    engineer, qa, operator = team["engineer"], team["qa"], team["operator"]
    step("工位改接设备网关（真实接入链路）")
    for station_id, spec in gateways["stations"].items():
        template = ensure_template(engineer, qa, spec)
        device_id = spec["device_id"]
        connection = {"base_url": f"https://{spec['host']}:8443/api/v1",
                      "ca_file": f"/run/secrets/ilcs/gateway/{device_id}.crt", "expected_device_id": device_id}
        credential = f"file:///run/secrets/ilcs/gateway/{device_id}.token"
        adapter = engineer.get(f"/stations/{station_id}/adapter")
        if (adapter.get("template") or {}).get("id") != template["id"] or adapter.get("template_connection") != connection \
                or adapter.get("credential_ref") != credential:
            adapter = engineer.patch(f"/stations/{station_id}/adapter", {
                "template_id": template["id"], "template_connection": connection, "credential_ref": credential,
                "row_version": adapter["row_version"],
                "signature_id": engineer.sign("设备集成配置变更批准", station_id, adapter["row_version"]),
            })
            ok(f"工位 {station_id}", f"套用 {template['code']} r{template['revision']}：{connection['base_url']}，"
               f"设备编号 {device_id}（已签名保存）")
        else:
            ok(f"工位 {station_id}", f"{connection['base_url']}（沿用）")
        if (operator.get("/gate").get("blocked_stations") or {}).get(station_id):
            operator.post(f"/stations/{station_id}/adapter/reconnect", expect=(200, 201, 409))
        run_row = wait_for(f"{station_id} 接入验收放行",
                           lambda: _accepted(engineer, station_id, adapter["config_version"]), timeout=timeout)
        wait_for(f"{station_id} 在线", lambda: not (operator.get("/gate").get("blocked_stations") or {}).get(station_id),
                 timeout=60)
        ok(f"接入验收 {station_id}", f"配置 v{adapter['config_version']} 已由 {run_row['id']} 放行")
        # 读设备自报的方法目录：之后排程只往报过这个程序的工位排；型号和工位资产对不上时这里就报出来
        described = engineer.post(f"/stations/{station_id}/adapter/describe")
        if described.get("warning"):
            raise Failed(f"{station_id}：{described['warning']}")
        programs = "、".join(str(row.get("program") or row.get("code") or row.get("name") or "")
                            for row in described.get("methods") or []) or "—"
        ok(f"方法目录 {station_id}", f"型号 {described.get('reported_model') or '—'}，程序 {programs}")


def disconnect(team: dict[str, Actor], gateways: dict, line: dict) -> None:
    """gateways.json 里的工位切回内置模拟（与 register 新登记时一样：打开示意检测值），重连一次。"""
    engineer, operator = team["engineer"], team["operator"]
    adapter_defaults = line.get("adapter") or {}
    step("工位切回内置模拟")
    for station_id in gateways["stations"]:
        adapter = engineer.get(f"/stations/{station_id}/adapter")
        if adapter.get("kind") == "simulation":
            ok(f"工位 {station_id}", "已是内置模拟")
            continue
        engineer.patch(f"/stations/{station_id}/adapter", {
            "kind": "simulation", "driver": "simulation", "protocol": adapter_defaults.get("protocol", "内置模拟"),
            "config": adapter_defaults.get("adapter_config") or {}, "credential_ref": "", "template_id": "",
            "row_version": adapter["row_version"],
            "signature_id": engineer.sign("设备集成配置变更批准", station_id, adapter["row_version"]),
        })
        operator.post(f"/stations/{station_id}/adapter/reconnect", expect=(200, 201, 409))
        wait_for(f"{station_id} 在线", lambda: not (operator.get("/gate").get("blocked_stations") or {}).get(station_id),
                 timeout=60)
        ok(f"工位 {station_id}", "切回内置模拟（已签名保存）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="登记 C 公司电解液产线，按配方表跑一批（模拟阶段）")
    parser.add_argument("command", choices=("register", "run", "connect", "disconnect"))
    parser.add_argument("--base", default=os.environ.get("ILCS_BASE_URL", "http://127.0.0.1:8090"))
    parser.add_argument("--line", default=str(LINE), help="产线定义（缺省 scripts/lines/c-electrolyte/line.json）")
    parser.add_argument("--table", default=str(FORMULA), help="配方表 .xlsx / .csv（缺省参考配方）")
    parser.add_argument("--bottles", type=int, help="分装瓶数（缺省按模板）")
    parser.add_argument("--volume", type=float, help="每瓶分装量 mL（缺省按模板）")
    parser.add_argument("--plan-name", default="")
    parser.add_argument("--timeout", type=float, default=1800, help="等批次完成的秒数")
    parser.add_argument("--gateways", default=str(GATEWAYS), help="哪些工位接哪台设备网关（connect / disconnect 用）")
    parser.add_argument("--acceptance-timeout", type=float, default=180, help="connect 等只读级验收放行的秒数")
    args = parser.parse_args(argv)
    try:
        refuse_production()
        team = actors(http_transport(args.base))
        if args.command in ("connect", "disconnect"):
            gateways = json.loads(Path(args.gateways).read_text(encoding="utf-8"))
            if args.command == "connect":
                connect(team, gateways, args.acceptance_timeout)
            else:
                disconnect(team, gateways, load_line(Path(args.line)))
            print(f"\n完成：{'、'.join(gateways['stations'])} 已{'接设备网关' if args.command == 'connect' else '切回内置模拟'}")
            return 0
        context = register(team, load_line(Path(args.line)))
        if args.command == "register":
            print("\n完成：产线已登记。导入配方表：scripts/load-electrolyte-line.py run --table <文件>")
            return 0
        gate = team["operator"].get("/gate")
        if not gate["open"]:
            raise Failed(f"执行门关着（执行器在跑吗？）：{gate['reasons']}")
        params = {key: value for key, value in (("bottles", args.bottles), ("volume", args.volume)) if value is not None}
        table = Path(args.table)
        every = 3.0
        outcome = run(team, context, table.name, table.read_bytes(), params,
                      pump=lambda: time.sleep(every), rounds=max(1, int(args.timeout / every)), plan_name=args.plan_name)
        print(f"\n完成：流程 {outcome['recipe']} · 方案 {outcome['plan']} · 批次 {outcome['batch']} · 报告 {outcome['report']}")
        return 0
    except Failed as exc:
        print(f"\n失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
