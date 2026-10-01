# 设备模块：Neware 充放电柜（BTS 8.0）

Neware 充放电柜经 BTS 8.0 的 TCP XML 接口接进 ILCS：网关跑在 BTS 那台 Windows 机器上，ILCS 侧用 `http_json_v1` 接入，
能力 `cap.test`。和 BTS 通信用开源的 [aurora-neware](https://github.com/EmpaEconversion/aurora-neware)（MIT，Empa），
版本固定在 `requirements.txt`；去重、台账、查询、令牌、TLS 由 `ilcs_gateway` 负责。

| 文件 | 内容 |
|---|---|
| `driver/bts_api.py` | BTS 的调用面（`Bts`）：读通道、启动、停止；驱动只依赖这几个方法 |
| `driver/bts.py` | 真实接口：包 aurora-neware 的 `NewareAPI`，补上超时、断线重连、「没发出去」与「发了没回」的区分 |
| `driver/device.py` | 把 BTS 映射成 `ilcs_gateway.Device`：一颗电芯一个通道、通道白名单、工步选择、条码认作业、状态汇总、终止不误停 |
| `driver/config.py` | 网关配置（`--config`）：连哪台 BTS、白名单通道、工步文件 |
| `simulator/fake_bts.py` | 模拟接口：同一组方法的假 BTS，按时长跑完、可注入故障 |
| `simulator/bts_server.py` | 假 BTS 的 TCP XML 服务：测真实接口那条线；现场没有 BTS 时也能起来联调 |
| `simulator/neware-sim.json` | 容器里跑模拟网关用的配置（8 通道，ILCS 必须指定通道） |
| `gateway.py` | 入口：`--simulate` 用假 BTS |
| `profile.json` | ILCS 设备接入模板文件（草稿），导入后核对、由另一个人发布 |
| `tests/` | `test_module.py` 对假 BTS 跑 ILCS 接入验收清单（含故障项目）与驱动规则（含多电芯）；`test_bts_wire.py` 走 aurora-neware → TCP → 假 BTS，没装 aurora-neware 时跳过 |
| `deploy/windows-service.md` | 在 BTS 机器上注册成服务、第一次动作级验收怎么配 |
| `deploy/Dockerfile`、`deploy/compose.yml` | 在 ILCS 那台机器上起模拟网关（假 BTS），接进 ILCS 的后端网络，给联调与模拟实验用 |

```bash
api/.venv/bin/pytest devices/gateway/neware-bts/tests                              # 自测（wire 测试要另装 aurora-neware）
python devices/gateway/neware-bts/gateway.py --simulate --insecure --port 8443      # 本机联调（假 BTS，8 通道）
python devices/gateway/neware-bts/simulator/bts_server.py --port 5502               # 起一个假 BTS 给真实接口连
```

## 一条 ILCS 指令怎么落到柜子上

- **一条指令 = 一个工步文件，在一个或几个通道上各跑一次测试。** 工步（倍率、截止电压、循环数）在 BTS 里编辑、另存成
  工步文件，登记在配置的 `programs` 里；ILCS 设备方法的「程序」选用哪一个，没带方法的指令用 `default_program`。
  指令只带通道，**带别的工艺参数一律拒绝**，不悄悄忽略：
  - 一颗电芯：`channel`（白名单里的序号，1 起；或通道号 `21-1-3`）；
  - 几颗电芯：ILCS 的逐孔参数 `wells: {孔位: {"channel": …}}`，一个孔位一颗电芯，各启动一个通道。孔位没写通道时用
    顶层的 `channel`；两颗落到同一个通道就拒绝。要用的通道有一个不空闲，一个都不启动。
- **一颗都不能漏**：已经启动了几个、后面的通道被 BTS 拒绝，这条指令只做了一部分——按结果未知处理，交人到 BTS 核查，
  不报「没动」，也不自动去停已启动的。
- **通道白名单是安全边界**：网关只在 `channels` 里的通道上启动、停止；柜子上人工在用的通道别列进去。
- **条码认作业**：每颗电芯的条码是 `ILCS-<指令号与孔位的 SHA-256 前 12 位>`，回执里带 `bts_barcode`。查状态、启动没拿到
  应答后找回、终止，都先核对通道上的条码；对不上（通道已经跑了别的测试）就不认、**也不去停它**。
- **状态**：`working` → 在跑，`pause`（在 BTS 上人工暂停）→ 保持，`finish` → 完成，`stop` → 失败（被停止），
  `protect` → 失败（保护停机，带 log_code）；其他状态不下结论，网关照报原状态。几颗电芯时：有一颗在跑就是在跑，
  全部结束后都完成才算完成，有一颗失败就是失败（错误里写明哪个孔位、哪个通道）。
- **明确拒绝 vs 结果未知**：启动命令发出之前出的错（通道不空闲、工步文件不在、连不上 BTS、读不到通道状态）是明确拒绝，
  通道没动；发出之后没拿到应答是结果未知，网关按条码去 BTS 找回（每颗都找到才认），绝不重发。
- **实测值**：一颗电芯时回执 `delivered` 里直接是通道、条码、循环号、工步号、工步类型、容量、能量；几颗时在
  `delivered.wells[孔位]` 里逐颗给（ILCS 按孔位写成每个样本的检测结果）。遥测是电压、电流（几颗时名字带孔位，如
  `voltage@A1`）。数值是 BTS 接口报的原值，单位以 BTS 设置为准（aurora-neware 录的样例看是 V、A、Ah、Wh），现场核对
  后再在 ILCS 侧关联指标（指标单位要和回报单位一致，ILCS 不换算）。

## ILCS 里怎么建

- **工位**：通道数 = 白名单通道数，**按样本计通道**（`channel_unit: sample`，一颗电芯占一个通道）；能力 `cap.test`
  的参数 `channel`（整数，单位「号」），工位极限 `[1, 通道数]`。
- **接入**：导入 `profile.json` 成接入模板、另一个人发布，工位套用模板，连接参数填网关地址、证书、设备编号。
- **设备方法**：程序写工步的键（如 `CC-CV`），适用型号写这台柜子的型号；输出项 `capacity`、`cycle`（需要的话 `energy`）
  关联指标，回报值就按样本写成「设备回报」检测结果、待复核。
- **流程**：先放一个「上柜」人工步骤，表单里有按样本录入的数值字段「所在通道」；循环步骤引用设备方法，`channel`
  不写固定值，用逐样本前馈取自上柜步骤（来源单位「号」、预期范围 `[1, 通道数]`）。这样电芯放在哪个通道由现场记下，
  ILCS 一条指令带着每颗电芯的通道发给网关。

## 在 ILCS 那台机器上模拟联调

```bash
# 1. 起模拟网关（假 BTS，8 通道，容器 ilcs-neware-sim，接 ILCS 的后端网络 ilcs_backend）
docker compose -f devices/gateway/neware-bts/deploy/compose.yml up -d --build
# 2. deploy/.env 的 ILCS_ADAPTER_ALLOWED_HOSTS 加上 neware-sim，重建 api 与 executor（docker compose up -d）
# 3. 登记柜子并跑一个单独的实验任务（4 颗扣电，上柜 → 循环 → QA 复核 → 报告）
python3 scripts/load-neware-cycler.py register --acceptance
python3 scripts/load-neware-cycler.py run --cells 4
```

模拟网关自报为模拟器：只读级验收就放行；它回报的值在 ILCS 里带「模拟」标记、仪器注明模拟设备，不进闭环训练数据。
正式环境（`ILCS_ENVIRONMENT=production`）拒绝接入模拟器。

## 还没做 / 要现场核对的

- [ ] **保持 / 续跑**：BTS 接口有 `continue`、`chl_ctrl` 命令，aurora-neware 还没实现、报文格式没核实，所以契约声明不支持保持。
- [ ] **按 ILCS 程序表生成工步文件**：ILCS 能力可以定义「充放电工步」程序表参数；要把它转成 Neware 工步 XML 还得先拿到
  真实工步文件核对格式。现在只认在 BTS 里做好的工步文件。
- [ ] **曲线数据**：BTS 把 .nda/.ndax 存到 `data_dir`；取数走结果文件接收器（`devices/connectors/result_files`），
  解析 .ndax 可以用 [NewareNDA](https://github.com/d-cogswell/NewareNDA)（BSD-3），接收器现在只认 csv / 键值表。
  BTS `inquire` 报的容量是当前（结束时是最后一个）工步的值，不一定是放电容量：要放电容量、保持率就得从数据文件算。
- [ ] 现场核对：BTS 的 `workstatus` 有没有上面没列的值（如等待、预约）、条码长度与字符限制、`log_code` 的含义、
  `getdevinfo` 里通道后面的 true/false 是什么意思。
- [ ] `profile.json` 是草稿：核对连接参数示例后导入，由另一个人发布。
