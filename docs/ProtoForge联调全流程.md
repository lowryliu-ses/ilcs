# ProtoForge 联调线全流程（九台设备、多设备矩阵）

用本机的 ProtoForge 模拟设备，在 ILCS 上把一条测试流程从头配到尾：设备接入 → 环境采集 → SOP → 能力与设备方法 →
实验流程 → 多设备矩阵方案 → 实验任务 → 排程 → 批次执行 → 数据复核 → 报告。ProtoForge 里的九台设备（七种协议）都经
驱动宿主接入，ILCS 只走 SiLA 2；一个实验任务、一个批次把九台都用上。

> 2026-10-06 起 ProtoForge 里从站 2 的握手 PLC 已删，Modbus 从站 1 是温湿度传感器（ST-PF-MB，只读写点位的环境传感器）；
> 同一天 ProtoForge 新加了六台设备（S7-1500 OPC UA、S7-1200 S7 协议、S7-1200 Modbus TCP、PROFINET、MQTT 环境监测传感器、
> 三菱 FX5U MC 协议），流程改成九台一起跑（SOP-PF-01 v6、流程「ProtoForge 全设备多协议联调」）。
`scripts/load-protoforge-flow.py` 按本文一次跑完，界面上每一步都能对着看、也能手工照做。

## 设备与分工

六台设备参与设定：方案用设计点给每个样本一组设定值，六台设备依次为每个样本写设定值、回读。它们一次只能做一个设定，
由驱动宿主里的映射插件**按样本依次执行**：一条指令带全部样本的设定值，插件一孔一孔跑完，回执按孔位回报，结果落到各自的样本上。
另外三台只读写点位，读数当环境 / 过程读数，开跑检查与设备步骤下发前按它们核对。

| 工位 | 设备（ProtoForge） | 接入（驱动宿主插件，SiLA 端口） | 在流程里做什么 |
|---|---|---|---|
| ST-PF-MB | Modbus TCP 从站 1 的温湿度传感器 | `PF-MB-PLC`（modbus_map，50201），只读写点位 | 环境传感器：温度、相对湿度记成「设备模拟联调区」的读数 |
| ST-PF-OPCUA | OPC UA 温控 / 压力节点 | `PF-OPCUA`（opcua_map，50202） | 「温控器设定温度」（`cap.tc_setpoint`）：写 `Temperature` 节点、回读；高报为真判故障。兼作压力传感器 |
| ST-PF-HTTP | HTTP REST 设备 | `PF-HTTP`（rest_map，50203） | 「环境箱设定温度」（`cap.chamber_setpoint`）：`POST /temperature`、按点表读回；状态不是 normal 判故障 |
| ST-PF-S71500 | 西门子 S7-1500（OPC UA，`ns=2;s=CPU.*`、`DB1.*` 节点） | `PF-S7-1500`（opcua_map，50204） | 「S7-1500 压力设定」（`cap.pressure_setpoint`）：写 `DB1.DBD0`、回读；CPU 状态 0（STOP）判故障、2（HOLD）判保持 |
| ST-PF-S7 | 西门子 S7-1200（S7 协议，机架 0 槽 1） | `PF-S7-1200`（s7_map，50205） | 「S7-1200 速度设定」（`cap.speed_setpoint`）：写 `DB1.DBD8`、回读；运行状态 `DB1.DBX0.0` 为假判故障 |
| ST-PF-S7MB | 西门子 S7-1200（Modbus TCP 从站 2） | `PF-S7-1200-MB`（modbus_map，50206） | 「S7-1200 模拟量输出」（`cap.analog_output`）：写 `ao0`（保持寄存器 8–9，mA）、回读；运行模式 0 判故障 |
| ST-PF-PN | PROFINET S7-1200（ProtoForge 的 TCP 模拟） | `PF-PROFINET`（profinet_sim_map，50207） | 「PROFINET 模拟量输出」（`cap.pn_analog_output`）：读改写整幅过程映像写 `QW64`（V）、回读 |
| ST-PF-MQTT | MQTT 环境监测传感器（ProtoForge 自带 MQTT 服务器） | `PF-MQTT-ENV`（mqtt_map，50208），只读写点位 | 环境传感器：温度、湿度、CO2、PM2.5、噪声记成「ProtoForge 环境监测点」的读数 |
| ST-PF-FX5U | 三菱 FX5U（MC 协议 3E 二进制帧） | `PF-FX5U`（mc_map，50209），只读写点位 | 过程读数：压力、模块温度记成它自己（ST-PF-FX5U）的读数 |

