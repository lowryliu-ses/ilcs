#!/usr/bin/env python
"""设备接入验收：对一个工位的驱动跑统一检查清单，输出报告（Markdown，可另存 JSON）。

    # 只读：身份与方法目录、健康检查、契约声明、查询不存在的指令号——不会让设备动作
    python scripts/device-acceptance.py ST-05

    # 加上会让设备动作的项目：正常完成、重复提交、重建驱动后查回、保持、终止
    # 真实设备要写 --confirm <工位>，表示现场负责人已批准（DEC-02）；自报为模拟器的设备不需要
    python scripts/device-acceptance.py ST-07 --physical --confirm ST-07 --output /data/acceptance-ST-07.md

    # 模拟设备的故障项目：丢回执、设备忙、联锁、失联（目前内置 HTTPS 网关模拟设备的注入器）
    python scripts/device-acceptance.py ST-07 --physical --faults

部署环境里在 api 容器中执行：`docker compose exec api python ../scripts/device-acceptance.py ST-05`。
验收指令的编号以 ACC- 开头，与生产指令一眼可分；工位上还有在途指令时拒绝运行，不与正在跑的批次抢设备。
报告不含凭据与配置原文，只给配置摘要；退出码 0 表示没有不通过的项目。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.adapters.acceptance import run_acceptance  # noqa: E402
from app.adapters.base import CommandRequest  # noqa: E402
from app.adapters.registry import REAL_IMPLEMENTATIONS, contract_of, describe  # noqa: E402
from app.adapters.simulation import SimulationAdapter  # noqa: E402
from app.core.db import SessionLocal, engine  # noqa: E402
from app.core.schema import verify  # noqa: E402
from app.models import Adapter, Station  # noqa: E402
from app.repositories.execution import CommandRepository  # noqa: E402


class GatewaySimulatorInjector:
    """HTTPS 网关模拟设备（simulators/http_gateway）的故障注入：与驱动同一套 TLS 与凭据，调模拟器的控制接口。"""

    def __init__(self, record: Adapter):
        from app.adapters.http_client import HttpTransport

        self.transport = HttpTransport(record.config or {}, record.credential_ref or "", driver=record.driver,
                                       label="模拟设备控制接口")

    def set(self, mode: str, parameter: float = 0.0) -> None:
        self.transport.request("POST", "/simulator/fault", {"mode": mode, "parameter": parameter})

    def executions(self, command_id: str) -> int | None:
        state = self.transport.request("GET", "/simulator/state") or {}
        return int((state.get("executions") or {}).get(command_id, 0))


INJECTORS = {"http_json_v1": GatewaySimulatorInjector}


def _template(station: Station, capability: str, params: dict | None) -> CommandRequest:
    limits = (station.limits or {}).get(capability)
    if limits is None:
        raise SystemExit(f"{station.id} 没有能力 {capability}（工位能力：{'、'.join(sorted(station.limits or {})) or '无'}）")
    if params is None:
        # 缺省取工位极限的中点：落在工位承接范围内，不会因为参数越界被拒而测不到后面的项目
        params = {name: round((window[0] + window[1]) / 2, 6) for name, window in limits.items() if window}
    return CommandRequest(
        command_id="", station_id=station.id, capability=capability, params=params, type="dispatch",
        batch_id="ACCEPTANCE", step_index=0, step_id="acceptance",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("station", help="工位编号，如 ST-05")
    parser.add_argument("--capability", help="验收用的能力，缺省取工位的第一个能力")
    parser.add_argument("--params", help='验收指令的参数（JSON），缺省取工位极限的中点，如 \'{"mass": 0.0152}\'')
    parser.add_argument("--physical", action="store_true", help="加上会让设备动作的项目")
    parser.add_argument("--confirm", default="", help="真实设备跑动作项目时填工位编号，表示现场负责人已批准")
    parser.add_argument("--faults", action="store_true", help="模拟设备的故障项目（需要该驱动有内置注入器）")
    parser.add_argument("--timeout", type=float, default=60.0, help="等动作完成的秒数")
    parser.add_argument("--output", help="报告另存为 Markdown 文件")
    parser.add_argument("--json", dest="json_path", help="报告另存为 JSON 文件")
    args = parser.parse_args()

    verify(engine)  # 结构不对就别动设备
    with SessionLocal() as db:
        station = db.get(Station, args.station)
        record = db.get(Adapter, args.station)
        if station is None or record is None:
            raise SystemExit(f"工位 {args.station} 不存在或没有登记适配器")
        busy = CommandRepository(db).in_flight(args.station)
        if busy:
            raise SystemExit(f"{args.station} 还有 {len(busy)} 条在途指令：等批次跑完或改到空闲时段再验收")
        capability = args.capability or next(iter(sorted(station.limits or {})), "")
        template = _template(station, capability, json.loads(args.params) if args.params else None)
        capabilities = tuple(sorted(station.limits or {}))
        db.expunge(record)

    def factory():
        if record.kind == "real":
            implementation = REAL_IMPLEMENTATIONS.get(record.driver)
            if implementation is None:
                raise SystemExit(f"驱动 {record.driver} 没有登记")
            return implementation(record)
        return SimulationAdapter(record.station_id, record.protocol, capabilities)

    simulator = record.kind != "real"
    if not simulator:
        try:
            probe = factory()
            simulator = bool(((probe.identity() if hasattr(probe, "identity") else {}) or {}).get("simulator"))
        except Exception:  # noqa: BLE001  读不到身份：按真实设备从严处理，读不到的原因由报告写出
            simulator = False
    if args.physical and not simulator and args.confirm != args.station:
        raise SystemExit(
            f"{args.station} 是真实设备：动作项目会让它真的动作。现场负责人批准后加 --confirm {args.station} 再跑"
        )
    injector = None
    if args.faults:
        if not simulator:
            raise SystemExit("故障项目只对自报为模拟器的设备开放；真实设备请在网络路径上注入（中间代理丢应答）")
        builder = INJECTORS.get(record.driver)
        if builder is None:
            print(f"提示：{record.driver} 没有内置故障注入器，故障项目在报告里标为跳过；"
                  f"可在模拟器容器里用 simulators/*/fault.py 手工注入后观察系统反应", file=sys.stderr)
        else:
            injector = builder(record)

    report = run_acceptance(
        record, factory, template, contract=contract_of(record, capabilities).as_dict(),
        describe=lambda instance: describe(instance, record), physical=args.physical,
        injector=injector, poll_timeout=args.timeout,
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
