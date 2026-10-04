"""试点设备预设（设备仓库的 simulators/pilot-devices.json）：每个示例工位的驱动与连接配置。

- 每条预设都能构造出驱动（主机、证书、凭据位置都校验）；ILCS 只经 sila2_v1、http_json_v1 接设备，预设里没有别的驱动；
- 一个绑定托盘的批次按预设走真实驱动：ST-06 注液走 SiLA 2，ST-07 充放电走 HTTPS 网关（厂家 SDK 接口服务）；
  ST-05（干燥与称重）、AGV 转运没有预设，用内置模拟适配器。
"""
import importlib.util
import json
import time
from pathlib import Path

import pytest

from sim_harness import DEVICES, DEVICES_FOUND, needs_devices
from tests.api.test_labware_transfer import _batch_with_labware, _dispatch, clean_labware  # noqa: F401

pytestmark = needs_devices
ROOT = Path(__file__).resolve().parents[3]
PRESET_FILE = DEVICES / "simulators" / "pilot-devices.json"
PRESETS = json.loads(PRESET_FILE.read_text(encoding="utf-8"))["stations"] if DEVICES_FOUND else {}
SECRETS = "/run/secrets/ilcs"


def _pilot_script():
    spec = importlib.util.spec_from_file_location("configure_pilot_adapters", ROOT / "scripts" / "configure-pilot-adapters.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preset_is_read_from_the_devices_repo_or_stdin(monkeypatch, tmp_path):
    """api 容器里没有设备仓库：`--preset -` 从标准输入读；给的文件不存在时说清楚怎么办，不抛 FileNotFoundError。"""
    import io

    script = _pilot_script()
    assert Path(script.PRESET).resolve() == PRESET_FILE.resolve(), "缺省读设备仓库里的预设，和测试找的是同一个设备仓库"
    monkeypatch.setattr("sys.stdin", io.StringIO(PRESET_FILE.read_text(encoding="utf-8")))
    assert script._read_preset("-") == PRESETS
    with pytest.raises(SystemExit, match="ILCS_DEVICES"):
        script._read_preset(str(tmp_path / "missing.json"))


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
        # 设备实现 ILCS 契约：能力与参数按指令原样下发，不在驱动里映射；工位要在种子里
        assert station_id in stations, f"预设的工位 {station_id} 不在种子里"


@pytest.fixture()
def pilot(reset_runtime, tmp_path, monkeypatch):
    """按预设把 ST-06 / ST-07 接到本机模拟设备；ST-05、AGV 照种子用内置模拟适配器。"""
    from app.adapters.registry import reset_cache
    from app.core.config import settings
    from app.core.db import SessionLocal
    from app.models import Adapter
    from app.services.execution_service import ExecutorLoop
    from sim_harness import _device, free_port
    from simulators.http_gateway.server import SimulatorRunner as GatewayRunner, parse as gateway_args
    from simulators.sila_device.server import SimulatorRunner as SilaRunner, parse as sila_args

    monkeypatch.setattr(settings, "adapter_credential_root", str(tmp_path))
    ports = {name: free_port() for name in ("sila", "gateway")}
    common = ["--address", "127.0.0.1", "--host-name", "127.0.0.1"]
    sila = SilaRunner(sila_args(["--device-id", "SIM-LH-01", "--profile", "liquid_handler", "--port", str(ports["sila"]),
                                 "--cert-dir", str(tmp_path / "sila"), *common]),
                      _device("SIM-LH-01", "liquid_handler", task_seconds=0.2, material_map={
                          "electrolyte": {"material": "电解液 LP57", "unit": "mL", "factor": 0.001}}))
    gateway = GatewayRunner(gateway_args(["--device-id", "SIM-CYC-01", "--profile", "cycler", "--channels", "8",
                                          "--port", str(ports["gateway"]), "--cert-dir", str(tmp_path / "gateway"), *common]),
                            _device("SIM-CYC-01", "cycler", task_seconds=0.3, channels=8))
    runners = [sila, gateway]
    for runner in runners:
        runner.start()
    endpoints = {("sila-sim-lh", 50052): ports["sila"], ("gateway-sim-cycler", 8443): ports["gateway"]}
    stations = ("ST-06", "ST-07")
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
        yield {"sila": sila, "gateway": gateway}
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
        pilot["sila"].device.tick()
        pilot["gateway"].device.tick()
        executor(simulate_heartbeat=False)
        detail = operator.get(f"/api/batches/{batch_id}").json()
        if detail["state"] in {"done", "fault"}:
            break
        time.sleep(0.2)
    assert detail["state"] == "done", detail.get("failure_reason")

    origins = {c["payload"]["origin"] for c in detail["checkpoints"]}
    assert {"real:sila2_v1", "real:http_json_v1", "simulation"} <= origins, origins
    assert detail["labware"]["location_id"] == "ST-07/N1", "托盘按转运回执送到充放电柜"
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
    blocked = Namespace(station=None, preset=str(PRESET_FILE), only=None,
                        skip_missing=True, channels=None, backup_dir=str(tmp_path))
    assert script.apply(blocked) == 2, "白名单缺主机：一个工位都不改"

    monkeypatch.setattr(settings, "adapter_allowed_hosts", ",".join(sorted(hosts)))
    with SessionLocal() as db:
        before = {row.station_id: (row.kind, row.driver) for row in db.query(Adapter).all()}
    assert script.apply(blocked) == 0
    with SessionLocal() as db:
        for station_id, preset in PRESETS.items():
            adapter = db.get(Adapter, station_id)
            assert (adapter.kind, adapter.driver) == ("real", preset["driver"])
            assert adapter.config == preset["config"] and adapter.credential_ref == preset.get("credential_ref", "")
            assert adapter.connected is False, "在线与否等执行器探测"
        assert db.get(Station, "ST-07").channels == 8
        audits = db.query(AuditEvent).filter(AuditEvent.action == "试点切换设备适配器").count()
        assert audits >= len(PRESETS)

    revert = Namespace(station=None, preset=None, only=None, skip_missing=False, channels=None,
                       backup_dir=str(tmp_path))
    assert script.revert(revert) == 0
    with SessionLocal() as db:
        assert {row.station_id: (row.kind, row.driver) for row in db.query(Adapter).all()} == before


def test_simulate_switches_stations_back_to_the_builtin_adapter(tmp_path, monkeypatch, reset_runtime):
    """运维切回内置模拟（驱动删掉了、或工位不再接外部模拟设备）：清掉驱动与连接配置，留审计、写备份，revert 能还原。"""
    from argparse import Namespace

    from app.core.config import settings
    from app.core.db import SessionLocal
    from app.models import AcceptanceRun, Adapter, AuditEvent

    script = _pilot_script()
    monkeypatch.setattr(settings, "adapter_allowed_hosts", ",".join(sorted(script.hosts_of(PRESETS["ST-06"]["config"]))))
    fields = ("kind", "driver", "protocol", "version", "config", "credential_ref", "note", "acceptance_required")
    with SessionLocal() as db:
        seeded = {field: getattr(db.get(Adapter, "ST-06"), field) for field in fields}
    real = Namespace(station=None, preset=str(PRESET_FILE), only=["ST-06"],
                     skip_missing=True, channels=None, backup_dir=str(tmp_path))
    assert script.apply(real) == 0
    with SessionLocal() as db:
        assert db.get(Adapter, "ST-06").driver == PRESETS["ST-06"]["driver"]
        assert db.query(AcceptanceRun).filter(AcceptanceRun.station_id == "ST-06", AcceptanceRun.state == "queued").count()

    assert script.simulate(Namespace(station=None, backup_dir=str(tmp_path))) == 2, "不给工位不动"
    assert script.simulate(Namespace(station=["ST-06", "NOPE"], backup_dir=str(tmp_path))) == 2
    with SessionLocal() as db:
        assert db.get(Adapter, "ST-06").kind == "real", "有一个工位不存在就一个都不改"
    assert script.simulate(Namespace(station=["ST-06"], backup_dir=str(tmp_path))) == 0
    with SessionLocal() as db:
        adapter = db.get(Adapter, "ST-06")
        assert (adapter.kind, adapter.driver, adapter.protocol) == ("simulation", "simulation", "内置模拟")
        assert adapter.config == {} and adapter.credential_ref == "" and adapter.acceptance_required == ""
        assert adapter.connected and adapter.accepts_commands
        assert not db.query(AcceptanceRun).filter(AcceptanceRun.station_id == "ST-06", AcceptanceRun.state == "queued").count()
        assert db.query(AuditEvent).filter(AuditEvent.action == "试点切回内置模拟", AuditEvent.target == "ST-06").count() == 1

    # 两份备份：revert 取包含 ST-06 的最近一份，还原的是切回模拟之前（按预设接的真实驱动）
    assert len(list(tmp_path.glob("pilot-adapters-*.json"))) >= 1
    revert = Namespace(station=["ST-06"], preset=None, only=None, skip_missing=False, channels=None, backup_dir=str(tmp_path))
    assert script.revert(revert) == 0
    with SessionLocal() as db:
        adapter = db.get(Adapter, "ST-06")
        assert adapter.driver == PRESETS["ST-06"]["driver"]
        # 交还种子里的模拟适配器；验收记录只追加不删，排着的作废
        for field, value in seeded.items():
            setattr(adapter, field, value)
        adapter.connected = adapter.accepts_commands = True
        db.query(AcceptanceRun).filter(AcceptanceRun.station_id == "ST-06", AcceptanceRun.state == "queued").update(
            {"state": "cancelled"}, synchronize_session=False)
        db.commit()
