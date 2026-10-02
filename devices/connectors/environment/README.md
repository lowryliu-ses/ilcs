# 环境读数连接器（手套箱水氧、温湿度、露点）

按周期读传感器，经 ILCS 的 `POST /api/runtime/environment` 上报。ILCS 侧的步骤可以声明环境要求，例如电解液线的加料、
开盖、分装步骤：

```json
"environment": [{"metric": "h2o_ppm", "max": 1, "zone": "配液段手套箱"}, {"metric": "o2_ppm", "max": 1, "zone": "配液段手套箱"}]
```

开跑检查与每步投递前按「区域 × 指标」的最新读数核对：没有读数、读数超过 30 min（`ILCS_ENVIRONMENT_MAX_AGE_MIN`）没更新、
超出要求，都挡住投递。本连接器就是把读数送过去的那一端。指标名用 ILCS 认的：`o2_ppm`、`h2o_ppm`、`temperature`、
`humidity`、`dew_point`、`pressure_diff`、`particles`（别的名字也收，只是界面上没有中文名）。

| 文件 | 内容 |
|---|---|
| `poller.py` | 连接器：Modbus TCP、OPC UA、串口 / 网口文本命令三种读法，按区域分批上报 |
| `glovebox_sim.py` | 模拟手套箱：Modbus TCP 上给出氧、水、箱压与状态字，可模拟漏气、传感器故障 |

测试在 `api/tests/api/test_environment_connector.py`（模拟手套箱 → 连接器 → 真实 API → 步骤的水氧要求放行 / 挡住）。

## 读法：和厂家无关，按点配

手套箱控制器（MBraun、米开罗那、Vigor、Etelux、Jacomex……）一般给 Modbus TCP、OPC UA 或文本命令，**地址、节点、命令按
厂家手册或集成商给的点表填**——不同厂家、甚至同厂家不同控制器版本的寄存器表都不一样，这里不预置任何一家的地址。

```json
{
  "ilcs_url": "http://api:8000",
  "source": "glovebox-sensors",
  "secret_file": "/run/secrets/ilcs/environment/secret",
  "poll_sec": 10,
  "sources": [
    {"kind": "modbus", "name": "配液段手套箱", "host": "192.168.10.50", "port": 502, "unit": 1, "readings": [
      {"zone": "配液段手套箱", "metric": "o2_ppm", "unit": "ppm", "table": "input", "address": 0, "type": "float32",
       "word_order": "big", "valid": [0, 1000]},
      {"zone": "配液段手套箱", "metric": "h2o_ppm", "unit": "ppm", "table": "input", "address": 2, "type": "float32",
       "word_order": "big", "valid": [0, 1000]}]},
    {"kind": "opcua", "name": "测试段手套箱", "endpoint": "opc.tcp://192.168.10.51:4840", "readings": [
      {"zone": "测试段手套箱", "metric": "o2_ppm", "unit": "ppm", "node": "ns=2;s=Glovebox.O2", "valid": [0, 1000]}]},
    {"kind": "line", "name": "露点仪", "link": {"kind": "serial", "port": "/dev/ttyUSB0", "baudrate": 9600},
     "readings": [{"zone": "干燥间", "metric": "dew_point", "unit": "℃", "send": "DP?", "pattern": "^(?P<value>[-\\d.]+)"}]}
  ]
}
```

- Modbus：`table` 取 `input`（输入寄存器）或 `holding`（保持寄存器），`address` 是 0 基地址（手册上写 30001 / 40001 的减去
  基数），`type` 取 `float32` / `uint16` / `int16` / `uint32` / `int32`，32 位的值 `word_order` 写 `big`（高位字在前，常见）或
  `little`；整数寄存器常带倍率，用 `scale`（如 0.1）、`offset` 换算成 ppm。
- OPC UA：匿名或用户名口令（`username` + `password_file`），有源时间戳就用它当测量时间。
- 文本命令：发 `send`、按 `pattern` 的命名组 `value` 取数。
- **`valid` 一定要写**：传感器故障时很多控制器给的是故障码（-9999、65535）或负数。超出合理范围的读数丢掉、记日志，**不上报**；
  读不到的点同样不上报。宁可让 ILCS 判读数过期、挡住投递，也不能报一个错的数让它放行。

## ILCS 侧

1. 「系统治理 · 服务身份」签发一个服务身份，**授权环境区域**填这几个区域名（或勾「全部区域」），密钥写到 `secret_file`；
2. 区域名与步骤环境要求里的 `zone` 一致（不写 `zone` 的要求按工位所在区域找）；
3. 起连接器：Compose 里是 `environment` 服务（`--profile environment`），配置放 `secrets/environment/environment.json`、
   密钥放 `secrets/environment/secret`；或在手套箱旁的工控机上直接跑 `python poller.py --config …`。

```bash
python devices/connectors/environment/poller.py --config environment.json --once   # 读一轮、打印结果（排障）
python devices/connectors/environment/glovebox_sim.py --port 5021                    # 起一个模拟手套箱
```

## 还没做 / 要现场核对的

- [ ] 各家手套箱控制器的点表：向厂家 / 集成商要 Modbus 寄存器表或 OPC UA 节点，按上面的写法填，现场对着控制器面板核对读数。
- [ ] 传感器自身的状态（再生中、传感器故障位）：有状态字的，作为单独的读数上报（如 `metric: "glovebox_status"`），
      或者在 `valid` 之外再按状态位决定不报——需要时再加。
- [ ] 读数时间：没有源时间戳时取连接器读到的时刻；连接器所在机器要对时（ILCS 拒收超前 5 分钟以上的读数）。