六台设定设备在 ProtoForge 里都没有启动信号，写完设定值就生效，所以按**设定类动作**接：点表映射的启动写
`{"write_only": true}` 并配 `idle_after_start: "done"`，状态点用设备自己的运行状态（OPC UA 温控用高报、S7 用运行状态位、
Modbus 用运行模式、PROFINET 用数据状态）；REST 映射的能力请求本身就是写设定值的请求。写法见驱动宿主插件配置
（`ilcs-devices/host/插件配置.md`）「逐孔依次执行」与各协议插件一节。驱动宿主的现场配置在 `ilcs-devices/host/sites/local/devices/`
（本机在用的是 `data/driver-host/site`），也可以在驱动宿主的设备管理台（`http://127.0.0.1:50200`）上看状态、改映射、起停各台设备的
服务；改了映射照样要在「设备连接」签名批准、重新验收。

前提：驱动宿主在跑、九台设备已经接入并验收（`scripts/load-driver-host-devices.py register`，见 `ilcs-devices/host/README.md`），
ProtoForge 里七个协议都启动了、九台设备按 `ilcs-devices/simulators/protoforge/README.md` 配好（S7-1200 的 Modbus 用从站 2；FX5U 的点
排成 D0、D2、D4、D8、D12、D16——ProtoForge 的 MC 服务把 Dn 当字节偏移，点的字节范围不能重叠），执行器在跑。

## 一键跑

```bash
python3 scripts/load-protoforge-flow.py register
```

```bash
python3 scripts/load-protoforge-flow.py run --temps 40,60,80 --repeats 1
```

`--temps` 给 2–8 个设计温度（0–100 ℃），`--repeats` 是每组设定值的重复次数（1–2）。另外四台的设定值在各自的范围里按样本数等分
（见下）。只走 HTTP、和界面调同一组接口，签名用演示账号逐次签署；已有的先查后用，重复运行不会多建。每次 `run` 新建一个方案、
任务、批次、报告，留在本地库里当测试案例。

## 在界面上怎么配（脚本做的就是这些）

| 环节 | 菜单 | 谁 | 配什么 |
|---|---|---|---|
| 驱动配置 | 工位与接入 → 设备连接 | 自动化工程师 | 驱动宿主的现场配置改了（例如给设备加了设定类能力），设备服务报的驱动配置就变了：核对后签名批准这次变更，只读级验收通过后放行；六台设定设备再做动作级验收 |
| 资产 | 仪器设备 | 自动化工程师 | 九台工位各关联一个占位资产 AS-PF-MB、AS-PF-OPCUA、AS-PF-HTTP、AS-PF-S71500、AS-PF-S7、AS-PF-S7MB、AS-PF-PN、AS-PF-MQTT、AS-PF-FX5U（模拟设备，校准不适用；已关联资产的工位沿用）：设备步骤的开跑检查要核对工位资产 |
| 环境采集 | 工位与接入 → 设备连接（ST-PF-MB、ST-PF-OPCUA、ST-PF-MQTT、ST-PF-FX5U） | 自动化工程师 | 连接配置加 `environment`（见下），签名保存；不用重新握手。「环境监测」里随后出现来源 `device:<工位>` 的读数 |
| 能力 | 能力字典 | 自动化工程师 | `cap.tc_setpoint`、`cap.chamber_setpoint`（`temp` ℃）、`cap.pressure_setpoint`（`pressure` MPa）、`cap.speed_setpoint`（`speed` RPM）、`cap.analog_output`（`current` mA）、`cap.pn_analog_output`（`voltage` V），各落在一台工位上，工位范围缺省 0–100。只读写点位的三台不承接能力 |
| 检测指标 | 指标与规则 | 研究员 | `pf_tc_temp`、`pf_chamber_temp`（℃）、`pf_s71500_pressure`（MPa）、`pf_s7_speed`（RPM）、`pf_s7mb_current`（mA）、`pf_pn_voltage`（V）六个回读指标，样本类型「联调样品」 |
| 设备方法 | 设备方法 | 工程师起草、QA 发布 | 六个方法，各自把设备回报的设定回读写成对应指标的检测结果（逐样本回报按孔位落到样本上） |
| 资质 | 人员与资质 | 管理员 | 操作员 P-003 加六项能力资质（开跑检查要） |
| SOP | SOP 规程 | 研究员起草、QA 批准发布、操作员阅读确认 | `SOP-PF-01` v6（`scripts/lines/protoforge/sop.json`，附件 PDF 同目录，`render-sop.py` 生成） |
| 实验流程 | 实验流程 | 研究员起草、QA 批准并发布 | 「ProtoForge 全设备多协议联调」：关联 SOP-PF-01，九步（见下），写风险评估；原来的「ProtoForge 多温度矩阵控温联调」出修订版取代 |
| 实验方案 | 实验方案 | 研究员起草、锁定、提交，QA 批准 | 矩阵方案（见下）：六个设定因子按设计点对齐，每个样本一组设定值；要求六项指标 |
| 实验任务 | 任务中心 | 研究员建、分配给操作员，操作员接受 | 关联方案 |
| 排程 | 批次管理 → 排程 | 操作员 | 建批次（关联任务）→ 排程：六台设备各一个时间窗，人工步骤不占工位 |
| 批次执行 | 批次管理 | 操作员签名下发、QA 审核 | 开跑检查（含环境核对）→ 签名下发 → 两个人工节点 → 六个设备步骤（执行器经驱动宿主下发，按样本依次执行）→ QA 审核节点 |
| 数据复核 | 数据审核 | QA | 设备回报逐样本复核（带「模拟」标记） |
| 报告 | 报告管理 | 研究员起草、QA 批准并发布 | 批次报告 |

