# 设备模块：电芯开路电压 / 交流内阻检测仪表（SCPI 文本命令，映射模块）

电芯装配之后、上柜循环之前的检查：开路电压（OCV）与 1 kHz 交流内阻（ACIR）。现场仪表的品牌还没定，这里先给最常见的
几类台式仪表现成的**驱动宿主设备配置**（`ilcs-host-device-profile/1`），都走驱动宿主（[devices/host](../../host/README.md)）的
串口 / TCP 文本命令插件 `line_command`，ILCS 工位用 `sila2_v1` 接驱动宿主：接真仪表只填连接参数（地址、端口或串口），
不写代码、不起网关、ILCS 不重启。

| 配置文件 | 仪表 | 接口 | 测什么 | 输出 |
|---|---|---|---|---|
| `profile-keithley-2450.json` | Keithley 2450 SourceMeter（SCPI 命令集） | LAN 原始套接字 TCP 5025，结束符 LF | 源 0 A、四线测电压 → 开路电压 | `ocv_V`（V） |
| `profile-keithley-2400.json` | Keithley 2400 SourceMeter（2400 系列的老 SCPI；2450 设成 SCPI2400 命令集也能用） | RS-232（出厂 9600 8N1、结束符 CR），经串口服务器或本机串口 | 同上 | `ocv_V`（V） |
| `profile-hioki-bt3562.json` | Hioki BT3561A / BT3562A / BT3563A（LAN）、BT3562 / BT3563（RS-232C） | LAN TCP（缺省命令端口 23）或 RS-232C，结束符 CR+LF | ΩV 模式：1 kHz 交流四端法内阻 + 直流电压 | `ir_mohm`（mΩ）、`ocv_V`（V） |

没有做的：Hioki BT3554 系列（USB 是虚拟串口，命令手册在随机光盘上，公开资料里查不到）；同惠 TH2523 / TH2522（公开的
TH2523/A 操作手册列了命令，但没有 `FETCH?` 的回复格式、`*IDN?` 与错误查询，写不出能核对的回复正则）；GPIB 接口
（`line_command` 插件只有 TCP 与串口通道）。这几种以后拿到命令手册，照下面的写法再加一份配置即可。

## 怎么接：驱动宿主的设备文件 + ILCS 的 sila2_v1 工位

驱动宿主现场目录里一台仪表一个设备文件（`sites/<现场>/devices/<设备>.json`）：profile 的 `config` 原样放进去、加上
连接 `transport`（profile 的 `connection` 是示例），`supports` 照抄 profile，`port` 是这台设备的 SiLA 端口：

```json
{"plugin": "line_command", "port": 50211, "config_version": "r1",
 "supports": {"hold": false, "abort": false, "query": true, "dedup": true},
 "config": {"transport": {"kind": "tcp", "host": "10.20.1.51", "port": 5025}, "...": "profile config 的其余各项"}}
```

ILCS 工位的设备连接（`sila2_v1`）：`{"host": "driver-host", "port": 50211, "ca_file": …, "expected_device_id": "<仪器序列号>"}`，
接入验收的缺省照 profile 的 `acceptance` 写进连接配置（`{"capability": "cap.cell_check", "params": {}}`）。接真仪表时不要
`simulator_control`。仪表主机要在驱动宿主 `host.json` 的 `allowed_hosts` 里。本机的三台模拟仪表就是这样接的
（`devices/host/sites/local/devices/` 的 OCV-K2450、OCV-K2400、ACIR-BT3562，`scripts/load-driver-host-devices.py register --acceptance`）。

## 文件

