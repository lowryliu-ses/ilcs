#!/usr/bin/env python3
"""ProtoForge 联调线全流程：三台 ProtoForge 模拟设备经驱动宿主接入（只走 SiLA 2），按 SOP → 能力 / 设备方法 → 流程 →
多温度矩阵方案 → 实验任务 → 排程 → 批次执行 → 数据复核 → 报告跑一遍。

    python3 scripts/load-driver-host-devices.py register            # 先把三台设备经驱动宿主接进来（见 ilcs-devices/host/README.md）
    python3 scripts/load-protoforge-flow.py register [--base http://127.0.0.1:8090]
    python3 scripts/load-protoforge-flow.py run [--temps 40,60,80] [--repeats 1]

两台设备参与温度设定，每个样本一个温度（方案的设计点），一次只做一个设定的设备由驱动按样本依次执行：
- ST-PF-OPCUA（OPC UA 温控 / 压力节点）：「温控器设定温度」（cap.tc_setpoint）写温度节点、回读；
- ST-PF-HTTP（HTTP REST 设备）：「环境箱设定温度」（cap.chamber_setpoint）POST 写温度、回读。
ST-PF-MB（Modbus TCP 从站 1 的温湿度传感器，只读写点位）是实验区的环境传感器：温度、相对湿度记成「设备模拟联调区」的读数；
OPC UA 节点的压力记成它自己（ST-PF-OPCUA）的读数。流程步骤写了环境要求，开跑检查与设备步骤下发前都按最新读数核对
（两台设定设备的温度会被设定步骤改写，不当环境温度）。ST-PF-MB 原来是从站 2 的握手 PLC（cap.plc_run），ProtoForge 里已删掉，
流程里没有 PLC 那一步了。

- register：温湿度传感器登记环境采集（仍只读写点位）、两台设定设备接成参与自动流程并登记环境采集（签名保存）；设备服务报了
  新的驱动配置就签名批准；欠验收的申请动作级验收并等放行；两项能力、两个检测指标、两个设备方法（工程师起草、QA 发布）、
  操作员资质、SOP-PF-01（研究员起草、QA 批准发布、操作员阅读确认）、流程（研究员起草、关联 SOP、QA 批准发布；原来带 PLC
  那一步的流程出修订版取代）。已有的先查后用，重复运行不会多建。
- run：先照 register 补齐，再 矩阵方案（两个设定温度因子按设计点对齐，每个样本一个温度，QA 批准）→ 实验任务 → 批次 →
  排程 → 开跑检查（含环境核对）→ 签名下发 → 人工节点 → 两个设备步骤（执行器经驱动宿主下发，驱动按样本依次执行）→
  QA 审核节点 → 逐样本列出设计温度与两台设备的回读 → 结果复核 → 报告发布。要求执行器在跑、三台设备在线。

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
SENSOR, TC, CHAMBER = "ST-PF-MB", "ST-PF-OPCUA", "ST-PF-HTTP"
AMBIENT = "设备模拟联调区"
OPERATOR = "P-003"
SAMPLE_TYPE = "联调样品"
TEMP_RANGE = (0, 100)
RECOVERY = {"maxHoldMin": 0, "pausable": False, "retryable": True, "hold": "设定类动作没有保持",
            "sideEffect": "重试会再写一次设定值", "verify": ["设定回读"]}
# 两个设定温度的设备：能力、工位、方法、输出 → 指标。步骤编号沿用原来的（s03 是删掉的「PLC 控温运行」，不复用：
# 出修订版时同一个编号还指同一步）
DEVICES = {
    "tc": {"station": TC, "capability": "cap.tc_setpoint", "capability_name": "温控器设定温度", "step": "s04",
           "step_name": "温控器设定温度", "factor": "温控器设定温度",
           "method": "ProtoForge 温控器设定温度", "dur_min": 1,
           "method_note": "OPC UA 温控节点：写 Temperature、回读；只写设定值，没有启动信号；高报为真时判故障",
           "outputs": {"temp": "pf_tc_temp"}},
    "chamber": {"station": CHAMBER, "capability": "cap.chamber_setpoint", "capability_name": "环境箱设定温度", "step": "s05",
                "step_name": "环境箱设定温度", "factor": "环境箱设定温度",
                "method": "ProtoForge 环境箱设定温度", "dur_min": 1,
                "method_note": "HTTP REST：POST /temperature 写设定、按点表读回温度；状态 normal 即空闲",
                "outputs": {"temp": "pf_chamber_temp"}},
}
METRICS = {"pf_tc_temp": ("温控器回读温度", "℃"), "pf_chamber_temp": ("环境箱回读温度", "℃")}
# 三台设备登记占位资产（模拟设备，校准不适用）：设备步骤要核对工位的资产（校准与容量）。已经关联了资产的工位沿用，
# 新环境里（ST-PF-MB 由 load-driver-host-devices.py 新建）照这里补
ASSETS = {
    SENSOR: {"asset_no": "AS-PF-MB", "name": "ProtoForge 温湿度传感器（Modbus TCP）",
             "model": "ProtoForge Modbus 温湿度传感器",
             "note": "ProtoForge 从站 1 的温湿度传感器，经驱动宿主 PF-MB-PLC 接入（只读写点位）"},
    TC: {"asset_no": "AS-PF-OPCUA", "name": "ProtoForge 温控 / 压力节点（OPC UA）", "model": "ProtoForge OPC UA",
         "note": "ProtoForge OPC UA 设备（Pressure / Temperature / Setpoint 节点），经驱动宿主 PF-OPCUA 接入"},
    CHAMBER: {"asset_no": "AS-PF-HTTP", "name": "ProtoForge 环境箱（HTTP REST）", "model": "ProtoForge HTTP REST",
              "note": "ProtoForge HTTP 设备 http-rest，经驱动宿主 PF-HTTP 接入"},
}
# 原来是握手 PLC 时的占位资产：还是这个型号就改成温湿度传感器（人改过的不动）
LEGACY_ASSET_MODELS = {SENSOR: "ProtoForge Modbus PLC"}
# 环境采集（连接配置 environment）：温湿度传感器给实验区的温度、湿度，OPC UA 节点给它自己的压力；
# 两台设定设备的温度会被设定步骤改写，不当环境温度。HTTP 设备不再兼作湿度传感器（实验区湿度归温湿度传感器）
SENSORS = {
    SENSOR: {"zone": AMBIENT, "interval_sec": 30, "points": {"temperature": "temperature", "humidity": "humidity"}},
    TC: {"zone": TC, "interval_sec": 30, "points": {"pressure": "pressure"}},
}
AMBIENT_REQUIREMENTS = [{"metric": "temperature", "min": 15, "max": 35, "zone": AMBIENT},
                        {"metric": "humidity", "max": 80, "zone": AMBIENT}]
PROCESS_REQUIREMENTS = [{"metric": "pressure", "min": 0.5, "max": 10, "zone": TC}]
RECIPE_NAME = "ProtoForge 多温度矩阵控温联调"
# 原来带 PLC 那一步的流程叫这个名字：找到已发布的就出修订版取代它
FORMER_RECIPE_NAMES = ("ProtoForge PLC 控温运行联调",)
RISK = "RA-PF-01 v4（联调占位：ProtoForge 模拟设备，无真实样品、加热体与压力容器）"
RECIPE_DESIGN = ("两台 ProtoForge 设备经驱动宿主接入、参与温度设定：OPC UA 温控器与 HTTP 环境箱写设定温度并回读；每个样本"
                 "一个温度（方案设计点），设备按样本依次执行。Modbus 温湿度传感器给实验区的温度、湿度，OPC UA 节点给压力，"
                 "步骤环境要求核对；QA 复核")


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


# ---------------------------------------------------------------- 设备：能力、接入、闸门、环境采集


def register_capabilities(engineer: Actor) -> None:
    """三项能力（参数 temp ℃）。新登记的连同工位一起登记，工位的范围缺省 0–100 ℃。"""
    existing = {row["id"]: row for row in engineer.get("/capabilities")}
    for device in DEVICES.values():
        capability = device["capability"]
        current = existing.get(capability)
        if current is None:
            engineer.post("/capabilities", {
                "id": capability, "name": device["capability_name"], "params": {"temp": "设定温度"},
                "param_specs": {"temp": {"type": "number", "unit": "℃"}}, "recovery": RECOVERY,
                "stations": [device["station"]], "signature_id": engineer.sign("能力模型变更批准", capability),
            })
            ok("能力", f"{capability} {device['capability_name']}（新登记，落在 {device['station']}）")
            continue
        if "temp" not in (current.get("params") or {}):
            raise Failed(f"能力 {capability} 已存在但没有参数 temp")
        if device["station"] not in (current.get("stations") or []):
            raise Failed(f"能力 {capability} 已存在但 {device['station']} 没有声明实现它：在能力字典里加上这台工位")
        ok("能力", f"{capability} {current['name']}（沿用）")
    stations = {row["id"]: row for row in engineer.get("/stations")}
    for device in DEVICES.values():
        window = ((stations[device["station"]].get("limits") or {}).get(device["capability"]) or {}).get("temp")
        if not window or float(window[0]) > TEMP_RANGE[0] or float(window[1]) < TEMP_RANGE[1]:
            raise Failed(f"{device['station']} 的 {device['capability']} 温度范围是 {window}，要覆盖 {TEMP_RANGE}")


def register_assets(engineer: Actor) -> None:
    """三台设备的工位关联占位资产（开跑检查按资产核对校准与容量）：模拟设备，校准不适用并写明豁免理由。"""
    stations = {row["id"]: row for row in engineer.get("/stations")}
    for station_id, spec in ASSETS.items():
        station = stations[station_id]
        if station.get("asset_id"):
            asset = engineer.get(f"/assets/{station['asset_id']}")
            if asset.get("asset_no") == spec["asset_no"] and asset.get("model") == LEGACY_ASSET_MODELS.get(station_id):
                engineer.patch(f"/assets/{asset['id']}", {key: spec[key] for key in ("name", "model", "note")}
                               | {"row_version": asset["row_version"]})
                ok("资产", f"{station_id} 关联的 {spec['asset_no']} 改成 {spec['name']}（{spec['model']}）")
            else:
                ok("资产", f"{station_id}（沿用已关联的资产）")
            continue
        found = [row for row in _items(engineer.get(f"/assets?keyword={spec['asset_no']}&page_size=100"))
                 if row["asset_no"] == spec["asset_no"]]
        asset_id = found[0]["id"] if found else engineer.post("/assets", {
            **spec, "vendor": "ProtoForge", "capacity": 1, "calibration_applicable": False,
            "calibration_exempt_reason": "ProtoForge 模拟设备，联调占位；接真机前按实物登记序列号与校准",
        })["id"]
        engineer.patch(f"/stations/{station_id}", {"asset_id": asset_id, "row_version": station["row_version"]})
        ok("资产", f"{station_id} 关联 {spec['asset_no']} {spec['name']}（校准不适用：模拟设备）")


def configure_stations(engineer: Actor) -> None:
    """两台设定设备参与自动流程（去掉 tasks: false），温湿度传感器仍只读写点位；登记环境采集的登记上、不再采集的去掉。
    改了就签名保存（改 tasks 要重新握手、欠动作级验收）。"""
    for station_id in (SENSOR, TC, CHAMBER):
        adapter = engineer.get(f"/stations/{station_id}/adapter")
        if adapter.get("kind") != "real":
            raise Failed(f"{station_id} 还没接成真实设备：先跑 scripts/load-driver-host-devices.py register")
        config = dict(adapter.get("config") or {})
        acting = station_id != SENSOR
        wanted = {key: value for key, value in config.items() if key not in {"environment", *(("tasks",) if acting else ())}}
        if not acting and wanted.get("tasks") is not False:
            raise Failed(f"{station_id} 应只读写点位（tasks: false）：先跑 scripts/load-driver-host-devices.py register --only {station_id}")
        if station_id in SENSORS:
            wanted["environment"] = SENSORS[station_id]
        role = "参与自动流程" if acting else "只读写点位"
        sampling = (f"环境采集 {SENSORS[station_id]['zone']}：{'、'.join(SENSORS[station_id]['points'])}"
                    if station_id in SENSORS else "不采环境读数")
        if wanted == config:
            ok("设备连接", f"{station_id}：{role}，{sampling}（沿用）")
            continue
        engineer.patch(f"/stations/{station_id}/adapter", {
            "config": wanted, "row_version": adapter["row_version"],
            "signature_id": engineer.sign("设备集成配置变更批准", station_id, adapter["row_version"]),
        })
        ok("设备连接", f"{station_id}：{role}，{sampling}（已签名保存）")


def clear_gates(engineer: Actor, timeout: float) -> None:
    """三台设备的接入闸门：设备服务报了新的驱动配置就签名批准（随后只读级验收）；两台设定设备欠动作级的按自己的能力
    申请动作级验收（温湿度传感器只读写点位，只欠只读级）。"""
    for station_id, device in [(SENSOR, None), *((row["station"], row) for row in DEVICES.values())]:

        def adapter():
            return engineer.get(f"/stations/{station_id}/adapter")

        current = wait_for(f"{station_id} 握上手", lambda: (lambda row: row if row["connected"] or row["driver_awaiting_approval"]
                                                         or row["acceptance"]["required"] else None)(adapter()), timeout)
        if current.get("driver_awaiting_approval"):
            reported = current.get("driver_info") or {}
            engineer.post(f"/stations/{station_id}/adapter/driver-approval", {
                "reason": f"核对了驱动项目里的改动（{reported.get('plugin')} 配置 {reported.get('config_version')}），批准这一份",
                "signature_id": engineer.sign("批准驱动配置变更", station_id, current["config_version"]),
            })
            ok("批准驱动配置变更", f"{station_id} {reported.get('plugin')} {str(reported.get('config_digest') or '')[:19]}")
        current = wait_for(f"{station_id} 只读级验收出结论", lambda: (lambda row: row if row["acceptance"]["required"] != "readonly"
                                                                  and not row["driver_awaiting_approval"] else None)(adapter()),
                           timeout)
        if current["acceptance"]["required"] == "physical" and device:
            requested = engineer.post(f"/stations/{station_id}/adapter/acceptance", {
                "level": "physical", "faults": False, "capability": device["capability"], "params": {"temp": 45},
                "approval": "ProtoForge 模拟设备，经驱动宿主接入；没有真实设备与样品",
                "signature_id": engineer.sign("批准设备接入验收", station_id, current["config_version"]),
            })
            run = wait_for(f"{station_id} 动作级验收出结论", lambda: (lambda row: row if row["state"] in {"done", "error", "cancelled"}
                                                                   else None)(engineer.get(f"/acceptance-runs/{requested['id']}")),
                           timeout, every=2)
            if not run.get("ok"):
                states = {check["key"]: check["state"] for check in run.get("checks") or []}
                raise Failed(f"{station_id} 动作级验收没通过：{states}\n{run.get('report_md', '')[:1500]}")
            ok("接入验收（动作级）", f"{station_id} {device['capability']} 通过")
        current = adapter()
        if current["acceptance"]["required"]:
            raise Failed(f"{station_id} 还欠 {current['acceptance']['required_label']}：{current['acceptance']['reason']}")
        approved = (current.get("approved_driver") or {}).get("config_digest", "")
        ok("接入闸门", f"{station_id} 已放行；批准的驱动配置 {str(approved)[:19]}")


def wait_environment(engineer: Actor, timeout: float) -> None:
    wanted = {(spec["zone"], metric) for spec in SENSORS.values() for metric in spec["points"]}

    def sampled():
        latest = {(row["zone"], row["metric"]): row for row in engineer.get("/environment/readings")
                  if str(row.get("source") or "").startswith("device:") and not row.get("stale")}
        return latest if wanted <= set(latest) else None

    latest = wait_for("执行器记下传感器的环境读数", sampled, timeout=timeout)
    ok("环境读数", "；".join(f"{zone} {row['metric_label']} {row['value']:g}{row['unit']}"
                            for (zone, metric), row in sorted(latest.items()) if (zone, metric) in wanted))


# ---------------------------------------------------------------- 指标、方法、资质


def register_metrics(researcher: Actor) -> dict[str, str]:
    existing = {(row["code"], row.get("version")): row for row in _items(researcher.get("/metrics?page_size=200"))}
    ids = {}
    for code, (name, unit) in METRICS.items():
        row = existing.get((code, "v1")) or researcher.post("/metrics", {
            "code": code, "name": name, "unit": unit, "value_type": "number", "sample_types": [SAMPLE_TYPE],
            "rules": {"min": 0, "max": 150}})
        ids[code] = row["id"]
    ok("检测指标", "、".join(f"{name} {unit}" for name, unit in METRICS.values()))
    return ids


def _outputs(device: dict, metrics: dict[str, str]) -> list[dict]:
    return [{"key": key, "label": METRICS[code][0], "unit": METRICS[code][1], "lo": 0, "required": True,
             "metric_id": metrics[code]} for key, code in device["outputs"].items()]


def _same_outputs(current: list[dict], wanted: list[dict]) -> bool:
    return sorted((row.get("key"), row.get("metric_id")) for row in current or []) == \
        sorted((row["key"], row["metric_id"]) for row in wanted)


def register_method(engineer: Actor, qa: Actor, device: dict, metrics: dict[str, str]) -> str:
    """工程师起草、QA 发布：输出项关联指标（设备回报写成检测结果）。已发布的输出项不一致就出修订版。"""
    capability, outputs = device["capability"], _outputs(device, metrics)
    released = [row for row in engineer.get(f"/device-methods?state=released&capability_id={capability}")
                if row["name"] == device["method"]]
    if released and _same_outputs(engineer.get(f"/device-methods/{released[0]['id']}").get("outputs"), outputs):
        ok("设备方法", f"{released[0]['code']} v{released[0]['version']} {device['method']}（已发布，沿用）")
        return released[0]["id"]
    drafts = [row for row in engineer.get(f"/device-methods?state=draft&capability_id={capability}")
              if row["name"] == device["method"]]
    if drafts:
        draft = drafts[0]
    elif released:
        draft = engineer.post(f"/device-methods/{released[0]['id']}/revise")
    else:
        draft = engineer.post("/device-methods", {
            "name": device["method"], "capability_id": capability, "program": "", "params": {}, "outputs": outputs,
            "dur_min": device["dur_min"], "note": device["method_note"]})
    if not _same_outputs(draft.get("outputs"), outputs):
        draft = engineer.patch(f"/device-methods/{draft['id']}", {"outputs": outputs, "note": device["method_note"],
                                                                  "row_version": draft["row_version"]})
    done = qa.post(f"/device-methods/{draft['id']}/release", {"row_version": draft["row_version"]})
    ok("设备方法已发布", f"{done['code']} v{done['version']} {done['name']}：回报 "
                        + "、".join(METRICS[code][0] for code in device["outputs"].values()))
    return done["id"]


def grant_qualifications(admin: Actor) -> None:
    capabilities = {device["capability"]: device["capability_name"] for device in DEVICES.values()}
    for label, code in (("操作员资质", OPERATOR), ("管理员资质", common.ADMIN_PERSON)):
        common.grant_capabilities(admin, code, capabilities)
        ok(label, f"{code} 有 " + "、".join(capabilities))


# ---------------------------------------------------------------- 流程


def recipe_steps(methods: dict[str, str]) -> list[dict]:
    steps = [
        {"step_id": "s01", "kind": "manual", "name": "核对环境与设备在线", "dur": 2, "requires_signature": False,
         "environment": AMBIENT_REQUIREMENTS,
         "form": [{"key": "devices_online", "label": "ST-PF-MB、ST-PF-OPCUA、ST-PF-HTTP 在线", "type": "bool",
                   "required": True}]},
        {"step_id": "s02", "kind": "manual", "name": "装样并核对各样本设定温度", "dur": 2, "requires_signature": False,
         "requires_sample_check": True,
         "form": [{"key": "setpoint_ok", "label": "已按方案核对每个样本的设定温度", "type": "bool", "required": True}]},
    ]
    for key, device in DEVICES.items():
        steps.append({
            "step_id": device["step"], "kind": "device", "name": device["step_name"], "cap": device["capability"],
            "params": {"temp": 60}, "dur": device["dur_min"], "method": {"id": methods[key]},
            "environment": [*PROCESS_REQUIREMENTS, *AMBIENT_REQUIREMENTS] if key == "tc" else AMBIENT_REQUIREMENTS,
        })
    steps.append({"step_id": "s06", "kind": "review", "name": "QA 复核运行数据", "review_role": "qa"})
    return steps


def _sop_steps(sop_id: str, researcher: Actor) -> dict[str, str]:
    """SOP 结构化步骤：标题 → 稳定标识（流程节点按它引用 SOP 的那一步）。"""
    detail = researcher.get(f"/sops/{sop_id}")
    return {row["title"]: row["key"] for row in detail.get("steps") or [] if row.get("key")}


def release_recipe(researcher: Actor, qa: Actor, methods: dict[str, str], sop: dict) -> str:
    """同名已发布的流程（步骤与方法、风险评估、SOP 都一致）沿用；不一致出修订版；没有就新建。研究员起草，QA 批准发布。"""
    steps = recipe_steps(methods)
    keys = _sop_steps(sop["id"], researcher)
    for index, row in enumerate(steps, start=1):
        if keys.get(row["name"]):  # 节点记 SOP 步骤的稳定标识（批次页据此把 SOP 说明带到执行人面前）与序号
            row["sop_step_key"], row["sop_step"] = keys[row["name"]], index

    def shape(rows):
        return [(row.get("step_id"), row.get("kind"), (row.get("method") or {}).get("id")) for row in rows]

    names = (RECIPE_NAME, *FORMER_RECIPE_NAMES)
    released = [row for row in _items(researcher.get("/recipes")) if row.get("name") in names and row.get("state") == "released"]
    for row in released:
        detail = researcher.get(f"/recipes/{row['id']}")
        if row.get("name") == RECIPE_NAME and shape(detail.get("steps") or []) == shape(steps) \
                and detail.get("risk") == RISK and (detail.get("sop_version_id") or "") == sop["id"]:
            ok("流程", f"{row['id']} {RECIPE_NAME}（已发布，沿用）")
            return row["id"]
    if released:
        draft = researcher.post(f"/recipes/{released[0]['id']}/revision")
        note(f"已发布的 {released[0]['id']} 和这里的定义不同：出修订版 {draft['id']}")
    else:
        draft = researcher.post("/recipes", {"name": RECIPE_NAME, "plate": 8})
    current = researcher.get(f"/recipes/{draft['id']}")
    researcher.patch(f"/recipes/{draft['id']}", {
        "name": RECIPE_NAME, "steps": steps, "bom": [], "design": RECIPE_DESIGN, "risk": RISK, "sop_version_id": sop["id"],
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
    ok("流程已发布", f"{draft['id']} {RECIPE_NAME}（核对环境 → 装样 → 温控器设定 → 环境箱设定 → QA 复核，"
                    f"关联 {sop['code']} {sop['version']}）"
       + (f"；原版 {released[0]['id']} 随之退役" if released else ""))
    return draft["id"]


def register(team: dict[str, Actor], args: argparse.Namespace) -> dict:
    engineer, qa, operator, researcher, admin = (team[k] for k in ("engineer", "qa", "operator", "researcher", "admin"))
    step("设备：能力、接入、闸门、环境采集")
    register_capabilities(engineer)
    register_assets(engineer)
    configure_stations(engineer)
    clear_gates(engineer, args.timeout)
    wait_environment(engineer, args.timeout)
    step("指标、设备方法、资质")
    metrics = register_metrics(researcher)
    methods = {key: register_method(engineer, qa, device, metrics) for key, device in DEVICES.items()}
    grant_qualifications(admin)
    step("SOP 与流程")
    sop = common.register_sop(researcher, qa, operator, SOP)
    recipe_id = release_recipe(researcher, qa, methods, sop)
    return {"metrics": metrics, "methods": methods, "sop": sop, "recipe": recipe_id}


# ---------------------------------------------------------------- run


def approve_plan(researcher: Actor, qa: Actor, recipe_id: str, metrics: dict[str, str], temps: list[float],
                 repeats: int, name: str) -> str:
    """多温度矩阵：两个设定温度因子分别落到两个设备步骤，设计点把它们对齐成「每个样本一个温度」。"""
    factors = [{"name": device["factor"], "unit": "℃", "levels": temps,
                "target": {"step_id": device["step"], "param": "temp"}} for device in DEVICES.values()]
    plan = researcher.post("/plans", {
        "name": name, "recipe_id": recipe_id, "plan_type": "matrix", "repeats": repeats, "layout": "sequential",
        "factors": factors, "design_points": [[temp] * len(factors) for temp in temps],
        "goal": (f"{len(temps)} 个设定温度（{'、'.join(f'{t:g}' for t in temps)} ℃）× {repeats} 次重复：两台 ProtoForge 设备经驱动宿主"
                 "按样本依次设定温度并回读，温湿度传感器给实验区读数，验证矩阵条件下发、逐样本执行、环境核对、复核与报告全链路"
                 "（模拟设备）"),
        "required_metrics": list(metrics.values()),
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
    ok("方案已批准", f"{plan_id} {name}（矩阵：{len(temps)} 个设计点 × {repeats} 次重复 = {len(temps) * repeats} 个样品）")
    return plan_id


def create_task(researcher: Actor, operator: Actor, plan_id: str, title: str) -> str:
    task = researcher.post("/experiment-tasks", {"plan_id": plan_id, "title": title, "priority": 2,
                                                  "note": "ProtoForge 联调线全流程（多温度矩阵）"})
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
               for row in scheduled.get("allocations") or [] if row.get("kind") == "work"]
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
    """推到批次结束：人工节点由操作员提交，审核节点由 QA 批准；设备步骤由执行器经驱动宿主下发、驱动按样本依次执行。"""
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
                form = {"devices_online": True} if "核对环境" in run["step_name"] else {"setpoint_ok": True}
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
    return detail


def show_wells(detail: dict) -> None:
    """逐样本：方案给的设计温度、两台设备回报的值（设备按样本依次执行，回执按孔位）。"""
    steps = {device["step"]: key for key, device in DEVICES.items()}
    snapshot_steps = (detail.get("snapshot") or {}).get("steps") or []
    by_step: dict[str, dict] = {}
    for checkpoint in detail.get("checkpoints") or []:
        index = checkpoint["step_index"]
        step_id = snapshot_steps[index].get("step_id") if index < len(snapshot_steps) else ""
        wells = ((checkpoint.get("payload") or {}).get("delivered") or {}).get("wells") or {}
        if step_id in steps and wells:
            by_step[steps[step_id]] = wells
    step("逐样本回报（设计温度 → 温控器回读 · 环境箱回读）")
    for sample in sorted(detail["samples"], key=lambda row: row["position"]):
        well = sample["well"]
        tc = by_step.get("tc", {}).get(well, {})
        chamber = by_step.get("chamber", {}).get(well, {})
        levels = sample.get("levels") or []
        designed = f"{levels[0]:g} ℃" if levels else "—"
        ok(f"{well} {sample['id']}", f"设计 {designed} → 温控器 {tc.get('temp', '—')} · 环境箱 {chamber.get('temp', '—')}")


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
            "reason": "ProtoForge 模拟设备回报的值，复核只为验证接入、矩阵下发与数据链路",
            "signature_id": qa.sign("数据复核通过", row["id"], row["result_version"])})
    fresh = device_results(researcher, detail)
    ok("设备回报结果已复核", f"{len(fresh)} 条（{len(detail['samples'])} 个样品 × {len(METRICS)} 个指标）")
    return fresh


def publish_report(researcher: Actor, qa: Actor, batch_id: str, detail: dict, results: list[dict], temps: list[float]) -> str:
    conclusion = (
        f"批次 {batch_id} 按 {RECIPE_NAME}（多温度矩阵）完成 {len(detail['samples'])} 个联调样品：设定温度 "
        f"{'、'.join(f'{t:g}' for t in temps)} ℃，每个样品一个温度；ST-PF-OPCUA（温控器）、ST-PF-HTTP（环境箱）经驱动宿主以 "
        "SiLA 2 接入，驱动按样本依次执行并按样本回报；开跑检查与下发前按 ST-PF-MB（温湿度传感器）的实验区温度、湿度与 "
        f"ST-PF-OPCUA 的压力读数核对了环境要求；{len(results)} 条设备回报已复核。模拟设备：数值不是实测，"
        "只用于验证 SOP、流程、矩阵方案、任务、排程、执行、复核与报告链路。")
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
    blocked = {station: reason for station, reason in (gate.get("blocked_stations") or {}).items()
               if station in {device["station"] for device in DEVICES.values()}}
    if not gate["open"] or blocked:
        raise Failed(f"执行门没开或设备被挡：{gate.get('reasons')} {blocked}")
    temps = args.temps
    name = args.plan_name or f"ProtoForge 联调：{'/'.join(f'{t:g}' for t in temps)} ℃ 多温度矩阵 × {args.repeats}"
    step("方案与实验任务")
    plan_id = approve_plan(researcher, qa, context["recipe"], context["metrics"], temps, args.repeats, name)
    task_id = create_task(researcher, operator, plan_id, name)
    step("排程与下发")
    batch_id = launch(operator, plan_id, task_id, f"ProtoForge 联调：{len(temps) * args.repeats} 个样品多温度矩阵")
    step("执行")
    detail = drive(operator, qa, batch_id, args.timeout)
    show_wells(detail)
    step("数据复核与报告")
    results = review_results(qa, researcher, detail)
    report_id = publish_report(researcher, qa, batch_id, detail, results, temps)
    return {"plan": plan_id, "task": task_id, "batch": batch_id, "report": report_id}


def _temps(text: str) -> list[float]:
    values = [float(part) for part in text.split(",") if part.strip()]
    if not 2 <= len(values) <= 8 or len(set(values)) != len(values):
        raise argparse.ArgumentTypeError("给 2–8 个互不相同的温度，逗号分隔")
    if any(not TEMP_RANGE[0] <= value <= TEMP_RANGE[1] for value in values):
        raise argparse.ArgumentTypeError(f"温度要在 {TEMP_RANGE[0]}–{TEMP_RANGE[1]} ℃")
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ProtoForge 联调线全流程（经驱动宿主、只走 SiLA 2，多温度矩阵）")
    parser.add_argument("command", choices=("register", "run"))
    parser.add_argument("--base", default=os.environ.get("ILCS_BASE_URL", "http://127.0.0.1:8090"))
    parser.add_argument("--temps", type=_temps, default=[40.0, 60.0, 80.0], help="设计温度 ℃，逗号分隔（2–8 个）")
    parser.add_argument("--repeats", type=int, default=1, help="每个温度的重复次数（1–2）")
    parser.add_argument("--plan-name", default="")
    parser.add_argument("--timeout", type=float, default=600, help="等握手、验收、读数、批次完成的秒数")
    args = parser.parse_args(argv)
    try:
        refuse_production()
        if not 1 <= args.repeats <= 2 or len(args.temps) * args.repeats > 8:
            raise Failed("样品数（温度个数 × 重复次数）不能超过 8，重复次数 1–2")
        team = actors(http_transport(args.base))
        context = register(team, args)
        if args.command == "register":
            print(f"\n完成：SOP {context['sop']['code']} {context['sop']['version']} · 流程 {context['recipe']}")
            return 0
        outcome = run(team, context, args)
        print(f"\n完成：方案 {outcome['plan']} · 任务 {outcome['task']} · 批次 {outcome['batch']} · 报告 {outcome['report']}")
        return 0
    except Failed as exc:
        print(f"\n失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
