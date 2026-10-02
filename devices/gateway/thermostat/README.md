# 设备模块：恒温循环器 / 冷水机 + 制冷搅拌（Huber / Julabo / LAUDA）

一台恒温循环器（冷水机）经串口或网口接进 ILCS：网关跑在接冷水机的那台电脑上，ILCS 侧用 `http_json_v1` 接入。
品牌还没定，网关配置里选 `huber`（PB 命令）、`julabo`、`lauda`（LAUDA 命令集）三种之一，换品牌只改配置。
两个动作：

- **控温**（`cap.thermostat`）：参数 `temp`（℃）、`time`（s，到温之后保温多久）；
- **制冷搅拌**（`cap.ely.stir`，配了 `stirrers` 才有）：电解液线的「加料后制冷搅拌 −10 ℃ / 60 s / 400 rpm」、
  「冷藏搅拌 5 ℃」。冷水机冷着一块冷块，冷块上每个位置一块 IKA 磁力搅拌板、放一瓶，板子只管搅（IKA 板不能制冷）。

去重、台账、查询、令牌、TLS 由 `ilcs_gateway` 负责；到温判断、计时、停板、回待机、部分启动回退在本模块。

| 文件 | 内容 |
|---|---|
| `driver/link.py` | 文本链路（TCP / 串口，pyserial 用到时才导入）：`ask` 一问一答、`send` 只发不读；按厂家的行尾、命令间隔、串口参数；断线丢掉连接、下次重连，发之前清掉晚到的旧字节 |
| `driver/chillers.py` | 三家冷水机的命令统一成同一组方法：读数（浴温、设定值、启停、报警、远程、重启过）、写设定值、启停；区分「读不懂 / 没确认」与「冷水机明确不收」 |
| `driver/namur.py` | 一块 IKA 板的 NAMUR 电机命令（取自 ika-stirrer）：转速、设定值回读、启停；停的时候顺手关加热 |
| `driver/device.py` | 映射成 `ilcs_gateway.Device`：参数核对、共用一个冷浴温度、启动前只读预检、改设定值 / 开控温的明确拒绝与结果未知、后台线程（到温、保温、各瓶计时、停板、回待机）、终止、作业记录、重启不续做 |
| `driver/config.py` | 网关配置（`--config`）：冷水机（厂家、链路、温度范围、到温判据、作业之后怎样）、搅拌位置、能力、程序 |
| `simulator/chillers.py` | 假冷水机：TCP 上说三家的真协议，一阶浴温模型（时间常数、制冷能力下限、停机后漂回室温、模型时间放快）；可单独起 |
| `simulator/namur_server.py` | 假搅拌板（NAMUR，写命令不回，转速爬升）；可单独起 |
| `simulator/__init__.py` | 模拟工位：按厂家起一台假冷水机、每个位置一块假板，真实接口照常连它们；故障注入在设备层（`FaultState`） |
| `simulator/thermostat-sim.json` | 容器里跑模拟网关用的配置（SIM-CHILL-01，缺省 Huber，4 个搅拌位置，自动挑位置） |
| `gateway.py` | 入口：`--simulate [--kind julabo]` 用假设备；`--check` 只读地问一遍冷水机和每块板 |
| `profile.json` | ILCS 设备接入模板文件（草稿），导入后核对、由另一个人发布 |
| `tests/` | `test_module.py`：三家各跑一遍 ILCS 接入验收清单（含故障项目），控温、制冷搅拌、拒绝、终止、运行中出事、写了没确认、重启；`test_chillers.py`：真实接口对着假设备走 TCP，逐家核对命令与应答 |
| `deploy/README.md` | 在接冷水机的电脑上运行：接线（三家的串口参数、网口端口、面板设置）、注册服务、停止 / 重启时设备怎样、第一次动作级验收 |
| `deploy/Dockerfile`、`deploy/compose.yml` | 在 ILCS 那台机器上起模拟网关，接进 ILCS 的后端网络 |

```bash
api/.venv/bin/pytest devices/gateway/thermostat/tests                                   # 自测（不要 pyserial、不要 ILCS 数据库）
python devices/gateway/thermostat/gateway.py --simulate --insecure --port 8443           # 本机联调（假 Huber + 4 块假板）
python devices/gateway/thermostat/gateway.py --simulate --kind lauda --insecure --port 8443
python devices/gateway/thermostat/simulator/chillers.py --kind julabo --port 4100        # 起一台假冷水机给真实接口连
python devices/gateway/thermostat/gateway.py --config thermostat.json --check            # 现场接好线：只读地问一遍
```