| 文件 | 内容 |
|---|---|
| `profile-*.json` | 三份驱动宿主设备配置（映射 + 示例连接 + 支持标志 + 验收缺省），照上面放进驱动宿主的设备文件 |
| `simulator/scpi.py` | 假仪表的公共部分：命令头按手册写法匹配（长短写法都认）、一行多条命令、错误怎么记、待测电芯、故障与测量计数 |
| `simulator/keithley.py` | 假 Keithley 2450 / 2400：本配置用到的源、测量、输出、错误队列、读数缓冲区命令 |
| `simulator/hioki.py` | 假 Hioki BT3562 系列：ΩV 测量、按量程定宽的读数格式、标准事件寄存器 / ESR0 / 状态字节 |
| `simulator/server.py` | 假仪表的 TCP 口（CR、LF、CR+LF 都认）与统一控制口；可以直接运行 |
| `deploy/Dockerfile`、`deploy/compose.yml` | 在 ILCS 那台机器上起三台模拟仪表（k2450-sim、k2400-sim、bt3562-sim，各带统一控制口），接进 ILCS 的后端网络；驱动宿主按 `devices/host/sites/local` 接它们，`scripts/load-device-simulators.py register` 登记对应工位 |
| `tests/` | 配置检查（建得出插件、命令列表的规矩、读数正则对手册格式）、插件直接测（完成、重投、回复丢失、忙、联锁、溢出……）、经驱动宿主跑 ILCS 接入验收清单三台各一遍、假仪表自己的行为 |

```bash
api/.venv/bin/pytest -q -p no:cacheprovider devices/gateway/scpi-cell-meter/tests     # 自测（不连库）
api/.venv/bin/python devices/gateway/scpi-cell-meter/simulator/server.py --model keithley-2450 --port 5025
```

## 一次测量怎么走（三份配置共同的规矩）

- **读数就是结果，当场完成。** 能力 `cap.cell_check` 的启动命令依次是：清状态 → 配置测量 → 查错 →（Keithley：开输出、
  再查错、确认输出真开了）→ `:READ?`。`:READ?` 是唯一的动作命令（`motion: true`），它的回复按 `result.pattern` 取数
  （科学计数法照转），作业在提交时就结束、回执里带读数。这是驱动的「即时动作」写法（读码器就是这么接的），
  不用「先触发、再轮询、再取数」：一问一答，没有触发和取数之间的空档。
- **设置命令不回复。** SCPI 的设置命令一律 `reply: false`；驱动看不到它们有没有被接受，所以动作命令之前一定查一次错：
  Keithley 查错误队列 `:SYST:ERR?`（期望 `0,"No error…"`），Hioki 查标准事件寄存器 `*ESR?`（期望 0）。配置被拒、
  仪表忙，都在这里变成**明确失败、仪表没测**。开头先 `*CLS`，旧错误、Hioki 开机的 PON 位不会被当成这次的错。
- **溢出与测量异常是明确失败。** `:READ?` 回 9.9E+37（Keithley 超量程）或 Hioki 的 ±OF / 测量异常值（指数 E+5 以上）时，
  动作命令的 `reject` 正则命中，驱动判明确失败：这一笔没有有效读数，可以按恢复规则重测（夹好电芯再来）。
- **状态与复位。** `line_command` 插件必须有状态查询；这些仪表没有「运行中」，状态查询回答的是「仪表里有没有一笔还没
  清掉的读数」：Keithley 查读数缓冲区里有几个读数（2450 `:TRAC:ACT? "defbuffer1"`、2400 `:TRAC:POIN:ACT?`），
  0 是空闲、非 0 是测完了；Hioki 查状态字节 `*STB?` 的 bit0（启动时 `:ESE0 1` 把测量结束位 EOM 汇总到这一位），
  奇数是测完了。每次开测前驱动先读状态，「测完了」就发复位命令（`acknowledge`：清缓冲区 / `*CLS`）再测——
  所以仪表里只会有这一次的读数。
- **回复丢了。** `:READ?` 发出去没回：驱动判**结果未知**、不重发。之后按指令号查询时读状态：仪表里有新读数，就判完成、
  用实测查询（Keithley `:FETC?` / `:TRAC:DATA?`、Hioki `:FETC?`）取回读数，质量标 `uncertain`；没有新读数（不知道
  命令到没到），就一直是结果未知，转人工核查。执行器重启后照样按作业台账查回。
- **状态查询必须不清零。** 读了就清的寄存器（Hioki 的 `:ESR0?`、`*ESR?`）不能拿来当状态：驱动每个实例各有一份
  作业台账的内存副本，两个实例（执行器、接入验收）先后读状态，后读的看不到「测完了」，还会把旧结论写回台账。
  缓冲区读数个数与状态字节读了都不变，所以选它们。