### 环境采集配置

设定设备的温度会被设定步骤改写，不当环境温度。实验区的温度、湿度由温湿度传感器给：

```json
"environment": {"zone": "设备模拟联调区", "interval_sec": 30, "points": {"temperature": "temperature", "humidity": "humidity"}}
```

这是 ST-PF-MB 的（连接配置里照旧 `"tasks": false`）。其余三台：

| 工位 | `environment` |
|---|---|
| ST-PF-OPCUA | `{"zone": "ST-PF-OPCUA", "interval_sec": 30, "points": {"pressure": "pressure"}}` |
| ST-PF-MQTT | `{"zone": "ProtoForge 环境监测点", "interval_sec": 30, "points": {"temperature": "temperature", "humidity": "humidity", "co2": "co2", "pm25": "pm25", "noise": "noise"}}` |
| ST-PF-FX5U | `{"zone": "ST-PF-FX5U", "interval_sec": 30, "points": {"pressure": "pressure", "temperature": "temperature"}}` |

MQTT 传感器单独用一个区域：它的温度、湿度是另一处的读数，和实验区的混在一个区域里，「最新读数」就会在两台之间来回跳。
`co2`、`pm25`、`noise` 不是 ILCS 预置的指标名，照样收、照样核对，只是界面上没有中文名。ST-PF-HTTP 不再兼作湿度传感器，
连接配置里没有 `environment`。写法见 [设备适配器配置模板](设备适配器配置模板.md)「点位读数记成环境读数」。

### 流程的九步

| 步 | 类型 | 内容 | 环境要求 |
|---|---|---|---|
| s01 核对环境与设备在线 | 人工 | 勾选九台设备在线 | 设备模拟联调区：温度 15–35 ℃、相对湿度 ≤ 80 %RH；环境监测点：温度 10–40 ℃、湿度 ≤ 85 %RH、CO2 ≤ 2000 ppm、PM2.5 ≤ 200 μg/m³、噪声 ≤ 90 dB；ST-PF-FX5U：压力 0.1–3 MPa、模块温度 20–60 ℃ |
| s02 装样并核对各样本设定值 | 人工（核对样本） | 勾选已按方案核对每个样本的六项设定值 | — |
| s04 温控器设定温度 | 设备（ST-PF-OPCUA，`cap.tc_setpoint`） | 按样本依次写设定温度、回读 | ST-PF-OPCUA 压力 0.5–10 bar，加上实验区温度、湿度 |
| s05 环境箱设定温度 | 设备（ST-PF-HTTP，`cap.chamber_setpoint`） | 按样本依次写设定温度、回读 | 实验区温度、湿度 |
| s07 S7-1500 压力设定 | 设备（ST-PF-S71500，`cap.pressure_setpoint`） | 按样本依次写压力设定、回读 | 实验区温度、湿度 |
| s08 S7-1200 速度设定 | 设备（ST-PF-S7，`cap.speed_setpoint`） | 按样本依次写速度设定、回读 | 实验区温度、湿度 |
| s09 S7-1200 模拟量输出 | 设备（ST-PF-S7MB，`cap.analog_output`） | 按样本依次写输出电流、回读 | 实验区温度、湿度 |
| s10 PROFINET 模拟量输出 | 设备（ST-PF-PN，`cap.pn_analog_output`） | 按样本依次写输出电压、回读 | 实验区温度、湿度 |
| s06 QA 复核运行数据 | 审核（QA） | QA 批准 | — |

