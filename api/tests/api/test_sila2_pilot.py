"""系统级试点：工位接到外部 SiLA 2 模拟设备，批次从下发到完成全程走真实驱动。"""
import sys
import time
from pathlib import Path

import pytest

from test_failure_paths import running_batch  # noqa: F401  （复用 fixture）

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATIONS = ("ST-05", "ST-06", "ST-07")


@pytest.fixture()
def sila_device(reset_runtime):
    import socket

    from app.adapters.registry import reset_cache
    from app.core.db import SessionLocal
    from app.models import Adapter
    from simulators.sila_device.device import SimulatedDevice
    from simulators.sila_device.server import SimulatorRunner, parse

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    args = parse(["--device-id", "SIM-PILOT", "--profile", "liquid_handler", "--address", "127.0.0.1",
                  "--port", str(port), "--insecure", "--task-seconds", "0.2"])
    device = SimulatedDevice(
        args.device_id, args.profile, task_seconds=args.task_seconds,
        material_map={"electrolyte": {"material": "电解液 LP57", "unit": "mL", "factor": 0.001}},
    )
    runner = SimulatorRunner(args, device)
    runner.start()
    originals = {}
    with SessionLocal() as db:
        for station_id in STATIONS:
            adapter = db.get(Adapter, station_id)
            originals[station_id] = {
                key: getattr(adapter, key) for key in ("kind", "driver", "protocol", "config", "config_version")
            }
            adapter.kind, adapter.driver, adapter.protocol = "real", "sila2_v1", "SiLA 2"
            adapter.config = {"host": "127.0.0.1", "port": port, "insecure": True,
                              "request_timeout_sec": 1, "probe_interval_sec": 0.5}
            adapter.config_version += 1
            adapter.connected = False  # 在线与否由执行器探测决定，不沿用旧状态
        db.commit()
    reset_cache()
    from app.services.execution_service import ExecutorLoop

    with SessionLocal() as db:
        # 先让执行器探测一次：SiLA 设备的在线状态来自探测，不是配置时的假设
        assert ExecutorLoop(db).probe_devices() == len(STATIONS)
        db.commit()
    try:
        yield device
    finally:
        runner.stop()
        with SessionLocal() as db:
            for station_id, values in originals.items():
                adapter = db.get(Adapter, station_id)
                for key, value in values.items():
                    setattr(adapter, key, value)
                adapter.connected = True
                adapter.current_command_id = ""
            db.commit()
        reset_cache()


def _run_until(operator, batch_id, executor, states, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        executor(simulate_heartbeat=False)
        detail = operator.get(f"/api/batches/{batch_id}").json()
        if detail["state"] in states:
            return detail
        time.sleep(0.2)
    raise AssertionError(f"批次停在 {detail['state']}：{detail['failure_reason']}")


def test_batch_runs_end_to_end_on_sila_device(sila_device, operator, running_batch, executor):
    detail = _run_until(operator, running_batch, executor, {"done"})
    origins = {c["payload"]["origin"] for c in detail["checkpoints"]}
    assert origins == {"real:sila2_v1"}, "每个检查点都来自真实驱动，没有模拟回落"
    assert all(count == 1 for count in sila_device.executions.values()), "每条指令设备只动作一次"
    reservation = next(r for r in detail["reservations"])
    assert reservation["consumed_qty"] != "0.000000", "配液回报的实际用量入库存"


def test_lost_receipt_faults_without_resending(sila_device, operator, running_batch, executor):
    """回执在返回途中丢失：设备其实已经在动。指令判结果未知、批次挂起转人工核查，
    之后无论执行器跑多少轮都不重发——重发一次注液的代价不是一条日志。"""
    sila_device.set_fault("lost_receipt")
    executor(simulate_heartbeat=False)  # 投递首条指令，回执丢失
    sila_device.set_fault("none")
    first = operator.get(f"/api/batches/{running_batch}").json()["commands"][0]
    assert (first["state"], first["delivery_state"]) == ("unknown", "maybe_sent")
    for _ in range(3):
        executor(simulate_heartbeat=False)
    detail = operator.get(f"/api/batches/{running_batch}").json()
    assert detail["state"] == "fault", "结果未知时先挂起，不盲目继续"
    assert sila_device.executions[first["id"]] == 1, "设备只动作一次"
    assert len([c for c in detail["commands"] if c["type"] == "dispatch"]) == 1, "没有生成新指令重试"
