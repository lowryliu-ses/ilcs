"""系统级试点：同一批次的步骤分别落在 Modbus TCP、OPC UA、HTTPS 网关三台外部模拟设备上，全程走真实驱动。

ST-05 真空干燥 / 称重 → Modbus TCP（数值参数按槽位写寄存器）；ST-06 注液 → OPC UA（孔位矩阵 JSON，加密会话）；
ST-07 充放电 → HTTPS 网关（TLS + 令牌，执行器按 /health 探测在线）。
"""
import time

import pytest

from sim_harness import (
    MATERIALS, gateway_config, gateway_sim, modbus_config, modbus_sim, opcua_config, opcua_sim,
)
from test_failure_paths import running_batch  # noqa: F401  （复用 fixture）

PROTOCOLS = {"ST-05": "modbus_tcp_v1", "ST-06": "opcua_v1", "ST-07": "http_json_v1"}
FIELDS = ("kind", "driver", "protocol", "config", "credential_ref", "config_version")


@pytest.fixture()
def devices(reset_runtime, tmp_path, monkeypatch):
    from app.adapters.registry import reset_cache
    from app.core.config import settings
    from app.core.db import SessionLocal
    from app.models import Adapter
    from app.services.execution_service import ExecutorLoop

    monkeypatch.setattr(settings, "adapter_credential_root", str(tmp_path))
    with modbus_sim("SIM-VAC-T", task_seconds=0.2) as (modbus, _, modbus_port), \
            opcua_sim(tmp_path, "SIM-LH-T", profile="liquid_handler", task_seconds=0.2,
                      material_map=MATERIALS) as (opcua, _, opcua_port), \
            gateway_sim(tmp_path, "SIM-CYC-T", profile="cycler", channels=8, task_seconds=0.2) as (gateway, _, gateway_port):
        opcua_settings, opcua_credential = opcua_config(opcua_port, tmp_path, "SIM-LH-T", expected_device_id="SIM-LH-T")
        gateway_settings, gateway_credential = gateway_config(gateway_port, tmp_path, "SIM-CYC-T")
        configs = {
            "ST-05": ("Modbus TCP", modbus_config(modbus_port, expected_device_id="SIM-VAC-T"), ""),
            "ST-06": ("OPC UA", opcua_settings, opcua_credential),
            "ST-07": ("HTTPS JSON", gateway_settings, gateway_credential),
        }
        originals = {}
        with SessionLocal() as db:
            for station_id, (protocol, config, credential) in configs.items():
                adapter = db.get(Adapter, station_id)
                originals[station_id] = {key: getattr(adapter, key) for key in FIELDS}
                adapter.kind, adapter.driver, adapter.protocol = "real", PROTOCOLS[station_id], protocol
                adapter.config, adapter.credential_ref = config, credential
                adapter.config_version += 1
                adapter.connected = False  # 在线与否由执行器探测决定，不沿用旧状态
            db.commit()
        reset_cache()
        with SessionLocal() as db:
            assert ExecutorLoop(db).probe_devices() == len(PROTOCOLS), "三种协议的设备都由执行器主动探测"
            db.commit()
            assert all(db.get(Adapter, station_id).connected for station_id in PROTOCOLS)
        try:
            yield {"ST-05": modbus, "ST-06": opcua, "ST-07": gateway}
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


def _run_until(operator, batch_id, executor, devices, states, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for device in devices.values():  # 模拟设备的主循环在测试里由这里代劳
            device.tick()
        executor(simulate_heartbeat=False)
        detail = operator.get(f"/api/batches/{batch_id}").json()
        if detail["state"] in states:
            return detail
        time.sleep(0.2)
    raise AssertionError(f"批次停在 {detail['state']}：{detail['failure_reason']}")


def test_batch_runs_across_three_protocols(devices, operator, running_batch, executor):
    detail = _run_until(operator, running_batch, executor, devices, {"done"})
    origins = {c["payload"]["origin"] for c in detail["checkpoints"]}
    assert origins == {f"real:{driver}" for driver in PROTOCOLS.values()}, "每个检查点都来自对应协议的真实驱动"
    for station_id, device in devices.items():
        assert device.executions, f"{station_id} 的设备确实收到了指令"
        assert all(count == 1 for count in device.executions.values()), f"{station_id} 每条指令只动作一次"
    reservation = next(r for r in detail["reservations"])
    assert reservation["consumed_qty"] != "0.000000", "OPC UA 配液站回报的实际用量入库存"


def test_modbus_lost_ack_faults_the_batch_without_resending(devices, operator, running_batch, executor):
    """第一步在 Modbus 设备上：写了触发、等不到应答。指令判结果未知、批次挂起转人工核查，不重写触发。"""
    devices["ST-05"].set_fault("lost_receipt")
    executor(simulate_heartbeat=False)
    devices["ST-05"].set_fault("none")
    first = operator.get(f"/api/batches/{running_batch}").json()["commands"][0]
    assert (first["state"], first["delivery_state"]) == ("unknown", "maybe_sent")
    for _ in range(3):
        executor(simulate_heartbeat=False)
    detail = operator.get(f"/api/batches/{running_batch}").json()
    assert detail["state"] == "fault"
    assert devices["ST-05"].executions[first["id"]] == 1, "设备只动作一次"
    assert len([c for c in detail["commands"] if c["type"] == "dispatch"]) == 1, "没有生成新指令重试"