步骤编号沿用原来的：s03 是删掉的「PLC 控温运行」，s06 是 QA 复核，都不复用（从旧流程出修订版时，同一个编号还指同一步），
新加的四个设备步骤接着用 s07–s10。设备步骤的参数在流程里写的是缺省值，方案的因子会按样本改写。监测点与 FX5U 的范围比 ProtoForge
生成的读数宽一圈：读数一直在动，只拦真的越界。每步关联 SOP-PF-01 对应的步骤。

### 多设备矩阵方案

方案类型「矩阵」，六个因子各落到一个设备步骤的参数：

| 因子 | 水平（3 个样本时） | 落到 |
|---|---|---|
| 温控器设定温度 | 40、60、80 ℃（`--temps`） | s04 `temp` |
| 环境箱设定温度 | 40、60、80 ℃（`--temps`） | s05 `temp` |
| S7-1500 压力设定 | 2、2.5、3 MPa（2–3 等分） | s07 `pressure` |
| S7-1200 速度设定 | 40、60、80 RPM（40–80 等分） | s08 `speed` |
| S7-1200 输出电流 | 8、12、16 mA（8–16 等分） | s09 `current` |
| PROFINET 输出电压 | 2.5、5、7.5 V（2.5–7.5 等分） | s10 `voltage` |

设计点是 `[[40, 40, 2, 40, 8, 2.5], [60, 60, 2.5, 60, 12, 5], [80, 80, 3, 80, 16, 7.5]]`：第 i 个样本取每台设备的第 i 档，
而不是全组合。3 个设计点 × 1 次重复 = 3 个样品（A1、A2、A3）。批次里每个设备步骤是**一条指令**，参数带逐孔设定值：

```json
{"pressure": 2.5, "wells": {"A1": {"pressure": 2.0}, "A2": {"pressure": 2.5}, "A3": {"pressure": 3.0}}}
```

驱动按 A1、A2、A3 的顺序一孔一孔跑，每孔有自己的运行号（`<指令号>/<序号>`）。提交时先把每一孔的整套启动拼一遍，
有一孔拼不出来就整条拒绝、设备一次都没动；某一孔没做成，整条指令到此结束，结论写明是第几孔、后面几孔没有执行。

## 看结果

- **批次管理 → 批次详情**：六个设备步骤的检查点里是逐孔回报（`delivered.wells`，`real:sila2_v1`）。
- **数据审核 / 结果分析**：一个样品六条结果，各台设备的回读等于各样本的设计值。
- **环境监测**：设备模拟联调区的温度、相对湿度（来源 `device:ST-PF-MB`），环境监测点的温度、湿度、CO2、PM2.5、噪声
  （`device:ST-PF-MQTT`），ST-PF-OPCUA、ST-PF-FX5U 的压力（FX5U 还有模块温度），每 30 s 一条。
- **报告管理**：已发布的批次报告。

脚本最后会逐样本打印六台设备的设计值和回读，例如：

```
✓ A1 S261006007-A1：温控器设定温度 40 → 40.0 ℃；环境箱设定温度 40 → 40.0 ℃；S7-1500 压力设定 2 → 2.0 MPa；S7-1200 速度设定 40 → 40.0 RPM；S7-1200 模拟量输出 8 → 8.0 mA；PROFINET 模拟量输出 2.5 → 2.5 V
```

## 演练环境不满足时

- 把温湿度传感器的湿度改到 80 以上（ProtoForge 界面，或 ST-PF-MB「设备连接 → 点位」签名手动写 `humidity`；传感器的可写范围
  30–80 %RH，要演练超限就在 ProtoForge 里改），等下一次采集：新批次的开跑检查不放行，写明「设备模拟联调区 相对湿度高于要求 80」；
  已经下发的批次在设备步骤投递前同样被拦下。演练建的批次在批次管理里退回待排程、签名终止；湿度写回 50 后下一次采集就恢复。
- OPC UA 的压力高于 10 bar 或低于 0.5 bar：温控器那一步的环境要求不满足，开跑检查与它投递前都被拦下。
- MQTT 传感器停了（ProtoForge 里停掉这台设备）：驱动宿主 30 s 收不到新消息就判读不到，环境监测点的读数不再更新，超过读数时效后
  开跑检查报读数过期。
- 停掉驱动宿主（`docker stop ilcs-driver-host`）：九台设备离线、读数不再更新，超过环境读数时效后开跑检查报读数过期；
  执行门挡住六台设定设备。
