#!/usr/bin/env python
"""试点运维：把工位适配器切到外部模拟设备（SiLA 2 / Modbus TCP / OPC UA / HTTPS 网关），或还原为切换前的配置。

界面上修改适配器要求管理员电子签名；这个命令用于部署窗口里由运维执行的试点切换，
每个工位写一条系统来源的审计（含前后配置），不绕过留痕。切换后适配器先标为离线，
在线与否由执行器探测决定——不沿用切换前的「在线」结论。

    python scripts/configure-pilot-adapters.py apply \\
        --station ST-06=sila-sim-lh:50052:SIM-LH-01 \\
        --station ST-07=sila-sim-cycler:50053:SIM-CYC-01 --channels ST-07=8
    python scripts/configure-pilot-adapters.py apply \\
        --station ST-02=modbus_tcp_v1@modbus-sim-mixer:5020:SIM-MIX-01 \\
        --station ST-04=opcua_v1@opcua-sim-calender:4840:SIM-CAL-01 \\
        --station ST-03=http_json_v1@gateway-sim-coater:8443:SIM-COAT-01
    python scripts/configure-pilot-adapters.py revert --station ST-06 --station ST-07

工位规格是 `工位=[驱动@]主机:端口:设备ID`，不写驱动时是 sila2_v1。各驱动的证书 / 令牌路径按 compose 里模拟设备
写入的位置拼出；Modbus 的能力码与参数槽位按工位能力限值依次编号（模拟 PLC 不认参数名，按槽位回报）。

apply 会把原配置记在 /data/pilot-adapters-<时间>.json；revert 读最近一份还原。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.adapters.registry import reset_cache  # noqa: E402
from app.core.clock import now  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal, engine  # noqa: E402
from app.core.schema import verify  # noqa: E402
from app.models import Adapter, AuditEvent, Station  # noqa: E402

FIELDS = ("kind", "driver", "protocol", "version", "config", "credential_ref", "supports_hold",
          "supports_abort", "supports_query", "supports_dedup", "note")
SECRETS = "/run/secrets/ilcs"
PROTOCOLS = {"sila2_v1": "SiLA 2", "modbus_tcp_v1": "Modbus TCP", "opcua_v1": "OPC UA", "http_json_v1": "HTTPS JSON"}
COMMON = {"request_timeout_sec": 10, "probe_interval_sec": 10}


def _connection(driver: str, host: str, port: int, device_id: str, station: Station) -> tuple[dict, str]:
    """(连接配置, credential_ref)。"""
    if driver == "sila2_v1":
        return {"host": host, "port": port, "ca_file": f"{SECRETS}/sila/{device_id}.crt",
                "expected_device_id": device_id, **COMMON}, ""
    if driver == "modbus_tcp_v1":
        capabilities = sorted(station.limits or {})
        params = sorted({name for cap in capabilities for name in (station.limits[cap] or {})})
        if len(params) > 16:
            raise SystemExit(f"{station.id} 有 {len(params)} 个参数，超过 Modbus 任务寄存器的 16 个槽位")
        return {"host": host, "port": port, "unit_id": 1, "expected_device_id": device_id, **COMMON,
                "capabilities": {cap: index for index, cap in enumerate(capabilities, start=1)},
                "params": {name: index for index, name in enumerate(params, start=1)}}, ""
    if driver == "opcua_v1":
        return {"endpoint": f"opc.tcp://{host}:{port}/ilcs/", "security_policy": "Basic256Sha256",
                "security_mode": "SignAndEncrypt", "server_certificate": f"{SECRETS}/opcua/{device_id}.crt",
                "application_uri": "urn:ilcs:client", "expected_device_id": device_id, **COMMON}, \
            f"file://{SECRETS}/opcua/ilcs-client.json"
    if driver == "http_json_v1":
        # 网关模拟设备不推心跳：改成由执行器读 /health 探测
        return {"base_url": f"https://{host}:{port}/api/v1", "ca_file": f"{SECRETS}/gateway/{device_id}.crt",
                "expected_device_id": device_id, "heartbeat_mode": "probe", **COMMON}, \
            f"file://{SECRETS}/gateway/{device_id}.token"
    raise SystemExit(f"不支持的试点驱动 {driver}；可选 {', '.join(PROTOCOLS)}")


def _snapshot(adapter: Adapter, station: Station) -> dict:
    return {**{field: getattr(adapter, field) for field in FIELDS}, "channels": station.channels or 1}


def _audit(db, station: Station, action: str, before: dict, after: dict) -> None:
    db.add(AuditEvent(
        org_id=station.org_id, user="运维命令", user_id="system", role="system", action=action,
        target=station.id, time=now(),
        detail=json.dumps({"before": before, "after": after}, ensure_ascii=False, default=str)[:4000],
    ))


def apply(args) -> int:
    targets = {}
    for spec in args.station:
        station_id, _, rest = spec.partition("=")
        driver, _, rest = rest.rpartition("@")
        host, port, device_id = rest.split(":")
        targets[station_id] = (driver or "sila2_v1", host, int(port), device_id)
    channels = dict(item.split("=") for item in args.channels or [])
    missing_hosts = sorted({host for _, host, _, _ in targets.values()} - settings.adapter_allowed_host_set)
    if missing_hosts:
        print(f"ILCS_ADAPTER_ALLOWED_HOSTS 未包含 {', '.join(missing_hosts)}，驱动会拒绝连接", file=sys.stderr)
        return 2
    backup: dict = {}
    with SessionLocal() as db:
        for station_id, (driver, host, port, device_id) in targets.items():
            station = db.get(Station, station_id)
            adapter = db.get(Adapter, station_id)
            if station is None or adapter is None:
                print(f"工位或适配器 {station_id} 不存在", file=sys.stderr)
                return 2
            before = _snapshot(adapter, station)
            backup[station_id] = before
            adapter.kind, adapter.driver, adapter.protocol, adapter.version = "real", driver, PROTOCOLS.get(driver, driver), "1.0"
            adapter.config, adapter.credential_ref = _connection(driver, host, port, device_id, station)
            adapter.supports_hold = adapter.supports_abort = adapter.supports_query = adapter.supports_dedup = True
            adapter.note = f"试点：外部 {adapter.protocol} 模拟设备 {device_id}"
            adapter.config_version += 1
            adapter.row_version += 1
            adapter.connected = False
            adapter.accepts_commands = False
            adapter.current_command_id = ""
            if station_id in channels:
                station.channels = int(channels[station_id])
                station.row_version += 1
            _audit(db, station, "试点切换设备适配器", before, _snapshot(adapter, station))
        db.commit()
    path = Path(args.backup_dir) / f"pilot-adapters-{now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(backup, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    reset_cache()
    print(f"已切换 {', '.join(targets)}；原配置 {path}；在线状态等执行器探测")
    return 0


def revert(args) -> int:
    files = sorted(Path(args.backup_dir).glob("pilot-adapters-*.json"))
    if not files:
        print("没有找到切换前的配置备份", file=sys.stderr)
        return 2
    # 每个工位取包含它的最近一份备份：不同协议分几次 apply 时，各自的原配置在不同文件里
    backup: dict = {}
    sources: dict = {}
    for file in files:
        for station_id, before in json.loads(file.read_text(encoding="utf-8")).items():
            backup[station_id], sources[station_id] = before, file.name
    wanted = set(args.station or json.loads(files[-1].read_text(encoding="utf-8")))
    missing = sorted(wanted - set(backup))
    if missing:
        print(f"没有 {', '.join(missing)} 的切换前备份", file=sys.stderr)
        return 2
    with SessionLocal() as db:
        for station_id, before in backup.items():
            if station_id not in wanted:
                continue
            station, adapter = db.get(Station, station_id), db.get(Adapter, station_id)
            current = _snapshot(adapter, station)
            for field in FIELDS:
                setattr(adapter, field, before[field])
            station.channels = before.get("channels", 1)
            adapter.config_version += 1
            adapter.row_version += 1
            adapter.current_command_id = ""
            adapter.connected = before["kind"] == "simulation"
            adapter.accepts_commands = True
            _audit(db, station, "试点还原设备适配器", current, before)
        db.commit()
    print("已还原 " + "，".join(f"{station_id}（{sources[station_id]}）" for station_id in sorted(wanted)))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["apply", "revert"])
    parser.add_argument("--station", action="append", help="apply：工位=[驱动@]主机:端口:设备ID；revert：工位")
    parser.add_argument("--channels", action="append", help="工位=并行通道数")
    parser.add_argument("--backup-dir", default="/data")
    args = parser.parse_args()
    verify(engine)
    return apply(args) if args.action == "apply" else revert(args)


if __name__ == "__main__":
    raise SystemExit(main())
