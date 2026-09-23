# ILCS SiLA 2 模拟设备

在系统外部独立运行的 SiLA 2 设备（gRPC + TLS），实现 `contracts/sila2/TaskExecution.sila.xml`
任务契约。系统侧把工位适配器配成 `sila2_v1` 驱动接入，和接一台真设备走同一条路。它用于在
真机到位之前验证驱动、执行器、对账与异常处置，**不是**系统内置的模拟适配器。

## 设备类型

| `--profile` | 行为 |
|---|---|
| `liquid_handler` | 配液工作站：按 `params.wells` 逐孔位执行，回执 `delivered.wells` 为每孔实际加入量（确定性的 ±0.5% 偏差），`delivered.materials` 按 `--material-map` 折算为物料消耗，系统据此入库存 |
| `cycler` | 充放电柜：`--channels` 个通道，每个任务占一个通道，满了明确拒绝（`DeviceBusy`）；运行中可把电压 / 电流推到 ILCS 遥测接口 |
| `generic` | 通用：回执回显参数 |

## 运行

```bash
# 本机联调（不加密，仅限开发）
api/.venv/bin/python simulators/sila_device/server.py --device-id SIM-LH-01 --profile liquid_handler \
  --port 50052 --insecure --task-seconds 5 \
  --material-map '{"electrolyte": {"material": "电解液 LP57", "unit": "mL", "factor": 0.001}}'

# Compose 试点：随 ilcs 项目启动两台（配液 + 8 通道充放电柜），只在后端网络可见
docker compose --profile pilot up -d sila-sim-lh sila-sim-cycler
```

首次启动在 `--cert-dir` 生成自签证书 `<设备ID>.crt/.key`（主机名取 `--host-name`，须与系统连接用的主机名一致）。
系统侧适配器配置：

```json
{"host": "sila-sim-lh", "port": 50052, "ca_file": "/run/secrets/ilcs/sila/SIM-LH-01.crt",
 "expected_device_id": "SIM-LH-01", "request_timeout_sec": 10, "probe_interval_sec": 10}
```

`host` 必须列入 `ILCS_ADAPTER_ALLOWED_HOSTS`。在线状态由执行器按 `probe_interval_sec` 主动读取设备身份得到，
模拟设备不需要、也不会往系统推心跳。设了 `ILCS_URL`、`ILCS_SERVICE_SOURCE`、`ILCS_SERVICE_SECRET`、
`ILCS_STATION_ID` 时，运行中的任务会周期性推送遥测。

## 故障注入

通过 `SimulatorControl.SetFault(Mode, Parameter)`（任一 SiLA 2 客户端均可调用）。容器里自带命令行，沿用容器的证书与端口：

```bash
docker compose exec sila-sim-lh python simulators/sila_device/fault.py lost_receipt   # 注入
docker compose exec sila-sim-lh python simulators/sila_device/fault.py state          # 当前故障、任务、executions
docker compose exec sila-sim-lh python simulators/sila_device/fault.py none           # 恢复（故障持续生效，直到恢复）
```


| Mode | 效果 | 系统应有的反应 |
|---|---|---|
| `offline` | 停止 gRPC 服务 Parameter 秒后恢复；设备内部任务照常推进 | 探测判失联；恢复后按原指令号查回任务 |
| `slow_submit` | 已登记任务，Parameter 秒后才应答 | 超时判结果未知，不重发 |
| `lost_receipt` | 已开始动作，但回执丢失 | 结果未知、批次挂起转人工核查，不重发 |
| `no_dedup` | 同一指令号重复提交会再动作一次（真设备常见） | 系统本身保证只投递一次 |
| `fail` / `partial` | 执行失败 / 执行到一半停住（回报部分交付量） | 故障；现场核查可判「部分执行」只能终止 |
| `stuck` | 永不结束 | 超时报警，超过硬上限转结果未知 |
| `interlock` / `busy` | 明确拒绝（`Interlocked` / `DeviceBusy`），设备没动 | 明确失败 |
| `clock_skew` | 设备时间偏移 Parameter 秒 | 遥测超前 5 分钟以上被拒收 |

`SimulatorState` 返回每个指令号的实际动作次数（`executions`）——验收「不重复执行」就看它。

## 正式环境

设备身份里 `simulator: true`。`ILCS_ENVIRONMENT=production` 时驱动的健康检查直接拒绝它，
所以模拟设备不会被误当作真设备进入正式环境。
