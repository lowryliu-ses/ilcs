#!/usr/bin/env python3
"""本机经驱动宿主（ilcs-devices/host，现场 ilcs-devices/host/sites/local）接进 ILCS 的设备：ILCS 只经 SiLA 2 读写点位、下发作业。

    ILCS_HOST_SITE=$PWD/data/driver-host/site docker compose -f ../ilcs-devices/host/deploy/compose.yml up -d   # 驱动宿主
    python3 scripts/load-driver-host-devices.py register [--base http://127.0.0.1:8090] [--acceptance] [--only 工位,…]

- ST-PF-MB（ProtoForge 从站 2 的握手 PLC）：工位已有，原来是 `modbus_map_v1` 直连 PLC，改成 `sila2_v1` 接驱动宿主上的
  PF-MB-PLC（50201）。换驱动要求设备上没有在动作的指令，否则保存被拒。`--acceptance` 再申请一次动作级验收（cap.plc_run）。
- ST-PF-OPCUA（ProtoForge OPC UA 压力传感器）、ST-PF-HTTP（ProtoForge HTTP REST 传感器）：没有就登记（只读写点位，
  不登记能力），`sila2_v1` 接 PF-OPCUA（50202）/ PF-HTTP（50203），`"tasks": false`。HTTP 传感器要 ProtoForge 接进
  ILCS 的后端网络（`docker network connect ilcs_backend protoforge`），驱动宿主才连得到它的 8080。

- ST-OCV-SIM、ST-OCV2-SIM、ST-ACIR-SIM（模拟电芯检测仪表，设备模块 ilcs-devices/gateway/scpi-cell-meter）：工位已有，原来是
  `line_command_v1` 直连模拟仪表（接入模板 TPL-SCPI-*），改成 `sila2_v1` 接驱动宿主上的 OCV-K2450（50211）/ OCV-K2400
  （50212）/ ACIR-BT3562（50213），映射照抄模板、搬到驱动宿主；模拟设备控制口（simulator_control）留在 ILCS 的连接配置里，
  验收的故障项目照做。`--acceptance` 跑动作级 + 故障项目（cap.cell_check）。

都用驱动宿主的自签证书（`secrets/host/driver-host.crt`）和给 ILCS 的令牌（`secrets/host/ilcs.token`），等执行器跑完只读级验收
——首次接入的验收通过时，同时批准设备服务报的这一份驱动配置。只走 HTTP，签名用演示账号口令逐次签署；已经接好的不重复改。
正式环境（ILCS_ENVIRONMENT=production）拒绝运行。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import sys
from typing import Any

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("load_neware_cycler", HERE / "load-neware-cycler.py")
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)  # 复用它的 HTTP 传输、演示账号与签名、等待与输出
Actor, Failed, actors, http_transport = common.Actor, common.Failed, common.actors, common.http_transport
step, ok, note, wait_for = common.step, common.ok, common.note, common.wait_for

HOST = "driver-host"
SECRETS = "/run/secrets/ilcs/host"
STATIONS = {
    "ST-PF-MB": {
        "name": "ProtoForge PLC（经驱动宿主）", "port": 50201, "device_id": "PF-MB-PLC-01", "tasks": True,
        "supports": {"supports_hold": False, "supports_abort": False, "supports_query": True, "supports_dedup": True},
        "acceptance": ("cap.plc_run", {"temp": 60}), "approval": "本机 ProtoForge 模拟 PLC，经驱动宿主接入；没有真实设备与样品",
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
        "approval": "本机模拟电芯检测仪表（ilcs-devices/gateway/scpi-cell-meter），经驱动宿主接入；没有真实仪表与电芯",
    } for station_id, name, port, device_id, sim, unit in (
        ("ST-OCV-SIM", "模拟 Keithley 2450 开路电压（经驱动宿主）", 50211, "ILCS-SIMULATOR-2450-01", "k2450-sim", "SIM-K2450-01"),
        ("ST-OCV2-SIM", "模拟 Keithley 2400 开路电压（经驱动宿主）", 50212, "ILCS-SIMULATOR-2400-01", "k2400-sim", "SIM-K2400-01"),
        ("ST-ACIR-SIM", "模拟 Hioki BT3562 交流内阻（经驱动宿主）", 50213, "ILCS-SIMULATOR", "bt3562-sim", "SIM-BT3562-01"),
    )},
}


def refuse_production() -> None:
    if os.environ.get("ILCS_ENVIRONMENT", "").strip().lower() == "production":
        raise Failed("正式环境拒绝运行：本脚本把模拟设备接进 ILCS")


def config_of(station: dict[str, Any]) -> dict[str, Any]:
    config = {"host": HOST, "port": station["port"], "ca_file": f"{SECRETS}/driver-host.crt",
              "expected_device_id": station["device_id"], "connect_timeout_sec": 3, "request_timeout_sec": 10,
              "probe_interval_sec": 10}
    if station.get("simulator_control"):
        config["simulator_control"] = station["simulator_control"]
    return config if station["tasks"] else {**config, "tasks": False}


def ensure_station(engineer: Actor, station_id: str, station: dict[str, Any]) -> None:
    rows = {row["id"]: row for row in engineer.get("/stations")}
    if station_id in rows:
        ok("工位", f"{station_id}（沿用）")
        return
    island = (rows.get("ST-PF-MB") or {}).get("island")
    engineer.post("/stations", {
        "id": station_id, "name": station["name"], **({"island": island} if island else {}), "limits": {},
        "protocol": "sim", "adapter_kind": "simulation", "adapter_driver": "simulation",
        "signature_id": engineer.sign("工程变更批准", station_id),
    })
    ok("工位", f"{station_id} {station['name']}（只读写点位，不登记能力）")


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
        listed = engineer.get(f"/stations/{station_id}/adapter/acceptance")
        gate, runs = listed["gate"], listed.get("runs") or []
        if gate["required"] == "" and gate.get("accepted_config_version") == adapter["config_version"]:
            return next((row for row in runs if row["id"] == gate.get("accepted_run_id")), {"id": gate.get("accepted_run_id")})
        latest = next((row for row in runs if row.get("config_version") == adapter["config_version"]), None)
        if latest and latest.get("state") == "done" and not latest.get("ok") and latest.get("level") == "readonly":
            raise Failed(f"{station_id} 只读级验收没通过：{latest.get('error') or latest.get('report_md', '')[:800]}")
        return None

    run = wait_for(f"{station_id} 接入验收放行", accepted, timeout=timeout)
    current = engineer.get(f"/stations/{station_id}/adapter")
    driver = current.get("approved_driver") or {}
    ok("接入验收", f"{station_id} 配置 v{adapter['config_version']} 已由 {str(run['id'])[:8]} 放行；批准驱动配置 "
       f"{driver.get('plugin') or '—'} {str(driver.get('config_digest') or '')[:19]}")


def physical_acceptance(engineer: Actor, station_id: str, station: dict[str, Any], timeout: float) -> None:
    capability, params = station["acceptance"]
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


def register(team: dict[str, Actor], args: argparse.Namespace, keys: list[str]) -> None:
    engineer, operator = team["engineer"], team["operator"]
    for station_id in keys:
        station = STATIONS[station_id]
        step(f"{station_id}：{station['name']}")
        ensure_station(engineer, station_id, station)
        connect(engineer, operator, station_id, station, args.timeout)
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