## 三家的命令（这里用到的）

| | Huber（PB 命令） | Julabo | LAUDA |
|---|---|---|---|
| 串口缺省 | 9600 8N1，无握手 | 4800 7E1，硬件握手 RTS/CTS | 9600 8N1，可带可不带 RTS/CTS |
| 网口 | Pilot ONE：TCP 8101 | 照设备菜单 | 网口模块：TCP 54321（出厂） |
| 格式 | `{M` + 地址 2 位 + 值 4 位（十六进制补码，0.01 ℃）+ CR LF；回 `{S…`；读写一律有应答 | 命令 空格 参数 CR；`in` 回一行，`out` **不回** | 命令（`_` 或空格）CR LF；读回数值，写回 `OK` / `ERR_x` |
| 浴温 | `{M01****` 内部温度 | `in_pv_00` | `IN_PV_00` 出口温度 |
| 设定值 | `{M00****`；写 `{M00FC18`（−10 ℃），回写完之后的值 | `in_sp_00`；写 `out_sp_00 -10.00` | `IN_SP_00`；写 `OUT_SP_00_-10.00` |
| 启停 | `{M140001` / `{M140000`（控温） | `out_mode_05 1` / `0`，`in_mode_05` 读 | `START` / `STOP`（待机），`IN_MODE_02` 读 |
| 报警 | 状态字 `{M0A****` 第 8 位 + 错误号 `{M05****` | `status` 回负数开头的报警 | `STATUS` −1 + `STAT` |
| 其他 | 设定值上下限 `{M30****` / `{M31****`；状态字第 14 位（重启过）；序列号 `{M1B` / `{M1C` | `status` 00–03（面板 / 远程、停 / 开）；`version` | `TYPE`、`VERSION_R` |
| 出错时 | 没开放的地址回 `7FFF`；探头读不到 −151 ℃（`C504`）；格式不对**不回** | 值超范围不回话，下一次 `status` 报 −10 / −11；面板控制模式下 `out` 命令悄悄忽略 | `ERR_6` 值不允许、`ERR_3` 命令错误、`ERR_5` 语法错、`ERR_38` 一小时改设定值超过 20 次（WK / WKL） |

- 写命令没有应答的（Julabo `out`、IKA），写完紧跟一条读命令回读确认；有应答的（Huber、LAUDA）照样回读设定值。
- Huber、Julabo 用数据命令改的设定值不存盘，冷水机断电后回到面板上的值；Julabo 远程模式下来电不自动启动。
- 网关只在设定值真的要变时才写（LAUDA WK / WKL 一小时只许改 20 次）；已经在控温就不再发启动命令。

## 一条 ILCS 指令怎么落到设备上

- **控温**：写设定值、开控温（冷水机已经开着就只改设定值）。浴温连续 `settle_sec`（缺省 60）秒在设定值
  ±`tolerance_c`（缺省 0.5 ℃）以内算**到温**；`reach_timeout_sec`（缺省 3600）秒内没到温判失败，错误里写明最后读数。
  到温之后保温 `time` 秒（0 = 到温即完成），完成。实测值 `{setpoint, temp, time_to_reach_s, hold_s, deviation_c}`：
  `temp` 是结束时的浴温，`time_to_reach_s` 从开始到「到温」（含 settle_sec），`deviation_c` 是到温之后浴温离设定值最远多少。
  遥测 `temp`（浴温，`setpoint` 是设定值）。带 `wells` 时各孔位的温度、时长都要一样，实测值在 `delivered.wells[孔位]` 里逐孔给同一组。
- **制冷搅拌**：参数 `temp`、`time`、`rpm`、`position`；一瓶直接给，几瓶用 ILCS 的逐孔参数
  `wells: {孔位: {temp, time, rpm, position}}`（孔位没写的用顶层的值）。**所有瓶共用一个冷浴温度**：各瓶 `temp`
  不一样就拒绝（`同一个冷浴只能一个温度`）。先把冷浴带到温（同上），再按各瓶自己的 `rpm` 启动各自的板，各瓶从自己的板
  启动起计时、到 `time` 秒就停（ILCS 一次都不来查也照停），**每块板的停止都确认了**才算完成。`rpm` 为 0 的瓶只冷不搅、
  不碰它的板。实测值逐瓶 `{position, temp, rpm, duration_s}`（`temp` 是这瓶停下时的浴温，`rpm` 是停之前最后一次读数）；
  遥测 `temp` 与 `rpm@孔位`。
