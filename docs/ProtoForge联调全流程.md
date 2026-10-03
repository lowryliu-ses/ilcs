# ProtoForge 联调线全流程

用本机的 ProtoForge 模拟设备，在 ILCS 上把一条测试流程从头配到尾：设备接入 → 环境采集 → SOP → 能力与设备方法 →
实验流程 → 实验方案 → 实验任务 → 排程 → 批次执行 → 数据复核 → 报告。三台设备都经驱动宿主接入，ILCS 只走 SiLA 2。
`scripts/load-protoforge-flow.py` 按本文一次跑完，界面上每一步都能对着看、也能手工照做。

## 设备与分工

| 工位 | 设备（ProtoForge） | 接入 | 在流程里做什么 |
|---|---|---|---|
| ST-PF-MB | Modbus TCP 从站 2 的握手 PLC | 驱动宿主 `PF-MB-PLC`（50201），点位 + 任务 | 执行「PLC 控温运行」：写设定温度、置启动，约 5 s 后回报实测温度并复位 |
| ST-PF-HTTP | HTTP REST 温湿度传感器 | 驱动宿主 `PF-HTTP`（50203），只读写点位 | 环境采集：温度、相对湿度记成「设备模拟联调区」的读数 |
| ST-PF-OPCUA | OPC UA 压力传感器 | 驱动宿主 `PF-OPCUA`（50202），只读写点位 | 环境采集：压力记成 ST-PF-MB 工位的读数 |

OPC UA、HTTP 两台在 ProtoForge 里没有会动作的规则（写节点、POST 只改协议那一层），所以不承接工步；它们的读数经**环境采集**
进流程：步骤写了环境要求，开跑检查和 PLC 步骤下发前都按最新读数核对，没有读数、过期、超限都不放行。

前提：驱动宿主在跑、三台设备已经接入并验收（`scripts/load-driver-host-pilot.py register`，见 `devices/host/README.md`），
ProtoForge 里导入并启动了 `devices/simulators/protoforge/ilcs-plc-scenario.json`（从站 2 的握手 PLC），执行器在跑。

## 一键跑

```bash
python3 scripts/load-protoforge-flow.py register      # 环境采集、能力、指标、设备方法、资质、SOP、流程
python3 scripts/load-protoforge-flow.py run --samples 3 --temp 60
```

只走 HTTP、和界面调同一组接口，签名用演示账号逐次签署；已有的先查后用，重复运行不会多建（每次 `run` 新建一个方案、任务、批次、报告）。

## 在界面上怎么配（脚本做的就是这些）

| 环节 | 菜单 | 谁 | 配什么 |
|---|---|---|---|
| 环境采集 | 工位与接入 → 设备连接（ST-PF-HTTP、ST-PF-OPCUA） | 自动化工程师 | 连接配置加 `environment`（见下），签名保存；不用重新握手。「环境监测」里随后出现来源 `device:<工位>` 的读数 |
| 能力 | 能力字典 | 自动化工程师 | `cap.plc_run`（PLC 控温运行，参数 `temp` ℃）；ST-PF-MB 的范围 0–100 ℃ |
| 检测指标 | 指标与规则 | 研究员 | `pf_plc_temp` PLC 实测温度（℃，0–150），样本类型「联调样品」 |
| 设备方法 | 设备方法 | 工程师起草、QA 发布 | 「ProtoForge PLC 控温运行」：能力 `cap.plc_run`，输出 `temp` → 指标 PLC 实测温度（设备回报写成检测结果） |
| 资质 | 人员与资质 | 管理员 | 操作员 P-003 加能力资质 `cap.plc_run`（开跑检查要） |
| SOP | SOP 规程 | 研究员起草、QA 批准发布、操作员阅读确认 | `SOP-PF-01` v1（`scripts/lines/protoforge/sop.json`，附件 PDF 同目录） |
| 实验流程 | 实验流程 | 研究员起草、QA 批准并发布 | 「ProtoForge PLC 控温运行联调」：关联 SOP-PF-01，四步（见下），写风险评估 |
| 实验方案 | 实验方案 | 研究员起草、锁定、提交，QA 批准 | 单条件：N 个联调样品、设定温度按流程，要求指标 PLC 实测温度 |
| 实验任务 | 任务中心 | 研究员建、分配给操作员，操作员接受 | 关联方案 |
| 排程 | 批次管理 → 排程 / 排程 | 操作员 | 建批次（关联任务）→ 排程：人工步骤与 ST-PF-MB 的时间窗 |
| 批次执行 | 批次管理 | 操作员签名下发、QA 审核 | 开跑检查（含环境核对）→ 签名下发 → 两个人工节点 → 执行器经驱动宿主下发 PLC 控温运行 → QA 审核节点 |
| 数据复核 | 数据审核 | QA | 设备回报的实测温度逐条复核（带「模拟」标记） |
| 报告 | 报告管理 | 研究员起草、QA 批准并发布 | 批次报告 |

### 环境采集配置

```json
"environment": {"zone": "设备模拟联调区", "interval_sec": 30, "points": {"temperature": "temperature", "humidity": "humidity"}}
```

ST-PF-OPCUA 的是 `{"zone": "ST-PF-MB", "interval_sec": 30, "points": {"pressure": "pressure"}}`。写法见
[设备适配器配置模板](设备适配器配置模板.md)「点位读数记成环境读数」。

### 流程的四步

| 步 | 类型 | 内容 | 环境要求 |
|---|---|---|---|
| s01 核对环境与设备在线 | 人工 | 勾选三台设备在线 | 设备模拟联调区：温度 10–45 ℃、相对湿度 ≤ 80 %RH |
| s02 装样并核对设定温度 | 人工（核对样本） | 勾选已核对设定温度 | — |
| s03 PLC 控温运行 | 设备（ST-PF-MB，`cap.plc_run`，设定温度 60 ℃，设备方法） | 执行器下发；PLC 回报实测温度 | ST-PF-MB 压力 0.5–5 bar，加上实验区温湿度 |
| s04 QA 复核运行数据 | 审核（QA） | QA 批准 | — |

每步关联 SOP-PF-01 对应的步骤。

## 看结果

- **环境监测**：设备模拟联调区的温度、湿度，ST-PF-MB 的压力，来源 `device:ST-PF-HTTP` / `device:ST-PF-OPCUA`，每 30 s 一条。
- **批次管理 → 批次详情**：每步检查点；第 3 步的检查点里是 PLC 的回报（`real:sila2_v1`，实测温度 61.5 ℃——ProtoForge 场景的规则写死的值）。
- **数据审核 / 结果分析**：PLC 实测温度，一个样品一条，带「模拟」标记。
- **报告管理**：已发布的批次报告。

## 演练环境不满足时

- 把 ProtoForge HTTP 设备的湿度改到 80 以上（ProtoForge 界面，或「设备连接 → 点位」签名手动写 `humidity`），等下一次采集：
  新批次的开跑检查不放行，写明「设备模拟联调区 相对湿度 85%RH 高于要求 80」（第 1、3 步都列出）；已经下发的批次在 PLC 步骤
  投递前同样被拦下。演练建的批次在批次管理里退回待排程、签名终止；湿度写回 55 后下一次采集就恢复。
- 停掉驱动宿主（`docker stop ilcs-driver-host`）：三台设备离线、读数不再更新，超过环境读数时效后开跑检查报读数过期；
  执行门挡住 ST-PF-MB。
