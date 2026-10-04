# ProtoForge 模拟设备接 ILCS

[ProtoForge](https://github.com/suoten/ProtoForge)（MIT）是一个多协议设备仿真平台。这里给它准备了一台带「启动握手」的
Modbus PLC，让驱动宿主（devices/host）的 `modbus_map` 插件能像接真 PLC 一样下发、等完成、取实测值；ILCS 经 SiLA 2 接驱动宿主。

| 文件 | 内容 |
|---|---|
| `ilcs-plc-scenario.json` | ProtoForge 场景（「场景 → 导入」）：从站 2 上一台 PLC 与两条协同规则 |

三台 ProtoForge 设备（这台 PLC、OPC UA 温控 / 压力节点、HTTP REST 设备）在驱动宿主里的设备文件见
`devices/host/sites/local/devices/` 的 PF-MB-PLC、PF-OPCUA、PF-HTTP；ILCS 这边的工位（ST-PF-MB、ST-PF-OPCUA、ST-PF-HTTP，
`sila2_v1`）用 `scripts/load-driver-host-devices.py register` 接，全流程联调见 `docs/ProtoForge联调全流程.md`。

握手：插件写设定值 `sp`（40003，float32）、置线圈 `cmd_start`（00001）→ PLC 清掉 `cmd_start`、状态字 `state`（40001）= 1
（运行）→ 5 s 后写实测值 `pv`（40005）、`state` = 3（完成）；置线圈 `cmd_ack`（00002）→ `state` = 0（空闲）。
序列号 `serial`（40011 起，字符串）是 ILCS 核对的设备编号。场景规则触发时还会把 `sp` 写回 0，实测值写死 61.5 ℃。

已用 ProtoForge 1.4.2 镜像加 ILCS 的接入验收（动作级，经驱动宿主）核对过：识别、在线、契约、正常完成（accepted → running →
done）、重建驱动后按指令号查回都通过，保持、终止按契约跳过；ProtoForge 没有 ILCS 的统一控制口，故障项目跳过。

**OPC UA、HTTP 设备做不了会动作的 PLC**：ProtoForge 1.4.2 只有 Modbus 会把外部客户端的写入交给它自己的规则引擎；OPC UA 客户端
写节点、HTTP POST 写点都只改了协议那一层，场景规则看不到启动信号，状态永远不变。它们能承接的是**设定类动作**（写设定值、回读，
插件的 `start.write_only`）与**只读写点位**；要接「会动作」的模拟设备，用仓库自带的模拟设备（`devices/simulators/plc_device`、
`opcua_device`、`fleet`）挂到驱动宿主上。ProtoForge 的 HTTP 设备端口 8080 只在它自己的网络里：先把 ProtoForge 容器接进
`ilcs_backend` 网络（`docker network connect ilcs_backend protoforge`），驱动宿主才连得到。
