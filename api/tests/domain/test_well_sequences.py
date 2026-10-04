"""逐孔依次执行：ILCS 矩阵条件让一条指令带逐孔参数（`params.wells`），一次只做一个设定的设备由映射驱动按孔位顺序
一个一个跑，回执按孔位回报。PLC 点表（OPC UA / Modbus，真实协议）、只写设定值的动作、REST 接口各走一遍。"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest

from sim_harness import plc_config, plc_sim, record, request

WELLS = {"A2": {"temp": 120}, "A1": {"temp": 110}, "A10": {"temp": 140}}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "adapter_state_root", str(tmp_path / "adapter-state"))
    monkeypatch.setattr(settings, "adapter_credential_root", str(tmp_path))
    return tmp_path


def _plc(protocol: str, port: int, **config):
    if protocol == "opcua":
        from app.adapters.drivers.opcua_map import OpcUaMapAdapter as Adapter
    else:
        from app.adapters.drivers.modbus_map import ModbusMapAdapter as Adapter
    settings_, credential = plc_config(protocol, port, **config)
    return Adapter(record("PLC 点表", settings_, credential))


def _wait(adapter, command_id: str, seconds: float = 20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = adapter.query(command_id)
        if result is not None and result.state in {"done", "failed"}:
            return result
        time.sleep(0.05)
    raise AssertionError(f"指令 {command_id} 没有在时限内结束")


@pytest.mark.parametrize("protocol", ("opcua", "modbus"))
def test_plc_runs_each_well_in_turn_and_reports_each(protocol):
    with plc_sim(protocol) as (program, _, port):
        adapter = _plc(protocol, port)
        params = {"thickness": 180, "wells": WELLS}
        assert adapter.submit(request("CMD-W", params=params, capability="cap.coat")).state == "accepted"
        done = _wait(adapter, "CMD-W")
        assert done.state == "done", done
        wells = done.delivered["wells"]
        assert set(wells) == {"A1", "A2", "A10"}
        assert all(abs(wells[well]["temp"] - row["temp"]) < 2.5 for well, row in WELLS.items())
        assert all(abs(row["thickness"] - 180) < 2.5 for row in wells.values()), "固定参数是每孔的缺省值"
        assert sum(program.device.executions.values()) == 3, "每孔一个启动沿"
        assert program.memory["JobLatched"] == "CMD-W/3", "每孔写自己的运行号，按 A1、A2、A10 的顺序"
        assert len(done.telemetry) == 6, "每孔的实测值都进遥测，设定值取各孔自己的"


def test_a_well_the_plc_refuses_ends_the_command_and_says_which():
    with plc_sim("modbus") as (program, _, port):
        adapter = _plc("modbus", port)
        adapter.submit(request("CMD-F", params={"thickness": 180, "wells": WELLS}, capability="cap.coat"))
        program.device.fault = "busy"  # 第一孔已经在跑；后面的孔启动时 PLC 拒绝
        failed = _wait(adapter, "CMD-F")
        assert failed.state == "failed"
        assert set(failed.delivered["wells"]) == {"A1"}, "做完的孔照样回报"
        assert "第 2/3 孔（A2）" in failed.error and "后面 1 孔没有执行" in failed.error, failed.error
        assert sum(program.device.executions.values()) == 1


def test_one_bad_well_rejects_the_whole_command_before_anything_moves():
    from app.adapters import AdapterError

    with plc_sim("modbus") as (program, _, port):
        adapter = _plc("modbus", port)
        bad = {"thickness": 180, "wells": {"A1": {"temp": 110}, "A2": {"pressure": 3}}}
        with pytest.raises(AdapterError, match="孔位 A2"):
            adapter.submit(request("CMD-B", params=bad, capability="cap.coat"))
        assert sum(program.device.executions.values()) == 0, "整条拒绝，设备一次都没动"


def test_write_only_setpoint_runs_each_well_without_a_start_signal():
    """设定类动作（温控器设定值）：写完设定点就生效、没有启动信号；每孔写自己的设定、回读。"""
    from app.adapters import AdapterError

    with plc_sim("modbus") as (program, _, port):
        setpoint = {"write": {"temp": "sp_temp"}, "start": {"write_only": True}, "idle_after_start": "done",
                    "actuals": {"temp": "sp_temp"}}
        settings_, credential = plc_config("modbus", port)
        settings_["capabilities"] = {"cap.set": setpoint}
        from app.adapters.drivers.modbus_map import ModbusMapAdapter

        adapter = ModbusMapAdapter(record("PLC 点表", settings_, credential))
        adapter.submit(request("CMD-S", params={"wells": WELLS}, capability="cap.set"))
        done = _wait(adapter, "CMD-S")
        assert done.state == "done", done
        assert {well: row["temp"] for well, row in done.delivered["wells"].items()} == {"A1": 110, "A2": 120, "A10": 140}
        assert sum(program.device.executions.values()) == 0, "没有启动信号：PLC 一次作业都没开"

        settings_["capabilities"] = {"cap.set": {**setpoint, "idle_after_start": ""}}
        with pytest.raises(AdapterError, match="idle_after_start"):
            ModbusMapAdapter(record("PLC 点表", settings_, credential))


@contextmanager
def setpoint_device():
    """模仿 ProtoForge 的 HTTP 设备：GET /<点> 读 {value}，POST /<点> 写，GET /status 报 normal。"""
    state = {"temperature": 25.0, "status": "normal"}
    posts: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._reply({"name": self.path.strip("/"), "value": state[self.path.strip("/")]})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            posts.append({"path": self.path, **payload})
            state[self.path.strip("/")] = payload["value"]
            self._reply({"ok": True, "value": payload["value"]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], state, posts
    finally:
        server.shutdown()


def test_rest_setpoint_runs_each_well_and_reads_the_actual_from_a_point(monkeypatch):
    from app.adapters.drivers.rest_map import RestMapAdapter
    from app.core.config import settings

    monkeypatch.setattr(settings, "adapter_allowed_hosts", "127.0.0.1")
    with setpoint_device() as (port, state, posts):
        config = {
            "base_url": f"http://127.0.0.1:{port}", "allow_insecure_http": True,
            "points": {"temperature": {"path": "/temperature", "field": "value", "unit": "℃"}},
            "capabilities": {"cap.temp_set": {
                "method": "POST", "path": "/temperature", "body": {"value": "{temp}"}, "idle_after_start": "done",
                "actuals": {"temp": {"point": "temperature"}}}},
            "status": {"path": "/status", "field": "value", "states": {"normal": "idle", "error": "failed"}},
        }
        adapter = RestMapAdapter(record("REST 接口映射", config))
        adapter.submit(request("CMD-H", params={"wells": WELLS}, capability="cap.temp_set"))
        done = _wait(adapter, "CMD-H")
        assert done.state == "done", done
        assert {well: row["temp"] for well, row in done.delivered["wells"].items()} == {"A1": 110, "A2": 120, "A10": 140}
        assert [row["value"] for row in posts] == [110, 120, 140], "按孔位顺序一孔一个设定，数值保持数类型"
        assert state["temperature"] == 140
