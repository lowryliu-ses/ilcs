# 设备模块：电化学工作站（PalmSens MethodSCRIPT）

电池 / 电解液实验室的电化学工作站接进 ILCS：电解液电导率（电导池 EIS）、扣电界面阻抗（EIS）、电化学窗口（LSV）、
循环伏安（CV）、开路电位（OCP）、计时电流（CA）。网关跑在工作站 USB 插着的那台电脑上，ILCS 侧用 `http_json_v1` 接入，
能力 `cap.echem`（电化学测试）。品牌还没定，第一个后端是 **PalmSens MethodSCRIPT**（EmStat4 / EmStat Pico / Nexus /
Sensit）：通讯协议公开、纯文本，能在协议层模拟和测试；后端藏在一个调用面后面，别的品牌照着加（见「再接一个品牌」）。
去重、台账、查询、令牌、TLS 由 `ilcs_gateway` 负责。

| 文件 | 内容 |
|---|---|
| `driver/backend.py` | 工作站的调用面（`Potentiostat`）：身份、极限、加载（不开始）、开始、读数据流、终止、断线恢复；驱动只依赖它 |
| `driver/methodscript.py` | MethodSCRIPT 编解码（纯函数）：数据包、SI 前缀、脚本里的数、错误行、固件版本、各型号极限 |
| `driver/palmsens.py` | MethodSCRIPT 后端：按技术写脚本，`l` 加载、`r` 运行、读数据包、`Z` 终止、出错补发 `cell_off`、断线后同步 |
| `driver/link.py` | 文本链路（USB 虚拟串口 / 串口 / TCP，照 balance-dosing 改的）：读写分开加锁（读线程占着读端时照样能发终止） |
| `driver/techniques.py` | 技术（与品牌无关）：程序与参数核对、ILCS 指令参数覆盖、和仪器极限核对、数据 → 曲线与派生指标 |
| `driver/analysis.py` | 派生指标（纯函数）：体相电阻、电导率、起始电位、尾段平均、峰；曲线抽稀 |
| `driver/device.py` | 映射成 `ilcs_gateway.Device`：一个通道一个电池、后台读数、终止、断线、结论落盘、重启 |
| `driver/config.py` | 网关配置（`--config`）：仪器、链路、电池（面积、电导池常数）、极限、指令可改的参数、程序 |
| `simulator/fake_methodscript.py` | 假 MethodSCRIPT 仪器：TCP 上说同一套通讯协议，解析、核对、执行脚本，按电池模型出数据包；可单独起 |
| `simulator/cell_model.py` | 假仪器上的电池：电导池 EIS（R_b + CPE，可选半圆、引线电感）、扣电的 LSV / CV / CA、开路电位漂移 |
| `simulator/__init__.py` | `simulated_instrument()`：本进程里起假仪器，真实接口照常经 TCP 连它；故障注入在设备层（`FaultState`） |
| `simulator/potentiostat-sim.json` | 容器里跑模拟网关用的配置（设备编号 SIM-ECHEM-01） |
| `gateway.py` | 入口：`--simulate` 用假仪器；`--check` 只读地问一遍仪器 |
| `profile.json` | ILCS 设备接入模板文件（草稿），导入后核对、由另一个人发布 |
| `tests/` | `test_module.py` 对假仪器跑 ILCS 接入验收清单（含故障项目）、驱动规则、各技术的派生指标、ILCS 曲线校验、终止 / 出错 / 断线 / 重启、profile 摘要；`test_methodscript.py` 编解码（PalmSens 文档里的例子）与脚本；`test_wire.py` 后端走 TCP 对着假仪器 |
| `deploy/instrument-pc.md` | 在工作站那台电脑上装、注册成服务，停服务 / 崩溃时仪器怎样，第一次动作级验收 |
| `deploy/Dockerfile`、`deploy/compose.yml` | 在 ILCS 那台机器上起模拟网关（自带假仪器），接进 ILCS 的后端网络，给联调与模拟实验用 |

