"""真实接口：经开源的 aurora-neware（MIT，https://github.com/EmpaEconversion/aurora-neware）连 BTS 8.0。

BTS 要在「帮助 → 模式设置」里打开 API（可能要向 Neware 要激活码）；接口是 TCP（缺省 502 端口）上的 XML 命令。
aurora-neware 的 `NewareAPI` 负责拼命令、解析应答；这里补上它没管的三件事：

1. **超时与重连**：socket 设超时；任何通信异常都丢掉这条连接，下次调用重新连，不在半截应答上接着读；
2. **对端关连接**：原版收到空包会一直空转，这里当断线抛出；
3. **分清「没发出去」和「发了没回」**：请求发出之前出的错（通道不在 BTS 上、工步文件不在、通道不空闲）是
   `BtsRefused`，通道没动；发出之后出的错一律原样抛出，网关按结果未知处理。

aurora-neware 的版本固定在 requirements.txt：这里用到了 `NewareAPI` 的内部细节（`neware_socket`、`command`）。
"""
from __future__ import annotations

from typing import Any

from aurora_neware import NewareAPI

from .bts_api import BtsOffline, BtsRefused

FIELDS = {"workstatus": "workstatus", "barcode": "barcode", "cycle": "cycle_id", "step": "step_id",
          "step_type": "step_type", "voltage": "voltage", "current": "current", "capacity": "capacity",
          "energy": "energy", "log_code": "log_code"}
NUMBERS = {"cycle", "step", "voltage", "current", "capacity", "energy", "log_code"}


def _number(value: Any) -> Any:
    """aurora-neware 只把带小数点的串转成数：「2e-06」这类科学计数法会原样留成字符串。"""
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    return value


class _Api(NewareAPI):
    """记下启动命令有没有发出去（原版 `start()` 发启动之前还会先发一条 inquire）；对端关连接时报断线，不空转。"""

    start_sent = False

    def command(self, cmd: str) -> str:
        if cmd.startswith("<cmd>start</cmd>"):
            self.start_sent = True
        self.neware_socket.sendall(
            str.encode(self.start_message + cmd + self.end_message + self.termination, "utf-8"),
        )
        received = ""
        while not received.endswith(self.termination):
            chunk = self.neware_socket.recv(2048)
            if not chunk:
                raise ConnectionError("BTS 关闭了连接，应答没收全")
            received += chunk.decode()
        return received[: -len(self.termination)]


class AuroraBts:
    def __init__(self, host: str = "127.0.0.1", port: int = 502, timeout_sec: float = 10.0):
        self.host, self.port, self.timeout_sec = host, port, timeout_sec
        self._api: _Api | None = None

    def _client(self) -> _Api:
        if self._api is None:
            api = _Api(self.host, self.port)
            api.neware_socket.settimeout(self.timeout_sec)
            try:
                api.connect()
            except Exception:
                api.disconnect()
                raise
            self._api = api
        self._api.start_sent = False
        return self._api

    def _drop(self) -> None:
        if self._api is not None:
            self._api.disconnect()
            self._api = None

    def _call(self, action):
        api = self._client()
        try:
            return action(api)
        except Exception:
            self._drop()  # 连接状态不明：丢掉，下次重连
            raise

    def info(self) -> dict[str, Any]:
        def read(api: _Api) -> dict[str, Any]:
            api.channel_map = api.getdevinfo()
            return {"pipelines": sorted(api.channel_map), "version": "BTS 8.0", "server": f"{self.host}:{self.port}",
                    "simulator": False}
        return self._call(read)

    def channels(self, pipelines: list[str]) -> dict[str, dict[str, Any]]:
        def read(api: _Api) -> dict[str, dict[str, Any]]:
            rows = api.inquire(list(pipelines))
            return {pipeline: {key: _number(rows[pipeline].get(source)) if key in NUMBERS else rows[pipeline].get(source)
                               for key, source in FIELDS.items()}
                    for pipeline in pipelines}
        return self._call(read)

    def start(self, pipeline: str, barcode: str, step_file: str, save_dir: str) -> None:
        try:
            api = self._client()
        except OSError as exc:
            raise BtsOffline(f"连不上 BTS {self.host}:{self.port}：{exc}") from exc
        try:
            extra = {"save_location": save_dir} if save_dir else {}
            result = api.start(pipeline, barcode, step_file, **extra)
        except (KeyError, FileNotFoundError, ValueError) as exc:
            if not api.start_sent:
                raise BtsRefused(str(exc)) from exc  # 还没发给 BTS：通道没动
            self._drop()
            raise
        except Exception:
            self._drop()
            raise
        if not result or result[0].get("start") != "ok":
            raise BtsRefused(f"BTS 回 {result[0].get('start') if result else '空应答'}")

    def stop(self, pipeline: str) -> None:
        result = self._call(lambda api: api.stop(pipeline))
        if not result or result[0].get("stop") != "ok":
            raise BtsRefused(f"BTS 回 {result[0].get('stop') if result else '空应答'}")
