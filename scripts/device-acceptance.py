#!/usr/bin/env python
"""设备接入验收：对一个驱动跑统一检查清单，输出报告（Markdown，可另存 JSON）。

两种用法，检查清单相同（api/app/adapters/acceptance.py）：

    # 一、按库里登记的工位（部署环境，在 api 容器里执行）
    python scripts/device-acceptance.py ST-05                    # 只读：身份与方法目录、健康检查、契约声明、查询不存在的指令号
    python scripts/device-acceptance.py ST-07 --physical --confirm ST-07 --output /data/acceptance-ST-07.md
    python scripts/device-acceptance.py ST-07 --physical --faults   # 模拟设备的故障项目：丢回执、设备忙、联锁、失联

    # 二、不连 ILCS 库：给一份适配器登记 JSON（sila2_v1 或 http_json_v1，docs/设备适配器配置模板.md「通用结构」），
    #    设备开发者在自己电脑上对着自己的网关或驱动宿主跑
    python scripts/device-acceptance.py --adapter my-gateway.json --allow-host
    python scripts/device-acceptance.py --adapter my-gateway.json --capability cap.vacuum_dry --params '{"temp": 120}' \\
        --physical --confirm STANDALONE --allow-host

界面上的「接入验收」走同一份清单，由执行器执行、报告入库（工位配置 → 适配器配置 → 接入验收）。
真实设备跑动作项目要写 --confirm <工位>，表示现场负责人已批准（DEC-02）；自报为模拟器的设备不需要。
故障项目只对自报为模拟器的设备开放：适配器配置里登记 `simulator_control`（模拟设备统一控制口），
HTTPS 网关模拟设备不登记也行（控制接口在网关自己的 API 上）。
验收指令的编号以 ACC- 开头；按工位验收时，工位上还有可能在动作的指令就拒绝运行，不与正在跑的批次抢设备。
报告不含凭据与配置原文，只给配置摘要；退出码 0 表示没有不通过的项目。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.adapters.acceptance import (  # noqa: E402
    AcceptanceRecord, SimulatorControlInjector, default_template, injector_for, run_acceptance,
)
from app.adapters.registry import REAL_IMPLEMENTATIONS, contract_of, describe  # noqa: E402
from app.adapters.drivers.simulation import SimulationAdapter  # noqa: E402
from app.core.config import settings  # noqa: E402


class GatewaySimulatorInjector(SimulatorControlInjector):
    """HTTPS 网关模拟设备的控制接口在网关自己的 API 上（/simulator/*），与驱动同一套 TLS 与凭据。"""

    def __init__(self, record):
        config = dict(record.config or {})
        super().__init__({**config, "url": config.get("base_url")}, credential_ref=record.credential_ref or "",
                         label="模拟设备控制接口")


def _from_database(args) -> tuple[AcceptanceRecord, dict, tuple[str, ...]]:
    from app.core.db import SessionLocal, engine
    from app.core.schema import verify
    from app.models import Adapter, Station
    from app.repositories.execution import CommandRepository

    verify(engine)  # 结构不对就别动设备
    with SessionLocal() as db:
        station = db.get(Station, args.station)
        adapter = db.get(Adapter, args.station)
        if station is None or adapter is None:
            raise SystemExit(f"工位 {args.station} 不存在或没有登记适配器")
        busy = CommandRepository(db).acting_on_station(args.station)
        if busy:
            raise SystemExit(f"{args.station} 还有 {len(busy)} 条可能仍在动作的指令：等批次跑完或改到空闲时段再验收")
        return AcceptanceRecord.of(adapter), dict(station.limits or {}), tuple(sorted(station.limits or {}))


def _standalone(args) -> tuple[AcceptanceRecord, dict, tuple[str, ...]]:
    try:
        record = AcceptanceRecord.from_registration(json.loads(Path(args.adapter).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"读不了适配器登记 {args.adapter}：{exc}") from exc
    if args.allow_host:
        if settings.environment == "production":
            raise SystemExit("--allow-host 只用于开发机联调；正式环境按 ILCS_ADAPTER_ALLOWED_HOSTS")
        hosts = sorted(_hosts_of(record.config))
        settings.adapter_allowed_hosts = ",".join([settings.adapter_allowed_hosts, *hosts])
    declared = record.config.get("capabilities")
    names = set(declared) if isinstance(declared, dict) else set()
    return record, {}, tuple(sorted(names))


def _hosts_of(config) -> set[str]:
    """配置里引用的主机：host、endpoint / base_url / url 的主机名、网络串口地址、控制口。"""
    from urllib.parse import urlparse

    hosts: set[str] = set()
    if isinstance(config, dict):
        for key, value in config.items():
            if key == "host" and isinstance(value, str):
                hosts.add(value)
            elif key in {"endpoint", "base_url", "url", "port"} and isinstance(value, str) and "://" in value:
                hosts.add(urlparse(value).hostname or "")
            else:
                hosts |= _hosts_of(value)
    elif isinstance(config, list):
        for item in config:
            hosts |= _hosts_of(item)
    return hosts - {""}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("station", nargs="?", help="工位编号，如 ST-05（按库里的登记验收）")
    parser.add_argument("--adapter", help="不连 ILCS 库：适配器登记 JSON 文件")
    parser.add_argument("--allow-host", action="store_true",
                        help="不连库时把配置里的主机临时加进白名单（只限开发机，正式环境拒绝）")
    parser.add_argument("--capability", help="验收用的能力，缺省取工位（或配置 capabilities）的第一个能力")
    parser.add_argument("--params", help='验收指令的参数（JSON），缺省取工位极限的中点，如 \'{"mass": 0.0152}\'')
    parser.add_argument("--physical", action="store_true", help="加上会让设备动作的项目")
    parser.add_argument("--confirm", default="", help="真实设备跑动作项目时填工位编号，表示现场负责人已批准")
    parser.add_argument("--faults", action="store_true", help="模拟设备的故障项目（需要登记模拟设备控制口）")
    parser.add_argument("--timeout", type=float, default=None,
                        help="每个动作项目等完成的秒数（缺省取 ILCS_ACCEPTANCE_POLL_TIMEOUT_SEC）")
    parser.add_argument("--output", help="报告另存为 Markdown 文件")
    parser.add_argument("--json", dest="json_path", help="报告另存为 JSON 文件")
    args = parser.parse_args()
    if bool(args.station) == bool(args.adapter):
        raise SystemExit("给一个工位编号，或用 --adapter 给一份适配器登记（二选一）")

    record, limits, capabilities = _standalone(args) if args.adapter else _from_database(args)
    capability = args.capability or next(iter(capabilities), "")
    template = default_template(record.station_id, limits, capability, json.loads(args.params) if args.params else (
        None if limits else {}))
    if limits and capability not in limits:
        raise SystemExit(f"{record.station_id} 没有能力 {capability}（工位能力：{'、'.join(sorted(limits)) or '无'}）")

    def factory():
        if record.kind == "real":
            implementation = REAL_IMPLEMENTATIONS.get(record.driver)
            if implementation is None:
                raise SystemExit(f"驱动 {record.driver} 没有登记；已登记：{', '.join(sorted(REAL_IMPLEMENTATIONS))}")
            return implementation(record)
        return SimulationAdapter(record.station_id, record.protocol, capabilities)

    simulator = record.kind != "real"
    if not simulator:
        try:
            probe = factory()
            health = probe.healthcheck() or {}
            identity = (probe.identity() if hasattr(probe, "identity") else {}) or {}
            simulator = bool(health.get("simulator") or identity.get("simulator"))
        except SystemExit:
            raise
        except Exception:  # noqa: BLE001  读不到身份：按真实设备从严处理，读不到的原因由报告写出
            simulator = False
    if args.physical and not simulator and args.confirm != record.station_id:
        raise SystemExit(
            f"{record.station_id} 是真实设备：动作项目会让它真的动作。现场负责人批准后加 --confirm {record.station_id} 再跑"
        )
    injector, note = None, ""
    if args.faults:
        if not simulator:
            raise SystemExit("故障项目只对自报为模拟器的设备开放；真实设备请在网络路径上注入（中间代理丢应答）")
        injector, note = injector_for(record, capability)
        if injector is None:
            print(f"提示：{note}，故障项目在报告里标为跳过；可在模拟器容器里用 ilcs-devices/simulators/*/fault.py 手工注入",
                  file=sys.stderr)

    report = run_acceptance(
        record, factory, template, contract=contract_of(record, capabilities).as_dict(),
        describe=lambda instance: describe(instance, record), physical=args.physical,
        injector=injector, poll_timeout=args.timeout or settings.acceptance_poll_timeout_sec, fault_note=note,
    )
    text = report.markdown()
    print(text)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