```bash
api/.venv/bin/pytest -q -p no:cacheprovider devices/gateway/potentiostat/tests        # 自测（不要 pyserial、不要 ILCS 数据库）
python devices/gateway/potentiostat/gateway.py --simulate --insecure --port 8443 --time-scale 0.05   # 本机联调（假仪器快 20 倍）
python devices/gateway/potentiostat/simulator/fake_methodscript.py --port 4100        # 起一台假仪器给真实接口连（link 写 tcp 127.0.0.1:4100）
python devices/gateway/potentiostat/gateway.py --config echem.json --check            # 现场接好线：只读地问一遍仪器
```

## MethodSCRIPT（这里用到的）

依据 MethodSCRIPT 手册 v1.8、EmStat4 通讯协议 v1.3、EmStat Pico 通讯协议 v1.6（见「参考」），代码是照文档自己写的。

- **连接**：EmStat4 是 USB CDC（虚拟串口，设置不起作用）；UART 921600 波特、8N1，建议 RTS/CTS。EmStat Pico 的 UART
  230400 波特、8N1、XON/XOFF（上电时可能发一个 XON，链路丢掉 XON / XOFF 字符）。命令、应答都是一行、`\n` 结尾，
  仪器先回显命令的第一个字符；出错回 `<首字母>!XXXX`，之后 50–100 ms 不收命令（网关等 150 ms）。
- **命令**：`t` 固件版本（两行：`tes4_hr1100#Jan 28 2022 11:04:43` / `R*`，前 6 个字符是设备类型）、`i` 序列号、
  `v` MethodSCRIPT 版本；`l` + 脚本 + 空行加载（边收边做语法检查，成功回 `l`，出错回 `l!XXXX: Line L, Col C`）、
  `r` 运行（先回 `r`，然后脚本输出，最后一个空行）、`e` 加载并运行；`Z` 终止（输出流里回 `Z`，测量循环照样收尾、
  `on_finished:` 照样执行；没有脚本在跑回 `Z!0006`）。
- **脚本**：一行一条命令；数写成「整数 + SI 前缀」（`100m`、`200k`，不能写小数点），整数加 `i`；变量先 `var` 声明。
  测量循环 `meas_loop_ocp / lsv / cv / ca / eis … endloop`，循环体里 `pck_start / pck_add / pck_end` 发数据包；
  CV 多圈用 `nscans(n)`；EIS 要高速模式 `set_pgstat_mode 3`，振幅是有效值（Vrms）；开路电位要先 `cell_off`。
- **输出**：`MXXXX` 测量循环开始（技术号 0000 LSV、0005 CV、0007 CA、000B OCP、000D EIS）、`*` 结束；`Cnnnn` / `-`
  一圈开始 / 结束；数据包 `Pda7F0BDF9u;ba7678CD7p,10,20F,40`——变量类型 2 个字母（`da` 设定电位、`ab` 实测电位、`ba` 电流、
  `eb` 时间、`dc` 频率、`cc` / `cd` 阻抗实部 / 虚部），值 = (7 位十六进制 − 0x8000000) × SI 前缀（空格 = 没有前缀，`i` 整数，
  `     nan` 无效），后面的元数据 `1X` 状态位（1 时序不满足、2 过载、4 欠载、8 过载预警）、`2XX` 量程号、`4X` 噪声等级。
  运行时出错是 `!XXXX: Line L`，**不执行 on_finished**。

网关每个脚本都是：声明变量 → 选通道、PGStat 模式、带宽、电流量程与自动量程上下限、测开路电位时的电位量程 →（从开路电位起扫
时先 `cell_off` 静置、`meas_loop_ocp` 测开路电位到变量 `oc`）→ `set_e` 起点、`cell_on` → 测量循环 → `on_finished:` `cell_off`。
`tests/test_methodscript.py` 里有每个技术写出来的脚本。

## 一条 ILCS 指令怎么落到仪器上

- **一条指令 = 在接着的电池上做一次测量。** 设备方法的「程序」选测量（`programs` 的键：技术 + 参数），没带方法的指令
  （包括 ILCS 的接入验收）用 `default_program`。
