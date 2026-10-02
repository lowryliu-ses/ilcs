# 设备模块：天平称量加料站（MT-SICS 天平 / Quantos 加粉 / Cavro 注射泵加液）

一台梅特勒 MT-SICS 天平，可选两样加料装置，包成 ILCS 网关（`http_json_v1`）：

| 动作 | ILCS 能力（配置里对应） | 装置 | 做什么 |
|---|---|---|---|
| `weigh` 称量 | `cap.weigh` | 天平 | 读秤上容器的稳定净重 |
| `dose_solid` 固体称量加料 | `cap.ely.dose_solid` | 梅特勒 Quantos（XPE 平台 QX / Q2） | 按目标质量自动加粉，回报实际加粉量 |
| `dose_liquid` 液体称量加注 | `cap.ely.dose_liquid` | Cavro DT 协议注射泵 + 分配阀（Tecan Cavro XCalibur / XLP、润泽 SY-01B / SY-03B 的 ASCII 模式） | 按密度换算体积、先加九成，天平读数后补到目标 |

| 文件 | 内容 |
|---|---|
| `driver/link.py` | 一问一答的文本链路（TCP / 串口，经 pyserial）：一条链路一把锁、断线重连、区分「没发出去」与「发了没回」 |
| `driver/sics.py` | MT-SICS 天平（`I2`/`I3`/`I4`、`S`、`SI`、`T`、`Z`）与 Quantos 扩展（`QRD`/`QRA`：加样头、前门、目标、开始 / 停止加粉、结果 XML） |
| `driver/cavro.py` | Cavro DT 协议注射泵：阀转口、吸、推、查状态、终止 |
| `driver/device.py` | 把三样装置包成 `ilcs_gateway.Device`：物料核对、一次一瓶、后台执行、按实际量回报、结论落盘 |
| `driver/config.py` | 网关配置：天平链路、提供哪几项能力、加样头物质对照、阀端口与密度、验收用料 |
| `simulator/` | 假天平（MT-SICS + Quantos，`sics_server.py`）、假注射泵（Cavro DT，`cavro_server.py`）、共用的秤盘与储液（`world.py`）；真实接口照常经 TCP 连它们 |
| `gateway.py` | 入口：`--simulate` 在本进程里起模拟设备 |
| `profile.json` | ILCS 设备接入模板文件（草稿） |
| `tests/` | 协议客户端对着模拟设备（`test_sics.py`、`test_cavro.py`）、站的判断规则（`test_station.py`）、ILCS 接入验收清单三项能力各一遍与重启查回（`test_module.py`） |
| `deploy/` | 在 ILCS 那台机器上起模拟站的 Dockerfile 与 compose |

```bash
api/.venv/bin/pytest devices/gateway/balance-dosing/tests                         # 自测（不连库）
python devices/gateway/balance-dosing/gateway.py --simulate --insecure --port 8443 # 本机联调
docker compose -f devices/gateway/balance-dosing/deploy/compose.yml up -d --build  # 模拟站接进本机 ILCS
```

## 规则

- **加的料要对。** 投料步骤的指令带 `material`（ILCS 发给网关的物料名、单位、用量参数）。加粉前读 Quantos 加样头的 RFID，
  物质对不上（配置 `solid.substances` 把 ILCS 物料名对到加样头物质名）就拒绝；加液按 `liquid.materials` 找阀端口，
  没登记的料拒绝。设备都没动。没带物料的指令（ILCS 接入验收）只能用 `acceptance_material` 指定的那种料。
- **一次一瓶。** 秤上只有一个位置：指令的 `wells` 只能有一瓶，多瓶的指令明确拒绝（`unsupported`），由搬运按瓶分开下发。
  某瓶这种料是 0，不动设备，回报 0。
- **报实际量。** 加完一律回报天平称出来的 `mass` 与 `delivered.materials`（ILCS 据此入库存）。偏离目标不判失败：
  ILCS 按实际量入账、超过偏差阈值报警待复核。设备本身出错才判失败——加样头用完、出粉故障、连续两次加液秤上没变化
  （管路堵塞、气泡、储液用完）、泵报错——错误里写明这一瓶已经加了多少（没入账，要人补录）。
- **开始之前的错是没动。** 读不到天平、加样头不对、泵没初始化、Quantos 在「已收下」之前报错，都是明确拒绝。
  开始加粉的命令发出去了却没回，是结果未知，不重发。
- **泵没初始化不自己初始化。** 初始化时柱塞回 0 位，会把注射器里的残液从当前阀口推出去——可能推进秤上的样品瓶、
  又在去皮之前、天平算不进去。交现场把阀对着废液口初始化一次。
- **终止。** 加粉发 `QRA 61 4`、加液发泵的 `T`；称量没有可停的，终止了就不报读数。停止命令到之前已经做完的，
  料确实进瓶了：如实拒绝终止（`来不及终止`），原作业照报完成、按实际量入账。不支持保持。
- 加料在后台线程里做，`start` 立刻返回；结论写进状态目录（`<state>/runs/`），网关重启后照样按作业号答得上来。
  重启前还没出结论的，查询报结果未知，交人工核查。Quantos 加粉时不去问天平的身份（同一条链路上挂着「加完再回」的命令）。

## ILCS 里怎么建

- 工位：通道 1（秤上一个位置），能力 `cap.weigh`（无参数）加上 `cap.ely.dose_solid` / `cap.ely.dose_liquid`（参数 `mass`，g）。
  **验收用 `cap.weigh`**：不动料。工位没登记 `cap.weigh` 时，ILCS 会拿工位第一项能力、参数取极限中点去验收——
  加料站上就是真的加几十克验收用料。
- 设备方法：程序写 `DOSE-POWDER` / `DOSE-LIQUID` / `WEIGH`（或在网关的 `programs` 里登记自己的程序名，带各自的容差），
  输出项 `mass`（g）可以关联「实际加入量」这类指标。
- 投料步骤勾「消耗物料」、写物料与用量参数 `mass`：ILCS 把物料名带给网关核对、按回报的实际量入账。

## 还没做 / 要现场核对的

- [ ] **Quantos 命令要按梅特勒正式文档核对。** 梅特勒没有公开 Quantos 的 MT-SICS 命令手册，这里的 `QRD` / `QRA` 命令取自公开的
  开源驱动（heingroup/mtbalance，另有两个独立实现与它一致）。mtbalance 的 README 说这套命令未经梅特勒与 Hein 课题组许可
  不得公开传播：用于正式项目前向梅特勒要正式文档、确认使用许可。
- [ ] **XPR 平台的自动加样**（XPR226Q 一类）走 Web Service（SOAP），不认 `QRD` / `QRA`，这里没做。
- [ ] Quantos 的样品转盘（QS30）、加样头的自动换装：现在一次一瓶、加样头由配粉模组 / 人装好，网关只核对。
- [ ] 注射泵：确认是 ASCII（DT）模式——润泽出厂可能是自家二进制协议，要先切到 ASCII；按实物核对全行程步数
  （XCalibur 3000 / 24000，润泽 SY-03B 的手册里 6000 与 12000 两说）、地址、波特率；加一个废液口做初始化与排气泡。
- [ ] 天平：MT-SICS 网口端口（XPE 以太网选件缺省 8001，XPR 要在天平上看）、称量单位设成 g、关掉自动内校（远程控制时会报错）。
- [ ] 加液的首段比例、容差、最大补加轮数按实测调；挥发性溶剂（DMC、EMC）称量要考虑挥发与气流罩。
