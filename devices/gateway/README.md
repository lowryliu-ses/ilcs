# 设备网关：设备模块与网关 SDK

厂家只给 SDK / DLL、私有协议的设备，在设备旁起一个独立网关，把设备包成 ILCS 的 `http_json_v1` 网关契约接进来。
这里放两样东西，讲的是同一件事：

| 目录 | 是什么 |
|---|---|
| [`ilcs_gateway/`](ilcs_gateway/) | 网关 SDK（Python 包，以本目录为根）：去重、作业台账、按指令号查询、回执丢失的处理都做好了，设备开发者只写启动、读状态、停止 |
| [`sample-cycler/`](sample-cycler/) | 设备模块样板：一台（一类）设备一个交付目录；`scripts/new-device-module.py` 从它生成新模块，新模块也放在本目录下 |

ILCS 自己的驱动（主动去连设备的一端）不在这里，在 `api/app/adapters/`。HTTPS 网关模拟设备（`devices/simulators/http_gateway`）
也基于这个 SDK，给测试与试点环境用；样板是给设备开发者照着写的最小例子。

## 设备模块

一台（一类）设备怎么接进 ILCS，打成一个目录交付：真实接口、模拟接口、ILCS 侧登记、测试与部署文件放在一起，
设备开发者不用碰 ILCS 代码、不用 ILCS 数据库就能独立调试，交付物进 ILCS 不改代码、不重启。

```
devices/gateway/<厂家-型号>/
  driver/        真实接口：调厂家 SDK 或协议，只写启动、读状态、停止（能用映射驱动的设备没有这一层）
  simulator/     模拟接口：和厂家 SDK 同一组方法的「假 SDK」，带故障注入（丢回执、忙、联锁、失联）
  gateway.py     入口：--simulate 用模拟接口，否则连真实的厂家 SDK
  profile.json   ILCS 侧登记：设备接入模板文件（ilcs-device-template/1），在「工位与接入 → 接入模板」导入
  tests/         对模拟接口跑 ILCS 的接入验收清单，CI 里必须全过
  deploy/        Dockerfile、compose 片段、Windows 服务安装说明
  README.md
```

样板：[sample-cycler](sample-cycler/)（8 通道充放电柜，厂家只给 Windows SDK）。

已有模块：

| 模块 | 设备 | 接口 |
|---|---|---|
| [neware-bts](neware-bts/) | Neware 充放电柜 | 经开源的 aurora-neware 走 BTS 8.0 的 TCP XML 接口；一条指令几颗电芯，一颗一个通道 |
| [balance-dosing](balance-dosing/) | 天平称量加料站：梅特勒天平 / Quantos 加粉 / Cavro 协议注射泵加液 | MT-SICS（含 Quantos 的 QRD / QRA）与 Cavro DT 协议，串口或网口 |
| [ika-stirrer](ika-stirrer/) | IKA 磁力加热搅拌器（一个位置一台） | NAMUR 串口协议 |
| [raman-seabreeze](raman-seabreeze/) | 拉曼光谱仪（Ocean Insight） | python-seabreeze（USB），谱图回报成曲线 |
| [thermostat](thermostat/) | 恒温循环器 / 冷水机（Huber / Julabo / LAUDA，配置里选）+ 可选的 IKA 板做制冷搅拌 | Huber PB 命令、Julabo、LAUDA 命令集，串口或网口；板子走 NAMUR |
| [potentiostat](potentiostat/) | 电化学工作站（第一个后端 PalmSens EmStat4 / EmStat Pico / Nexus）：电导池 EIS 电导率、LSV 电化学窗口、CV、OCP、CA | MethodSCRIPT（USB 虚拟串口或网口），曲线 + 派生指标；别的品牌按 `driver/backend.py` 加后端 |
| [scpi-cell-meter](scpi-cell-meter/) | 电芯开路电压 / 交流内阻：Keithley 2450、2400 SourceMeter（OCV），Hioki BT3561A–63A / BT3562 / BT3563（1 kHz ACIR + OCV） | 映射模块：三份 `profile-*.json` 走内置 `line_command_v1`（SCPI，LAN 或 RS-232），没有网关代码 |

Neware 的 .nda / .ndax 充放电数据由结果文件接收器解析（`devices/connectors/result_files`，`format: "neware"`）。

