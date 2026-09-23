#!/usr/bin/env python
"""试点运维：把工位适配器切到外部 SiLA 2 设备，或还原为切换前的配置。

界面上修改适配器要求管理员电子签名；这个命令用于部署窗口里由运维执行的试点切换，
每个工位写一条系统来源的审计（含前后配置），不绕过留痕。切换后适配器先标为离线，
在线与否由执行器探测决定——不沿用切换前的「在线」结论。

    python scripts/configure-pilot-adapters.py apply \\
        --station ST-06=sila-sim-lh:50052:SIM-LH-01 \\
        --station ST-07=sila-sim-cycler:50053:SIM-CYC-01 --channels ST-07=8
    python scripts/configure-pilot-adapters.py revert --station ST-06 --station ST-07

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
CERT_DIR = "/run/secrets/ilcs/sila"


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
        host, port, device_id = rest.split(":")
        targets[station_id] = (host, int(port), device_id)
    channels = dict(item.split("=") for item in args.channels or [])
    missing_hosts = sorted({host for host, _, _ in targets.values()} - settings.adapter_allowed_host_set)
    if missing_hosts:
        print(f"ILCS_ADAPTER_ALLOWED_HOSTS 未包含 {', '.join(missing_hosts)}，驱动会拒绝连接", file=sys.stderr)
        return 2
    backup: dict = {}
    with SessionLocal() as db:
        for station_id, (host, port, device_id) in targets.items():
            station = db.get(Station, station_id)
            adapter = db.get(Adapter, station_id)
            if station is None or adapter is None:
                print(f"工位或适配器 {station_id} 不存在", file=sys.stderr)
                return 2
            before = _snapshot(adapter, station)
            backup[station_id] = before
            adapter.kind, adapter.driver, adapter.protocol, adapter.version = "real", "sila2_v1", "SiLA 2", "1.0"
            adapter.config = {
                "host": host, "port": port, "ca_file": f"{CERT_DIR}/{device_id}.crt",
                "expected_device_id": device_id, "request_timeout_sec": 10, "probe_interval_sec": 10,
            }
            adapter.credential_ref = ""
            adapter.supports_hold = adapter.supports_abort = adapter.supports_query = adapter.supports_dedup = True
            adapter.note = f"试点：外部 SiLA 2 模拟设备 {device_id}"
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
    backup = json.loads(files[-1].read_text(encoding="utf-8"))
    wanted = set(args.station or backup)
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
    print(f"已按 {files[-1].name} 还原 {', '.join(sorted(wanted))}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["apply", "revert"])
    parser.add_argument("--station", action="append", help="apply：工位=主机:端口:设备ID；revert：工位")
    parser.add_argument("--channels", action="append", help="工位=并行通道数")
    parser.add_argument("--backup-dir", default="/data")
    args = parser.parse_args()
    verify(engine)
    return apply(args) if args.action == "apply" else revert(args)


if __name__ == "__main__":
    raise SystemExit(main())