- **一个通道一个电池**：指令不带 `wells`，或逐孔参数 `wells` 里只有一个电池（孔位里的参数覆盖步骤顶层的）；多个回
  `NotSupported`「按电池分开下发」。上一次测量没结束时新指令报忙。
- **参数**：缺省一个都不认（测量参数都在程序里）。要让 ILCS 改，就在网关配置 `params` 里登记它和范围，而且只对用得上它的
  技术有效：OCP `duration_s`；LSV `e_end_V`、`scan_rate_V_s`、`area_cm2`；CV `scan_rate_V_s`、`cycles`、`area_cm2`；
  CA `e_V`、`duration_s`、`area_cm2`；EIS `e_dc_V`、`amplitude_Vrms`、`cell_constant_per_cm`。**带别的参数一律拒绝**，不悄悄忽略。
- **明确拒绝 vs 结果未知**：能力不对 `unsupported`；程序没登记、参数非法或超范围、超出网关 `limits` 或仪器自己的极限
  （按型号：电位范围、EmStat Pico 一次能扫的跨度、EIS 最高频率与最大振幅，手册附录 B）、配置写的型号和仪器自报的对不上
  （HR ±6 V 和 LR ±3 V 不能混）：`invalid`；仪器加载脚本时拒收按错误码判 `invalid` / `unsupported` / `busy`；读不到仪器：
  `busy`——这些仪器都没动。开始命令 `r` 写出去了却没拿到 `r` 的回答：结果未知（不重发），下次连仪器先发 `Z` 停掉它。
- **后台读数**：`start` 收到 `r` 就返回；数据包在后台线程里读，`status` 不碰仪器，只报已收到几个点（遥测
  `points`，`setpoint` 是预计点数）和最新一点的电位 / 电流 / 频率。测量进行中健康检查也不碰仪器，回上次读到的身份。
- **终止**：发 `Z`，等仪器结束（`on_finished:` 断开电池）；这次的数据不回报，判失败「被终止」。`Z` 到达之前已经测完的，
  如实拒绝终止「来不及终止」，原作业照报完成；`abort_timeout_sec`（缺省 10 s）内仪器没结束回结果未知。**不做保持**。
- **出错**：仪器运行时报错（如 `!0032` 电池严重过载）判失败，错误里写明错误码、含义和出错的脚本行；网关另发一个只有
  `cell_off` 的脚本断开电池，没确认就写明「请到现场核查」。链路断了、仪器太久没有输出：网关一直重连，连上先发 `Z` 停掉
  仪器上的测量，再判失败（数据收不全，可以重测）；重连之前这次作业一直报在测并写明在重连。
- **网关重启**：测完的结论写进状态目录（`<state-dir>/runs/`），重启后照样按指令号交得出结果。重启前还没测完的：仪器上的
  脚本可能还在跑，连上仪器、发 `Z` 停掉之后才判失败；连上之前不下结论（SDK 照报台账里的「在测」）。
- **过载**：自动量程到顶还装不下的点置过载位（读数削顶、不可靠）。照样判完成，回报 `overload_points` 与说明；ILCS 侧给它
  设上限 0 打标、交审核（同理 `timing_errors` 是时序没跟上的点数）。

## 技术与参数

程序里写（单位都在键名里；`current`：{"start_A": 起始量程, "autorange_A": [下限, 上限] 或 null 固定量程}，缺省 100 µA、
1 nA–10 mA；`bandwidth_Hz` 缺省是数据点频率的 4 倍、1–100 Hz；`potential_range_V` 测开路电位的量程，缺省仪器最大；
`cell` 覆盖网关级的电池参数）：