- **`idle_after_start` 是 `unknown`。** 即时动作的作业在提交时就结束了；只有回复丢失（未确认）的作业还会再查状态，
  而未确认的作业驱动从不按「回到空闲」判完成。这里显式写成 `unknown`：空闲只说明仪表里没有新读数，不能当成做完。
- **支持标志：** 保持 `false`、终止 `false`（测量一问一答就完，没有在途作业可保持、可停；ILCS 对这两类指令
  直接拒绝、按现场规程处理）；查询 `true`、去重 `true`（作业台账按指令号回放，同一指令号重投不会再测）。

| 情况 | 驱动的结论 |
|---|---|
| 读数正常 | 完成：回执 `delivered` 与遥测带读数（检测没有设定值） |
| 配置被拒（错误队列 / `*ESR?` 非零）、仪表忙、命令集不对 | 明确失败，仪表没测 |
| Keithley 输出打不开（联锁没接通） | 明确失败，仪表没测 |
| 读数溢出 / 测量异常（探针没压上） | 明确失败：这一笔没有有效读数 |
| `:READ?` 没回复 | 结果未知；之后仪表里有新读数 → 完成（`uncertain`），没有 → 一直是结果未知、转人工 |
| 连不上仪表 | 失联（健康检查失败），动作指令不投递 |

## 各台仪表的写法

### Keithley 2450（`profile-keithley-2450.json`）

| 命令 | 为什么 |
|---|---|
| `*CLS` | 清事件寄存器与事件日志 |
| `:OUTP:CURR:SMOD HIMP`、`:OUTP OFF` | 电流源的输出关断状态设成高阻（输出继电器断开）。缺省的 NORMal 关断状态是 0 V 源，接着电池会被拉电流——2450 手册的电池注意事项专门写了这一条 |
| `:ROUT:TERM FRON` | 前面板端子。**夹具接后面板三同轴就改成 `REAR`**，不然量的是空着的前面板端子 |
| `:SOUR:FUNC CURR`、`:SOUR:CURR:RANG:AUTO ON`、`:SOUR:CURR 0` | 源 0 A：电压表方式测开路电压，不充不放 |
| `:SOUR:VOLT:PROT PROT20`、`:SOUR:CURR:VLIM 10` | 过压保护 20 V、电压限值 10 V，都高于单颗电芯电压。**限值要高于被测电压**，否则仪表把电压钳在限值上、从电芯拉出大电流（2450 手册的注意事项）；测模组要改量程、限值与保护 |
| `:SENS:FUNC "VOLT"`、`:SENS:VOLT:RANG 20`、`:SENS:VOLT:NPLC 1`、`:SENS:COUN 1` | 测电压、20 V 固定量程（不等自动换挡）、1 个工频周期积分、一次一个读数 |
| `:SENS:VOLT:RSEN ON` | 四线（开尔文夹具）。0 A 时引线上没有压降，两线夹具改成 `OFF` 也一样准 |
| `:SYST:ERR?` | 配置有没有被接受 |
| `:OUTP ON`（等 50 ms）、`:SYST:ERR?`、`:OUTP?` | 开输出（HIMP 时输出继电器吸合，等它稳住）；开不了（联锁）就在这里明确失败 |
| `:READ? "defbuffer1"` | 动作命令：测一次、存进 defbuffer1、回读数 |
| `:OUTP OFF` | 测完断开 |

就绪查询 `*LANG?` 必须回 `SCPI`：2450 出厂是 SCPI 命令集，被人改成 TSP / SCPI2400 后这份配置的命令全报错，健康检查
报「不接受指令」，ILCS 不投递动作指令（在前面板 MENU → System → Settings → Command Set 改回 SCPI，仪表重启）。
设成 SCPI2400 的 2450 可以改用 2400 的配置（通道 TCP 5025、结束符 LF）。

### Keithley 2400（`profile-keithley-2400.json`）

