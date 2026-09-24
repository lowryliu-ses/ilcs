# ILCS 外部模拟设备

在系统外部独立运行、走真实网络协议的模拟设备。系统侧把工位适配器配成对应的真实驱动接入，和接一台真设备
走同一条路，用来在真机到位之前验证驱动、执行器、对账与异常处置。它们**不是**系统内置的模拟适配器。

四种协议共用同一套设备行为（`common/device.py`）：任务按 ILCS 指令号登记并去重，长任务后台推进，可保持 / 恢复 /
终止，故障注入一致，`executions` 记录每个指令号真正动作了几次——验收「不重复执行」就看它。协议层只做报文转换。

| 目录 | 协议 | 系统侧驱动 | 契约 | 安全 | Compose 服务（`--profile pilot`） |
|---|---|---|---|---|---|
| `sila_device/` | SiLA 2（gRPC） | `sila2_v1` | `contracts/sila2/TaskExecution.sila.xml` | TLS，自签证书 | `sila-sim-lh`（ST-06）、`sila-sim-cycler`（ST-07） |
| `modbus_device/` | Modbus TCP | `modbus_tcp_v1` | `contracts/modbus/TaskRegisters.json` | 无（隔离网段） | `modbus-sim-mixer`（ST-02） |
| `opcua_device/` | OPC UA（opc.tcp） | `opcua_v1` | `contracts/opcua/TaskExecution.json` | Basic256Sha256 + SignAndEncrypt，双向证书 | `opcua-sim-calender`（ST-04） |
| `http_gateway/` | HTTPS JSON | `http_json_v1` | docs/设备适配器配置模板.md「HTTPS JSON 网关驱动」 | TLS + Bearer 令牌 | `gateway-sim-coater`（ST-03） |

## 设备类型（`--profile`，各协议通用）

| 类型 | 行为 |
|---|---|
| `generic` | 按数值参数回报实测值（确定性的 ±0.5% 偏差） |
| `liquid_handler` | 按 `params.wells` 逐孔位执行，回报每孔实际加入量，按 `--material-map` 折算物料消耗 |
| `cycler` | `--channels` 个通道，每个任务占一个，满了明确拒绝（`DeviceBusy`） |

Modbus 只能传数值参数，所以 Modbus 模拟设备只用 `generic`：参数槽位没有名字，设备按槽位 p1…p16 回报实测值，
驱动再按适配器配置的 `params` 换回参数名。孔位矩阵这类结构化参数请用 OPC UA / SiLA 2 / 网关。

## 本机运行

```bash
api/.venv/bin/python simulators/modbus_device/server.py --device-id SIM-MIX-01 --port 5020
api/.venv/bin/python simulators/opcua_device/server.py  --device-id SIM-CAL-01 --port 4840 --cert-dir ./opcua-certs
api/.venv/bin/python simulators/http_gateway/server.py  --device-id SIM-COAT-01 --port 8443 --cert-dir ./gateway-certs
api/.venv/bin/python simulators/sila_device/server.py   --device-id SIM-LH-01 --port 50052 --insecure
```

参数也可以用同名环境变量给（`SIM_DEVICE_ID`、`SIM_PORT`、`SIM_PROFILE`、`SIM_TASK_SECONDS`、`SIM_CHANNELS`、
`SIM_MATERIAL_MAP`、`SIM_CERT_DIR`、`SIM_HOST_NAME` …）。首次启动在 `--cert-dir` 生成的凭据：

| 模拟设备 | 生成的文件 | 系统侧怎么用 |
|---|---|---|
| OPC UA | `<设备ID>.crt/.key`、`ilcs-client.crt/.key`、`ilcs-client.json` | `server_certificate` 钉住 `<设备ID>.crt`；`credential_ref` = `file://…/ilcs-client.json`。服务器只信任这张客户端证书 |
| HTTPS 网关 | `<设备ID>.crt/.key`、`<设备ID>.token` | `ca_file` 指向证书；`credential_ref` = `file://…/<设备ID>.token` |
| SiLA 2 | `<设备ID>.crt/.key` | `ca_file` 指向证书 |

证书主机名取 `--host-name`，须与系统连接用的主机名一致（Compose 里就是服务名）。主机都必须列入
`ILCS_ADAPTER_ALLOWED_HOSTS`。切换工位适配器用 `scripts/configure-pilot-adapters.py`（会留审计，可 revert）。

## 故障注入

每个目录都带 `fault.py`，在模拟器容器里执行，沿用容器的配置与凭据：

```bash
docker compose exec modbus-sim-mixer   python simulators/modbus_device/fault.py lost_receipt
docker compose exec opcua-sim-calender python simulators/opcua_device/fault.py offline 30
docker compose exec gateway-sim-coater python simulators/http_gateway/fault.py interlock
docker compose exec sila-sim-lh        python simulators/sila_device/fault.py state
docker compose exec <服务>             python simulators/<目录>/fault.py none     # 恢复
```

走的都是协议本身的通道：Modbus 写模拟器专有的 `simulator_control` 寄存器，OPC UA 调 `SimulatorControl.SetFault`，
网关调 `POST /simulator/fault`，SiLA 2 调 `SimulatorControl` 特性。

| Mode | Modbus TCP | OPC UA | HTTPS 网关 | 系统应有的反应 |
|---|---|---|---|---|
| `offline` | 关监听 N 秒，PLC 照常扫描 | 服务器停 N 秒 | 关监听 N 秒 | 探测判失联；恢复后按原指令号查回任务 |
| `slow_submit` | N 秒后才写应答序号 | 方法 N 秒后返回 | N 秒后才响应 | 超时判结果未知，不重发 |
| `lost_receipt` | 已动作，应答永远不写 | 已动作，返回 `BadUnexpectedError` | 已动作，直接断开连接 | 结果未知、批次挂起转人工核查，不重发 |
| `no_dedup` | 同一指令号重复触发会再动作一次 | 同左 | 同左 | 系统本身保证只投递一次 |
| `fail` / `partial` | 结果区错误码 1 / 2 | 回执 `failed` | 回执 `failed` | 故障；「部分执行」只能终止 |
| `stuck` | 永不结束 | 同左 | 同左 | 超时报警，超过硬上限转结果未知 |
| `interlock` / `busy` | 应答码 2 / 3 | `BadInvalidState` / `BadResourceUnavailable` | HTTP 423 | 明确失败，设备没动 |
| `clock_skew` | 设备时间偏移 N 秒 | 同左 | 同左 | 遥测超前 5 分钟以上被拒收 |

## 正式环境

所有模拟设备都在身份里自报 `simulator: true`（Modbus 是身份区标志位）。`ILCS_ENVIRONMENT=production` 时四个驱动的
健康检查都会拒绝它们，模拟设备不会被误当作真设备进入正式环境。
