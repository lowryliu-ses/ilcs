#!/usr/bin/env python3
"""登记模拟设备的工位，接到各自的模拟网关 / 模拟仪表：天平称量加料站、IKA 加热搅拌、拉曼光谱仪、冷水机制冷搅拌、
电化学工作站、电芯开路电压 / 内阻仪（Keithley 2450、Keithley 2400、Hioki BT3562）。

只走 HTTP，和界面调同一组接口；签名用演示账号口令逐次签署。设备侧是各设备模块的模拟容器
（`devices/gateway/<模块>/deploy/compose.yml`，接 ILCS 的后端网络）：网关模块（balance-sim、ika-stirrer-sim、raman-sim、
thermostat-sim、potentiostat-sim）ILCS 经 `http_json_v1` 走 HTTPS + 令牌接，网关自报为模拟器；电芯检测仪表是映射模块
（k2450-sim、k2400-sim、bt3562-sim），ILCS 用内置的 `line_command_v1` 直接连仪表口，另登记模拟设备的统一控制口。
和接真机是同一条路：

    docker compose -f devices/gateway/balance-dosing/deploy/compose.yml up -d --build     # 每个模块各起一组
    docker compose -f devices/gateway/ika-stirrer/deploy/compose.yml up -d --build
    docker compose -f devices/gateway/raman-seabreeze/deploy/compose.yml up -d --build
    docker compose -f devices/gateway/thermostat/deploy/compose.yml up -d --build
    docker compose -f devices/gateway/potentiostat/deploy/compose.yml up -d --build
    docker compose -f devices/gateway/scpi-cell-meter/deploy/compose.yml up -d --build
    python3 scripts/load-device-simulators.py register [--base http://127.0.0.1:8090] [--acceptance] [--only balance,chiller,…]

每台：能力（`cap.weigh`、`cap.thermostat`、`cap.echem`、`cap.cell_check` 没有就登记；`cap.ely.*` 用电解液线已有的）→
实验区 → 占位资产 → 工位（先按内置模拟登记）→
接入模板（工程师导入模块的 profile.json、QA 发布）→ 工位套用模板连到模拟网关（签名）→ 重连 → 等执行器跑完只读级验收；
`--acceptance` 再逐项能力申请动作级 + 故障项目验收（签名 + 批准说明）并等出结论。已有的先查后用，重复运行不会多建。

工位的型号取占位资产（XPE206DRQ、RCT digital、QE Pro、Unichiller 012 ……），和电解液线设备方法的适用型号不同，排程不会把电解液线的步骤
落到这几台上。正式环境（ILCS_ENVIRONMENT=production）拒绝运行。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_spec = importlib.util.spec_from_file_location("load_neware_cycler", HERE / "load-neware-cycler.py")
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)  # 复用它的 HTTP 传输、演示账号与签名、等待与输出
Actor, Failed, actors, http_transport = common.Actor, common.Failed, common.actors, common.http_transport
step, ok, note, wait_for, _items = common.step, common.ok, common.note, common.wait_for, common._items

ISLAND = {"id": 11, "name": "设备模拟联调"}
# 这几台用到、电解液线里没有的能力：没有就登记
CAPABILITIES = {
    "cap.weigh": {
        "id": "cap.weigh", "name": "称量", "params": {},
        "recovery": {"maxHoldMin": 0, "pausable": False, "retryable": True, "hold": "称量没有可保持的动作",
                     "sideEffect": "无：只读天平读数", "verify": ["秤上的容器"]},
    },
    "cap.thermostat": {
        "id": "cap.thermostat", "name": "控温", "params": {"temp": "温度", "time": "到温后保温时间"},
        "param_specs": {"temp": {"type": "number", "unit": "℃"}, "time": {"type": "number", "unit": "s"}},
        "recovery": {"maxHoldMin": 0, "pausable": False, "retryable": True, "hold": "不做保持：冷水机照最后的设定值控温",
                     "sideEffect": "重试会重新到温、重新计保温时间", "verify": ["浴温", "冷水机报警"]},
    },
    "cap.echem": {
        "id": "cap.echem", "name": "电化学测试", "params": {},
        "recovery": {"maxHoldMin": 0, "pausable": False, "retryable": True, "hold": "测量停不在半截：不做保持",
                     "sideEffect": "重测会再给电池加一次电位（EIS 小振幅、LSV 会极化到终点电位）",
                     "verify": ["接线与通道上的电池", "电池是否断开"]},
    },
    "cap.cell_check": {
        "id": "cap.cell_check", "name": "电芯开路电压 / 内阻检测", "params": {},
        "recovery": {"maxHoldMin": 0, "pausable": False, "retryable": True, "hold": "即时读数，没有可保持的动作",
                     "sideEffect": "无：源 0 A 测电压或 1 kHz 小信号测内阻", "verify": ["夹具上的电芯"]},
    },
}
SIMCTL = "/run/secrets/ilcs/simctl"
DEVICES: dict[str, dict[str, Any]] = {
    "balance": {
        "station": "ST-BAL-SIM", "name": "天平称量加料站（模拟）", "asset": "AS-BAL-SIM", "model": "XPE206DRQ",
        "vendor": "Mettler Toledo", "channels": 1, "channel_unit": "batch",
        "limits": {"cap.weigh": {}, "cap.ely.dose_solid": {"mass": [0, 20]}, "cap.ely.dose_liquid": {"mass": [0, 60]}},
        "module": "balance-dosing", "host": "balance-sim", "device_id": "SIM-BAL-DOSE-01",
        "acceptance": [("cap.weigh", {}), ("cap.ely.dose_solid", {"mass": 0.05}), ("cap.ely.dose_liquid", {"mass": 1.0})],
        "summary": "MT-SICS 天平 + Quantos 加粉 + Cavro 注射泵加液",
    },
    "stirrer": {
        "station": "ST-STIR-SIM", "name": "IKA 加热搅拌（模拟，4 位）", "asset": "AS-STIR-SIM", "model": "RCT digital",
        "vendor": "IKA", "channels": 4, "channel_unit": "sample",
        # 这几块板只能加热：温度下限是室温（25 ℃），转速下限 50 rpm（与网关配置一致）
        "limits": {"cap.ely.stir": {"temp": [25, 100], "time": [1, 7200], "rpm": [50, 1500]}},
        "module": "ika-stirrer", "host": "ika-stirrer-sim", "device_id": "SIM-IKA-STIR-01",
        "acceptance": [("cap.ely.stir", {"temp": 40, "time": 2, "rpm": 300})],
        "summary": "IKA NAMUR，4 个位置各一块板，模拟时网关自己分配位置",
    },
    "raman": {
        "station": "ST-RAM-SIM", "name": "拉曼光谱仪（模拟）", "asset": "AS-RAM-SIM", "model": "QE Pro",
        "vendor": "Ocean Insight", "channels": 1, "channel_unit": "batch",
        "limits": {"cap.ely.raman": {"repeats": [1, 5]}},
        "module": "raman-seabreeze", "host": "raman-sim", "device_id": "SIM-RAMAN-01",
        "acceptance": [("cap.ely.raman", {"repeats": 1})],
        "summary": "seabreeze 光谱仪，785 nm 外接激光，谱图按曲线回报",
    },
    "chiller": {
        "station": "ST-CHILL-SIM", "name": "冷水机制冷搅拌（模拟，4 位）", "asset": "AS-CHILL-SIM",
        "model": "Unichiller 012", "vendor": "Huber", "channels": 4, "channel_unit": "sample",
        # 温度照网关配置的 min_c–max_c；搅拌转速 0（只冷不搅）或 50–1500 rpm
        "limits": {"cap.thermostat": {"temp": [-20, 25], "time": [0, 3600]},
                   "cap.ely.stir": {"temp": [-20, 25], "time": [1, 7200], "rpm": [0, 1500]}},
        "module": "thermostat", "host": "thermostat-sim", "device_id": "SIM-CHILL-01",
        # 冷浴从室温（22 ℃）到 20 ℃ 要二三十秒（模拟的时间常数 15 s，连续 10 s 在 ±0.5 ℃ 以内算到温）
        "acceptance": [("cap.thermostat", {"temp": 20, "time": 1}), ("cap.ely.stir", {"temp": 20, "time": 2, "rpm": 300})],
        "summary": "Huber PB 命令的冷水机 + 4 块 IKA 板（只用电机），模拟时网关自己分配位置",
    },
    "echem": {
        "station": "ST-ECHEM-SIM", "name": "电化学工作站（模拟）", "asset": "AS-ECHEM-SIM", "model": "EmStat4 HR",
        "vendor": "PalmSens", "channels": 1, "channel_unit": "sample",
        "limits": {"cap.echem": {}},
        "module": "potentiostat", "host": "potentiostat-sim", "device_id": "SIM-ECHEM-01",
        "acceptance": [("cap.echem", {})],  # 缺省程序 OCP-10：开路电位 10 s，不加电位
        "summary": "PalmSens MethodSCRIPT：电导池 EIS、LSV 电化学窗口、CV、OCP、CA",
    },
    "k2450": {
        "station": "ST-OCV-SIM", "name": "电芯开路电压 Keithley 2450（模拟）", "asset": "AS-OCV-SIM", "model": "2450",
        "vendor": "Keithley", "channels": 1, "channel_unit": "sample",
        "limits": {"cap.cell_check": {}},
        "module": "scpi-cell-meter", "profile": "profile-keithley-2450.json", "simctl": "SIM-K2450-01",
        "connection": {"transport": {"kind": "tcp", "host": "k2450-sim", "port": 5025},
                       "expected_device_id": "ILCS-SIMULATOR-2450-01"},
        "control": "http://k2450-sim:9900",
        "acceptance": [("cap.cell_check", {})],
        "summary": "SCPI over LAN（line_command_v1 直接连仪表），源 0 A 四线测开路电压",
    },
    "k2400": {
        "station": "ST-OCV2-SIM", "name": "电芯开路电压 Keithley 2400（模拟）", "asset": "AS-OCV2-SIM", "model": "2400",
        "vendor": "Keithley", "channels": 1, "channel_unit": "sample",
        "limits": {"cap.cell_check": {}},
        "module": "scpi-cell-meter", "profile": "profile-keithley-2400.json", "simctl": "SIM-K2400-01",
        "connection": {"transport": {"kind": "serial", "port": "socket://k2400-sim:4001", "baudrate": 9600},
                       "expected_device_id": "ILCS-SIMULATOR-2400-01"},
        "control": "http://k2400-sim:9900",
        "acceptance": [("cap.cell_check", {})],
        "summary": "SCPI over RS-232（模拟时串口服务器换成 socket://），源 0 A 测开路电压",
    },
    "bt3562": {
        "station": "ST-ACIR-SIM", "name": "电芯交流内阻 Hioki BT3562（模拟）", "asset": "AS-ACIR-SIM", "model": "BT3562A",
        "vendor": "Hioki", "channels": 1, "channel_unit": "sample",
        "limits": {"cap.cell_check": {}},
        "module": "scpi-cell-meter", "profile": "profile-hioki-bt3562.json", "simctl": "SIM-BT3562-01",
        "connection": {"transport": {"kind": "tcp", "host": "bt3562-sim", "port": 2323},
                       "expected_device_id": "ILCS-SIMULATOR"},
        "control": "http://bt3562-sim:9900",
        "acceptance": [("cap.cell_check", {})],
        "summary": "SCPI over LAN，1 kHz 交流内阻（mΩ）+ 开路电压",
    },
}


def refuse_production() -> None:
    if os.environ.get("ILCS_ENVIRONMENT", "").strip().lower() == "production":
        raise Failed("正式环境拒绝运行：本脚本登记占位资产与模拟设备的连接")


def ensure_capabilities(engineer: Actor, needed: set[str]) -> None:
    existing = {row["id"] for row in engineer.get("/capabilities")}
    missing = sorted(needed - existing - set(CAPABILITIES))
    if missing:
        raise Failed(f"能力 {', '.join(missing)} 还没登记：先登记电解液线（scripts/load-electrolyte-line.py register）")
    for key in sorted(needed & set(CAPABILITIES) - existing):
        capability = CAPABILITIES[key]
        engineer.post("/capabilities", {**capability, "signature_id": engineer.sign("能力模型变更批准", key)})
        params = "、".join(capability["params"].values()) or "无参数"
        ok("能力", f"{key} {capability['name']}（新登记，{params}）")


def ensure_island(engineer: Actor) -> None:
    islands = {row["id"]: row["name"] for row in engineer.get("/islands")}
    if islands.get(ISLAND["id"]) != ISLAND["name"]:
        engineer.put(f"/islands/{ISLAND['id']}", {"name": ISLAND["name"]})
    ok("实验区", f"#{ISLAND['id']} {ISLAND['name']}")


def ensure_station(engineer: Actor, device: dict[str, Any]) -> None:
    stations = {row["id"] for row in engineer.get("/stations")}
    if device["station"] in stations:
        ok("工位", f"{device['station']}（沿用）")
        return
    found = [row for row in _items(engineer.get(f"/assets?keyword={device['asset']}&page_size=100"))
             if row["asset_no"] == device["asset"]]
    asset_id = found[0]["id"] if found else engineer.post("/assets", {
        "asset_no": device["asset"], "name": device["name"], "model": device["model"], "vendor": device["vendor"],
        "capacity": device["channels"], "calibration_applicable": False,
        "calibration_exempt_reason": "模拟阶段占位：接的是模拟网关，接真机前按实物登记序列号与校准",
        "note": f"模拟阶段占位资产：devices/gateway/{device['module']} 的模拟网关",
    })["id"]
    engineer.post("/stations", {
        "id": device["station"], "name": device["name"], "island": ISLAND["id"], "channels": device["channels"],
        "channel_unit": device["channel_unit"], "limits": device["limits"], "asset_id": asset_id,
        "protocol": "sim", "adapter_kind": "simulation", "adapter_driver": "simulation",
        "signature_id": engineer.sign("工程变更批准", device["station"]),
    })
    ok("工位", f"{device['station']} {device['name']}（{device['channels']} 通道，能力 {', '.join(device['limits'])}）")


def ensure_template(engineer: Actor, qa: Actor, device: dict[str, Any]) -> dict:
    path = ROOT / "devices" / "gateway" / device["module"] / device.get("profile", "profile.json")
    profile = json.loads(path.read_text(encoding="utf-8"))
    rows = [row for row in _items(engineer.get("/device-templates"))
            if row.get("code") == profile["code"] and int(row.get("revision") or 0) == int(profile["revision"])]
    template = next((row for row in rows if row.get("state") == "released"), None) or next(iter(rows), None)
    if template is not None and template.get("digest") != profile["digest"]:
        raise Failed(f"接入模板 {profile['code']} 修订 {profile['revision']} 已登记，但和 {path} 的摘要不同：改了要升修订号")
    if template is None:
        template = engineer.post("/device-templates/import", {"filename": path.name, "document": profile})
        note(f"接入模板 {profile['code']} 已导入（草稿）")
    if template["state"] == "draft":
        template = qa.post(f"/device-templates/{template['id']}/release", {
            "row_version": template["row_version"],
            "signature_id": qa.sign("发布设备接入模板", template["id"], template["row_version"]),
        })
    if template["state"] != "released":
        raise Failed(f"接入模板 {profile['code']} 状态 {template['state']}，没能发布")
    ok("接入模板", f"{profile['code']} 修订 {profile['revision']}（已发布）")
    return template


def connect(engineer: Actor, operator: Actor, template: dict, device: dict[str, Any], timeout: float) -> None:
    station = device["station"]
    if "connection" in device:  # 映射模块：直接连仪表口，另登记模拟设备的统一控制口（故障项目用）
        connection = {**device["connection"], "simulator_control": {
            "url": device["control"], "token_ref": f"file://{SIMCTL}/{device['simctl']}.token"}}
        credential = ""
        where = connection["transport"].get("host") or connection["transport"].get("port")
    else:
        device_id = device["device_id"]
        connection = {"base_url": f"https://{device['host']}:8443/api/v1",
                      "ca_file": f"/run/secrets/ilcs/gateway/{device_id}.crt", "expected_device_id": device_id}
        credential = f"file:///run/secrets/ilcs/gateway/{device_id}.token"
        where = connection["base_url"]
    adapter = engineer.get(f"/stations/{station}/adapter")
    if (adapter.get("template") or {}).get("id") != template["id"] or adapter.get("template_connection") != connection \
            or adapter.get("credential_ref") != credential:
        adapter = engineer.patch(f"/stations/{station}/adapter", {
            "template_id": template["id"], "template_connection": connection, "credential_ref": credential,
            "row_version": adapter["row_version"],
            "signature_id": engineer.sign("设备集成配置变更批准", station, adapter["row_version"]),
        })
        ok("设备连接", f"{station} 套用 {template['code']}：{where}，设备编号 {connection['expected_device_id']}（已签名保存）")
    else:
        ok("设备连接", f"{station} → {where}（沿用）")
    if (operator.get("/gate").get("blocked_stations") or {}).get(station):
        operator.post(f"/stations/{station}/adapter/reconnect")

    def accepted():
        listed = engineer.get(f"/stations/{station}/adapter/acceptance")
        gate, runs = listed["gate"], listed.get("runs") or []
        if gate["required"] == "" and gate.get("accepted_config_version") == adapter["config_version"]:
            return next((row for row in runs if row["id"] == gate.get("accepted_run_id")), {"id": gate.get("accepted_run_id")})
        latest = next((row for row in runs if row.get("config_version") == adapter["config_version"]), None)
        if latest and latest.get("state") == "done" and not latest.get("ok") and latest.get("level") == "readonly":
            raise Failed(f"{station} 只读级验收没通过：{latest.get('error') or latest.get('report_md', '')[:800]}")
        return None

    run = wait_for(f"{station} 接入验收放行", accepted, timeout=timeout)
    level = {"readonly": "只读级", "physical": "动作级"}.get(run.get("level"), run.get("level") or "")
    ok("接入验收", f"{station} 配置 v{adapter['config_version']} 已由 {run['id'][:8]}（{level}）放行"
       + ("；设备自报为模拟器，只读级即可放行" if run.get("simulator") else ""))
    wait_for(f"{station} 适配器在线", lambda: not (operator.get("/gate").get("blocked_stations") or {}).get(station),
             timeout=60)
    close_recovered_alarms(operator, station)


def physical_acceptance(engineer: Actor, device: dict[str, Any]) -> None:
    """逐项能力申请动作级 + 故障项目验收：模拟网关上真的跑一遍下发、查询、终止、丢回执、忙、联锁、失联。"""
    station = device["station"]
    for capability, params in device["acceptance"]:
        adapter = engineer.get(f"/stations/{station}/adapter")
        requested = engineer.post(f"/stations/{station}/adapter/acceptance", {
            "level": "physical", "faults": True, "capability": capability, "params": params,
            "approval": "模拟网关联调，没有真实设备与样品",
            "signature_id": engineer.sign("批准设备接入验收", station, adapter["config_version"]),
        })
        run = wait_for(f"{station} {capability} 动作级验收出结论", lambda: (
            lambda row: row if row["state"] in {"done", "failed"} else None)(
            engineer.get(f"/acceptance-runs/{requested['id']}")), timeout=900, every=5)
        states = {check["key"]: check["state"] for check in run.get("checks") or []}
        if not run.get("ok"):
            raise Failed(f"{station} {capability} 动作级验收没通过：{states}\n{run.get('report_md', '')[:2000]}")
        skipped = sorted(key for key, state in states.items() if state == "skip")
        ok("接入验收（动作级 + 故障项目）", f"{station} {capability}：{sum(1 for s in states.values() if s == 'pass')} 项通过"
           + (f"，跳过 {'、'.join(skipped)}" if skipped else ""))


def close_recovered_alarms(operator: Actor, station: str) -> None:
    """新适配器重连前执行器会报一次「适配器失联」：只关这个工位上条件已恢复的失联报警。"""
    closed = 0
    for alarm in operator.get("/alarms"):
        if (alarm.get("state") == "active" and alarm.get("source_id") == station
                and not alarm.get("condition_active") and "失联" in (alarm.get("message") or "")):
            operator.post(f"/alarms/{alarm['id']}/ack")
            operator.post(f"/alarms/{alarm['id']}/close")
            closed += 1
    if closed:
        ok("关闭已恢复的接入报警", f"{station} {closed} 条（新适配器重连前的「失联」）")


def register(team: dict[str, Actor], keys: list[str], args: argparse.Namespace) -> None:
    engineer, qa, operator = team["engineer"], team["qa"], team["operator"]
    step("能力与实验区")
    ensure_capabilities(engineer, {cap for key in keys for cap in DEVICES[key]["limits"]})
    ensure_island(engineer)
    for key in keys:
        device = DEVICES[key]
        step(f"{device['name']}：{device['summary']}")
        ensure_station(engineer, device)
        template = ensure_template(engineer, qa, device)
        connect(engineer, operator, template, device, args.acceptance_timeout)
        if args.acceptance:
            physical_acceptance(engineer, device)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="登记模拟设备的工位并接到模拟网关 / 模拟仪表")
    parser.add_argument("command", choices=("register",))
    parser.add_argument("--base", default=os.environ.get("ILCS_BASE_URL", "http://127.0.0.1:8090"))
    parser.add_argument("--only", default=",".join(DEVICES), help=f"只登记哪几台（{', '.join(DEVICES)}）")
    parser.add_argument("--acceptance", action="store_true", help="再跑动作级 + 故障项目验收（只在本机做）")
    parser.add_argument("--acceptance-timeout", type=float, default=180)
    args = parser.parse_args(argv)
    keys = [key.strip() for key in args.only.split(",") if key.strip()]
    unknown = [key for key in keys if key not in DEVICES]
    try:
        if unknown:
            raise Failed(f"不认识 {', '.join(unknown)}；可选 {', '.join(DEVICES)}")
        refuse_production()
        register(actors(http_transport(args.base)), keys, args)
        print("\n完成：" + "、".join(f"{DEVICES[key]['station']} {DEVICES[key]['name']}" for key in keys) + " 已接上模拟网关")
        return 0
    except Failed as exc:
        print(f"\n失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