| 技术 | 参数 | 点数 |
|---|---|---|
| `ocp` 开路电位 | `duration_s`、`interval_s` | ⌊duration / interval⌋（手册的例子：2 s / 100 ms = 20 点） |
| `lsv` 线性扫描 | `e_begin_V`（数，或 `"ocp"`：先开路静置 `rest_s`（缺省 10 s，间隔 `rest_interval_s`），从静置最后的开路电位起扫）、`e_end_V`、`scan_rate_V_s`、`e_step_V`；可选 `onset_threshold_mA_cm2`（要有面积）/ `onset_threshold_mA`、`stop_mA_cm2` / `stop_mA`（电流绝对值到这就提前停：`if c > I` `breakloop`） | \|Δ E\| / step + 1 |
| `cv` 循环伏安 | `e_begin_V`、`e_vertex1_V`、`e_vertex2_V`、`e_step_V`、`scan_rate_V_s`、`cycles`（1–50，ILCS 一个结果最多 50 条曲线） | 每圈 起点 → 顶点 1 → 顶点 2 → 起点 |
| `ca` 计时电流 | `e_V`、`duration_s`、`interval_s` | ⌊duration / interval⌋ |
| `eis` 阻抗谱 | `freq_start_Hz`、`freq_end_Hz`、`points_per_decade`、`amplitude_Vrms`（有效值）、`e_dc_V`（数或 `"ocp"`，缺省 0；`"ocp"` 时同 LSV 先静置） | round(十倍程数 × points_per_decade) + 1，对数等分 |

## 回报与派生指标

单个电池时整行就是 `delivered`，带了一个孔位时放在 `delivered.wells[孔位]`（ILCS 按孔位写成样本的检测结果）。每行都有
`program`、`technique`、`points`、`overload_points`、`timing_errors`，有提醒时有 `note`。曲线点数超过 `max_points`（≤ 2 万）
就合并相邻的点取平均；数取 6 位有效数字（曲线没被合并时，起始电位、峰电位就是曲线上的那个点）。

| 技术 | 曲线 | 数值 |
|---|---|---|
| EIS | `nyquist` {x: Z′（Ω）, y: −Z″（Ω）}；`bode` 两条：`|Z|（Ω）`、`−相位（°）`，x 频率 Hz | `r_bulk_ohm`、`r_bulk_method`、`r_bulk_freq_Hz`；有电导池常数时 `conductivity_mS_cm`、`cell_constant_per_cm`；`freq_range_Hz`、`amplitude_Vrms`、`e_dc_V`（从开路电位时是实测的） |
| LSV | `lsv` {x: 电位 V, y: 电流密度 mA/cm²（有面积）或电流 mA} | `current_unit`、`e_begin_V`、`e_end_V`（实际最后一点）、`scan_rate_V_s`、`rest_ocp_V`；配了阈值时 `onset_potential_V`、`onset_threshold`；提前停时 `stopped_at_cutoff` |
| CV | `cv` {traces: 第 1 圈、第 2 圈 …} | `cycles`、`scan_rate_V_s`、最后一圈的 `jpa_mA_cm2` / `jpc_mA_cm2`（没有面积时 `ipa_mA` / `ipc_mA`）与 `epa_V` / `epc_V` |
| OCP | `ocp_curve` {x: 时间 s, y: 电位 V} | `ocp_V`、`duration_s` |
| CA | `ca_curve` {x: 时间 s, y: mA/cm² 或 mA} | `i_end_mA`（有面积另有 `j_end_mA_cm2`）、`e_V` |

派生指标的取法（`driver/analysis.py`）：

- **体相电阻 R_b**（EIS 高频端与实轴的交点）：从最高频往低频找 −Z″ 由 ≤ 0 变成 > 0 的那一对相邻点，两点之间对 −Z″ 线性插值到 0
  （`zero_crossing`，有引线电感时常见）；高频端没有过零（阻塞电极、没有感抗段时常见）就取最高频起 |−Z″| 一路变小、到第一个
  局部最小的那一点的 Z′（`min_imag_hf`，多半就是最高频点）。后者比真值略大，大多少看那一点 CPE 的实部（模拟的电导池
  100 kHz 时约 1%）——要更准就把 `freq_start_Hz` 提高到仪器的上限（EmStat4 200 kHz）。有电感时交点那个频率上 CPE 的实部
  也算进去（物理上就是实轴交点）。有界面半圆时取的是半圆左端（R_b），不是右端。
