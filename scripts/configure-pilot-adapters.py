#!/usr/bin/env python
"""试点运维：把工位适配器切到外部模拟设备，或还原为切换前的配置。

界面上修改适配器要求管理员电子签名；这个命令用于部署窗口里由运维执行的试点切换，
每个工位写一条系统来源的审计（含前后配置），不绕过留痕。切换后适配器先标为离线，
在线与否由执行器探测决定——不沿用切换前的「在线」结论。

按预设切换（每个示例工位接哪种驱动、连哪台模拟设备，见 simulators/pilot-devices.json）：

    python scripts/configure-pilot-adapters.py apply --preset                 # 预设里的全部工位
    python scripts/configure-pilot-adapters.py apply --preset --only ST-05 --only AGV-01

按单个工位切换（旧写法，`工位=[驱动@]主机:端口:设备ID`，不写驱动时是 sila2_v1）：

    python scripts/configure-pilot-adapters.py apply \\
        --station ST-06=sila-sim-lh:50052:SIM-LH-01 --channels ST-07=8
    python scripts/configure-pilot-adapters.py revert --station ST-06 --station ST-07

apply 会把原配置记在 /data/pilot-adapters-<时间>.json；revert 每个工位取包含它的最近一份备份还原。
预设里引用的主机（含组合工位各路由的主机）必须都在 ILCS_ADAPTER_ALLOWED_HOSTS 里，否则一个都不改。
目标工位上还有可能仍在动作的指令（在途、已保持、结果未知）时同样一个都不改：换驱动后新实例查不回它们。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.adapters.registry import reset_cache  # noqa: E402
from app.core.clock import now  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal, engine  # noqa: E402
from app.core.schema import verify  # noqa: E402
from app.models import Adapter, AuditEvent, Station  # noqa: E402
from app.repositories.execution import CommandRepository  # noqa: E402
from app.services.acceptance_service import after_config_change  # noqa: E402

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


def _lock(db, station_ids) -> None:
    """按工位号顺序锁住这些工位的适配器行，直到提交。

    执行器投递前要锁同一行（见 ExecutionService.execute）：锁住之后不会再有新指令发出去，
    接下来核对「还有没有在动作的指令」才作数；提交后执行器读到的是新配置和验收闸门。
    """
    ids = sorted(set(station_ids))
    if ids:
        (db.query(Adapter).filter(Adapter.station_id.in_(ids)).order_by(Adapter.station_id)
         .with_for_update().populate_existing().all())


def _still_acting(db, station_ids) -> str:
    """这些工位上还有可能仍在动作的指令时，说明是哪些；换驱动后新实例查不回它们（见 domain/adapter_rules）。"""
    rows = []
    for station_id in sorted(station_ids):
        acting = CommandRepository(db).acting_on_station(station_id)
        if acting:
            rows.append(f"{station_id}（{'、'.join(command.id for command in acting[:5])}{' 等' if len(acting) > 5 else ''}）")
    return "；".join(rows)


def _snapshot(adapter: Adapter, station: Station) -> dict:
    return {**{field: getattr(adapter, field) for field in FIELDS}, "channels": station.channels or 1}


def _audit(db, station: Station, action: str, before: dict, after: dict) -> None:
    db.add(AuditEvent(
        org_id=station.org_id, user="运维命令", user_id="system", role="system", action=action,
        target=station.id, time=now(),
        detail=json.dumps({"before": before, "after": after}, ensure_ascii=False, default=str)[:4000],
    ))


def hosts_of(config) -> set[str]:
    """配置里引用的全部设备主机：host、endpoint / base_url 的主机名、网络串口地址、组合工位各路由。"""
    hosts: set[str] = set()
    if isinstance(config, dict):
        for key, value in config.items():
            if key == "host" and isinstance(value, str):
                hosts.add(value.lower())
            elif key in {"endpoint", "base_url", "url"} and isinstance(value, str):
                hosts.add((urlparse(value).hostname or "").lower())
            elif key == "port" and isinstance(value, str) and "://" in value:
                hosts.add((urlparse(value).hostname or "").lower())
            else:
                hosts |= hosts_of(value)
    elif isinstance(config, list):
        for item in config:
            hosts |= hosts_of(item)
    return hosts - {""}


def _targets(args) -> dict[str, dict]:
    targets: dict[str, dict] = {}
    for spec in args.station or []:
        station_id, _, rest = spec.partition("=")
        driver, _, rest = rest.rpartition("@")
        host, port, device_id = rest.split(":")
        targets[station_id] = {"driver": driver or "sila2_v1", "host": host, "port": int(port), "device_id": device_id}
    if args.preset:
        presets = json.loads(Path(args.preset).read_text(encoding="utf-8"))["stations"]
        wanted = set(args.only or presets)
        unknown = sorted(wanted - set(presets))
        if unknown:
            raise SystemExit(f"预设里没有 {', '.join(unknown)}")
        for station_id in wanted:
            targets[station_id] = {**presets[station_id], "preset": True}
    return targets


def apply(args) -> int:
    targets = _targets(args)
    if not targets:
        print("没有要切换的工位：给 --station 或 --preset", file=sys.stderr)
        return 2
    channels = dict(item.split("=") for item in args.channels or [])
    hosts = set()
    for target in targets.values():
        hosts |= {target["host"]} if not target.get("preset") else hosts_of(target["config"])
    missing_hosts = sorted(host for host in hosts if not settings.adapter_host_allowed(host))
    if missing_hosts:
        print(f"ILCS_ADAPTER_ALLOWED_HOSTS 未包含 {', '.join(missing_hosts)}，驱动会拒绝连接；一个工位都没改", file=sys.stderr)
        return 2
    backup: dict = {}
    with SessionLocal() as db:
        _lock(db, targets)
        busy = _still_acting(db, [station_id for station_id in targets if db.get(Adapter, station_id) is not None])
        if busy:
            print(f"这些工位上还有可能仍在动作的指令，切换驱动后查不回它们：{busy}；一个工位都没改", file=sys.stderr)
            return 2
        for station_id, target in targets.items():
            station = db.get(Station, station_id)
            adapter = db.get(Adapter, station_id)
            if station is None or adapter is None:
                if target.get("preset") and args.skip_missing:
                    print(f"  跳过 {station_id}：库里还没有这个工位")
                    continue
                print(f"工位或适配器 {station_id} 不存在", file=sys.stderr)
                return 2
            before = _snapshot(adapter, station)
            backup[station_id] = before
            driver = target["driver"]
            adapter.kind, adapter.driver, adapter.version = "real", driver, "1.0"
            if target.get("preset"):
                adapter.protocol = target.get("protocol") or PROTOCOLS.get(driver, driver)
                adapter.config, adapter.credential_ref = dict(target["config"]), target.get("credential_ref", "")
                adapter.note = f"试点：{target.get('note') or adapter.protocol}"
                if target.get("channels"):
                    channels.setdefault(station_id, target["channels"])
            else:
                adapter.protocol = PROTOCOLS.get(driver, driver)
                adapter.config, adapter.credential_ref = _connection(
                    driver, target["host"], target["port"], target["device_id"], station)
                adapter.note = f"试点：外部 {adapter.protocol} 模拟设备 {target['device_id']}"
            adapter.supports_hold = adapter.supports_abort = adapter.supports_query = adapter.supports_dedup = True
            adapter.config_version += 1
            adapter.row_version += 1
            adapter.connected = False
            adapter.accepts_commands = False
            adapter.current_command_id = ""
            if station_id in channels:
                station.channels = int(channels[station_id])
                station.row_version += 1
            # 换了驱动：先过接入验收再接指令（自动排一次只读级；自报为模拟器的设备只读级就够）
            after_config_change(db, adapter, before, org_id=station.org_id, requested_by="运维命令")
            _audit(db, station, "试点切换设备适配器", before, _snapshot(adapter, station))
        db.commit()
    path = Path(args.backup_dir) / f"pilot-adapters-{now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(backup, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    reset_cache()
    print(f"已切换 {', '.join(targets)}；原配置 {path}；在线状态等执行器探测，接入验收（只读级）已自动排队")
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
        _lock(db, wanted)
        busy = _still_acting(db, wanted)
        if busy:
            print(f"这些工位上还有可能仍在动作的指令，还原驱动后查不回它们：{busy}；一个工位都没改", file=sys.stderr)
            return 2
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
            after_config_change(db, adapter, current, org_id=station.org_id, requested_by="运维命令")
            _audit(db, station, "试点还原设备适配器", current, before)
        db.commit()
    print("已还原 " + "，".join(f"{station_id}（{sources[station_id]}）" for station_id in sorted(wanted)))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["apply", "revert"])
    parser.add_argument("--station", action="append", help="apply：工位=[驱动@]主机:端口:设备ID；revert：工位")
    parser.add_argument("--preset", nargs="?", const=str(ROOT / "simulators" / "pilot-devices.json"),
                        help="按预设文件切换（缺省 simulators/pilot-devices.json）")
    parser.add_argument("--only", action="append", help="只切换预设里的这些工位")
    parser.add_argument("--skip-missing", action="store_true", help="预设里的工位库里还没有就跳过（如演示用的 ARM-01）")
    parser.add_argument("--channels", action="append", help="工位=并行通道数")
    parser.add_argument("--backup-dir", default="/data")
    args = parser.parse_args()
    verify(engine)
    return apply(args) if args.action == "apply" else revert(args)


if __name__ == "__main__":
    raise SystemExit(main())
