"""映射驱动的两层：只配点表就能接（读值、手动写声明了可写的点）；参与自动流程才要能力映射与状态点。

PLC 点表（OPC UA / Modbus）真实走协议连外部 PLC 模拟设备；REST、串口命令各起一个最小的假设备。
"""
from contextlib import contextmanager
import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plugin_harness import free_port, plc_modbus_points, plc_opcua_points, plc_sim, record, request



@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from ilcs_host.settings import settings

    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "adapter-state"))
    monkeypatch.setattr(settings, "credential_root", str(tmp_path))
    return tmp_path


def _adapter(driver: str, config: dict):
    from ilcs_host.plugins.line_command import LineCommandAdapter
    from ilcs_host.plugins.modbus_map import ModbusMapAdapter
    from ilcs_host.plugins.opcua_map import OpcUaMapAdapter
    from ilcs_host.plugins.rest_map import RestMapAdapter

    plugins = {"opcua_map_v1": OpcUaMapAdapter, "modbus_map_v1": ModbusMapAdapter, "rest_map_v1": RestMapAdapter,
               "line_command_v1": LineCommandAdapter}
    return plugins[driver](record("点位", config))


def test_task_mode_keeps_its_requirements():
    """参与自动流程的确认要求不放松：配了能力映射就要状态点；任务用的控制信号不能声明成可写（插件构造时就拒绝）。"""
    from ilcs_host.plugins.base import AdapterError

    base = {"host": "127.0.0.1", "points": {"state": {"address": 1}, "sp": {"address": 10, "type": "float32"},
                                            "go": {"table": "coil", "address": 0, "type": "bool"}}}
    capabilities = {"cap.x": {"write": {"temp": "sp"}, "start": {"point": "go"}}}
    with pytest.raises(AdapterError, match="status"):
        _adapter("modbus_map_v1", {**base, "capabilities": capabilities})

    status = {"point": "state", "states": {"0": "idle", "1": "running", "3": "done"}}
    full = {**base, "capabilities": capabilities, "status": status}
    assert _adapter("modbus_map_v1", full).tasks is True
    for point in ("go", "state"):
        points = {**base["points"], point: {**base["points"][point], "writable": True}}
        with pytest.raises(AdapterError, match="控制信号"):
            _adapter("modbus_map_v1", {**full, "points": points})
    # 设定值可以声明成可写：任务之外也能由人调
    points = {**base["points"], "sp": {**base["points"]["sp"], "writable": True, "min": 0, "max": 100}}
    assert _adapter("modbus_map_v1", {**full, "points": points}).tasks is True

    with pytest.raises(AdapterError):
        _adapter("rest_map_v1", {"base_url": "http://127.0.0.1:1/api", "allow_insecure_http": True})
    assert _adapter("opcua_map_v1", {"endpoint": "opc.tcp://127.0.0.1:4840/", "security_policy": "None",
                                     "points": {"a": "ns=2;s=A"}}).tasks is False


def isolated(tmp_path, monkeypatch):
    from ilcs_host.settings import settings

    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "adapter-state"))
    monkeypatch.setattr(settings, "credential_root", str(tmp_path))
    return tmp_path


def _points(protocol: str) -> dict:
    """PLC 模拟设备的点表；设定值 sp_temp 声明成可手动写（0–200 ℃）。"""
    if protocol == "opcua":
        points = plc_opcua_points("Coater", ("thickness", "temp"))
        points["sp_temp"] = {"node": points["sp_temp"], "writable": True, "min": 0, "max": 200, "unit": "℃",
                             "label": "温度设定"}
        return points
    points = plc_modbus_points(("thickness", "temp"))
    points["sp_temp"] = {**points["sp_temp"], "writable": True, "min": 0, "max": 200, "unit": "℃", "label": "温度设定"}
    return points


def _points_only(protocol: str, port: int, **extra) -> dict:
    if protocol == "opcua":
        return {"endpoint": f"opc.tcp://127.0.0.1:{port}/plc/", "security_policy": "None", "request_timeout_sec": 2,
                "connect_timeout_sec": 1, "points": _points(protocol), **extra}
    return {"host": "127.0.0.1", "port": port, "unit_id": 1, "request_timeout_sec": 1, "points": _points(protocol),
            **extra}