- **一个冷浴同一时刻只做一条指令**：冷浴上有作业，新指令一律 `busy`（两条指令要两个温度，冷浴只有一个）。
- **作业之后**（完成、失败、被终止都算）：`after: keep`（缺省）照最后的设定值接着控温；`after: standby` 改到
  `standby_c`、接着控温（不关机：冷块突然回温会结露）。回待机写不进去就一直重试、一直报在跑；冷水机明确不收（Julabo
  被切到面板控制）就不再重试，结论里写明。冷水机报警时不再给它发命令。
- **明确拒绝 vs 结果未知**：
  - 动设备之前出的错是明确拒绝（设备没动）：参数不对、程序没登记、带物料、温度超出 `min_c`–`max_c` 或冷水机自己的
    设定范围、转速超出位置极限、位置不对（`invalid`）；冷浴 / 位置不空闲、搅拌子在转（不是本网关启动的）、读不到冷水机或板子、
    作业记录写不进去（`busy`）；冷水机报警、Julabo 没切到远程控制（`interlocked`）；
  - 冷水机明确不收设定值或启动命令（LAUDA `ERR_x`、Julabo −10 / −11、Huber 回读还是原值）：拒绝，设备没动；
  - **写出去了却没确认**：冷水机原来开着（改设定值它就在动）或启动命令发出去了——先把它恢复原样（改回原设定值、停下），
    再按结果未知抛出，交人核查，不报「没动」也不认这个作业；冷水机原来关着、只写了设定值的，设备没动，照样拒绝。
  - **搅拌只启动了一部分**（后面某块板启动出错）：先把这条指令启动了的板都停下（确认），再判失败，写明哪几块启动过。
- **运行中出事**：冷水机报警、被停了、切到了面板控制、设定值被改、重启过（Huber 状态字第 14 位），或 `lost_after_sec`
  （缺省 30）秒且至少连续 3 次读不到——作业判失败，先停板。某块板 `lost_after_sec` 秒读不到：那一瓶提前停下、判失败，别的瓶照常到点。
- **终止**：马上停这条指令的板、按 `after` 处理冷水机，确认之后才回；15 秒内确认不了回结果未知（网关接着重试）。
  已经做完、正在收尾时来的终止如实拒绝（`来不及终止`），原作业照报完成。**不做保持**。
- **网关重启不续做**：作业记录在状态目录里，重启后按作业号照样答得上（跑完的照报完成和实测值）。重启时还没结束的作业，
  先停板、按 `after` 处理冷水机，再判失败（写明计划、开始了多久）；启动途中崩掉的不认（结果未知）。停服务（SIGTERM）时先停再退出。

## ILCS 里怎么建

- **能力**：`cap.thermostat`（控温）参数 `temp`（℃）、`time`（s）——ILCS 的能力字典里还没有，先登记；制冷搅拌用电解液线
  已有的 `cap.ely.stir`，和 ika-stirrer 一样**要加** `position`（整数，单位「号」）。
- **工位极限要照冷水机的范围登记**：`temp` 取网关配置的 `min_c`–`max_c`（如 −20–25 ℃，不能超出冷水机和导热液的范围）；
  EL-D-STIR（−20–0 ℃）、EL-D-COLD（−20–10 ℃）现在登记的范围都落在 −20–25 ℃ 里，可以接这台。`rpm` `[0, max_rpm]`
  （1 到 `min_rpm` 之间的转速网关会拒，ILCS 的极限表达不了「0 或 ≥ 50」，设备方法缺省别落在这一段）；`position` `[1, 位置数]`。
- **工位**：只控温的站通道数 1；制冷搅拌的站通道数 = 位置数，**按样本计通道**（`channel_unit: sample`）。
- **温度只能是步骤固定参数**：一条指令所有瓶共用一个冷浴，设备方法里 `temp` 不要做成逐样本参数（逐样本时各瓶不一样会被拒）；
  `time`、`rpm`、`position` 可以逐样本。位置按 ika-stirrer 的办法逐样本前馈（放瓶步骤记下每瓶在哪个位置）。
- **设备方法**：程序写网关配置 `programs` 的键（电解液线的 `STIR-CHILL`、`STIR-COLD` 已经在样例和模拟配置里）；
  输出项控温 `temp`、`time_to_reach_s`、`hold_s`，搅拌 `temp`、`rpm`、`duration_s` 关联指标（单位 ℃、s、rpm，ILCS 不换算）。