和 2450 一样源 0 A、四线测电压，命令换成 2400 的写法：`:OUTP:SMOD HIMP`、`:SOUR:CLE:AUTO OFF`（不用自动关断，输出由配置
自己开关）、`:SOUR:CURR:MODE FIX`、`:SOUR:CURR:LEV 0`、`:SENS:FUNC:CONC OFF` + `:SENS:FUNC "VOLT"`（只测电压）、
`:SENS:VOLT:PROT 10`（电压限值，即 compliance）、`:SENS:VOLT:RANG 20`、`:SYST:RSEN ON`、`:FORM:ELEM VOLT`（读数串只有电压）、
`:TRIG:COUN 1`、`:ARM:COUN 1`。2400 输出关着（又没开自动关断）时 `:READ?` 报 +803、不测，所以先开输出。

「测完了」看数据缓冲区：每次开测前 `:TRAC:FEED:CONT NEV` → `:TRAC:CLE` → `:TRAC:FEED SENS` → `:TRAC:POIN 1` →
`:TRAC:FEED:CONT NEXT`，`:READ?` 的读数随之存进去（存满 1 个就停）。存储进行中改 `:TRACe:FEED` 会报 +800（Illegal with storage active），所以先停再改、
最后再开。RS-232 没有流控（出厂 NONE），配置在每条命令之间隔 20 ms（`inter_command_delay_ms`），不让仪表的输入缓冲溢出。

### Hioki BT3562 系列（`profile-hioki-bt3562.json`）

| 命令 | 为什么 |
|---|---|
| `*CLS`、`:ESE0 1` | 清事件寄存器；把 ESR0 的测量结束位（EOM）汇总到状态字节 bit0（状态查询看它；`*CLS` 不动使能寄存器，但开机清零，所以每次都设） |
| `:SYST:HEAD OFF` | 回复不带命令头（开机缺省就是 OFF，防有人改过） |
| `:FUNC RV` | ΩV 模式：一次测内阻与电压 |
| `:AUT OFF`、`:RES:RANG 300E-3`、`:VOLT:RANG 6` | 固定在 300 mΩ / 6 V 档。**mΩ 档（3 / 30 / 300 mΩ）的电阻回复永远是 `xxx.xxE-3`，正则只取尾数就是 mΩ**——驱动的实测值没有换算系数，靠这个拿到 `ir_mohm`。大电芯内阻小于 30 mΩ 可以换 30E-3 / 3E-3 档（照样是 E-3）；换到 3 Ω 以上的档要改正则与输出名 |
| `:SAMP:RATE MED` | 中速（EXFast / FAST / MEDium / SLOW 按节拍与噪声取舍） |
| `:TRIG:SOUR IMM`、`:INIT:CONT OFF` | 主机触发：连续测量关掉后 `:READ?` 触发一次测量（说明书「Importing by Host Triggering」）。按面板 LOCAL 会回到连续测量，所以每次都设 |
| `*ESR?`（之后等 100 ms） | 有没有命令错 / 执行错；说明书要求改完测量条件等 100 ms 再触发 |
| `:READ?` | 动作命令，回 `电阻,电压`（正数的符号位与前导 0 都是空格，驱动去掉两头空白，正则容许逗号后的空格） |

`*IDN?` 回 `HIOKI,<型号>,0,<软件版本>`：序列号位固定是 0，`expected_device_id` 防不了接错，按 IP 地址 / 串口口位区分，
现场贴标签。`:SYSTem:ERRor` 在这台仪器上设的是 EXT I/O 的 ERR 输出时机，不是错误队列——别拿它查错。
mΩ 档测电压时仪表的输入阻抗约 90 kΩ（说明书规格），对锂电芯开路电压的影响可以忽略。

## 接真仪表

1. **仪表设置**
   - 2450：前面板设好 LAN 地址；命令集 SCPI（见上）。原始套接字端口 5025（手册：23 Telnet、1024 VXI-11、5025 原始套接字、
     5030 断开死连接）。同一时刻只能有一个连接控制仪表：别让别的软件（KickStart 之类）同时连着。
   - 2400：MENU → COMMUNICATION → RS-232：9600、8 位、无校验、结束符 CR、流控 NONE（出厂就是这些）。用直通线
     （不是交叉线）接串口服务器或电脑。改了波特率、结束符，配置与连接参数跟着改（`write_terminator` / `read_terminator`）。
   - Hioki BT356xA：仪器上选 LAN 接口，用浏览器打开仪器地址设 IP（出厂 192.168.1.1 / 255.255.0.0，命令端口 23）。
     BT3562 / BT3563：仪器上选 RS-232C 与波特率（9600 / 19200 / 38400），8N1，CR+LF，用交叉线。