在 ILCS 那台机器上模拟联调：每个模块的 `deploy/compose.yml` 起一个模拟网关容器（接 ILCS 的后端网络 `ilcs_backend`），
`deploy/.env` 的 `ILCS_ADAPTER_ALLOWED_HOSTS` 加上它的主机名；然后 `scripts/load-neware-cycler.py register`（Neware 柜）、
`scripts/load-device-simulators.py register [--acceptance]`（天平称量加料站、IKA 加热搅拌、拉曼光谱仪、冷水机制冷搅拌、
电化学工作站、电芯开路电压 / 内阻仪）登记工位、
套用接入模板、连上模拟网关并验收。

### 两种模块

| 设备给的接口 | 模块里有什么 | ILCS 侧驱动 |
|---|---|---|
| 串口 / TCP 文本命令、Modbus 点表、OPC UA 节点、REST | 只有 `profile.json`（映射配置）+ 模拟设备 + 测试，不写代码 | 现有映射驱动（`line_command_v1` 等） |
| 厂家 SDK / DLL、私有协议、逻辑复杂 | `driver/` + `simulator/` + `gateway.py`，基于本目录的 `ilcs_gateway` | `http_json_v1`（本模块起的网关） |

第二种的网关是一个独立服务：挂了只影响这一台（ILCS 判它失联、进待命列表），其他工位照常；升级只重启这个服务，ILCS 不动。

### 开发与自测（不需要 ILCS 数据库）

```bash
# 新建一个模块（从样板复制并替换名称、型号、能力与参数），测试开箱就能过
python scripts/new-device-module.py acme-vd80 --title "ACME 真空干燥箱" --model VD-80 --vendor ACME \
    --capability cap.vacuum_dry --param temp=60:180 --param vacuum=0.1:5

# 对模拟接口跑 ILCS 的接入验收清单（含故障项目）
api/.venv/bin/pytest devices/gateway/acme-vd80/tests

# 手工联调：起模拟网关，另开一个终端用 ILCS 的验收命令对着它跑
python devices/gateway/acme-vd80/gateway.py --simulate --insecure --port 8443
python scripts/device-acceptance.py --adapter my-adapter.json --allow-host --physical --faults
```

把 `driver/` 里对假 SDK 的调用换成真实 SDK 时，测试仍然对着模拟接口跑——真实接口与模拟接口实现同一组方法，
驱动代码只有一份。

### 交付与上线

1. **交付物**：模块目录（含 `tests/` 全过的记录）+ `profile.json`。
2. **ILCS 侧导入**：「工位与接入 → 接入模板」导入 `profile.json`，成草稿；核对后由另一个人签名发布
   （起草人不能发布本人起草的模板）。文件摘要对不上（导出后被改过）会被拒绝。
3. **部署网关**（第二种模块）：按 `deploy/` 起服务；证书与令牌放进 ILCS 的凭据目录；主机在 ILCS 的设备白名单网段里就不用改配置。
4. **工位套用模板**：「设备连接」里选模板、填这台设备的连接参数（地址、证书、设备编号），签名保存。
5. **接入验收**：保存后自动跑只读级；第一次接真实设备还要动作级（签名 + 现场批准人，DEC-02）。通过了工位才接指令，
   报告存档，带驱动、固件、配置与模板版本。

模板出新修订时不会自动推给工位：「接入模板」页签列出还在用旧修订的工位，逐台切换、重新验收。

### 模块必须守的规矩

由 `ilcs_gateway` 保证、测试会查（见下文「网关 SDK」）：

- 同一 ILCS 指令号重复提交只动作一次，回放原结论；
- 先落盘再动设备：网关任何时刻重启，都能按原指令号回答查询；
- 回执丢了宁可不回，不编造结论；不知道设备动没动就报结果未知；
- 设备明确拒绝（参数非法、不支持、联锁、忙）时设备确实没动；
- 模拟接口自报 `simulator: true`（正式环境会拒绝接入）。

驱动自己要守的：参数范围与 ILCS 工位能力极限一致；急停 / 联锁如实上报；厂家 SDK 的「不确定」错误不要改判成明确失败。

## 网关 SDK（`ilcs_gateway`）
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
`build_server(...)` 与 `serve` 参数相同，只是不开始监听（调用方自己 `start()`）。
HTTPS 网关模拟设备（`devices/simulators/http_gateway`）也是用它起的，底下接的是设备行为模型。