- **步骤时长**：搅拌从到温之后才开始计时，一步的实际时长 = 到温 + `settle_sec` + `time`。冷浴已经在那个温度（连着几瓶、
  `after: keep`）时只多 `settle_sec`；从室温冷到 −10 ℃ 可能要几十分钟——设备方法的时长与排程按这个估。
- **接入**：导入 `profile.json` 成接入模板、另一个人发布，工位套用模板，连接参数填网关地址、证书、设备编号。

## 在 ILCS 那台机器上模拟联调

```bash
# 1. 起模拟网关（假冷水机 + 4 块假板，容器 ilcs-thermostat-sim，接 ILCS 的后端网络 ilcs_backend）
docker compose -f devices/gateway/thermostat/deploy/compose.yml up -d --build
# 2. deploy/.env 的 ILCS_ADAPTER_ALLOWED_HOSTS 加上 thermostat-sim，重建 api 与 executor（docker compose up -d）
# 3. 导入 profile.json、发布；工位套用模板，base_url https://thermostat-sim:8443/api/v1，设备编号 SIM-CHILL-01
```

模拟网关自报为模拟器：只读级验收就放行；它回报的值在 ILCS 里带「模拟」标记，不进闭环训练数据。假冷水机说哪家的协议
由 compose 里的 `GATEWAY_CHILLER_KIND` 选（缺省 huber）；浴温时间常数 15 s（`simulator` 段），到温要一两分钟。

## 参考

命令、串口参数、应答格式、出错表现都取自厂家手册；开源驱动只用来交叉核对事实，**没有拷贝代码**（本模块的代码是照
ika-stirrer 的写法自己写的；`driver/link.py`、`driver/namur.py`、`simulator/namur_server.py` 改自同一仓库的 ika-stirrer）。

| 来源 | 许可 | 用到的事实 |
|---|---|---|
| Huber《Data Communication》手册 V2.6.0en/31.07.23：https://www.huber-online.com/fileadmin/user_upload/huber-online.com/Downloads/Handb%C3%BCcher_Software/Handbuch_Datenkommunikation_PB_en.pdf | 厂家手册 | RS232 9600 8N1 无握手、Pilot ONE TCP 8101；PB 命令结构 `{M`/`{S`、`****` 只读、补码、0.01 ℃；`7FFF` 没开放、−151 ℃（`C504`）没探头、格式不对不回；地址 0x00 / 0x01 / 0x05 / 0x06 / 0x07 / 0x0A / 0x14 / 0x16 / 0x1B / 0x1C / 0x30 / 0x31 及状态字各位；等应答再发下一条、建议等 1 s；数据命令改的设置不存盘 |
| Julabo《Cryo-Compact Circulators CF30 / CF40》操作手册 19534866-V7（Cole-Parmer 转载）：https://pim-resources.coleparmer.com/instruction-manual/12150-62-67-julabo-cf30-cf40-operating-manual.pdf | 厂家手册 | 4800 波特、偶校验、硬件握手；命令 空格 参数 CR、`in` 应答以 LF 结尾、`out` 只在远程模式有效；两条命令至少隔 250 ms；`version`、`status`、`in_pv_00`、`in_sp_00`、`in_mode_05`、`out_sp_00`、`out_mode_05`；状态 00–03、错误 −01 … −33；远程模式下来电要重发 |
| Julabo《Recirculating Coolers FL》操作手册 19534829-V1（Cole-Parmer 转载）：https://pim-resources.coleparmer.com/instruction-manual/julabofl1.pdf | 厂家手册 | 同上（小写命令）；错误 −03 / −20 警告、−51 … −53 |
| LAUDA《LOOP L 100 / L 250》操作说明（Brookfield 转载）：https://www.brookfieldengineering.com/-/media/ametekbrookfield/product-manuals/lauda-loop-manual.pdf | 厂家手册 | RS232 8N1、2400–19200（出厂 9600）、可带可不带 RTS/CTS；CR / CRLF 结尾、应答 CRLF、等应答再发；`OUT_SP_00`、`START`、`STOP`、`IN_PV_00`、`IN_SP_00`、`IN_MODE_02`、`TYPE`、`VERSION_R`、`STATUS`、`STAT`；`OK` / `ERR_2/3/5/6/32`；`_` 可写成空格 |
| LAUDA《WK / WKL 水循环冷却器》操作说明 YAWE0019（2006）：https://s-a-le.nl/wp-content/uploads/2020/03/Lauda-wkl-230-manual.pdf | 厂家手册 | 设定值要几秒才转给控制器（`STAT` 第 5 位）、一小时最多改 20 次（`ERR_38`）、允许的数值格式；这个系列没有 `IN_SP_00` / `START` / `STOP`，本模块不支持 |
| croningp/PyLabware：https://github.com/croningp/pylabware（`huber_petite_fleur.py`、`julabo_cf41.py`） | MIT，Copyright (c) 2021 Cronin Group | 交叉核对：Huber `{M..` 命令、9600 8N1；Julabo CF41 用 9600 7E1 + RTS/CTS、大写命令、命令间隔 0.3 s、外置探头没接回 `---.--`（没有拷贝代码） |
| EfrenPy/JulaboFL1703-control：https://github.com/EfrenPy/JulaboFL1703-control | MIT | 交叉核对：FL1703 4800 7E1 RTS/CTS（**7 数据位、1 停止位**取自这里和 PyLabware：读到的两份 Julabo 手册只写了 4800 / 偶校验 / 硬件握手）、小写命令、面板切远程的按键操作 |
| jopekonk/julabolib：https://github.com/jopekonk/julabolib | MIT | 只看了 README：CF30 / CF40 走 RS232 |
| Jan-IngenHousz-Institute/julabo-valegro-350-500：https://github.com/Jan-IngenHousz-Institute/julabo-valegro-350-500 | 没写许可 | 只看了 README：`01 MANUAL START` 时 `out` 命令被悄悄忽略 |
| numat/huber、alexrudd2/huber：https://github.com/numat/huber | GPL-2.0 | 只看了 README（PB 命令走 TCP）；没读代码、没拷贝 |
| ika-stirrer（本仓库 devices/gateway/ika-stirrer） | 本仓库 | 链路、NAMUR 电机命令、假板、计时线程与停止确认、部分启动回退、作业记录的写法 |

