# 驱动宿主（devices/host）

驱动项目的第一块：一个进程托管同一网段的多台设备，**每台设备一个 SiLA 2 服务**（自己的端口与服务器 UUID）。
ILCS 用 `sila2_v1` 连它，不再自己连 PLC、仪表。契约见 [contracts/sila2/README.md](../contracts/sila2/README.md)。

| 一台设备实现什么 | 条件 |
|---|---|
| `DeviceInfo`（身份、状态、驱动与配置摘要） | 都有 |
| `PointAccess`（点位目录、批量读、带去重与回读的写点） | 插件配置里登记了 `points` |
| `TaskExecution`（按 ILCS 指令号提交、查询、保持、终止） | 插件配置里配了 `capabilities`（能力映射与状态） |
| `AuthorizationService`（每次调用带令牌） | `host.json` 配了 `tokens_file`（正式环境必须） |

## 协议插件

`ilcs_host/plugins/` 是从 ILCS 的 `api/app/adapters/drivers` 抽出来的映射驱动，**配置写法与 ILCS 里的一模一样**：
ILCS 工位上现成的 `modbus_map_v1` / `opcua_map_v1` / `rest_map_v1` 配置原样放进设备文件的 `config` 就能用。

| 插件 | 设备 |
|---|---|
| `modbus_map` | 有自己寄存器表的 PLC、温控仪表（含串口转以太网后的 RTU 设备） |
| `opcua_map` | 已有 OPC UA 服务器、节点是厂家自己的 PLC / 视觉系统 |
| `rest_map` | 设备或调度系统自有的 REST 接口 |

ILCS 矩阵条件的逐孔参数（`ParametersJson` 里的 `wells`）由插件按孔位**依次执行**、回执按孔位回报，写法见
[设备适配器配置模板](../../docs/设备适配器配置模板.md)「逐孔依次执行」。

和 ILCS 那份只差两处：配置项读驱动宿主的 `settings`；明确失败带 SiLA 错误码（`AdapterError.code`），驱动宿主按它报定义
错误，ILCS 按错误标识定故障类别，不靠报错文字。迁移期间 ILCS 里那几份冻结：只修 bug，修了两边一起改。

## 现场配置

```
sites/<现场>/
  host.json            宿主：运行环境、监听地址、主机白名单、凭据目录、台账目录、令牌文件、TLS
  devices/<设备>.json   一台设备一个文件，文件名就是设备键
```

`host.json`：

| 键 | 说明 |
|---|---|
| `environment` | development / test / production。正式环境必须配 TLS 与令牌，不接模拟设备，白名单不许 `*` 与过宽网段 |
| `address` | 监听地址，缺省 0.0.0.0 |
| `allowed_hosts` | 设备主机白名单，写法与 ILCS 的 `ILCS_ADAPTER_ALLOWED_HOSTS` 一样（主机名、IP、网段、`.域名后缀`） |
| `credential_root` | 设备凭据（OPC UA 客户端证书、REST 令牌）必须放在这个目录里 |
| `state_dir` | 作业台账、点位写入台账、配置摘要记录，**必须在持久卷上** |
| `tokens_file` | 接受的令牌，一行一个，`#` 开头是注释，每个不少于 32 个字符 |
| `certificate` / `private_key` | TLS 证书与私钥。都不配就不加密（只限非正式环境）；非正式环境文件不存在时生成自签证书 |
| `host_name` | 自签证书写进 SAN 的主机名，要和 ILCS 连接用的一致 |

`devices/<设备>.json`：

| 键 | 说明 |
|---|---|
| `plugin` | `modbus_map` / `opcua_map` / `rest_map` |
| `port` | 这台设备的 SiLA 服务端口，同一宿主内不能重复 |
| `config` | 插件配置（ILCS 适配器配置的写法） |
| `supports` | `{hold, abort, query, dedup}`，缺省只有查询与去重 |
| `simulator` | 模拟设备（含 ProtoForge 这类第三方模拟器）写 true；正式环境拒绝启动 |
| `device_id` | 设备报不出身份时按配置登记的编号（`DeviceInfo.Identity.IdentitySource` 报 config）。插件配置里映射了设备编号的，以设备为准：读出来是空就报缺失，不拿它顶替 |
| `credential_ref` | 设备凭据引用（env:// 或凭据目录内的 file://），原文不进配置 |
| `config_version` | 给人看的配置版本（例如驱动项目的提交号） |
| `server_uuid` | 可选；缺省按设备键生成，重新部署不变 |

