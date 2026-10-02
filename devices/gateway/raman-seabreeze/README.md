# 设备模块：拉曼光谱仪（Ocean Insight · python-seabreeze）

Ocean Insight 光谱仪（QE Pro 一类）经 USB 接进 ILCS 做拉曼检测：网关跑在光谱仪 USB 插着的那台电脑上，ILCS 侧用
`http_json_v1` 接入，能力 `cap.ely.raman`。和光谱仪通信用开源的 [python-seabreeze](https://github.com/ap--/python-seabreeze)
（MIT），版本固定在 `requirements.txt`；去重、台账、查询、令牌、TLS 由 `ilcs_gateway` 负责。**激光由外部开关与联锁，
网关不控制激光。**

| 文件 | 内容 |
|---|---|
| `driver/spectro_api.py` | 光谱仪的调用面（`Spectrometer`）：身份、积分时间范围、设积分时间、波长、采一张谱、满量程、关闭；驱动只依赖这几个方法 |
| `driver/seabreeze_spec.py` | 真实接口：包 python-seabreeze 的 `Spectrometer`，补上报错归一、断开重连、改积分时间后丢谱、只开配置的那一台 |
| `driver/spectrum.py` | 谱图处理（纯函数）：取平均、波长换算成拉曼位移、裁范围、合并像素、取整 |
| `driver/device.py` | 把光谱仪映射成 `ilcs_gateway.Device`：参数核对、单测量位、后台采谱、饱和判断、终止、结论落盘 |
| `driver/config.py` | 网关配置（`--config`）：哪台光谱仪、激光波长、积分时间、采谱程序、位移范围 |
| `simulator/fake_spectrometer.py` | 模拟接口：同一组方法的假光谱仪，测量位上是一瓶碳酸酯 / LiPF6 电解液的合成拉曼谱，可注入故障 |
| `simulator/__init__.py` | `simulated_instrument()`：`--simulate` 用的假光谱仪 + 网关配置 |
| `simulator/raman-sim.json` | 容器里跑模拟网关用的配置（设备编号 SIM-RAMAN-01） |
| `gateway.py` | 入口：`--simulate` 用假光谱仪 |
| `profile.json` | ILCS 设备接入模板文件（草稿），导入后核对、由另一个人发布 |
| `tests/` | `test_module.py` 对假光谱仪跑 ILCS 接入验收清单（含故障项目）、驱动规则、谱图形状与 ILCS 曲线校验、profile 摘要；`test_spectrum.py` 谱图处理；`test_seabreeze_spec.py` 真实接口对着假的 python-seabreeze 包（不用装 seabreeze） |
| `deploy/usb-host.md` | 在光谱仪那台电脑上装驱动、注册成服务（Windows / Linux），第一次动作级验收怎么做 |
| `deploy/Dockerfile`、`deploy/compose.yml` | 在 ILCS 那台机器上起模拟网关（假光谱仪），接进 ILCS 的后端网络，给联调与模拟实验用 |

```bash
api/.venv/bin/pytest devices/gateway/raman-seabreeze/tests                              # 自测（不用装 seabreeze / numpy）
python devices/gateway/raman-seabreeze/gateway.py --simulate --insecure --port 8443       # 本机联调（假光谱仪）
python devices/gateway/raman-seabreeze/gateway.py --simulate --insecure --time-scale 0.1  # 假光谱仪采得快十倍
```

## 一条 ILCS 指令怎么落到光谱仪上

- **一条指令 = 在测量位上采 `repeats` 张谱、逐像素取平均，回报一条拉曼谱。** 不带 `repeats` 时采 1 张；范围
  `1–max_repeats`，整数。
- **积分时间**：指令带的 `integration_ms`（毫秒）优先，其次是设备方法的「程序」在 `programs` 里写的，再次是配置的
  `integration_ms`；不能超过 `max_integration_ms`，也不能超出光谱仪自己的范围。没带设备方法的指令（包括 ILCS 的
  接入验收）用 `default_program`。
- **一次一瓶**：光谱仪只有一个测量位（一个探头 / 样品池）。指令不带 `wells`，或 ILCS 逐孔参数 `wells` 里只有一瓶
  （孔位里的参数覆盖步骤顶层的）；多瓶回 `NotSupported`「按瓶分开下发」。上一次采谱没结束时新指令报忙。
- 参数只认 `repeats`、`integration_ms`（孔位里也只认这两个），**带别的参数一律拒绝**，不悄悄忽略。
- **明确拒绝 vs 失败**：开始采谱之前的错（参数非法、程序没登记、积分时间超范围、读不到光谱仪、按激光波长换算后
  位移范围里没有像素）都是明确拒绝，光谱仪没动。采谱是在后台线程里做的，`start` 立刻返回；采谱途中光谱仪报错判失败，
  错误里写明采了几张——光谱仪是被动的，采谱不消耗样品，可以直接重测。
- **饱和**：任一张谱上任一像素到了满量程的 98%，照样判完成，回报 `saturated: true` 和 `note`（峰顶可能被削平，缩短
  积分时间重测）。错误栏只给失败用；ILCS 侧由输出项 `max_counts` 的上限打标、交审核（见下文）。
- **终止**：不再采下一张，这次的谱不回报。正在读出的那一张停不下来（seabreeze 的采谱阻塞约一个积分时间），网关最多
  等 3 秒；没等到也确认终止（光谱仪本身是被动的），那一张读完测量位才空闲，这期间新指令报忙。
- **网关重启**：采完的谱连同结论写进状态目录（`<state-dir>/runs/`，留最近 500 次），重启后按原指令号照样交得出谱图；
  重启前还没采完的判失败（进程没了采谱也就停了），可以重测。
- **回报**（`delivered`）：

  ```json
  {"spectrum": {"x": [160.24, 163.5, ...], "y": [2640.3, 2645.7, ...]}, "integration_ms": 1000.0, "repeats": 3,
   "laser_nm": 785.0, "max_counts": 19274.0, "saturated": false, "program": "RAMAN"}
  ```

  带了一瓶 `wells` 时整行放在 `delivered.wells[孔位]` 里（ILCS 按孔位写成样本的检测结果）。遥测只有 `max_counts`。
- **谱图怎么来的**：拉曼位移（cm-1）= 1e7 / 激光波长 − 1e7 / 像素波长（nm），按 x 严格递增排好（倒序的翻过来，
  不单调的波长标定报错），裁到 `shift_range_cm1`（与光谱仪覆盖范围取交集：785 nm 激发、795–1000 nm 的光谱仪是
  约 160–2000 cm-1），点数超过 `max_points`（≤ 2 万，ILCS 曲线的缺省上限）就合并相邻像素取平均（不挑点：挑点会把
  噪声当峰留下），x 取到 0.01 cm-1、y 取到 0.1 计数。y 是光谱仪读到的计数（开了 `correct_dark_counts` 就扣过电子
  暗电平），**没扣基线、没去宇宙射线、没做强度校正**。

## 激光

- 配置里 `laser.kind` 只能是 `external`：激光由外部钥匙开关与联锁控制，网关只用标称波长 `wavelength_nm` 换算拉曼位移。
  python-seabreeze 对多数型号没有激光控制（个别型号只有 TTL 灯控线），所以这一版不碰激光。
- **网关不知道激光开没开**：激光没开时采到的只是暗谱，网关照样报完成。流程里要有开激光、确认联锁的步骤（或现场规程）。
- 以后要由 ILCS 开关激光：带激光控制的光谱仪（如 Wasatch Photonics，开源的 Wasatch.PY 有 `set_laser_enable`）可以做成
  另一个实现同一个 `Spectrometer` 调用面的后端，驱动代码不用改。没做。

## ILCS 里怎么建

- **工位**：一个测量位（通道 1，一次一瓶）。能力 `cap.ely.raman` 的参数 `repeats`（整数，单位「次」），工位极限
  `[1, max_repeats]`（电解液线的拉曼工位是 `[1, 5]`，模拟配置照它写 5）。要让配方 / 方法改积分时间，就给能力加参数
  `integration_ms`（数值，ms），工位极限上限不超过 `max_integration_ms`、下限不低于光谱仪的最短积分时间；不加的话积分时间
  由设备方法的程序决定。
- **接入**：导入 `profile.json` 成接入模板、另一个人发布，工位套用模板，连接参数填网关地址、证书、设备编号。
- **设备方法**：程序写 `programs` 的键（如 `RAMAN`），适用型号写光谱仪的型号。输出项：
  - `spectrum`：输出类型「曲线」（`kind: "series"`），单位 `counts`，关联一个曲线型指标（`value_type: series`，单位
    `counts`，规则 `x_label: 拉曼位移`、`x_unit: cm-1`）；谱图就按样本写成「设备回报」检测结果、待复核；
  - `max_counts`：单位 `counts`，上限 `hi` 设成满量程的 98%（满量程看网关 `/health` 回报的 `max_intensity`；
    16 位的光谱仪是 65535，上限 64224）——饱和的谱越界打标、置可疑，交审核。
- **流程**：拉曼一瓶一步。电解液配液线（`scripts/lines/c-electrolyte`）里的拉曼方法现在没有输出项、谱图走结果文件；
  接这台网关后在方法上加上面两个输出项，谱图直接回报。

## 在 ILCS 那台机器上模拟联调

```bash
# 1. 起模拟网关（假光谱仪，容器 ilcs-raman-sim，接 ILCS 的后端网络 ilcs_backend）
docker compose -f devices/gateway/raman-seabreeze/deploy/compose.yml up -d --build
# 2. deploy/.env 的 ILCS_ADAPTER_ALLOWED_HOSTS 加上 raman-sim，重建 api 与 executor（docker compose up -d）
# 3. 「工位与接入 → 接入模板」导入 profile.json、另一个人发布；工位套用模板，连接参数见 deploy/compose.yml 的注释
```

模拟网关自报为模拟器：只读级验收就放行；它回报的谱图在 ILCS 里带「模拟」标记、仪器注明模拟设备，不进闭环训练数据。
正式环境（`ILCS_ENVIRONMENT=production`）拒绝接入模拟器。接真机见 [deploy/usb-host.md](deploy/usb-host.md)。

## 还没做 / 要现场核对的

- [ ] **激光控制**：网关不开关激光、不读激光联锁（见上文「激光」）；Wasatch.PY 一类的后端没做。
- [ ] **暗谱 / 背景扣除**：没有关激光采暗谱再扣；`correct_dark_counts` 只扣电子暗电平（遮光像素），扣不掉环境光与样品池背景。
- [ ] **x 轴标定**：按激光标称波长换算，没用标准物（环己烷 801.3 cm-1、硅 520.7 cm-1）校正；激光波长漂移、光谱仪
  波长标定偏差都会让整条谱平移。
- [ ] **宇宙射线（尖峰）去除**：没做；现在几张谱是直接平均，以后可以按中位数剔除尖峰。
- [ ] **荧光基线**：没扣；谱图是原始计数。要峰强、峰面积这类数值，就在解析或 ILCS 的派生指标里另算。
- [ ] 现场核对：
  - 改积分时间后的第一张谱是不是旧积分时间的（`flush_scans`，缺省丢 1 张）；QE Pro 有内部光谱缓存，核对两次采谱
    之间读到的是新谱；
  - 开了 `correct_dark_counts` 时，削顶的像素读数是「满量程 − 暗电平」：暗电平超过满量程的 2% 时 98% 的判据会漏判，
    这时关掉暗电平校正（饱和按原始计数判），或把 ILCS 侧 `max_counts` 的上限调低；
  - 固件版本读不到（seabreeze 的通用接口没有），身份里固件留空；
  - 探测器制冷（QE Pro 的 TEC）网关没去设：核对上电后是否自动制冷到设定温度（seabreeze 有 thermo_electric 功能，
    要管就以后加）；
  - USB 在两次采谱之间拔掉，要到下一次采谱（或重连）才发现；USB 读出卡死时那条作业一直报在采（终止照样确认，
    但测量位一直忙），只能重启网关服务。
- [ ] `profile.json` 是草稿：核对连接参数示例后导入，由另一个人发布。
