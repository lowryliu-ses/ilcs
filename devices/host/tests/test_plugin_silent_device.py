"""设备停了不回话（进程卡死、容器被暂停：连接建得上，请求没有回音）：映射驱动读一次点表只等一个超时，不是每个点等一个。"""
import time

import pytest

from plugin_harness import freezable_proxy, plc_config, plc_sim, record, silent_port


@pytest.mark.parametrize("protocol", ["modbus", "opcua"])
def test_silent_device_costs_one_timeout_per_read_not_one_per_point(protocol):
    if protocol == "opcua":
        from ilcs_host.plugins.opcua_map import OpcUaMapAdapter as Adapter
        timeouts = {"request_timeout_sec": 0.5, "connect_timeout_sec": 0.5}
    else:
        from ilcs_host.plugins.modbus_map import ModbusMapAdapter as Adapter
        timeouts = {"request_timeout_sec": 0.5}
    with silent_port() as port:
        config, credential = plc_config(protocol, port, **timeouts)
        adapter = Adapter(record("不回话的 PLC", config, credential))
        started = time.monotonic()
        rows = adapter.read_points()
        took = time.monotonic() - started
    assert len(rows) > 5 and all(row["value"] is None and row["error"] for row in rows), rows
    assert sum("没有再读" in row["error"] for row in rows) == len(rows) - 1, "第一个点没回话之后就不再读"
    assert took < 3, f"读 {len(rows)} 个点等了 {took:.1f} s：每个点都等满了超时"


def test_a_point_the_device_quickly_rejects_does_not_stop_the_others():
    """设备很快回了错（节点不存在）：只算那一个点，其余照读。"""
    from ilcs_host.plugins.opcua_map import OpcUaMapAdapter

    with plc_sim("opcua") as (_, _, port):
        config, credential = plc_config("opcua", port)
        config["points"] = {"bogus": "ns=2;s=NoSuchNode", **config["points"]}
        rows = OpcUaMapAdapter(record("PLC 点表", config, credential)).read_points()
    assert rows[0]["name"] == "bogus" and rows[0]["error"] and "没有再读" not in rows[0]["error"]
    assert all(row["error"] == "" for row in rows[1:]), [row for row in rows[1:] if row["error"]]


def test_link_that_goes_silent_mid_session_costs_one_timeout():
    """会话建好之后断网（连接还挂着、没有回音）：读点等一个超时就答复，关旧会话不再多等一个超时。"""
    from ilcs_host.plugins.opcua_map import OpcUaMapAdapter

    with plc_sim("opcua") as (_, _, port), freezable_proxy(port) as proxy:
        config, credential = plc_config("opcua", proxy.port, request_timeout_sec=1.5, connect_timeout_sec=1)
        adapter = OpcUaMapAdapter(record("PLC 点表", config, credential))
        assert all(row["error"] == "" for row in adapter.read_points()), "会话经代理建好"
        proxy.freeze()
        started = time.monotonic()
        rows = adapter.read_points()
        took = time.monotonic() - started
        adapter.close()
    assert all(row["error"] for row in rows), rows
    assert took < 2.5, f"等了 {took:.1f} s：关旧会话又等了一个超时"
