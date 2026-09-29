# ILCS 外部模拟设备

在系统外部独立运行、走真实网络协议的模拟设备。系统侧把工位适配器配成对应的真实驱动接入，和接一台真设备
走同一条路，用来在真机到位之前验证驱动、执行器、对账与异常处置。它们**不是**系统内置的模拟适配器。

设备行为共用 `common/device.py`：长任务后台推进，可保持 / 恢复 / 终止，故障注入一致，`executions` 记录每次
真正的物理动作——验收「不重复执行」就看它。协议层只做报文转换。其中两类设备有本质区别：

- **认识 ILCS 指令号**（SiLA 2 / OPC UA TaskExecution / Modbus 任务寄存器 / HTTPS 网关 / 数据库中间表）：设备自己按指令号去重、查询；
- **不认识 ILCS 指令号**（PLC 点表、串口命令仪器、天平、AGV 车队）：设备只知道「现在在做什么」，指令号、去重、
  重启对账由 ILCS 驱动的作业台账负责——这是现场设备的常态。

| 目录 | 协议 | 系统侧驱动 | 契约 / 点表 | 安全 |
|---|---|---|---|---|
| `sila_device/` | SiLA 2（gRPC） | `sila2_v1` | `contracts/sila2/TaskExecution.sila.xml` | TLS，自签证书 |
| `opcua_device/` | OPC UA（ILCS TaskExecution 节点） | `opcua_v1` | `contracts/opcua/TaskExecution.json` | Basic256Sha256 + SignAndEncrypt，双向证书 |
| `modbus_device/` | Modbus TCP（ILCS 任务寄存器） | `modbus_tcp_v1` | `contracts/modbus/TaskRegisters.json` | 无（隔离网段） |
| `http_gateway/` | HTTPS JSON（厂家 SDK 接口服务的样子） | `http_json_v1` | docs/设备适配器配置模板.md「HTTPS JSON 网关驱动」 | TLS + Bearer 令牌 |
| `plc_device/` | PLC 自有点表，`--protocol opcua` 或 `modbus` | `opcua_map_v1` / `modbus_map_v1` | 见 `plc_device/server.py` 文件头 | OPC UA 同上；Modbus 无 |
| `line_device/` | 串口 / TCP 文本命令：`--dialect oven`（真空干燥箱温控仪表）或 `ur`（UR 仪表盘服务） | `line_command_v1` | 见 `line_device/server.py` 文件头 | 无（串口服务器 / 隔离网段） |
| `mt_sics/` | MT-SICS 天平 | `mt_sics_v1` | MT-SICS 通用命令 | 无 |
| `fleet/` | AGV 车队 REST（MiR 机器人 API 子集） | `rest_map_v1` | 见 `fleet/server.py` 文件头 | Basic 认证（凭据文件） |
| `sql_device/` | 数据库中间表：设备侧软件轮询作业表 | `sql_table_v1` | `contracts/sql/ilcs_exchange.sql` | 数据库账号（口令走凭据引用） |

## 试点：每个示例工位一台（`docker compose --profile pilot`）

| 工位 | Compose 服务 | 设备 ID | 驱动 |
|---|---|---|---|
| ST-01-A 高通量匀浆站 A | `sila-sim-slurry-a` | SIM-SLR-A | `sila2_v1` |
| ST-01-B 高通量匀浆站 B | `sql-sim-slurry-b`（中间库 `sql-sim-exchange`） | SIM-SLR-B | `sql_table_v1` |
| ST-02 中试匀浆罐 | `plc-sim-mixer`（Modbus） | SIM-MIX-01 | `modbus_map_v1` |
| ST-03 涂布烘干线 | `plc-sim-coater`（OPC UA） | SIM-COAT-01 | `opcua_map_v1` |
| ST-04 辊压冲切机 | `opcua-sim-calender` | SIM-CAL-01 | `opcua_v1` |
| ST-05 真空干燥与称重站 | `line-sim-oven` + `mtsics-sim-balance` | SIM-OVEN-01 + SIM-BAL-01 | `composite_v1`（`line_command_v1` + `mt_sics_v1`） |
| ST-06 手套箱组装线 | `sila-sim-lh` | SIM-LH-01 | `sila2_v1` |
| ST-07 充放电测试柜 | `gateway-sim-cycler`（8 通道） | SIM-CYC-01 | `http_json_v1` |
| AGV-01 / AGV-02 | `fleet-sim` | AGV-01 / AGV-02 | `rest_map_v1` |
| ARM-01 手套箱机械臂（演示导入时登记） | `line-sim-arm`（UR 方言） | SIM-ARM-01 | `line_command_v1` |