2. **连接参数**（驱动宿主设备文件的 `config` 里填，profile 的 `connection` 是示例）：
   - TCP：`{"transport": {"kind": "tcp", "host": "10.20.1.51", "port": 5025}}`（Hioki 端口 23）；
   - 串口服务器：`{"transport": {"kind": "serial", "port": "rfc2217://moxa-01.lab.internal:4001", "baudrate": 9600}}`，
     串口服务器在 TCP 服务器（原始）模式时写 `socket://主机:端口`；本机串口 `/dev/ttyUSB0`、`/dev/serial/by-id/…`、`COM3`；
   - Hioki RS-232C：同上，另加 `"baudrate": 9600`；
   - 主机要在驱动宿主 `host.json` 的 `allowed_hosts` 里（设备网段写成网段即可）。
3. **放进驱动宿主、ILCS 接工位**：设备文件放进驱动宿主的现场目录、重启驱动宿主（`--check` 先核一遍配置）；ILCS 工位
   「设备连接」选 `sila2_v1`，填驱动宿主的地址、这台设备的 SiLA 端口、证书、令牌，`expected_device_id` 填 Keithley `*IDN?`
   第三段的序列号（防接错仪表；Hioki 不填），签名保存；保存后自动跑只读级接入验收，驱动宿主报的驱动配置随验收批准。
   第一次接真仪表还要动作级验收（签名 + 现场批准人）：**夹具上先放一颗参考电芯**（或标准电阻 + 稳定电压源的假电池），
   验收会真的测一次。
4. **可选：夹具盖联锁。** 现场把夹具盖开关接到 Keithley 后面板联锁、仪表上 Interlock 设成 On 时，盖子开着打不开输出，
   配置在开输出之后的查错里就判明确失败。想在启动前（健康检查）就看到联锁，给配置加一段（两台的 `TRIPped?` 都是 1 = 联锁接通）：

   ```json
   "interlock": {"send": ":OUTP:INT:TRIP?", "pattern": "^(?P<value>[01])$", "ok": ["1"]}
   ```

   没接联锁的仪表不要加：联锁信号一直不通，仪表会被一直判成联锁。
5. `simulator_control` 只给模拟仪表用，接真仪表时不要填。

## ILCS 里怎么建

- **能力**（模块不登记，按下面建一次）：

  ```json
  {"id": "cap.cell_check", "name": "电芯电压内阻检测", "params": {},
   "recovery": {"maxHoldMin": 0, "pausable": false, "retryable": true,
                "hold": "检测是一问一答的即时动作，没有可保持的",
                "sideEffect": "无：Keithley 源 0 A 只测电压；Hioki 加 1 kHz 交流小电流测内阻",
                "verify": ["夹具上的电芯", "探针接触"]}}
  ```

- **指标**：例如 `cell_ocv`「电芯开路电压」单位 V、`cell_acir`「电芯交流内阻（1 kHz）」单位 mΩ。ILCS 不换算单位：
  设备方法输出项的单位要与指标的标准单位一致，所以配置直接报 V 与 mΩ。
- **设备方法**（适用型号写工位资产的型号）：Keithley 的「开路电压」程序 `OCV`，输出项
  `{"key": "ocv_V", "label": "开路电压", "unit": "V", "lo": 2.5, "hi": 4.4, "required": true}`；Hioki 的「交流内阻 + 开路电压」
  程序 `ACIR-OCV`，再加 `{"key": "ir_mohm", "label": "交流内阻", "unit": "mΩ", "lo": 0, "hi": 100, "required": true}`。
  上下限按电芯规格写：**越界照常入库、打标、报警**，空夹具、没压好的读数靠它挡住。输出项关联指标（`metric_id`）之后，
  读数按样本写成「设备回报」检测结果，进数据审核。程序名只是目录登记，配置的命令里没有用到 `{program}`。
- **工位**：1 个通道，能力极限 `{"cap.cell_check": {}}`；资产型号写仪表自报的型号（2450、2400、BT3562A……），
  否则「读取设备方法目录」时报型号不一致。一台仪表一个工位。