- **电导率**：σ（mS/cm）= 1000 × K（cm⁻¹）/ R_b（Ω），K 是电导池常数（`cell_constant_per_cm`，用 KCl 标准液标定）。
- **起始电位**：沿扫描方向第一个 |j| ≥ 阈值的数据点的电位，不插值；一直没到阈值是 `null`（说明里写扫到了哪）。
  阈值也会被别的电流触发（双电层充电、杂质或内标的氧化还原峰）：扫描速率越快这些越大，阈值要按扫描速率定。
- **开路电位** `ocp_V`：最后 10% 数据点的平均（至少 1 点）；`i_end_mA` 同样是最后 10% 的平均。
- **CV 峰**：最后一圈电流的最大、最小值及其电位，不扣基线（要严格的峰电流在 ILCS 的派生指标或解析里另算）。

## ILCS 里怎么建

- **工位**：一个通道（一次一个电池）。能力 `cap.echem`「电化学测试」；能力参数只登记网关 `params` 里放开的（如
  `scan_rate_V_s` V/s、`e_end_V` V），工位极限和 `params` 的范围一致；不放开就没有参数，测量全由设备方法的程序定。
- **接入**：导入 `profile.json` 成接入模板、另一个人发布，工位套用模板，连接参数填网关地址、证书、设备编号。
  模板的 `request_timeout_sec`（20 s）要比网关的 `abort_timeout_sec` 大。
- **设备方法**：一个程序一个方法，程序写 `programs` 的键，适用型号写工作站型号。输出项（单位和回报的一致，ILCS 不换算）：
  - 曲线：输出类型「曲线」（`kind: "series"`），关联曲线型指标（`value_type: series`）：`nyquist`（单位 Ω，规则 `x_label: Z′`、
    `x_unit: Ω`）、`lsv` / `cv`（单位 mA/cm² 或 mA，`x_label: 电位`、`x_unit: V`）、`ocp_curve`（V，`x_label: 时间`、`x_unit: s`）、
    `ca_curve`（mA/cm² 或 mA，时间 s）；曲线按样本写成「设备回报」检测结果、待复核；
  - 数值：`conductivity_mS_cm`（mS/cm，按电解液体系给上下限，越界打标）、`r_bulk_ohm`（Ω）、`onset_potential_V`（V，**不要设成必报**：
    到 6.0 V 都没到阈值时是 `null`，说明窗口 ≥ 终点电位）、`ocp_V`（V）、`i_end_mA`（mA）、`jpa_mA_cm2` 等；
  - 质量：`overload_points` 上限 `hi: 0`——过载的点越界打标、置可疑，交审核。
- **流程**：一个电池一步。电解液配液线的电导率、电化学窗口步骤引用对应的设备方法；扣电的步骤前面放「上机」人工步骤
  （接线、确认通道上接的是这颗电池）——网关不知道通道上接的是什么。

## 在 ILCS 那台机器上模拟联调

```bash
# 1. 起模拟网关（自带假仪器，容器 ilcs-potentiostat-sim，接 ILCS 的后端网络 ilcs_backend）
docker compose -f devices/gateway/potentiostat/deploy/compose.yml up -d --build
# 2. deploy/.env 的 ILCS_ADAPTER_ALLOWED_HOSTS 加上 potentiostat-sim，重建 api 与 executor（docker compose up -d）
# 3. 「工位与接入 → 接入模板」导入 profile.json、另一个人发布；工位套用模板，连接参数见 deploy/compose.yml 的注释
```

模拟配置的程序：`OCP-10`（缺省，接入验收用，不加电位）、`OCP-60`、`EIS-COND`（电导池常数 1.0 cm⁻¹，R_b 约 80 Ω →
约 12.4 mS/cm）、`LSV-ESW`（开路 → 6.0 V，1 mV/s，阈值 0.01 mA/cm²，1 mA/cm² 提前停；起始电位约 4.49 V）、`CV-3`、
`CA-4V2`；电极面积 2.01 cm²（16 mm 垫片）。`GATEWAY_TIME_SCALE` 是 1（和真机一样慢：LSV-ESW 要四五十分钟）。
模拟网关自报为模拟器：只读级验收就放行；它回报的值在 ILCS 里带「模拟」标记、仪器注明模拟设备，不进闭环训练数据。
正式环境（`ILCS_ENVIRONMENT=production`）拒绝接入模拟器。接真机见 [deploy/instrument-pc.md](deploy/instrument-pc.md)。