## 还没做 / 要现场核对的

- [ ] **品牌、型号**：拿到实物后核对这一型号支持上表的命令（Huber 老控制器只支持 0–0x47 号地址、部分地址要 E-grade；
  Huber 非 Pilot 控制器（如 OLÉ）可能没有 PB 命令；LAUDA WK / WKL 没有 `IN_SP_00` / `START` / `STOP`，不支持）。
- [ ] **没核实的**：LAUDA 网口模块出厂端口 54321 只见于检索摘要（LRZ 921 手册网页打不开）；Julabo 串口的 7 数据位、
  1 停止位（手册摘录没写，取自开源驱动）、网口端口、新机型的波特率、命令大小写（CF 手册写大写、FL 手册写小写，配置 `uppercase`）；Julabo `status` 的错误消息读一次就清还是一直报；
  报警时 `in_mode_05` 回什么；LAUDA ECO / PRO 写设定值是不是马上能回读（网关最多等 5 s）、`STAT` 各位的含义（各系列不同）、
  报警时 `START` 回什么；Huber 各错误号的含义（模拟器里的 −1331 是随便取的）。
- [ ] **到温判据**：现在按冷水机的浴温（Huber 内部温度、Julabo 浴温、LAUDA 出口温度）判，冷块和瓶子会滞后。要按冷块 / 瓶里的
  温度判，就接外置 Pt100（Huber 0x07、Julabo `in_pv_02`、LAUDA `IN_PV_03`）、改用外控，现场确认之后再加。电解液线的制冷搅拌
  是为了带走溶解热，不是把溶液冷到设定值——判据和 `settle_sec` 要和工艺确认。
- [ ] **IKA 板放在冷块上**：IKA 板的允许环境温度（RCT digital 是 5–40 ℃）、冷块上结露结冰、隔着冷块磁力耦合够不够、
  转速读数是电机转速不是搅拌子——都要和厂家、现场确认；也可以换成冷块自带的搅拌。
- [ ] **Huber LAI 命令**：RS485 总线上挂多台 Huber 要用 LAI 命令，没做（PB 命令只能点对点）。
- [ ] **看门狗**：Huber 0x40（vWD1）、Julabo / LAUDA 的接口超时都没用。网关挂了冷水机照最后的设定值控温、板子一直转。
- [ ] **到温之后浴温又跑出容差**：只记在 `deviation_c` 里，不判失败；要不要判、判据多少和工艺确认。
- [ ] `profile.json` 是草稿：核对连接参数示例后导入，由另一个人发布；`cap.thermostat` 要先在 ILCS 能力字典里登记。