- **流程**：**一条检测指令测一颗电芯。** 检测步骤所在的批次只放一颗电芯（或按电芯拆批）：映射插件回的是批次级读数，
  一个批次有几颗电芯时 ILCS 会把同一个读数记到每颗上（并打「批次级读数」标记）——不要这样用。

## 映射插件做不到的、要知道的

- **一次一颗。** 检测步骤没有逐孔参数（矩阵条件才有），`line_command` 插件一条指令只测一颗、回一个读数。多工位夹具 + 外接扫描开关
  （多路切换器）要写一个网关模块（`ilcs_gateway`），在网关里逐个通道切换、测量、按孔位回报。
- **实测值不能换算单位。** 驱动的实测值没有系数；Hioki 的 mΩ 靠固定 mΩ 档 + 正则取尾数，Keithley 直接是 V。
- **Keithley 没有接触检查。** 夹具空着、探针没压上时 0 A 源下的电压读数不可信（可能飘到电压限值附近或接近 0 V），
  驱动照报完成；靠设备方法输出项的上下限打标。Hioki 有测量异常检测，判明确失败。
- **动作命令判失败或回复丢了，后面的命令不再发。** Keithley 的「测完关输出」在这两种情况下没发：输出开着（源 0 A、
  电压限值以内，不充不放），下一次测量开测时（复位命令）先关掉；要马上关就按面板 OUTPUT 键。反过来，读数回来了、
  「关输出」却没发出去（连接断了），驱动按「启动之后的命令失败」判结果未知，之后照样按缓冲区里的读数找回（`uncertain`）。
- **不能保持、终止。** 结果未知的作业由人核查；终止指令 ILCS 直接拒绝。
- **双重故障。** 回复丢了、恰好那一笔又是测量异常（Hioki）：状态说测完了，取回的读数是异常值、对不上正则，
  查询一直报读不懂、作业停在结果未知，转人工。
- **2400 真忙的样子不同。** 2400 在跑扫描（面板或别的控制器）时远程命令排队、查询不回：驱动读状态超时，报失联 / 结果未知，
  不是明确失败。模拟仪表把「忙」做成改设置被拒（错误队列报 -221），只代表另一种忙法。
- **经串口服务器慢一点。** pyserial 的 `socket://` / `rfc2217://` 每次断开等 0.3 s，加上 20 ms 的命令间隔，2400 一次测量
  约 1–2 s。串口服务器那一口只接这台仪表、只有 ILCS 连它时可以改 `keep_open: true`（但接入验收会同时开几个驱动实例，
  串口服务器要允许多个连接）。
- **继电器寿命。** HIMP 关断状态下每测一次输出继电器吸合、断开各一次；两台 Keithley 的手册都提醒频繁开关不要用 HIMP。
  节拍很快的产线可以权衡改成测量期间一直开着输出（源 0 A），改配置前与现场一起评估。

## 模拟仪表

`simulator/server.py` 起一台假仪表，走真实的文本命令协议，驱动宿主照常用 `line_command` 插件连（ILCS 经 sila2_v1 接驱动宿主）：

```bash
api/.venv/bin/python devices/gateway/scpi-cell-meter/simulator/server.py --model keithley-2450 --port 5025
api/.venv/bin/python devices/gateway/scpi-cell-meter/simulator/server.py --model keithley-2400 --port 4001   # ILCS 用 socket://主机:4001 当串口连
SIM_CONTROL_PORT=9900 api/.venv/bin/python devices/gateway/scpi-cell-meter/simulator/server.py --model hioki-bt3562 --port 2323 --ir 15 --ocv 3.85
```

- 身份里带 `ILCS-SIMULATOR`（序列号位），正式环境拒绝接入；电芯缺省 3.85 V、15 mΩ（`--ocv`、`--ir`）。
- 设了 `SIM_CONTROL_PORT` 就开统一控制口（`devices/simulators/common/control.py`，令牌文件 `SIM_CONTROL_TOKEN_FILE`）：
  `POST /simulator/fault {"mode": …, "parameter": N}`、`GET /simulator/state`（总测量次数 `motions`，仪表不认 ILCS 指令号）。
  ILCS 侧在连接参数里加 `"simulator_control": {"url": "http://<主机>:9900", "token_ref": "file://…"}`，接入验收的故障项目就能跑。