系统侧的完整连接配置（点表、命令模板、请求模板、证书与凭据位置）在 **`pilot-devices.json`**，一次切换：

```bash
docker compose exec api python ../scripts/configure-pilot-adapters.py apply --preset          # 全部示例工位
docker compose exec api python ../scripts/configure-pilot-adapters.py apply --preset --only ST-05
docker compose exec api python ../scripts/configure-pilot-adapters.py revert --station ST-05  # 还原
```

`deploy/.env` 的 `ILCS_ADAPTER_ALLOWED_HOSTS` 要列出这些服务名（含统一控制口所在的 `sql-sim-slurry-b`：
中间表工位的 ILCS 侧只连中间库，故障注入走设备侧进程的控制口），切换脚本会先核对，缺哪个一个都不改。
证书与凭据首次启动写到 `secrets/sila`、`secrets/opcua`、`secrets/gateway`、`secrets/fleet`，统一控制口的令牌写到
`secrets/simctl`（属主都是 10001；`simctl` 目录没建或属主不对时控制口不开，设备照常模拟，验收的故障项目标跳过）。
切换后执行器自动跑一次只读级接入验收，通过了工位才接指令（模拟设备自报为模拟器，只读级就够）。

## 设备类型（`--profile`，SiLA 2 / OPC UA / 任务寄存器 / 网关通用）

| 类型 | 行为 |
|---|---|
| `generic` | 按数值参数回报实测值（确定性的 ±0.5% 偏差） |
| `liquid_handler` | 按 `params.wells` 逐孔位执行，回报每孔实际加入量，按 `--material-map` 折算物料消耗 |
| `cycler` | `--channels` 个通道，每个任务占一个，满了明确拒绝（`DeviceBusy`） |

PLC 点表、串口命令、Modbus 任务寄存器只能传数值参数；孔位矩阵这类结构化参数请用 SiLA 2 / OPC UA TaskExecution / 网关。

## 本机运行

```bash
api/.venv/bin/python simulators/plc_device/server.py --protocol opcua --machine Coater --setpoints thickness,temp --port 4841 --insecure
api/.venv/bin/python simulators/plc_device/server.py --protocol modbus --machine Mixer --setpoints mass,volume,rate,temp,rpm,vacuum --port 5021
api/.venv/bin/python simulators/line_device/server.py --dialect oven --device-id SIM-OVEN-01 --port 4001 --methods VD-120,VD-90
api/.venv/bin/python simulators/line_device/server.py --dialect ur --device-id SIM-ARM-01 --port 29999 --methods load_glovebox
api/.venv/bin/python simulators/mt_sics/server.py --device-id SIM-BAL-01 --port 4305 --sample-mass 0.0152
api/.venv/bin/python simulators/fleet/server.py --robots AGV-01,AGV-02 --port 8080 --cert-dir ./fleet-certs
api/.venv/bin/python simulators/opcua_device/server.py --device-id SIM-CAL-01 --port 4840 --cert-dir ./opcua-certs
api/.venv/bin/python simulators/http_gateway/server.py --device-id SIM-CYC-01 --profile cycler --channels 8 --port 8443 --cert-dir ./gateway-certs
api/.venv/bin/python simulators/sila_device/server.py --device-id SIM-LH-01 --port 50052 --insecure
api/.venv/bin/python simulators/modbus_device/server.py --device-id SIM-MB-01 --port 5020
api/.venv/bin/python simulators/sql_device/worker.py --url sqlite:///./exchange.db --device-id SIM-SLR-B
```

参数也可以用同名环境变量给（`SIM_DEVICE_ID`、`SIM_PORT`、`SIM_PROFILE`、`SIM_TASK_SECONDS`、`SIM_CHANNELS`、
`SIM_METHODS`、`SIM_CERT_DIR`、`SIM_HOST_NAME`、`SIM_PROTOCOL`、`SIM_MACHINE`、`SIM_SETPOINTS`、`SIM_DIALECT` …）。
首次启动在 `--cert-dir` 生成的凭据：

| 模拟设备 | 生成的文件 | 系统侧怎么用 |
|---|---|---|
| OPC UA（`opcua_device`、`plc_device --protocol opcua`） | `<设备ID>.crt/.key`、`ilcs-client.crt/.key`、`ilcs-client.json` | `server_certificate` 钉住 `<设备ID>.crt`；`credential_ref` = `file://…/ilcs-client.json`。同一目录的几台共用这张客户端证书，服务器只信任它 |
| HTTPS 网关 | `<设备ID>.crt/.key`、`<设备ID>.token` | `ca_file` 指向证书；`credential_ref` = `file://…/<设备ID>.token` |
| SiLA 2 | `<设备ID>.crt/.key` | `ca_file` 指向证书 |
| AGV 车队 | `fleet.json`（`{"headers": {"Authorization": "Basic …"}}`） | `credential_ref` = `file://…/fleet.json` |