@contextmanager
def _rest_device():
    """最小的 REST 设备：GET /dev/<点> 回 {"name", "value"}，PUT /dev/<点> 写 {"value"}；setpoint 只收 0–80。"""
    values = {"temperature": 21.5, "setpoint": 30.0, "status": "normal"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            name = self.path.rsplit("/", 1)[-1]
            if name not in values:
                return self._reply(404, {"error": "not found"})
            self._reply(200, {"name": name, "value": values[name]})

        def do_PUT(self):
            name = self.path.rsplit("/", 1)[-1]
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if name != "setpoint" or not 0 <= payload["value"] <= 80:
                return self._reply(400, {"error": "rejected"})
            values[name] = payload["value"]
            self._reply(200, {"ok": True})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield values, server.server_address[1]
    finally:
        server.shutdown()


@contextmanager
def _line_device():
    """最小的文本命令仪表：PV? 回温度，SP? 回设定值，SP <值> 回 OK（负数回 ERR RANGE）。"""
    state = {"sp": 25.0}

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            for raw in self.rfile:
                text = raw.decode().strip()
                if text == "PV?":
                    reply = "21.3"
                elif text == "SP?":
                    reply = f"{state['sp']:.1f}"
                elif text.startswith("SP "):
                    value = float(text[3:])
                    if value < 0:
                        reply = "ERR RANGE"
                    else:
                        state["sp"], reply = value, "OK"
                else:
                    reply = "ERR SYNTAX"
                self.wfile.write((reply + "\r\n").encode())

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield state, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()

def _adapter(driver: str, config: dict):
    from ilcs_host.plugins.line_command import LineCommandAdapter
    from ilcs_host.plugins.modbus_map import ModbusMapAdapter
    from ilcs_host.plugins.opcua_map import OpcUaMapAdapter
    from ilcs_host.plugins.rest_map import RestMapAdapter

    plugins = {"opcua_map_v1": OpcUaMapAdapter, "modbus_map_v1": ModbusMapAdapter, "rest_map_v1": RestMapAdapter,
               "line_command_v1": LineCommandAdapter}
    return plugins[driver](record("点位", config))


def test_task_mode_keeps_its_requirements():
    """参与自动流程的确认要求不放松：配了能力映射就要状态点；任务用的控制信号不能声明成可写（插件构造时就拒绝）。"""
    from ilcs_host.plugins.base import AdapterError

    base = {"host": "127.0.0.1", "points": {"state": {"address": 1}, "sp": {"address": 10, "type": "float32"},
                                            "go": {"table": "coil", "address": 0, "type": "bool"}}}
    capabilities = {"cap.x": {"write": {"temp": "sp"}, "start": {"point": "go"}}}
    with pytest.raises(AdapterError, match="status"):
        _adapter("modbus_map_v1", {**base, "capabilities": capabilities})

    status = {"point": "state", "states": {"0": "idle", "1": "running", "3": "done"}}
    full = {**base, "capabilities": capabilities, "status": status}
    assert _adapter("modbus_map_v1", full).tasks is True
    for point in ("go", "state"):
        points = {**base["points"], point: {**base["points"][point], "writable": True}}
        with pytest.raises(AdapterError, match="控制信号"):
            _adapter("modbus_map_v1", {**full, "points": points})
    # 设定值可以声明成可写：任务之外也能由人调
    points = {**base["points"], "sp": {**base["points"]["sp"], "writable": True, "min": 0, "max": 100}}
    assert _adapter("modbus_map_v1", {**full, "points": points}).tasks is True

    with pytest.raises(AdapterError):
        _adapter("rest_map_v1", {"base_url": "http://127.0.0.1:1/api", "allow_insecure_http": True})
    assert _adapter("opcua_map_v1", {"endpoint": "opc.tcp://127.0.0.1:4840/", "security_policy": "None",
                                     "points": {"a": "ns=2;s=A"}}).tasks is False


@pytest.mark.parametrize("protocol", ("opcua", "modbus"))
def test_a_plc_with_only_a_point_table_reads_and_writes_points(protocol):
    from ilcs_host.plugins.base import AdapterError

    driver = f"{protocol}_map_v1"
    with plc_sim(protocol) as (program, _, port):
        config = _points_only(protocol, port)
        adapter = _adapter(driver, config)
        assert adapter.tasks is False
        assert adapter.healthcheck()["reachable"] is True  # 没配身份点：读一个点证明设备在回话

        rows = {row["name"]: row for row in adapter.read_points()}
        assert rows["sp_temp"]["writable"] and rows["sp_temp"]["unit"] == "℃" and rows["sp_temp"]["error"] == ""
        assert isinstance(rows["serial"]["value"], str) and rows["serial"]["value"].startswith("SIM-PLC-T")
        assert rows["pv_temp"]["writable"] is False

        written = adapter.write_point_manually("sp_temp", 42.5)
        assert written["matches"] and abs(written["after"] - 42.5) < 1e-3, written
        assert abs(adapter.read_point_value("sp_temp") - 42.5) < 1e-3

        for name, value, fragment in (("pv_temp", 1, "没有声明可写"), ("sp_temp", 500, "超出"),
                                      ("sp_temp", True, "要写一个数"), ("nope", 1, "没有在点表里登记")):
            with pytest.raises(AdapterError, match=fragment):
                adapter.write_point_manually(name, value)
        assert sum(program.device.executions.values()) == 0, "读写点位不碰任务：PLC 没有启动过作业"

        # 只读写点位的设备不参与自动流程：下发指令明确拒绝，设备没动
        with pytest.raises(AdapterError, match="只读写点位"):
            adapter.submit(request("CMD-P1", capability="cap.coat", params={"temp": 60}))
        assert sum(program.device.executions.values()) == 0


def test_an_unreachable_point_only_device_is_not_reported_online():
    adapter = _adapter("modbus_map_v1", {"host": "127.0.0.1", "port": free_port(), "request_timeout_sec": 0.5,
                                         "points": {"t": {"address": 0, "type": "float32"}}})
    with pytest.raises(Exception):
        adapter.healthcheck()
    row = adapter.read_points()[0]
    assert row["value"] is None and row["error"], "读不到的点只在那一行写明"


def test_rest_points_read_and_write_without_a_task_mapping():
    from ilcs_host.plugins.base import AdapterError

    with _rest_device() as (values, port):
        config = {"base_url": f"http://127.0.0.1:{port}/dev", "allow_insecure_http": True, "request_timeout_sec": 2,
                  "points": {
                      "temperature": {"path": "/temperature", "field": "value", "unit": "℃"},
                      "setpoint": {"path": "/setpoint", "field": "value", "writable": True, "min": 0, "max": 100,
                                   "write": {"method": "PUT", "path": "/setpoint", "body": {"value": "{value}"}}},
                      "missing": {"path": "/temperature", "field": "nope"},
                  }}
        adapter = _adapter("rest_map_v1", config)
        assert adapter.healthcheck()["reachable"]
        rows = {row["name"]: row for row in adapter.read_points()}
        assert rows["temperature"]["value"] == 21.5 and rows["setpoint"]["value"] == 30.0
        assert rows["missing"]["value"] is None and "没有字段" in rows["missing"]["error"]

        assert adapter.write_point_manually("setpoint", 55)["after"] == 55 and values["setpoint"] == 55
        with pytest.raises(AdapterError):  # 设备回 400：明确不收，设备上的值没变
            adapter.write_point_manually("setpoint", 90)
        assert values["setpoint"] == 55


def test_line_command_points_read_and_write_without_a_task_mapping():
    from ilcs_host.plugins.base import AdapterError

    with _line_device() as (state, port):
        adapter = _adapter("line_command_v1", {
            "transport": {"kind": "tcp", "host": "127.0.0.1", "port": port}, "request_timeout_sec": 1,
            "error_pattern": "^ERR",
            "points": {
                "temp": {"send": "PV?", "pattern": "^(?P<value>[-\\d.]+)$", "unit": "℃"},
                "setpoint": {"send": "SP?", "pattern": "^(?P<value>[-\\d.]+)$", "writable": True, "min": -50,
                             "write": {"send": "SP {value:.1f}", "expect": "^OK$"}},
            }})
        assert adapter.healthcheck()["reachable"]
        rows = {row["name"]: row for row in adapter.read_points()}
        assert rows["temp"]["value"] == 21.3 and rows["setpoint"]["value"] == 25.0
        assert adapter.write_point_manually("setpoint", 61.5) == {"before": 25.0, "after": 61.5, "matches": True}
        with pytest.raises(AdapterError, match="设备拒绝"):  # 仪表回 ERR：明确没执行
            adapter.write_point_manually("setpoint", -10)
        assert state["sp"] == 61.5