| 故障 | 模拟仪表的样子 |
|---|---|
| `lost_receipt` | `:READ?` 照测、读数存进缓冲区，回复不发、连接断开 |
| `stuck` | `:READ?` 收到了，不测也不回 |
| `slow_submit` | 读数迟到 N 秒 |
| `fail` | 这一笔溢出（Keithley 9.9E+37）/ 测量异常（Hioki +1E+10） |
| `busy` | 改设置的命令被拒：2450 记 -200、2400 记 -221、Hioki 置 EXE 位 |
| `interlock` | Keithley 打开输出被拒（2450 记 -200、2400 记 +802 OUTPUT blocked by interlock），`:OUTP:INT:TRIP?` 回 0；Hioki 注入不了（验收判跳过） |
| `offline` | 停听 N 秒，之后在同一个端口恢复 |

手册写清楚的行为照手册（各文件头列了出处）；手册没写、按模拟需要定下的（忙、联锁时报哪条错、2450 输出关着时读 0 V、
空夹具读数顶到电压限值）只代表模拟，不当真表的行为。另外：不认 SCPI 的「当前路径」（一行多条时每条都按绝对路径解析），
不模拟 Hioki 的比较器、统计、存储与零位调整。

## 参考

| 资料 | 用来核对什么 | 许可 |
|---|---|---|
| Keithley 2450 Reference Manual 2450-901-01 Rev. E（2019）：<https://download.tek.com/manual/2450-901-01E_Sept_2019_Ref.pdf>（读的是同版本的镜像 <https://res.cloudinary.com/iwh/image/upload/q_auto,g_center/assets/1/7/model_2450_sourcemeter_instrument_reference_manual.pdf>） | `:READ?` / `:FETCh?` / `:TRACe:ACTual?` / `:TRACe:CLEar`、`:SYSTem:ERRor?` 的回复、`*CLS`、`*IDN?`、`*LANG`、输出关断状态、联锁、`RSENse`、`VLIMit`、过压保护、超量程 9.9e+37、LAN 端口、电池注意事项、远程时连续测量停止 | Keithley / Tektronix 版权，公开下载；只读，没有抄录 |
| Keithley 2450 Quick Start Guide 2450-903-01 Rev. E：<https://download.tek.com/manual/2450-903-01E_QSG_Aug2019_web.pdf> | 命令集（SCPI / TSP / SCPI 2400）怎么切、联锁行为 | 同上 |
| Keithley 技术简报 No. 3234「Using the Model 2450 to Measure Resistance Using SCPI Commands」：<https://download.tek.com/document/2450%20SCPI%20Commands%20Resistance.pdf> | SCPI 写法（`SOUR:CURR:VLIM`、`SENS:FUNC`、`OUTP ON` …） | 同上 |
| Keithley 应用文章「Rechargeable Battery Charge and Discharge (Galvanic) Cycling Using the Keithley Model 2450 or 2460」：<https://www.tek.com/en/documents/application-note/rechargeable-battery-charge-and-discharge-galvanic-cycling-using-keithley> | 接电池时输出关断状态用高阻、四线接法 | 同上 |
| Keithley 应用文章「Keithley Instrumentation for Electrochemical Test Methods and Applications」：<https://www.tek.com/en/documents/application-note/keithley-instrumentation-electrochemical-test-methods-and-applications> | 开路电位：源 0 A、测电压、四线 | 同上 |
| Keithley Series 2400 SourceMeter User's Manual 2400S-900-01 Rev. G（官方页 <https://www.tek.com/en/keithley-source-measure-units/keithley-smu-2400-series-sourcemeter-manual/series-2400-sourcemeter>；读的是 <https://research.physics.illinois.edu/bezryadin/labprotocol/Keithley2400Manual.pdf>） | 自动关断、输出关着不能测（+803）、联锁（+802、`TRIPped?`）、输出关断状态、compliance、数据缓冲区与数据流、`*OPC?`、错误队列、`*IDN?` 格式、`:FORMat:ELEMents`、溢出 +9.9E37、RS-232 出厂设置与直通线 | 同上 |
| Hioki BT3561A/BT3562A/BT3563A/BT3562/BT3563 Instruction Manual BT3562A981-12（2024-06）：<https://shop.hioki.eu/media/c4/b8/fa/1736505866/BT3562A981-12_Manual_EN.pdf> | 第 8 章全部（RS-232C / LAN 设置、端口 23、结束符、`*IDN?`、事件寄存器、`*STB?`、`:READ?` / `:FETCh?`、读数格式与 ±OF / 测量异常值、主机触发）、测量异常的成因、出厂设置、输入阻抗 | HIOKI E.E. 版权，公开下载；只读 |
| Hioki BT3554-50/51 Instruction Manual BT3554F961-04：<https://shop.hioki.eu/media/8a/b3/0b/1736507721/BT3554F961-04_Manual_EN.pdf> | 确认 USB 是虚拟串口、命令说明在随机光盘上（所以没做） | 同上 |
| 同惠 TH2523/A Operation Manual Ver1.2：<https://nippon-sokki.vn/assets/tenant/uploads/media-uploader/sokki/pdf_full/tonghuith2523-may-do-dien-tro-thap-ac-tonghui-th2523-3ko-69168_2.pdf> | 确认命令有、回复格式没写（所以没做） | 常州同惠版权；只读 |
| PyMeasure（`pymeasure/instruments/keithley/keithley2450.py`、`keithley2400.py`）：<https://github.com/pymeasure/pymeasure> | 命令拼写与参数（`:OUTP:CURR:SMOD`、`:SOUR:CURR:VLIM`、`:SOUR:CLE:AUTO`、`:SYST:RSEN`、关断状态的四种取值） | MIT |
| QCoDeS（`src/qcodes/instrument_drivers/Keithley/Keithley_2450.py`、`Keithley_2400.py`）：<https://github.com/microsoft/Qcodes> | `:TRACe:ACTual?` / `:TRACe:CLEar` / `:FETCh?` 带缓冲区名的写法、`*LANG?` 判命令集、2450 测量要先开输出、2400 输出关着读电压会报错 | MIT |
| SweepMe! instrument-drivers（`src/SMU-Keithley_2450`）：<https://github.com/SweepMe/instrument-drivers> | 对照看过（它也先查 `*LANG?`），配置没有用它的写法 | MIT |