**配置摘要**（`DeviceInfo.Driver.ConfigDigest`）：插件、是否模拟设备、支持标志、按配置登记的设备编号、凭据引用，加上去掉
`connect_timeout_sec` / `request_timeout_sec` / `probe_interval_sec` / `acceptance` 的插件配置，按规范化 JSON 算 sha256。
端口、服务器 UUID、配置版本号不参与。`python -m ilcs_host --site <现场> --check` 打印每台设备的摘要。

**超时**：插件配置里的 `connect_timeout_sec` / `request_timeout_sec` 是驱动宿主对设备的超时。设备不回话（进程卡死、
断网、容器被暂停）时，读点只等一个超时、后面的点不再读；写点读不到当前值就报 `DeviceUnreachable`（没写）。所以一次调用
最长大约 `connect_timeout_sec + request_timeout_sec`，ILCS 那边 `sila2_v1` 的 `request_timeout_sec` 要比它长，否则 ILCS 先放弃，
没写成的手动写入也只能记「结果未知」。

**在途作业时不许换配置**：设备的作业台账里有没结束的作业时，摘要变了的配置拒绝启动这台设备（等作业结束，或现场核对后
处理掉台账里的那条作业）。只改超时不算换配置。

## ILCS 这一侧

工位的设备连接选 `sila2_v1`：

```json
{"host": "driver-host", "port": 50201, "ca_file": "/run/secrets/ilcs/host/driver-host.crt",
 "expected_device_id": "PF-MB-PLC-01", "request_timeout_sec": 10, "probe_interval_sec": 10}
```

- 只读写点位的设备（驱动宿主上没有 `TaskExecution`）加 `"tasks": false`：只欠只读级接入验收，下发一律明确拒绝。
- `credential_ref` 指向给 ILCS 的令牌文件（如 `file:///run/secrets/ilcs/host/ilcs.token`，内容就是一个令牌）。
- 点位面板、签名手动写和映射驱动一样用；写入记录号作为 `RequestId` 发给驱动宿主，重发只回放原结论。
- **驱动配置闸门**：接入验收通过时，ILCS 把这次验收看到的驱动与配置摘要记为已批准。之后执行器探测读到的摘要变了，
  就照配置变更处理：配置版本加一、欠验收（改映射欠只读级，换插件欠动作级）、停派工、报警，在途指令转人工核查。
  有权限的人在「设备连接」核对后签名批准这次变更，执行器跑只读级验收，验收看到的正是批准的那一份才放行。

## 运行

```bash
# 本机（用 ILCS 的虚拟环境，依赖相同）
cd devices/host && ../../api/.venv/bin/python -m ilcs_host --site sites/<现场>

# 镜像（从仓库根目录）
docker build -f devices/host/deploy/Dockerfile -t ilcs-driver-host:latest .
docker compose -f devices/host/deploy/compose.yml up -d
```

令牌：`python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成一个，写进 `secrets/host/tokens.txt`（驱动宿主）
和 `secrets/host/ilcs.token`（ILCS 的 credential_ref）。

## 测试

```bash
api/.venv/bin/python -m pytest devices/host/tests
```

在本进程里拉起外部 PLC 模拟设备（真实走 Modbus TCP / OPC UA），起宿主，用 SiLA 客户端带令牌调用：三组特性、错误码、
令牌、设备忙、动作前连不上、重启后按台账查回不重发、在途作业时不许换配置、只读写点位的设备。ILCS 的
`api/tests/domain/test_driver_host.py` 会跑这一套，并从 ILCS 的 `sila2_v1` 经驱动宿主驱动 PLC；
`api/tests/api/test_driver_host_points.py` 走 API 验证点位面板、签名手动写与驱动配置闸门。

## 本机 ProtoForge 试点（`sites/protoforge`）

| 设备 | 插件 | SiLA 端口 | 说明 |
|---|---|---|---|
| `PF-MB-PLC` | modbus_map | 50201 | 从站 2 的握手 PLC：点位 + 任务（`cap.plc_run`），配置照抄 ILCS 工位 ST-PF-MB |
| `PF-OPCUA` | opcua_map | 50202 | OPC UA 压力传感器：只读写点位，`setpoint` 可写 0–10 bar |
| `PF-HTTP` | rest_map | 50203 | HTTP REST 传感器（接口前缀 `/api/v1`）：读温度、湿度、气压、状态，湿度可写 0–100。它的 8080 只在 ProtoForge 自己的网络里：先 `docker network connect ilcs_backend protoforge` |

ProtoForge 的 OPC UA、HTTP 设备做不了会动作的 PLC（它的规则引擎只接 Modbus 写入），所以任务执行只用握手 PLC 验证。
