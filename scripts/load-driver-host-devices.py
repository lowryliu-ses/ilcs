#!/usr/bin/env python3
"""本机经驱动宿主（ilcs-devices/host，现场 ilcs-devices/host/sites/local）接进 ILCS 的设备：ILCS 只经 SiLA 2 读写点位、下发作业。

    ILCS_HOST_SITE=$PWD/data/driver-host/site docker compose -f ../ilcs-devices/deploy/compose.yml up -d   # 驱动宿主（ilcs-devices 组）
    python3 scripts/load-driver-host-devices.py register [--base http://127.0.0.1:8090] [--acceptance] [--only 工位,…]

哪个工位接驱动宿主上的哪台设备，以设备仓库为准：现场目录的工位对照表 `host/sites/<现场>/ilcs-stations.json`（现场缺省
local，环境变量 ILCS_DRIVER_HOST_SITE 换；106.51 与本机共用 local 这份，只差 ProtoForge 的主机名）。端口、设备编号、契约
（保持 / 终止 / 查询 / 去重）、插件与网关请求超时取自同目录的设备文件，网关的 `wells_per_command`（只给 ILCS 用的连接配置键）
取自模块 profile.json；这里不另写一份。现在接的是：

- ProtoForge 三台（ST-PF-MB 温湿度传感器、ST-PF-OPCUA 压力传感器、ST-PF-HTTP HTTP 传感器）：只读写点位（`"tasks": false`），
  没有就登记（不登记能力）。ST-PF-MB 原来是从站 2 的握手 PLC：还叫原来的名字就改名。HTTP 传感器要 ProtoForge 接进 ILCS 的
  后端网络（`docker network connect ilcs_backend protoforge`），驱动宿主才连得到它的 8080。ProtoForge 全流程
  （load-protoforge-flow.py）之后再把 ST-PF-OPCUA、ST-PF-HTTP 改成参与自动流程、给三台登记环境采集：重跑本脚本会把它们
  改回这里的连接配置，之后要再跑一次它的 register。
- 只读写点位的工位不承接能力：已登记的能力极限签名移除，排程不会再往它上面排。
- 模拟电芯检测仪表 ST-OCV-SIM、ST-OCV2-SIM、ST-ACIR-SIM（驱动宿主的 line_command，映射照抄设备配置模板
  host/profiles/scpi-cell-meter）：模拟设备控制口（simulator_control）留在 ILCS 的连接配置里，验收的故障项目照做。
- 设备模块的模拟网关（驱动宿主的 http_json 插件转成 SiLA 服务，GW-*）：ST-BAL-SIM、ST-STIR-SIM、ST-RAM-SIM、ST-CHILL-SIM、
  ST-ECHEM-SIM、ST-NW-01，电解液线的 EL-D-BAL、EL-D-ADD、EL-D-PWD、EL-D-STIR、EL-D-COLD、EL-D-MIX、EL-T-RAM，A-Lab 上位机
  EL-ALAB。原来直连网关（`http_json_v1` 套用接入模板）的改成 `sila2_v1` 接驱动宿主；模拟设备控制口指向网关自己的 API
  （HTTPS + 网关令牌，ILCS 直连它：网关主机名要在 ILCS 的 ILCS_ADAPTER_ALLOWED_HOSTS 里，不然故障项目跳过；A-Lab 的网关
  接上位机 REST 接口、不开控制口，不申请故障项目）。连上后重读设备自报的方法目录（排程只往
  报过这个程序的工位排）。驱动宿主上改了设备文件（配置摘要变了）再跑一次：签名批准新的驱动配置，等只读级验收放行。

`--acceptance` 逐项能力跑动作级（有控制口的再跑故障项目）。都用驱动宿主的自签证书（`secrets/host/driver-host.crt`）和给
ILCS 的令牌（`secrets/host/ilcs.token`），等执行器跑完只读级验收——首次接入的验收通过时，同时批准设备服务报的这一份驱动配置。
只走 HTTP，签名用演示账号口令逐次签署；已经接好的不重复改。正式环境（ILCS_ENVIRONMENT=production）拒绝运行。
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
# 驱动宿主容器里网关的凭据目录与 ILCS 容器里的是同一个 secrets/gateway（两边挂的路径不同）
HOST_GATEWAY_SECRETS, ILCS_GATEWAY_SECRETS = "/run/secrets/ilcs-host/gateway/", "/run/secrets/ilcs/gateway/"
SITE = os.environ.get("ILCS_DRIVER_HOST_SITE") or "local"


def _ilcs_secret(path: str, device: str) -> str:
    if not path.startswith(HOST_GATEWAY_SECRETS):
        raise Failed(f"{device}：网关凭据 {path} 不在驱动宿主的 {HOST_GATEWAY_SECRETS} 下，推不出 ILCS 那边的路径")
    return ILCS_GATEWAY_SECRETS + path[len(HOST_GATEWAY_SECRETS):]


def load_stations(site: str = SITE) -> dict[str, dict[str, Any]]:
    """工位 → 怎么接驱动宿主。对照表在设备仓库的现场目录（host/sites/<现场>/ilcs-stations.json）：接哪台设备、工位名、
    只读写点位、设备自报的身份、接入验收；端口、设备编号、契约（supports）与网关的请求超时取自同目录的设备文件，
    模拟网关的统一控制口按设备文件里的网关地址与凭据推出来。设备怎么接以设备仓库为准，这里不再另写一份。"""
    root = DEVICES_REPO / "host" / "sites" / site
    table = root / "ilcs-stations.json"
    if not table.exists():
        raise Failed(f"找不到设备仓库的工位对照表 {table}：环境变量 ILCS_DEVICES 指向 ilcs-devices 的检出")
    stations: dict[str, dict[str, Any]] = {}
    for station_id, row in json.loads(table.read_text(encoding="utf-8"))["stations"].items():
        device = json.loads((root / "devices" / f"{row['device']}.json").read_text(encoding="utf-8"))
        config, gateway = device.get("config") or {}, device["plugin"] == "http_json"
        entry: dict[str, Any] = {
            "name": row["name"], "device": row["device"], "port": device["port"], "tasks": row.get("tasks", True),
            "device_id": row.get("device_id") or config.get("expected_device_id") or device.get("device_id"),
            "supports": {f"supports_{key}": bool(device["supports"][key]) for key in ("hold", "abort", "query", "dedup")},
            "renamed_from": tuple(row.get("renamed_from") or ()),
        }
        if gateway:
            # 驱动宿主对网关一次调用最长 连接 3 s + 请求超时，ILCS 再留 5 s；连上后重读网关自报的方法目录
            entry.update(module=row["module"], describe=True,
                         request_timeout_sec=3 + config.get("request_timeout_sec", 10) + 5)
        control = row.get("simulator_control", "derived")
        if control == "derived" and gateway and device.get("simulator"):
            # 模拟网关的统一控制口就是网关自己的 API（HTTPS + 网关令牌）
            control = {"url": config["base_url"], "ca_file": _ilcs_secret(config["ca_file"], row["device"]),
                       "token_ref": "file://" + _ilcs_secret(device["credential_ref"].removeprefix("file://"), row["device"])}
        elif isinstance(control, dict):
            control = {"url": control["url"], "token_ref": f"file:///run/secrets/ilcs/{control['token']}"}
        else:
            control = None
        if control:
            entry["simulator_control"] = control
        if row.get("acceptance"):
            entry.update(acceptance=[(capability, params) for capability, params in row["acceptance"]],
                         faults=bool(control),
                         approval=f"模拟设备 {row['device']}（驱动宿主现场 {site}），经驱动宿主接入；没有真实设备与样品")
        stations[station_id] = entry
    return stations


STATIONS = load_stations()
# 经驱动宿主接的网关工位：别的登记脚本（load-device-simulators.py、load-neware-cycler.py、load-electrolyte-line.py）
# 见到工位已经这样接着，就沿用、不改回直连
GATEWAY_STATIONS = {station_id for station_id, station in STATIONS.items() if station.get("describe")}


def via_driver_host(adapter: dict) -> bool:
    """工位现在经驱动宿主接（sila2_v1 连 driver-host）。"""
    return adapter.get("driver") == "sila2_v1" and (adapter.get("config") or {}).get("host") == HOST


def refuse_production() -> None:
    if os.environ.get("ILCS_ENVIRONMENT", "").strip().lower() == "production":
        raise Failed("正式环境拒绝运行：本脚本把模拟设备接进 ILCS")


def _profile(module: str) -> dict[str, Any]:
    return json.loads((DEVICES_REPO / "gateway" / module / "profile.json").read_text(encoding="utf-8"))


def config_of(station: dict[str, Any]) -> dict[str, Any]:
    config = {"host": HOST, "port": station["port"], "ca_file": f"{SECRETS}/driver-host.crt",
              "expected_device_id": station["device_id"], "connect_timeout_sec": 3,
              "request_timeout_sec": station.get("request_timeout_sec", 10), "probe_interval_sec": 10}
    if station.get("simulator_control"):
        config["simulator_control"] = station["simulator_control"]
    if station.get("module"):
        profile = _profile(station["module"])
        config.update({key: profile["config"][key] for key in ILCS_KEYS if key in profile["config"]})
    return config if station["tasks"] else {**config, "tasks": False}


def supports_of(station: dict[str, Any]) -> dict[str, bool]:
    """工位声明的契约：照驱动宿主上的设备文件（网关设备的又照模块 profile.json：A-Lab 上位机能暂停，天平、拉曼这些不能）。"""
    return station["supports"]


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
    config, credential, supports = config_of(station), f"file://{SECRETS}/ilcs.token", supports_of(station)
    declared = {f"supports_{key}": value for key, value in (adapter.get("capabilities") or {}).items()}
    if adapter.get("driver") != "sila2_v1" or adapter.get("config") != config \
            or adapter.get("credential_ref") != credential or declared != supports:
        adapter = engineer.patch(f"/stations/{station_id}/adapter", {
            "kind": "real", "driver": "sila2_v1", "protocol": "SiLA 2（驱动宿主）", "config": config,
            "credential_ref": credential, **supports, "template_id": "",
            "row_version": adapter["row_version"],
            "signature_id": engineer.sign("设备集成配置变更批准", station_id, adapter["row_version"]),
        })
        ok("设备连接", f"{station_id} → sila2_v1 {HOST}:{station['port']}（{'参与自动流程' if station['tasks'] else '只读写点位'}，已签名保存）")
    else:
        ok("设备连接", f"{station_id} → {HOST}:{station['port']}（沿用）")
    if (operator.get("/gate").get("blocked_stations") or {}).get(station_id):
        # 执行器可能已经在跑这次改动的只读级验收（409 acceptance_running）：不用重连，下面等它出结论
        operator.call("POST", f"/stations/{station_id}/adapter/reconnect", expect=(200, 201, 409))

    def accepted():
        current = engineer.get(f"/stations/{station_id}/adapter")
        if current.get("driver_awaiting_approval"):  # 驱动宿主上的设备文件改过：核对后签名批准，随后只读级验收
            approve_driver(engineer, station_id, current)
            return None
        listed = engineer.get(f"/stations/{station_id}/adapter/acceptance")
        gate, runs = listed["gate"], listed.get("runs") or []
        version = current["config_version"]  # 发现驱动配置变了，ILCS 会把配置版本加一：按当前的比
        if gate["required"] == "":
            # 不欠验收就是放行了：要验收的改动保存时就记下欠的级别；只动了环境采集这类键的改动不欠验收，
            # 配置版本照样加一，最近一次放行的还是旧版本——不能等「放行版本 = 当前版本」
            return next((row for row in runs if row["id"] == gate.get("accepted_run_id")), {"id": gate.get("accepted_run_id")})
        latest = next((row for row in runs if row.get("config_version") == version), None)
        if latest and latest.get("state") == "done" and not latest.get("ok") and latest.get("level") == "readonly":
            raise Failed(f"{station_id} 只读级验收没通过：{latest.get('error') or latest.get('report_md', '')[:800]}")
        return None

    run = wait_for(f"{station_id} 接入验收放行", accepted, timeout=timeout)
    current = engineer.get(f"/stations/{station_id}/adapter")
    driver = current.get("approved_driver") or {}
    accepted_version = (current.get("acceptance") or {}).get("accepted_config_version")
    passed = (f"配置 v{current['config_version']} 已由 {str(run['id'])[:8]} 放行" if accepted_version == current["config_version"]
              else f"配置 v{current['config_version']} 的改动不欠验收（最近一次放行 v{accepted_version}）")
    ok("接入验收", f"{station_id} {passed}；批准驱动配置 {driver.get('plugin') or '—'} "
       f"{str(driver.get('config_digest') or '')[:19]}")


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
