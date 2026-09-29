"""试点设备预设（devices/simulators/pilot-devices.json）：每个示例工位的驱动与连接配置。

- 每条预设都能构造出驱动（主机、证书、凭据位置都校验），声明的能力与参数和种子里的工位极限对得上；
- 一个绑定托盘的批次按预设全程走真实驱动：AGV 转运走车队 REST（rest_map_v1），ST-05 干燥箱走串口命令、
  天平走 MT-SICS（composite_v1），ST-06 注液走 SiLA 2，ST-07 充放电走 HTTPS 网关（厂家 SDK 接口服务）。
"""
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

from tests.api.test_labware_transfer import _batch_with_labware, _dispatch, clean_labware  # noqa: F401

ROOT = Path(__file__).resolve().parents[3]
DEVICES = ROOT / "devices"  # simulators、connectors 包所在的目录
if str(DEVICES) not in sys.path:
    sys.path.insert(0, str(DEVICES))
PRESETS = json.loads((ROOT / "devices" / "simulators" / "pilot-devices.json").read_text(encoding="utf-8"))["stations"]
SECRETS = "/run/secrets/ilcs"
CONTRACT_DRIVERS = {"sila2_v1", "opcua_v1", "http_json_v1", "modbus_tcp_v1"}


def _pilot_script():
    spec = importlib.util.spec_from_file_location("configure_pilot_adapters", ROOT / "scripts" / "configure-pilot-adapters.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def localize(value, endpoints: dict, secrets: Path):
    """把预设里的服务名:端口换成本机模拟设备，证书目录换成测试目录。"""
    if isinstance(value, dict):
        out = {key: localize(item, endpoints, secrets) for key, item in value.items()}
        if (out.get("host"), out.get("port")) in endpoints:
            out["host"], out["port"] = "127.0.0.1", endpoints[(out["host"], out["port"])]
        return out
    if isinstance(value, list):
        return [localize(item, endpoints, secrets) for item in value]
    if isinstance(value, str):
        for (host, port), local in endpoints.items():
            value = value.replace(f"{host}:{port}", f"127.0.0.1:{local}")
        return value.replace(SECRETS, str(secrets))
    return value


def _capabilities(preset: dict) -> dict[str, dict]:
    config = preset["config"]
    if preset["driver"] == "composite_v1":
        return {cap: route["config"]["capabilities"].get(cap, {}) for route in config["routes"] for cap in route["capabilities"]}
    return config.get("capabilities") or {}


def test_every_preset_matches_its_station_and_builds_its_driver(tmp_path, monkeypatch, db):
    from app.adapters.registry import REAL_IMPLEMENTATIONS
    from app.core.config import settings
    from app.models import Station
    from simulators.common.certs import self_signed_certificate
    from sim_harness import record

    hosts = set()
    for preset in PRESETS.values():
        hosts |= _pilot_script().hosts_of(preset["config"])
    monkeypatch.setattr(settings, "adapter_allowed_hosts", ",".join(sorted(hosts)))
    monkeypatch.setattr(settings, "adapter_credential_root", str(tmp_path))
    monkeypatch.setattr(settings, "adapter_state_root", str(tmp_path / "state"))
    # 预设引用的证书 / 令牌：CA 要能真正加载，其余只校验存在
    for preset in PRESETS.values():
        for path in json.dumps(preset).split('"'):
            if path.startswith(SECRETS) or path.startswith(f"file://{SECRETS}"):
                local = Path(path.replace("file://", "").replace(SECRETS, str(tmp_path)))
                local.parent.mkdir(parents=True, exist_ok=True)
                if local.suffix == ".crt":
                    local.write_bytes(self_signed_certificate("localhost", "ILCS test")[1])
                elif local.name == "ilcs-client.json":
                    local.write_text('{"certificate": "ilcs-client.crt", "private_key": "ilcs-client.key"}')
                    for name in ("ilcs-client.crt", "ilcs-client.key"):
                        (local.parent / name).write_text("placeholder")
                else:
                    local.write_text("placeholder")

    stations = {row.id: row for row in db.query(Station).all()}
    for station_id, preset in PRESETS.items():
        assert preset["driver"] in REAL_IMPLEMENTATIONS, f"{station_id} 的驱动 {preset['driver']} 没有登记"
        config = localize(preset["config"], {}, tmp_path)
        credential = localize(preset.get("credential_ref", ""), {}, tmp_path)
        REAL_IMPLEMENTATIONS[preset["driver"]](record(preset["protocol"], config, credential))  # 构造即校验
        if station_id not in stations:
            assert station_id == "ARM-01", "只有演示用机械臂由导入脚本登记"
            continue
        limits = stations[station_id].limits or {}
        declared = _capabilities(preset)
        if preset["driver"] in CONTRACT_DRIVERS:
            continue  # 设备实现 ILCS 契约：能力与参数按指令原样下发，不在驱动里映射
        assert set(declared) <= set(limits), f"{station_id} 预设的能力 {set(declared) - set(limits)} 不在工位极限里"
        for capability, spec in declared.items():
            written = set((spec.get("write") or {}).keys())
            assert written <= set(limits[capability]), f"{station_id} {capability} 写了工位没有的参数 {written}"
        if preset["driver"] == "composite_v1":
            assert set(declared) == set(limits), "组合工位的每项能力都要有一条路由"


@pytest.fixture()
def pilot(reset_runtime, tmp_path, monkeypatch):
    """按预设把 ST-05 / ST-06 / ST-07 / AGV 接到本机模拟设备。"""
    from app.adapters.registry import reset_cache
    from app.core.config import settings
    from app.core.db import SessionLocal
    from app.models import Adapter
    from app.services.execution_service import ExecutorLoop
    from sim_harness import _device, free_port
    from simulators.fleet.server import SimulatorRunner as FleetRunner, parse as fleet_args
    from simulators.http_gateway.server import SimulatorRunner as GatewayRunner, parse as gateway_args
    from simulators.line_device.server import SimulatorRunner as LineRunner, parse as line_args
    from simulators.mt_sics.server import Balance, SimulatorRunner as BalanceRunner, parse as balance_args
    from simulators.sila_device.server import SimulatorRunner as SilaRunner, parse as sila_args

    monkeypatch.setattr(settings, "adapter_credential_root", str(tmp_path))
    ports = {name: free_port() for name in ("oven", "balance", "sila", "gateway", "fleet")}
    common = ["--address", "127.0.0.1", "--host-name", "127.0.0.1"]
    oven = LineRunner(line_args(["--dialect", "oven", "--device-id", "SIM-OVEN-01", "--port", str(ports["oven"]),
                                 "--address", "127.0.0.1"]),
                      _device("SIM-OVEN-01", task_seconds=0.3, methods=[{"program": "VD-120"}, {"program": "VD-90"}]))
    balance = BalanceRunner(balance_args(["--device-id", "SIM-BAL-01", "--port", str(ports["balance"]), "--address", "127.0.0.1"]),
                            Balance("SIM-BAL-01", "XPR226", 0.0152))
    sila = SilaRunner(sila_args(["--device-id", "SIM-LH-01", "--profile", "liquid_handler", "--port", str(ports["sila"]),
                                 "--cert-dir", str(tmp_path / "sila"), *common]),
                      _device("SIM-LH-01", "liquid_handler", task_seconds=0.2, material_map={
                          "electrolyte": {"material": "电解液 LP57", "unit": "mL", "factor": 0.001}}))
    gateway = GatewayRunner(gateway_args(["--device-id", "SIM-CYC-01", "--profile", "cycler", "--channels", "8",
                                          "--port", str(ports["gateway"]), "--cert-dir", str(tmp_path / "gateway"), *common]),
                            _device("SIM-CYC-01", "cycler", task_seconds=0.3, channels=8))
    fleet = FleetRunner(fleet_args(["--robots", "AGV-01,AGV-02", "--port", str(ports["fleet"]), "--address", "127.0.0.1",
                                    "--task-seconds", "0.3", "--cert-dir", str(tmp_path / "fleet")]))
    runners = [oven, balance, sila, gateway, fleet]
    for runner in runners:
        runner.start()
    endpoints = {("line-sim-oven", 4001): ports["oven"], ("mtsics-sim-balance", 4305): ports["balance"],
                 ("sila-sim-lh", 50052): ports["sila"], ("gateway-sim-cycler", 8443): ports["gateway"],
                 ("fleet-sim", 8080): ports["fleet"]}
    stations = ("ST-05", "ST-06", "ST-07", "AGV-01", "AGV-02")
    originals = {}
    with SessionLocal() as db:
        for station_id in stations:
            preset = PRESETS[station_id]
            adapter = db.get(Adapter, station_id)
            originals[station_id] = {key: getattr(adapter, key) for key in
                                     ("kind", "driver", "protocol", "config", "credential_ref", "config_version")}
            adapter.kind, adapter.driver, adapter.protocol = "real", preset["driver"], preset["protocol"]
            adapter.config = localize(preset["config"], endpoints, tmp_path)
            adapter.config["probe_interval_sec"] = 0.5
            adapter.credential_ref = localize(preset.get("credential_ref", ""), endpoints, tmp_path)
            adapter.config_version += 1
            adapter.connected = False
        db.commit()
    reset_cache()
    with SessionLocal() as db:
        assert ExecutorLoop(db).probe_devices() == len(stations)
        db.commit()
        offline = {s: db.get(Adapter, s).note for s in stations if not db.get(Adapter, s).connected}
        assert not offline, f"按预设接入后没有上线：{offline}"
    try:
        yield {"oven": oven, "balance": balance, "sila": sila, "gateway": gateway, "fleet": fleet}
    finally:
        with SessionLocal() as db:
            for station_id, values in originals.items():
                adapter = db.get(Adapter, station_id)
                for key, value in values.items():
                    setattr(adapter, key, value)
                adapter.connected = True
                adapter.current_command_id = ""
            db.commit()
        reset_cache()
        for runner in runners:
            (getattr(runner, "close", None) or runner.stop)()


def test_tray_batch_runs_through_every_preset_driver(pilot, operator, clean_labware, executor):
    batch_id, labware = _batch_with_labware(operator)
    assert _dispatch(operator, batch_id).status_code == 200
    deadline = time.monotonic() + 60
    detail = {}
    while time.monotonic() < deadline:
        pilot["oven"].device.tick()
        pilot["sila"].device.tick()
        pilot["gateway"].device.tick()
        executor(simulate_heartbeat=False)
        detail = operator.get(f"/api/batches/{batch_id}").json()
        if detail["state"] in {"done", "fault"}:
            break
        time.sleep(0.2)
    assert detail["state"] == "done", detail.get("failure_reason")

    origins = {c["payload"]["origin"] for c in detail["checkpoints"]}
    assert {"real:line_command_v1", "real:mt_sics_v1", "real:sila2_v1", "real:http_json_v1"} <= origins
    assert detail["labware"]["location_id"] == "ST-07/N1", "托盘由车队按转运回执送到充放电柜"
    fleet = pilot["fleet"].fleet
    missions = [m for robot in fleet.robots.values() for m in robot.queue]
    assert len(missions) == 3 and all(m["state"] == "Done" for m in missions), "板库 → ST-05 → ST-06 → ST-07 三次转运"
    assert all(m["message"].startswith("ILCS ") for m in missions), "车队任务里带着 ILCS 指令号"
    assert sum(pilot["oven"].device.executions.values()) == 1 and pilot["balance"].balance.weighings == 1
    assert all(count == 1 for count in pilot["sila"].device.executions.values())
    reservation = next(r for r in detail["reservations"])
    assert reservation["consumed_qty"] != "0.000000", "SiLA 配液站回报的实际用量入库存"


def test_preset_switch_is_audited_and_reverts(tmp_path, monkeypatch, reset_runtime):
    """运维切换脚本按预设一次改完全部示例工位（留审计、先标离线），revert 按每个工位的备份还原。"""
    from argparse import Namespace

    from app.core.config import settings
    from app.core.db import SessionLocal
    from app.models import Adapter, AuditEvent, Station

    script = _pilot_script()
    hosts = set()
    for preset in PRESETS.values():
        hosts |= script.hosts_of(preset["config"])
    monkeypatch.setattr(settings, "adapter_allowed_hosts", "127.0.0.1")
    blocked = Namespace(station=None, preset=str(ROOT / "devices" / "simulators" / "pilot-devices.json"), only=None,
                        skip_missing=True, channels=None, backup_dir=str(tmp_path))
    assert script.apply(blocked) == 2, "白名单缺主机：一个工位都不改"

    monkeypatch.setattr(settings, "adapter_allowed_hosts", ",".join(sorted(hosts)))
    with SessionLocal() as db:
        before = {row.station_id: (row.kind, row.driver) for row in db.query(Adapter).all()}
    assert script.apply(blocked) == 0
    with SessionLocal() as db:
        for station_id, preset in PRESETS.items():
            if station_id == "ARM-01":
                continue
            adapter = db.get(Adapter, station_id)
            assert (adapter.kind, adapter.driver) == ("real", preset["driver"])
            assert adapter.config == preset["config"] and adapter.credential_ref == preset.get("credential_ref", "")
            assert adapter.connected is False, "在线与否等执行器探测"
        assert db.get(Station, "ST-07").channels == 8
        audits = db.query(AuditEvent).filter(AuditEvent.action == "试点切换设备适配器").count()
        assert audits >= len(PRESETS) - 1

    revert = Namespace(station=None, preset=None, only=None, skip_missing=False, channels=None,
                       backup_dir=str(tmp_path))
    assert script.revert(revert) == 0
    with SessionLocal() as db:
        assert {row.station_id: (row.kind, row.driver) for row in db.query(Adapter).all()} == before
