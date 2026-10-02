# ProtoForge 模拟 PLC 接 ILCS

[ProtoForge](https://github.com/suoten/ProtoForge)（MIT）是一个多协议设备仿真平台。这里给它准备了一台带「启动握手」的
Modbus PLC，让 ILCS 的 `modbus_map_v1` 能像接真 PLC 一样下发、等完成、取实测值。

| 文件 | 内容 |
|---|---|
| `ilcs-plc-scenario.json` | ProtoForge 场景（「场景 → 导入」）：从站 2 上一台 PLC 与两条协同规则 |
| `ilcs-stations.json` | ILCS「工位与接入 → 设备连接」里填的驱动、协议、配置与支持标志 |

握手：ILCS 写设定值 `sp`（40003，float32）、置线圈 `cmd_start`（00001）→ PLC 清掉 `cmd_start`、状态字 `state`（40001）= 1
（运行）→ 5 s 后写实测值 `pv`（40005）、`state` = 3（完成）；置线圈 `cmd_ack`（00002）→ `state` = 0（空闲）。
序列号 `serial`（40011 起，字符串）是 ILCS 核对的设备编号。

已用 ProtoForge 1.4.2 镜像加 ILCS 的接入验收（动作级）核对过：识别、在线、契约、正常完成（accepted → running → done）、
重建驱动后按指令号查回都通过，保持、终止按契约跳过；ProtoForge 没有 ILCS 的统一控制口，故障项目跳过。

**OPC UA、HTTP 设备做不了会动作的 PLC**：ProtoForge 1.4.2 只有 Modbus 会把外部客户端的写入交给它自己的规则引擎；OPC UA 客户端
写节点、HTTP POST 写点都只改了协议那一层，场景规则看不到启动信号，状态永远不变。这两种协议要接「会动作」的模拟设备，用仓库
自带的模拟设备（`devices/simulators/plc_device`、`opcua_device`、`fleet`）。

**只读写点位不受这个限制**：映射驱动只配点表就能接（见 `docs/设备适配器配置模板.md`「两层：点位读写与任务执行」）。ProtoForge 的
OPC UA 压力传感器按下面配置接进来，「设备连接 → 点位」能读压力、温度、报警，手动把 `Setpoint` 写成 6.0（签名、执行器写、回读）：

```json
{"endpoint": "opc.tcp://host.docker.internal:4840/protoforge", "security_policy": "None",
 "points": {"pressure": {"node": "ns=2;s=Pressure", "unit": "bar"}, "temperature": {"node": "ns=2;s=Temperature", "unit": "℃"},
            "alarm_high": "ns=2;s=AlarmHigh", "alarm_low": "ns=2;s=AlarmLow",
            "setpoint": {"node": "ns=2;s=Setpoint", "unit": "bar", "writable": true, "min": 0, "max": 10}}}
```

HTTP 设备同理用 `rest_map_v1` 的点表（`{"path": "/temperature", "field": "value"}`），但 ProtoForge 的 HTTP 设备端口 8080 要先映射出来
（或把 ProtoForge 容器接进 `ilcs_backend` 网络），ILCS 才连得到。
