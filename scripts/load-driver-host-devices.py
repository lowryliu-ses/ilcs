#!/usr/bin/env python3
"""本机经驱动宿主（ilcs-devices/host，现场 ilcs-devices/host/sites/local）接进 ILCS 的设备：ILCS 只经 SiLA 2 读写点位、下发作业。

    ILCS_HOST_SITE=$PWD/data/driver-host/site docker compose -f ../ilcs-devices/deploy/compose.yml up -d   # 驱动宿主（ilcs-devices 组）
    python3 scripts/load-driver-host-devices.py register [--base http://127.0.0.1:8090] [--acceptance] [--only 工位,…]

- ST-PF-MB（ProtoForge 从站 1 的温湿度传感器）：`sila2_v1` 接驱动宿主上的 PF-MB-PLC（50201，设备文件 r2 起只读写点位：
  温度、湿度与两个报警点，没有 TaskExecution），`"tasks": false`，不声明查询、去重。它原来是从站 2 的握手 PLC（能力
  cap.plc_run）：工位还叫原来的名字就改成传感器的名字，工位上还登记着能力就移除（签名），驱动配置摘要变了就签名批准。
- ST-PF-OPCUA（ProtoForge OPC UA 压力传感器）、ST-PF-HTTP（ProtoForge HTTP REST 传感器）：没有就登记（只读写点位，
  不登记能力），`sila2_v1` 接 PF-OPCUA（50202）/ PF-HTTP（50203），`"tasks": false`。HTTP 传感器要 ProtoForge 接进
  ILCS 的后端网络（`docker network connect ilcs_backend protoforge`），驱动宿主才连得到它的 8080。
- 只读写点位（`"tasks": false`）的工位不承接能力：已登记的能力极限签名移除，排程不会再往它上面排。ProtoForge 全流程
  （load-protoforge-flow.py）之后再把 ST-PF-OPCUA、ST-PF-HTTP 改成参与自动流程、给三台登记环境采集：重跑本脚本会把它们
  改回这里的连接配置，之后要再跑一次它的 register。

- ST-OCV-SIM、ST-OCV2-SIM、ST-ACIR-SIM（模拟电芯检测仪表 ilcs-devices/simulators/scpi_meter，映射照抄驱动宿主的设备配置模板
  ilcs-devices/host/profiles/scpi-cell-meter）：工位已有，原来是
  `line_command_v1` 直连模拟仪表（接入模板 TPL-SCPI-*），改成 `sila2_v1` 接驱动宿主上的 OCV-K2450（50211）/ OCV-K2400
  （50212）/ ACIR-BT3562（50213），映射照抄模板、搬到驱动宿主；模拟设备控制口（simulator_control）留在 ILCS 的连接配置里，
  验收的故障项目照做。`--acceptance` 跑动作级 + 故障项目（cap.cell_check）。

- 设备模块的模拟网关（ilcs-devices/gateway/<模块>，ILCS 网关契约）：ST-BAL-SIM、ST-STIR-SIM、ST-RAM-SIM、ST-CHILL-SIM、
  ST-ECHEM-SIM（scripts/load-device-simulators.py）、ST-NW-01（load-neware-cycler.py）、EL-D-BAL、EL-D-PWD、EL-T-RAM
  （load-electrolyte-line.py connect）、EL-ALAB（A-Lab 上位机的网关，load-electrolyte-line.py connect --gateways
  gateways-alab.json）原来是 `http_json_v1` 直连网关（套用接入模板），改成 `sila2_v1` 接驱动宿主上的
  GW-*（50231–50240，驱动宿主的 http_json 插件，设备文件在 ilcs-devices/host/sites/local/devices）。网关照旧在原来的容器里跑；模拟设备控制口指向网关自己的
  API（HTTPS + 网关令牌），验收的故障项目照做；模板里给 ILCS 用的 `wells_per_command`（一条指令最多几个样本）照模块的
  profile.json 写进连接配置。连上后重读设备自报的方法目录（排程只往报过这个程序的工位排）。驱动宿主上改了设备文件
  （配置摘要变了）再跑一次：签名批准新的驱动配置，等只读级验收放行。
  `--acceptance` 逐项能力跑动作级 + 故障项目。

都用驱动宿主的自签证书（`secrets/host/driver-host.crt`）和给 ILCS 的令牌（`secrets/host/ilcs.token`），等执行器跑完只读级验收
——首次接入的验收通过时，同时批准设备服务报的这一份驱动配置。只走 HTTP，签名用演示账号口令逐次签署；已经接好的不重复改。
正式环境（ILCS_ENVIRONMENT=production）拒绝运行。
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
# 设备仓库（设备模块的 profile.json 在它的 gateway/<模块>/ 下）：环境变量 ILCS_DEVICES，缺省是 ILCS 旁边的 ../ilcs-devices
DEVICES_REPO = Path(os.environ.get("ILCS_DEVICES") or HERE.parent.parent / "ilcs-devices")
# 网关模板里给 ILCS 自己用的连接配置键：换成经驱动宿主接以后照样写进工位的 sila2_v1 配置（例如一条指令最多几个样本）
ILCS_KEYS = ("wells_per_command",)
_spec = importlib.util.spec_from_file_location("load_neware_cycler", HERE / "load-neware-cycler.py")
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)  # 复用它的 HTTP 传输、演示账号与签名、等待与输出
Actor, Failed, actors, http_transport = common.Actor, common.Failed, common.actors, common.http_transport
step, ok, note, wait_for = common.step, common.ok, common.note, common.wait_for

HOST = "driver-host"
SECRETS = "/run/secrets/ilcs/host"
STATIONS = {
    "ST-PF-MB": {
        # 驱动宿主上的设备文件（PF-MB-PLC r2）只读写点位：没有 TaskExecution，查询、去重也不声明（只读级验收就不去查指令）
        "name": "ProtoForge 温湿度传感器（经驱动宿主）", "port": 50201, "device_id": "PF-MB-PLC-01", "tasks": False,
        "supports": {"supports_hold": False, "supports_abort": False, "supports_query": False, "supports_dedup": False},
        # 原来是从站 2 的握手 PLC：工位还叫这些名字就改成传感器的名字（人改过的名字不动）
        "renamed_from": ("ProtoForge PLC（Modbus TCP）", "ProtoForge PLC（经驱动宿主）"),
    },
    "ST-PF-OPCUA": {
        "name": "ProtoForge 压力传感器（经驱动宿主）", "port": 50202, "device_id": "PF-OPCUA-PRESSURE", "tasks": False,
        "supports": {"supports_hold": False, "supports_abort": False, "supports_query": True, "supports_dedup": True},
    },
    "ST-PF-HTTP": {
        "name": "ProtoForge HTTP 传感器（经驱动宿主）", "port": 50203, "device_id": "http-rest", "tasks": False,
        "supports": {"supports_hold": False, "supports_abort": False, "supports_query": True, "supports_dedup": True},
    },
    **{station_id: {
        "name": name, "port": port, "device_id": device_id, "tasks": True,
        "supports": {"supports_hold": False, "supports_abort": False, "supports_query": True, "supports_dedup": True},
        "simulator_control": {"url": f"http://{sim}:9900", "token_ref": f"file:///run/secrets/ilcs/simctl/{unit}.token"},
        "acceptance": ("cap.cell_check", {}), "faults": True,
        "approval": "本机模拟电芯检测仪表（ilcs-devices/simulators/scpi_meter），经驱动宿主接入；没有真实仪表与电芯",
    } for station_id, name, port, device_id, sim, unit in (
        ("ST-OCV-SIM", "模拟 Keithley 2450 开路电压（经驱动宿主）", 50211, "ILCS-SIMULATOR-2450-01", "k2450-sim", "SIM-K2450-01"),
        ("ST-OCV2-SIM", "模拟 Keithley 2400 开路电压（经驱动宿主）", 50212, "ILCS-SIMULATOR-2400-01", "k2400-sim", "SIM-K2400-01"),
        ("ST-ACIR-SIM", "模拟 Hioki BT3562 交流内阻（经驱动宿主）", 50213, "ILCS-SIMULATOR", "bt3562-sim", "SIM-BT3562-01"),
    )},
    # 设备模块的模拟网关：经驱动宿主的 http_json 插件转成 SiLA 服务（驱动宿主对网关一次调用最长 连接 3 s + 请求超时）
    **{station_id: {
        "name": name, "port": port, "device_id": device_id, "tasks": True,
        "supports": {"supports_hold": False, "supports_abort": True, "supports_query": True, "supports_dedup": True},
        "simulator_control": {"url": f"https://{host}:8443/api/v1", "ca_file": f"/run/secrets/ilcs/gateway/{device_id}.crt",
                              "token_ref": f"file:///run/secrets/ilcs/gateway/{device_id}.token"},
        "request_timeout_sec": 3 + gateway_timeout + 5, "acceptance": acceptance, "faults": True, "describe": True,
        "module": module, "approval": f"本机设备模块的模拟网关（{host}），经驱动宿主接入；没有真实设备与样品",
    } for station_id, name, port, device_id, host, module, gateway_timeout, acceptance in (
        ("ST-BAL-SIM", "天平称量加料站（模拟，经驱动宿主）", 50231, "SIM-BAL-DOSE-01", "balance-sim", "balance-dosing", 10,
         [("cap.weigh", {}), ("cap.ely.dose_solid", {"mass": 0.05}), ("cap.ely.dose_liquid", {"mass": 1.0})]),
        ("ST-STIR-SIM", "IKA 加热搅拌（模拟，经驱动宿主）", 50232, "SIM-IKA-STIR-01", "ika-stirrer-sim", "ika-stirrer", 10,
         [("cap.ely.stir", {"temp": 40, "time": 2, "rpm": 300})]),
        ("ST-RAM-SIM", "拉曼光谱仪（模拟，经驱动宿主）", 50233, "SIM-RAMAN-01", "raman-sim", "raman-seabreeze", 10,
         [("cap.ely.raman", {"repeats": 1})]),
        ("ST-CHILL-SIM", "冷水机制冷搅拌（模拟，经驱动宿主）", 50234, "SIM-CHILL-01", "thermostat-sim", "thermostat", 10,
         [("cap.thermostat", {"temp": 20, "time": 1}), ("cap.ely.stir", {"temp": 20, "time": 2, "rpm": 300})]),
        ("ST-ECHEM-SIM", "电化学工作站（模拟，经驱动宿主）", 50235, "SIM-ECHEM-01", "potentiostat-sim", "potentiostat", 20,
         [("cap.echem", {})]),
        ("ST-NW-01", "Neware 充放电柜（模拟，经驱动宿主）", 50236, "SIM-NW-BTS-01", "neware-sim", "neware-bts", 10,
         [("cap.test", {"channel": 1})]),
        ("EL-D-BAL", "电解液线配液天平（模拟，经驱动宿主）", 50237, "SIM-EL-D-BAL", "el-d-bal-sim", "balance-dosing", 10,
         [("cap.ely.dose_liquid", {"mass": 1.0})]),
        ("EL-D-PWD", "电解液线配粉天平（模拟，经驱动宿主）", 50238, "SIM-EL-D-PWD", "el-d-pwd-sim", "balance-dosing", 10,
         [("cap.ely.dose_solid", {"mass": 0.05})]),
        ("EL-T-RAM", "电解液线拉曼（模拟，经驱动宿主）", 50239, "SIM-EL-T-RAM", "el-t-ram-sim", "raman-seabreeze", 10,
         [("cap.ely.raman", {"repeats": 1})]),
        # A-Lab 上位机的网关（调假上位机的实验任务接口）：验收参数 {} 是整线自检，不带瓶、不动料
        ("EL-ALAB", "A-Lab 上位机（模拟，经驱动宿主）", 50240, "SIM-ALAB-01", "alab-sim", "alab-electrolyte", 15,
         [("cap.ely.run", {})]),
    )},
}
# A-Lab 的网关接的是上位机的 REST 接口，不开统一控制口（/simulator/* 回 404）：不登记控制口，也不申请故障项目
STATIONS["EL-ALAB"].pop("simulator_control")
STATIONS["EL-ALAB"]["faults"] = False
# 经驱动宿主接的网关工位：别的登记脚本（load-device-simulators.py、load-neware-cycler.py、load-electrolyte-line.py）
# 见到工位已经这样接着，就沿用、不改回直连
GATEWAY_STATIONS = {station_id for station_id, station in STATIONS.items() if station.get("describe")}


def via_driver_host(adapter: dict) -> bool:
    """工位现在经驱动宿主接（sila2_v1 连 driver-host）。"""
    return adapter.get("driver") == "sila2_v1" and (adapter.get("config") or {}).get("host") == HOST


def refuse_production() -> None:
    if os.environ.get("ILCS_ENVIRONMENT", "").strip().lower() == "production":
        raise Failed("正式环境拒绝运行：本脚本把模拟设备接进 ILCS")


def config_of(station: dict[str, Any]) -> dict[str, Any]:
    config = {"host": HOST, "port": station["port"], "ca_file": f"{SECRETS}/driver-host.crt",
              "expected_device_id": station["device_id"], "connect_timeout_sec": 3,
              "request_timeout_sec": station.get("request_timeout_sec", 10), "probe_interval_sec": 10}
    if station.get("simulator_control"):
        config["simulator_control"] = station["simulator_control"]
    if station.get("module"):
        profile = json.loads((DEVICES_REPO / "gateway" / station["module"] / "profile.json").read_text(encoding="utf-8"))
        config.update({key: profile["config"][key] for key in ILCS_KEYS if key in profile["config"]})
    return config if station["tasks"] else {**config, "tasks": False}


def ensure_station(engineer: Actor, station_id: str, station: dict[str, Any]) -> None:
    rows = {row["id"]: row for row in engineer.get("/stations")}
    if station_id in rows:
        current = rows[station_id]
        if current["name"] in station.get("renamed_from", ()):
            engineer.patch(f"/stations/{station_id}", {"name": station["name"], "row_version": current["row_version"]})
            ok("工位", f"{station_id} 改名：{current['name']} → {station['name']}")
            current = {row["id"]: row for row in engineer.get("/stations")}[station_id]
        else:
            ok("工位", f"{station_id}（沿用）")
        if not station["tasks"] and current.get("limits"):
            drop_capabilities(engineer, station_id, current)
        return
    island = (rows.get("ST-PF-MB") or {}).get("island")
    engineer.post("/stations", {
        "id": station_id, "name": station["name"], **({"island": island} if island else {}), "limits": {},
        "protocol": "sim", "adapter_kind": "simulation", "adapter_driver": "simulation",
        "signature_id": engineer.sign("工程变更批准", station_id),
    })
    ok("工位", f"{station_id} {station['name']}（只读写点位，不登记能力）")


def drop_capabilities(engineer: Actor, station_id: str, current: dict[str, Any]) -> None:
    """只读写点位的设备不承接能力：工位上还登记着的（比如原来是握手 PLC）签名移除，排程不再往它上面排，
    引用这些能力的流程随之重校验。"""
    capabilities = sorted(current["limits"])
    changed = engineer.patch(f"/stations/{station_id}/limits", {
        "remove": capabilities, "row_version": current["row_version"],
        "signature_id": engineer.sign("修改能力极限", station_id, current["row_version"]),
    })
    broken = changed.get("broken_recipes") or []
    ok("能力极限", f"{station_id} 只读写点位：移除 {'、'.join(capabilities)}（已签名）"
       + (f"；引用它的流程待修订：{'、'.join(str(row.get('id') if isinstance(row, dict) else row) for row in broken)}"
          if broken else ""))


def connect(engineer: Actor, operator: Actor, station_id: str, station: dict[str, Any], timeout: float) -> None:
    adapter = engineer.get(f"/stations/{station_id}/adapter")
    config, credential = config_of(station), f"file://{SECRETS}/ilcs.token"
    if adapter.get("driver") != "sila2_v1" or adapter.get("config") != config or adapter.get("credential_ref") != credential:
        adapter = engineer.patch(f"/stations/{station_id}/adapter", {
            "kind": "real", "driver": "sila2_v1", "protocol": "SiLA 2（驱动宿主）", "config": config,
            "credential_ref": credential, **station["supports"], "template_id": "",
            "row_version": adapter["row_version"],
            "signature_id": engineer.sign("设备集成配置变更批准", station_id, adapter["row_version"]),
        })
        ok("设备连接", f"{station_id} → sila2_v1 {HOST}:{station['port']}（{'参与自动流程' if station['tasks'] else '只读写点位'}，已签名保存）")
    else:
        ok("设备连接", f"{station_id} → {HOST}:{station['port']}（沿用）")
    if (operator.get("/gate").get("blocked_stations") or {}).get(station_id):
        operator.post(f"/stations/{station_id}/adapter/reconnect")

    def accepted():
        current = engineer.get(f"/stations/{station_id}/adapter")
        if current.get("driver_awaiting_approval"):  # 驱动宿主上的设备文件改过：核对后签名批准，随后只读级验收
            approve_driver(engineer, station_id, current)
            return None
        listed = engineer.get(f"/stations/{station_id}/adapter/acceptance")
        gate, runs = listed["gate"], listed.get("runs") or []
        version = current["config_version"]  # 发现驱动配置变了，ILCS 会把配置版本加一：按当前的比
        if gate["required"] == "" and gate.get("accepted_config_version") == version:
            return next((row for row in runs if row["id"] == gate.get("accepted_run_id")), {"id": gate.get("accepted_run_id")})
        latest = next((row for row in runs if row.get("config_version") == version), None)
        if latest and latest.get("state") == "done" and not latest.get("ok") and latest.get("level") == "readonly":
            raise Failed(f"{station_id} 只读级验收没通过：{latest.get('error') or latest.get('report_md', '')[:800]}")
        return None

    run = wait_for(f"{station_id} 接入验收放行", accepted, timeout=timeout)
    current = engineer.get(f"/stations/{station_id}/adapter")
    driver = current.get("approved_driver") or {}
    ok("接入验收", f"{station_id} 配置 v{current['config_version']} 已由 {str(run['id'])[:8]} 放行；批准驱动配置 "
       f"{driver.get('plugin') or '—'} {str(driver.get('config_digest') or '')[:19]}")


def approve_driver(engineer: Actor, station_id: str, current: dict[str, Any]) -> None:
    """设备服务报的驱动配置和批准的那份不一样（驱动宿主上改了设备文件）：签名批准这一份。"""
    reported = current.get("driver_info") or {}
    engineer.post(f"/stations/{station_id}/adapter/driver-approval", {
        "reason": f"驱动宿主上的设备文件改了（{reported.get('plugin')} 配置 {reported.get('config_version') or '—'}），"
                  "核对后批准这一份",
        "signature_id": engineer.sign("批准驱动配置变更", station_id, current["config_version"]),
    })
    ok("批准驱动配置变更", f"{station_id} {reported.get('plugin')} {str(reported.get('config_digest') or '')[:19]}")


def physical_acceptance(engineer: Actor, station_id: str, station: dict[str, Any], timeout: float) -> None:
    """逐项能力跑动作级（带故障项目的设备再跑故障项目）。`acceptance` 是一项 (能力, 参数) 或它们的列表。"""
    rows = station["acceptance"] if isinstance(station["acceptance"], list) else [station["acceptance"]]
    for capability, params in rows:
        _physical(engineer, station_id, station, capability, params, timeout)


def _physical(engineer: Actor, station_id: str, station: dict[str, Any], capability: str, params: dict,
              timeout: float) -> None:
    adapter = engineer.get(f"/stations/{station_id}/adapter")
    requested = engineer.post(f"/stations/{station_id}/adapter/acceptance", {
        "level": "physical", "faults": bool(station.get("faults")), "capability": capability, "params": params,
        "approval": station["approval"],
        "signature_id": engineer.sign("批准设备接入验收", station_id, adapter["config_version"]),
    })
    run = wait_for(f"{station_id} 动作级验收出结论", lambda: (
        lambda row: row if row["state"] in {"done", "error", "cancelled"} else None)(
        engineer.get(f"/acceptance-runs/{requested['id']}")), timeout=timeout, every=3)
    states = {check["key"]: check["state"] for check in run.get("checks") or []}
    if not run.get("ok"):
        raise Failed(f"{station_id} 动作级验收没通过：{states}\n{run.get('report_md', '')[:2000]}")
    skipped = sorted(key for key, state in states.items() if state == "skip")
    ok("接入验收（动作级）", f"{station_id} {capability}：{sum(1 for s in states.values() if s == 'pass')} 项通过"
       + (f"，跳过 {'、'.join(skipped)}" if skipped else ""))


def describe(engineer: Actor, station_id: str) -> None:
    """重读设备自报的方法目录（经驱动宿主时取自 TaskSupport）：排程只往报过这个程序的工位排。"""
    described = engineer.post(f"/stations/{station_id}/adapter/describe")
    if described.get("warning"):
        raise Failed(f"{station_id}：{described['warning']}")
    programs = "、".join(str(row.get("program") or "") for row in described.get("methods") or []) or "—"
    ok("方法目录", f"{station_id}：{programs}（来源 {described.get('described_from') or '—'}）")


def register(team: dict[str, Actor], args: argparse.Namespace, keys: list[str]) -> None:
    engineer, operator = team["engineer"], team["operator"]
    for station_id in keys:
        station = STATIONS[station_id]
        step(f"{station_id}：{station['name']}")
        ensure_station(engineer, station_id, station)
        connect(engineer, operator, station_id, station, args.timeout)
        if station.get("describe"):
            describe(engineer, station_id)
        if args.acceptance and station.get("acceptance"):
            physical_acceptance(engineer, station_id, station, args.timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机设备经驱动宿主接进 ILCS（只走 SiLA 2）")
    parser.add_argument("command", choices=("register",))
    parser.add_argument("--base", default=os.environ.get("ILCS_BASE_URL", "http://127.0.0.1:8090"))
    parser.add_argument("--acceptance", action="store_true", help="参与自动流程的设备再跑一次动作级验收（模拟仪表带故障项目）")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--only", default=",".join(STATIONS), help=f"只接哪几个工位（{', '.join(STATIONS)}）")
    args = parser.parse_args(argv)
    keys = [key.strip() for key in args.only.split(",") if key.strip()]
    try:
        unknown = [key for key in keys if key not in STATIONS]
        if unknown:
            raise Failed(f"不认识 {', '.join(unknown)}；可选 {', '.join(STATIONS)}")
        refuse_production()
        register(actors(http_transport(args.base)), args, keys)
        print("\n完成：" + "、".join(keys) + " 已经驱动宿主接入")
        return 0
    except Failed as exc:
        print(f"\n失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