## 再接一个品牌

驱动只依赖 `driver/backend.py` 的 `Potentiostat`：`identity()`、`limits(technique)`、`prepare(plan)`（交给仪器、不开始；
拒收抛 `BackendRejected`，链路不通抛 `BackendError`）、`begin()`（写出去没确认抛 `StartUnknown`）、`stream(on_point)`（数据点用
统一的键：`e_V`、`i_A`、`t_s`、`f_Hz`、`z_re_ohm`、`z_im_ohm`，`segment` 是 ocp / lsv / cv / ca / eis，`scan` 圈号，`status`
过载 / 时序位）、`abort()`、`recover()`、`close()`，再加一个 `ready` 属性（仪器上没有网关不知道的测量在跑）。新后端写一个
实现这组方法的类，`driver/config.py` 的 `BACKENDS` 和 `gateway.py` 的 `build` 加一个 `kind`，`simulator/` 里在那家 SDK 的
调用面上做一个假的，`tests/test_module.py` 对它照样全过。技术、派生指标、ILCS 契约那一层不用动。可以参考的开源实现：

| 品牌 | 接口 | 开源参考（许可证） |
|---|---|---|
| Gamry | GamryCOM（Windows COM，随 Gamry Framework 装） | HELAO `helao-async` 的 `helao/deploy/hte/drivers/pstat/gamry/`、`helao/hexagon/adapters/native/gamry_com.py`（MIT） |
| BioLogic | EC-Lab 的 OLE/COM（EC-Lab ≥ 11.11，`eclab /regserver`）；或 EC-Lab Development Package 的 EClib DLL | `aurora-biologic`（MIT，Empa，走 OLE/COM）；`easy-biologic`（MIT / Apache-2.0 双许可，走 EClib） |
| Metrohm Autolab | Autolab SDK（.NET，随 NOVA 或单独装；专有），Python 经 pythonnet | `helgestein/metrohm_autolab_python`（MIT，示例）；`pyMetrohmAUTOLAB`（PyPI，没声明许可证） |
| PalmSens（不走 MethodSCRIPT 的老型号，如 PalmSens4、EmStat3） | PalmSens .NET SDK | PyPalmSens（`palmsens-sdk`，BSD-3 改版：附加「为 PalmSens 设备设计、授权、使用」条款；要 .NET 运行时，经 pythonnet） |

## 参考

