# ILCS 设备网关 SDK（`ilcs_gateway`）

把一台设备（多半是厂家 SDK / DLL、私有协议）包成 ILCS 的 `http_json_v1` 网关契约。只用 Python 标准库
（自签证书要装 `cryptography`），能跑在设备旁的 Windows 工控机上。

设备开发者实现 `Device`：

```python
from ilcs_gateway import Device, Job, Rejected, Status, serve

class Oven(Device):
    def identity(self):            # device_id、model、vendor、firmware、methods、interlock、accepts_commands、simulator
        ...
    def start(self, job: Job) -> str:   # 让设备开始，返回设备作业号；设备明确不做就 raise Rejected("invalid" / "busy" / ...)
        ...
    def status(self, job: Job) -> Status:   # running / held / done / failed + 实测值
        ...
    def hold(self, job): ...
    def resume(self, job): ...
    def abort(self, job): ...
    def lookup(self, job) -> str | None:    # 可选：启动没拿到应答时按指令号在设备侧找回作业

serve(Oven(), device_id="OVEN-01", state_dir="./state", port=8443, token_file="./secrets/OVEN-01.token",
      cert="./secrets/OVEN-01.crt", key="./secrets/OVEN-01.key", host_name="oven-gw.lab.internal")
```

SDK 负责（契约里容易写错的部分）：

| 规矩 | 在哪 |
|---|---|
| 按 ILCS 指令号去重，重投回放原结论，不再调设备 | `gateway.py` |
| 先落盘再动设备；台账写不进去就不接活；台账损坏拒绝工作 | `ledger.py` |
| 按指令号查询；查不到 404；读不到设备状态照报台账里的状态，不猜 | `gateway.py` |
| 调设备出了意外（不知道设备动没动）报结果未知，绝不重发；之后按 `lookup` 找回 | `gateway.py` |
| 保持 / 终止按控制指令号去重；终止已经结束的作业照样确认；终止设备侧还没找到的作业回结果未知，不谎报已停 | `gateway.py` |
| `type: resume` 接续被保持的原作业，不另开一个 | `gateway.py` |
| HTTPS 必须带 Bearer 令牌（首次启动生成，令牌、私钥、台账创建时即属主只读）、TLS（自签或现场证书）；明文 `--insecure` 缺省只监听 127.0.0.1，不带令牌不许监听别的地址；状态码与 ILCS 驱动的判定一一对应 | `server.py` |
| 模拟设备的统一控制口（`/simulator/state`、`/simulator/fault`），接入验收的故障项目直接能用 | `server.py`、`simulation.py` |

异常的含义是契约的一部分：`Rejected` = 设备明确没动；`ReceiptLost` = 设备动了但应答要丢（只给模拟设备用）；
其他任何异常 = 不知道。别把厂家 SDK 的超时改判成 `Rejected`。

`ilcs_gateway.testing.acceptance(...)` 对一个网关跑 ILCS 的接入验收清单（需要能找到 ILCS 仓库的 `api/`）。
模块结构与交付见 [device-modules/README.md](../device-modules/README.md)。