只读了上面这些资料、没有拷代码；labdrivers 没有查。

## 没核实 / 要现场核对的

- [ ] **2450**：联锁挡住输出、仪表忙（面板或别的控制器在跑触发模型）时错误队列里具体报哪条（模拟仪表报 -200）；
  `:SOUR:VOLT:PROT PROT20` 在电流源下是否原样生效、`VLIM 10` 不报警告；空夹具时的读数；`*LANG?` 在 TSP 命令集下照样回答；
  HIMP 吸合后 50 ms 是否够稳（读几次比较）。
- [ ] **2400**：`:READ?` 的读数确实存进 `:TRACe` 缓冲区、`:TRAC:POIN:ACT?` 测完回 1（手册的数据流图是这么画的）；
  `:TRAC:POIN:ACT?` 与正数错误码（`+802` 还是 `802`）的回复写法（正则两种都认）；9600 波特、20 ms 命令间隔下不丢命令；
  `:OUTP?` 回 1 / 0。
- [ ] **Hioki**：`*STB?` 在 `:ESE0 1` 之后测完一笔是奇数、`*CLS` 后变偶数（MAV 位 16 带不带都认）；非 A 型号（BT3562 / BT3563）
  认配置里的每一条命令（`*ESR?` 是 0）；实机读数的空格补位与正则；LAN 口是否只允许一个连接。
- [ ] 三台：请求超时 5 s 够不够（中速 + 平均次数、NPLC 调大时要加大）；动作级验收用的参考电芯读数与仪表面板一致。
- [ ] 电芯规格定下来后填设备方法输出项的上下限；量程（Hioki 30 mΩ / 3 mΩ 档、Keithley 2 V 档）按规格收窄。
- [ ] 模块还没登记进 `devices/gateway/README.md` 的模块表（不在本模块目录里，另行补）。
