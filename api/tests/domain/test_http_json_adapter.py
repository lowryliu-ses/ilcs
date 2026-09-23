"""通用 HTTP JSON 真实适配器的协议契约测试。"""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest


@pytest.fixture()
def device_gateway(monkeypatch):
    state = {"commands": {}, "submit_count": 0, "authorization": "", "idempotency": ""}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def send_json(self, status: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        @staticmethod
        def result(command_id: str, command_state: str = "done") -> dict:
            return {
                "command_id": command_id,
                "state": command_state,
                "device_ts": datetime.now(timezone.utc).isoformat(),
                "quality": "good",
                "delivered": {"speed": 120},
                "telemetry": [{"metric": "speed", "value": 119.8, "setpoint": 120}],
            }

        def do_GET(self):
            if self.path == "/redirect":
                # 把带凭据的请求引到白名单之外、并从 HTTPS 降级：驱动必须拒绝跟随
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/garbage":
                body = b"<html>gateway error page</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path in {"/offset", "/naive"}:
                payload = self.result("CMD-TZ", "done")
                payload["device_ts"] = (
                    "2026-09-20T16:00:00+08:00" if self.path == "/offset" else "2026-09-20T16:00:00"
                )
                return self.send_json(200, payload)
            if self.path == "/health":
                state["authorization"] = self.headers.get("Authorization", "")
                return self.send_json(
                    200, {"reachable": True, "device_id": "GW-ST-01", "version": "1.4.2"}
                )
            if self.path == "/slow":
                time.sleep(0.2)
                return self.send_json(200, {"reachable": True})
            command_id = self.path.removeprefix("/commands/")
            if command_id not in state["commands"]:
                return self.send_json(404, {"error": "not found"})
            return self.send_json(200, self.result(command_id, "done"))

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            command_id = payload["command_id"]
            state["authorization"] = self.headers.get("Authorization", "")
            state["idempotency"] = self.headers.get("Idempotency-Key", "")
            if self.path == "/conflict":
                return self.send_json(409, {"error": "duplicate command in progress"})
            if self.path == "/commands":
                if command_id not in state["commands"]:
                    state["submit_count"] += 1
                    state["commands"][command_id] = payload
                return self.send_json(200, self.result(command_id, "accepted"))
            if self.path.endswith("/hold") or self.path.endswith("/abort"):
                return self.send_json(200, self.result(command_id, "done"))
            return self.send_json(404, {"error": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "ILCS_TEST_DEVICE_CREDENTIAL",
        json.dumps({"headers": {"Authorization": "Bearer test-device-token"}}),
    )
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(command_id: str = "CMD-HTTP-1"):
    from app.adapters import CommandRequest

    return CommandRequest(
        command_id=command_id,
        station_id="ST-HTTP-TEST",
        capability="cap.mix",
        params={"speed": 120},
        type="dispatch",
        batch_id="B-HTTP-1",
        step_index=0,
        step_id="mix-1",
    )


def test_http_json_driver_health_dedup_query_and_controls(device_gateway):
    from app.adapters.http_json import HttpJsonAdapter, record_for_test

    base_url, state = device_gateway
    adapter = HttpJsonAdapter(record_for_test(
        config={
            "base_url": base_url,
            "allow_insecure_http": True,
            "expected_device_id": "GW-ST-01",
        },
        credential_ref="env://ILCS_TEST_DEVICE_CREDENTIAL",
    ))

    health = adapter.healthcheck()
    assert health == {
        "reachable": True,
        "driver": "http_json_v1",
        "protocol": "HTTPS JSON",
        "device_id": "GW-ST-01",
        "gateway_version": "1.4.2",
    }
    first = adapter.submit(request())
    duplicate = adapter.submit(request())
    assert first.state == duplicate.state == "accepted"
    assert state["submit_count"] == 1
    assert state["idempotency"] == "CMD-HTTP-1"
    assert state["authorization"] == "Bearer test-device-token"

    finished = adapter.query("CMD-HTTP-1")
    assert finished.state == "done"
    assert finished.origin == "real:http_json_v1"
    assert finished.telemetry == (("speed", 119.8, 120),)
    assert adapter.query("CMD-NOT-FOUND") is None
    assert adapter.hold(request("CMD-HOLD-1")).state == "done"
    assert adapter.abort(request("CMD-ABORT-1")).state == "done"


def test_http_json_driver_timeout_is_result_unknown(device_gateway):
    from app.adapters import AdapterUnreachable
    from app.adapters.http_json import HttpJsonAdapter, record_for_test

    base_url, _ = device_gateway
    adapter = HttpJsonAdapter(record_for_test(config={
        "base_url": base_url,
        "allow_insecure_http": True,
        "request_timeout_sec": 0.05,
        "paths": {"health": "/slow"},
    }))

    with pytest.raises(AdapterUnreachable):
        adapter.healthcheck()


def test_http_json_driver_rejects_identity_mismatch_and_inline_http_by_default(device_gateway):
    from app.adapters import AdapterError
    from app.adapters.http_json import HttpJsonAdapter, record_for_test

    base_url, _ = device_gateway
    with pytest.raises(AdapterError, match="必须使用 HTTPS"):
        HttpJsonAdapter(record_for_test(config={"base_url": base_url}))

    adapter = HttpJsonAdapter(record_for_test(config={
        "base_url": base_url,
        "allow_insecure_http": True,
        "expected_device_id": "WRONG-DEVICE",
    }))
    with pytest.raises(AdapterError, match="设备身份不匹配"):
        adapter.healthcheck()


def _adapter(base_url: str, **config):
    from app.adapters.http_json import HttpJsonAdapter, record_for_test

    return HttpJsonAdapter(record_for_test(config={
        "base_url": base_url, "allow_insecure_http": True, **config,
    }))


def test_http_json_driver_refuses_redirects(device_gateway):
    """一次 30x 不能把带凭据的请求带出白名单。"""
    from app.adapters import AdapterError

    base_url, _ = device_gateway
    with pytest.raises(AdapterError, match="不跟随重定向"):
        _adapter(base_url, paths={"health": "/redirect"}).healthcheck()


def test_http_json_driver_treats_conflict_and_bad_receipts_as_unknown(device_gateway):
    """网关收到了请求却给不出确定结论时，设备可能已经在动作：只能判结果未知。"""
    from app.adapters import AdapterError, AdapterIndeterminate, AdapterUnreachable

    base_url, _ = device_gateway
    with pytest.raises(AdapterIndeterminate) as conflict:
        _adapter(base_url, paths={"submit": "/conflict"}).submit(request())
    assert isinstance(conflict.value, AdapterUnreachable)
    assert not isinstance(conflict.value, AdapterError)

    with pytest.raises(AdapterIndeterminate, match="不是有效 JSON"):
        _adapter(base_url, paths={"query": "/garbage"}).query("CMD-HTTP-1")

    # 回执 command_id 与请求不符：字段不合规同样不能证明设备没动
    with pytest.raises(AdapterIndeterminate, match="command_id 不匹配"):
        _adapter(base_url, paths={"query": "/offset"}).query("CMD-OTHER")


def test_http_json_driver_converts_device_time_to_utc(device_gateway):
    base_url, _ = device_gateway
    offset = _adapter(base_url, paths={"query": "/offset"}).query("CMD-TZ")
    assert offset.device_ts == datetime(2026, 9, 20, 8, 0)

    naive = _adapter(base_url, paths={"query": "/naive"}, device_timezone="Asia/Shanghai")
    assert naive.query("CMD-TZ").device_ts == datetime(2026, 9, 20, 8, 0)
    assert _adapter(base_url, paths={"query": "/naive"}).query("CMD-TZ").device_ts == datetime(
        2026, 9, 20, 16, 0
    ), "未声明设备时区时按 UTC 解读"


def test_http_json_driver_uses_connect_timeout_for_connection(device_gateway, monkeypatch):
    import http.client

    base_url, _ = device_gateway
    seen: list[float] = []
    original = http.client.socket.create_connection

    def recording(address, timeout=None, *args, **kwargs):
        seen.append(timeout)
        return original(address, timeout, *args, **kwargs)

    monkeypatch.setattr(http.client.socket, "create_connection", recording)
    _adapter(base_url, connect_timeout_sec=0.7, request_timeout_sec=5).healthcheck()
    assert seen == [0.7]