模拟设备在身份里带 `ILCS-SIMULATOR`（或 `simulator: true`）：`ILCS_ENVIRONMENT=production` 时所有驱动的健康检查都拒绝它们。

## 故障注入

每个目录都带 `fault.py`，在模拟器容器里执行，沿用容器的配置与凭据：

```bash
docker compose exec line-sim-oven      python simulators/line_device/fault.py interlock     # 门开：DOOR? → OPEN
docker compose exec line-sim-arm       python simulators/line_device/fault.py lost_receipt  # play 了但不回复
docker compose exec mtsics-sim-balance python simulators/mt_sics/fault.py busy              # S → "S I"
docker compose exec plc-sim-coater     python simulators/plc_device/fault.py fail           # 作业报警 ErrorCode 17
docker compose exec fleet-sim          python simulators/fleet/fault.py estop --robot AGV-01
docker compose exec gateway-sim-cycler python simulators/http_gateway/fault.py stuck
docker compose exec sila-sim-lh        python simulators/sila_device/fault.py offline 30
docker compose exec <服务>             python simulators/<目录>/fault.py none               # 恢复
docker compose exec <服务>             python simulators/<目录>/fault.py state              # 当前故障与动作次数
```

走的都是协议本身的通道或模拟器专用扩展：文本命令设备与天平收 `SIM:FAULT` / `SIM:STATE?`，PLC 写 `SimFault` 点，中间库写设备表的 `sim_fault` 列，
车队与网关调 `POST /simulator/fault`，SiLA 2 / OPC UA 调 `SimulatorControl`，Modbus 任务寄存器写 `simulator_control` 块。

**统一控制口**（`common/control.py`）：每台模拟设备另外开一个与协议无关的 HTTP 控制口（设 `SIM_CONTROL_PORT` 才开，试点是 9900，
只在后端网络可见；令牌文件 `SIM_CONTROL_TOKEN_FILE` 首次启动生成）：

```
GET  /simulator/state[?unit=AGV-01]   故障模式、总动作次数 motions、认指令号时各指令号的动作次数
POST /simulator/fault {"mode": "lost_receipt", "parameter": 0, "unit": ""}
```

接入验收的故障项目只认这一个口（适配器配置里的 `simulator_control`），不用为每种协议各写一个注入器；
不认 ILCS 指令号的设备（文本命令、PLC 点表、天平、车队）报总动作次数，验收据此判断「重投有没有让设备再动一次」。

| 模式 | 效果 | 系统应有的反应 |
|---|---|---|
| `offline` | 关掉监听 N 秒（PLC / 仪表照常运行） | 探测判失联；恢复后按原指令号（或作业台账）查回作业 |
| `slow_submit` | 启动命令的应答迟到 N 秒 | 超时判结果未知，不重发 |
| `lost_receipt` | 已经动作，但应答丢了（不回复 / 断开连接 / 内部错误） | 结果未知、批次挂起转人工核查；之后见到设备在运行或按指令号找回，质量标 uncertain |
| `interlock` | 联锁：SiLA `Interlocked`、OPC UA `BadInvalidState`、网关 423、干燥箱门开、PLC SafetyOk=false、UR 保护停止、天平 `S I` | 明确失败，设备没动 |
| `busy` | 忙 / 不在远程模式：PLC RemoteMode=false、UR 非 RUNNING、车队 Error、天平 `S I` | 明确失败，设备没动 |
| `estop`（车队） | 急停：状态 EmergencyStop，执行中的任务冻结 | 联锁；新转运明确拒绝 |
| `fail` / `partial` | 执行失败 / 执行到一半停住（PLC ErrorCode 17 / 23，干燥箱 ALARM E05 / E07，天平 `S +`，车队任务 Aborted） | 故障，带设备给出的原因 |
| `stuck` | 永不结束 | 超时报警，超过硬上限转结果未知 |
| `no_dedup` | 同一指令号重复提交会再动作一次（只对认识指令号的设备有意义） | 系统本身保证只投递一次 |
| `clock_skew` | 设备时间偏移 N 秒 | 遥测超前 5 分钟以上被拒收 |