| 来源 | 用来核对什么 | 许可 / 说明 |
|---|---|---|
| [MethodSCRIPT 手册 v1.8（2025-10-15）](https://www.palmsens.com/app/uploads/2025/10/MethodSCRIPT-v1_8.pdf) | 脚本格式、变量与 SI 前缀、数据包与元数据、测量循环输出、技术号、各测量命令、`on_finished`、`abort`、错误码（附录 A）、各型号极限（附录 B）、变量类型（附录 C） | PalmSens 公开文档；只引用事实，错误码说明是自己写的 |
| [EmStat4 通讯协议 v1.3（2024-03-25）](https://assets.palmsens.com/app/uploads/2024/03/EmStat4-communication-protocol-V1.3.pdf) | 串口设置、`t` / `i` / `v` / `l` / `r` / `e` / `Z` / `Y` / `h` / `H` 的格式、错误格式、例子里的数据包 | PalmSens 公开文档 |
| [EmStat Pico 通讯协议 v1.6（2025-09-30）](https://assets.palmsens.com/app/uploads/2025/10/Emstat-Pico-communication-protocol-V1.6.pdf) | Pico 的串口设置（230400、XON/XOFF）、固件版本格式 | PalmSens 公开文档 |
| [PalmSens/MethodSCRIPT_Examples](https://github.com/PalmSens/MethodSCRIPT_Examples) | Python 例子的通讯流程（同步时先发换行再发 `Z`、`Z!0006` 表示没有脚本在跑、EIS 的 −Z″ 约定）、测量循环例子脚本的写法 | 代码的 LICENSE.txt 是 BSD 式但附加「只能和 PalmSens 的部件一起用」——**只读了，没有拷**（假仪器不是 PalmSens 部件）；`MethodSCRIPTs/Measurement_Loops/*.mscr` 文件头写的是 MIT。这里的解析器、脚本都是照手册自己写的 |
| [pyserial](https://github.com/pyserial/pyserial) 3.5 | 串口 / USB 虚拟串口 / rfc2217 | BSD-3-Clause |
| [helao-async](https://github.com/High-Throughput-Experimentation/helao-async)、[aurora-biologic](https://github.com/EmpaEconversion/aurora-biologic)、[easy-biologic](https://github.com/bicarlsen/easy-biologic)、[metrohm_autolab_python](https://github.com/helgestein/metrohm_autolab_python)、[PyPalmSens](https://pypi.org/project/PyPalmSens/) | 别的品牌怎么接（上表），没有用到代码 | MIT；MIT；MIT / Apache-2.0；MIT；LicenseRef-Modified-BSD-3-Clause-PalmSens |

## 还没做 / 要现场核对的

- [ ] **没在真机上跑过**：协议全按文档写、对着自己写的假仪器测的。第一次接真机先用 `--check`，再在 dummy cell 上把每个程序跑一遍，
  对照 PSTrace 的 Connection viewer 看收发。下面几条文档没写清、要在真机上看：
  - 运行时出错之后还有没有一个空行（手册说没有、通讯协议的例子里有）：两种都按结束处理；
  - `l` 加载出错时，错误前面带不带 `l`、后面还会不会多一行：只认 `!XXXX`；
  - 同步时先发的那个空行仪器回什么（PalmSens 的例子也这样发）：读到 `Z…` 之前的行都丢掉；
  - 用开路静置的输出变量 `oc`（类型 `ab`）当 LSV 起点、EIS 直流电位行不行；`if c > I` + `breakloop` 在 `meas_loop_lsv` 里
    能不能提前结束、`*` 还发不发；
  - `set_range ab`（测开路电位的量程，HR 写 6 V）在 EmStat4 上的效果，EIS 要不要 `set_autoranging ab`；
  - 带宽取「数据点频率 × 4，1–100 Hz」是自己定的（PSTrace 的规则不知道）；
  - `Z` 多久生效：低频 EIS 一个点几十秒、长间隔的 OCP 时 10 s 等不等得到（等不到报结果未知）；
  - 各型号 EIS 的最低频率没核对（只查最高频率和最大振幅）。
- [ ] **接线与电位符号**：两电极扣电 RE 和 CE 并在锂对电极上、WE 接不锈钢 / 正极，电位是 WE 对 RE；接反了 LSV 往错的方向扫。
  网关不知道通道上接的是什么，流程里要有上机确认。
- [ ] **电导池常数**：要用 KCl 标准液标定后写进配置；温度没测、没做温度补偿（电导率随温度约 2%/℃），流程里要记温度。
- [ ] **保持**：协议有 `h` / `H`（暂停 / 继续脚本），但暂停时电池照样加着电位，契约先声明不支持。
- [ ] **网关不在时的数据**：仪器不缓存输出，网关崩了那次测量的数据就没了（判失败、可以重测）。有板载存储 / SD 卡的型号能用
  `file_open` 把数据同时写进仪器，以后要防这种情况可以边发边存、重启后取回。没做。
- [ ] **多通道**：MultiEmStat4 每个通道是一个独立的 USB 设备，一个通道起一个网关（各自的设备编号）；Nexus 的 BiPot、MUX 多路
  没做。串口自动识别没做（要在配置里写口）。
- [ ] **iR 补偿、电流中断法、GEIS（恒电流阻抗）、恒电流技术**没做；EIS 只回报 Z′ / Z″，没做等效电路拟合。
- [ ] `profile.json` 是草稿：核对连接参数示例后导入，由另一个人发布。
