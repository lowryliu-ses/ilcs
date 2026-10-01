# 设备模块：Neware 充放电柜（BTS 8.0）

Neware 充放电柜经 BTS 8.0 的 TCP XML 接口接进 ILCS：网关跑在 BTS 那台 Windows 机器上，ILCS 侧用 `http_json_v1` 接入，
能力 `cap.test`。和 BTS 通信用开源的 [aurora-neware](https://github.com/EmpaEconversion/aurora-neware)（MIT，Empa），
版本固定在 `requirements.txt`；去重、台账、查询、令牌、TLS 由 `ilcs_gateway` 负责。

| 文件 | 内容 |
|---|---|
| `driver/bts_api.py` | BTS 的调用面（`Bts`）：读通道、启动、停止；驱动只依赖这几个方法 |
| `driver/bts.py` | 真实接口：包 aurora-neware 的 `NewareAPI`，补上超时、断线重连、「没发出去」与「发了没回」的区分 |
| `driver/device.py` | 把 BTS 映射成 `ilcs_gateway.Device`：通道白名单、工步选择、条码认作业、状态映射、终止不误停 |
| `driver/config.py` | 网关配置（`--config`）：连哪台 BTS、白名单通道、工步文件 |
| `simulator/fake_bts.py` | 模拟接口：同一组方法的假 BTS，按时长跑完、可注入故障 |
| `simulator/bts_server.py` | 假 BTS 的 TCP XML 服务：测真实接口那条线；现场没有 BTS 时也能起来联调 |
| `gateway.py` | 入口：`--simulate` 用假 BTS |
| `profile.json` | ILCS 设备接入模板文件（草稿），导入后核对、由另一个人发布 |
| `tests/` | `test_module.py` 对假 BTS 跑 ILCS 接入验收清单（含故障项目）与驱动规则；`test_bts_wire.py` 走 aurora-neware → TCP → 假 BTS，没装 aurora-neware 时跳过 |
| `deploy/windows-service.md` | 在 BTS 机器上注册成服务、第一次动作级验收怎么配 |

```bash
api/.venv/bin/pytest devices/gateway/neware-bts/tests                              # 自测（wire 测试要另装 aurora-neware）
python devices/gateway/neware-bts/gateway.py --simulate --insecure --port 8443      # 本机联调（假 BTS，8 通道）
python devices/gateway/neware-bts/simulator/bts_server.py --port 5502               # 起一个假 BTS 给真实接口连
```

## 一条 ILCS 指令怎么落到柜子上

- **一条指令 = 一个通道上按一个工步文件跑一次测试。** 工步（倍率、截止电压、循环数）在 BTS 里编辑、另存成工步文件，
  登记在配置的 `programs` 里；ILCS 设备方法的「程序」选用哪一个。指令只带通道参数 `channel`（白名单里的序号 1 起，
  或通道号 `21-1-3`），**带别的工艺参数一律拒绝**，不悄悄忽略。
- **通道白名单是安全边界**：网关只在 `channels` 里的通道上启动、停止；柜子上人工在用的通道别列进去。
- **条码认作业**：启动时条码写 `ILCS-<指令号 SHA-256 前 12 位>`，回执里带 `bts_barcode`。查状态、启动没拿到应答后找回、
  终止，都先核对通道上的条码；对不上（通道已经跑了别的测试）就不认、**也不去停它**。
- **状态映射**：`working` → 在跑，`pause`（在 BTS 上人工暂停）→ 保持，`finish` → 完成，`stop` → 失败（被停止），
  `protect` → 失败（保护停机，带 log_code）；其他状态不下结论，网关照报原状态。
- **明确拒绝 vs 结果未知**：启动命令发出之前出的错（通道不空闲、工步文件不在、连不上 BTS、读不到通道状态）是明确拒绝，
  通道没动；发出之后没拿到应答是结果未知，网关按条码去 BTS 找回，绝不重发。
- **实测值**：回执 `delivered` 里有通道、条码、循环号、工步号、工步类型、容量、能量，遥测有电压、电流。数值是 BTS 接口
  报的原值，单位以 BTS 设置为准（aurora-neware 录的样例看是 V、A、Ah、Wh），现场核对后再在 ILCS 侧关联指标。

## 还没做 / 要现场核对的

- [ ] **保持 / 续跑**：BTS 接口有 `continue`、`chl_ctrl` 命令，aurora-neware 还没实现、报文格式没核实，所以契约声明不支持保持。
- [ ] **按 ILCS 程序表生成工步文件**：ILCS 能力可以定义「充放电工步」程序表参数；要把它转成 Neware 工步 XML 还得先拿到
  真实工步文件核对格式。现在只认在 BTS 里做好的工步文件。
- [ ] **曲线数据**：BTS 把 .nda/.ndax 存到 `data_dir`；取数走结果文件接收器（`devices/connectors/result_files`），
  解析 .ndax 可以用 [NewareNDA](https://github.com/d-cogswell/NewareNDA)（BSD-3），接收器现在只认 csv / 键值表。
- [ ] 现场核对：BTS 的 `workstatus` 有没有上面没列的值（如等待、预约）、条码长度与字符限制、`log_code` 的含义、
  `getdevinfo` 里通道后面的 true/false 是什么意思。
- [ ] `profile.json` 是草稿：核对连接参数示例后导入，由另一个人发布。
