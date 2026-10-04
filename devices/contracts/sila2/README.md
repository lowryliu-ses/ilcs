# ILCS 设备接入契约（SiLA 2）

**状态：草案，试点中。** 目标是 ILCS 只经 SiLA 2 接设备，设备驱动放在 ILCS 之外的驱动项目里。服务端实现是驱动宿主
[`devices/host`](../../host/README.md)，客户端是 ILCS 的 `sila2_v1`（`api/app/adapters/drivers/sila2.py`）。

| 文件 | 版本 | 现状 |
|---|---|---|
| [`DeviceInfo.sila.xml`](DeviceInfo.sila.xml) | 1.0 | 驱动宿主实现；ILCS 读身份、状态、驱动与配置摘要 |
| [`PointAccess.sila.xml`](PointAccess.sila.xml) | 1.0 | 驱动宿主实现；ILCS 的点位面板与签名手动写走它 |
| [`TaskExecution.sila.xml`](TaskExecution.sila.xml) | 1.1 | 驱动宿主与 SiLA 模拟设备实现；ILCS 读 `TaskSupport` 的方法目录，按错误标识定结论与类别 |
| [`SimulatorControl.sila.xml`](SimulatorControl.sila.xml) | 1.0 | 只给模拟设备（故障注入），不属于设备契约 |

`api/tests/domain/test_sila_contracts.py` 守着这几份定义：按 SiLA 官方 XSD 解析、核对每个命令声明的错误、用固定版本的
sila2 库（0.14.0）往返收发一遍。

## 一台设备一个 SiLA 服务

- 一个 SiLA 服务器代表一台物理设备。驱动宿主可以在一个进程里托管多台，每台有自己的端口、证书和服务器 UUID
  （持久保存，重新部署不变）。
- 服务器 UUID 标识「设备服务」，`DeviceInfo.Identity` 标识背后的「物理设备」。工位核对的期望设备编号对的是后者。
- 不用 SiLA 自动发现（mDNS 跨 Docker 网络、跨网段不可靠）。ILCS 按配置的主机和端口连，主机要在
  `ILCS_ADAPTER_ALLOWED_HOSTS` 白名单里。

## 谁实现哪些特性

| 设备 | DeviceInfo | PointAccess | TaskExecution |
|---|---|---|---|
| 只读写点位（传感器、只给 ILCS 看数的仪表） | 必须 | 必须 | — |
| 参与自动流程、没有点表（厂家 SDK 设备） | 必须 | — | 必须 |
| 参与自动流程、有点表（PLC） | 必须 | 必须 | 必须 |

ILCS 读 `SiLAService` 的「已实现特性」来判断：有 TaskExecution 才参与自动流程（接指令），有 PointAccess 才有点位面板，
不再连上就要求 TaskExecution。只实现 TaskExecution 1.0 的老服务器（ST-01-A、ST-06 的模拟设备）照常可用：身份读
`TaskExecution.DeviceIdentity`，支持标志用工位登记的。

## 连接与鉴权

- 正式环境必须 TLS。证书由现场私有 CA 签发，SAN 覆盖 ILCS 连接用的主机名；ILCS 的 `ca_file` 指向这个 CA，一个 CA
  管所有设备服务。
- 每次调用都要认证 ILCS：按 SiLA 核心特性 `AuthorizationService` 的 `AccessToken` 元数据带令牌。令牌是预共享的随机串
  （不少于 32 个字符），驱动宿主本地校验，不依赖单独的授权服务；ILCS 用 `credential_ref` 引用令牌文件，令牌原文不进配置。
  DeviceInfo、PointAccess、TaskExecution 的全部命令和属性都受它约束，`SiLAService` 不受（ILCS 先读它判断已实现特性）。
- 不用双向 TLS 的原因：sila2 0.14.0 的服务端不能强制校验客户端证书；令牌走标准元数据，各语言的 SiLA SDK 都支持。
- 令牌缺失或错误：服务端拒绝调用，设备没有动作；ILCS 按明确失败处理，并提示凭据有问题。
- **超时要配得上**：ILCS 每次调用的截止时间（`sila2_v1` 的 `request_timeout_sec`）必须长于驱动宿主答复一次调用的最长
  时间，大约是插件的 `connect_timeout_sec + request_timeout_sec`。ILCS 先超时，就只能按「没有结论」处理：读点位报设备
  无响应，手动写点记「结果未知」——哪怕驱动宿主随后查明根本没写。驱动宿主这边：设备不回话时读点只等一个超时，后面的点
  不再读；写点读不到当前值就报 `DeviceUnreachable`（没写）。

## DeviceInfo（所有设备）

| 属性 | 内容 | 读不到时 |
|---|---|---|
| `Identity` | `DeviceId`（必须从设备读）、`IdentitySource`（device / config）、厂商、型号、序列号、固件、`Simulator` | `DeviceUnreachable`，ILCS 按离线处理 |
| `Status` | `State`（idle / running / held / done / failed / unknown）、`ActiveCommandId`、`Interlock`、`AcceptsCommands`、`ObservedAt` | `DeviceUnreachable`，ILCS 按离线处理 |
| `Driver` | 插件、插件版本、宿主版本、配置版本、配置摘要、`OfflineAfterSeconds` | 从驱动配置给，设备离线也能读 |

- 设备报不出身份时 `IdentitySource` 填 `config`，`DeviceId` 取自驱动配置。ILCS 照常核对，但要提示：这种设备接错了发现不了。
- 任何模拟设备都必须报 `Simulator = true`，包括 ProtoForge 这类第三方模拟器（它们自己不报，在驱动配置里声明）。
  正式环境拒绝接入模拟设备。
- `OfflineAfterSeconds` 是驱动发现设备不见了要多久（例如 PLC 心跳超时），接入验收的失联项至少等这么久。

## 配置摘要与 ILCS 的批准

设备配置（连接、点表、能力映射、状态与故障码）放在驱动项目里改，ILCS 看不到改了什么。`Driver.ConfigDigest` 就是让
ILCS 重新挂上闸门的那把锁。

**怎么算：** `"sha256:" + sha256(规范化 JSON).hexdigest()`。
- 规范化 JSON 是 UTF-8 编码，等价于 `json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`。
- `material` 是决定「连谁、怎么判结论」的全部内容：插件、是否模拟设备、支持标志、按配置登记的设备编号、凭据引用，以及
  去掉 `connect_timeout_sec`、`request_timeout_sec`、`probe_interval_sec`、`acceptance` 的插件配置（这几个键和 ILCS「有在途
  指令也能改」的键一致，只决定等多久）。端口、服务器 UUID、配置版本号是部署信息，不参与。
- 凭据只写引用、不写原文。

**ILCS 侧**：接入验收通过、闸门放开时，把这次验收看到的驱动与摘要记为已批准（验收记录里也存一份，作为上线证据）。执行器
探测时读 `Driver` 比对，报的和上一次不同、又不是已批准的那份，就照配置变更处理：
- 配置版本加一（按旧配置排队的验收、手动写点作废），欠接入验收、停派工，报警；
- 改映射欠只读级；换插件欠动作级（动作级照旧要签名与现场批准）；
- 指令在途期间变了，这条指令转人工核查；
- 不自动验收：有权限的人核对驱动项目里的这次改动，签名「批准驱动配置变更」（批准的是具体的摘要，记下谁、哪个签名），
  批准时排一次只读级验收；验收看到的正是批准的那一份才放开闸门、记为已批准，报警条件复位。没批准时，验收通过了也放不开，
  签名放行也不行。同一份新配置只处理一次。
- ILCS 自己改了连接配置（换了设备服务）时，原来的批准作废，等这次验收重新批准。

**驱动宿主侧**：设备的作业台账里有没结束的作业时，拒绝加载新配置。强行重启换了配置的，重启后这条作业按结果未知回报。

## PointAccess（有点位的设备）

- **点名**是稳定标识（`[A-Za-z_][A-Za-z0-9_]*`），`Label` 是显示名。点位目录 `Points` 从配置给，设备离线也能读。
- **值**（`PointValue`）是限定了类型的 SiLA `Any`：Real、Integer、Boolean、String 四种，都是工程值（已乘比例系数）。
  实现时 `Any` 的类型 XML 必须带 SiLA 命名空间，例如 `<DataType xmlns="http://www.sila-standard.org"><Basic>Real</Basic></DataType>`；
  sila2 0.14.0 按字符串比对 `AllowedTypes`，不带命名空间会被拒收。
- **上下限** `Minimum` / `Maximum` 是最多一个元素的列表，空表示不限。没有用 `MaximalElementCount` 约束：
  sila2 0.14.0 的客户端解不开结构体里带约束的列表。
- **ReadPoints**：`Names` 为空就读全部。读不到的点只在自己那一行写 `Error`、`Quality = bad`，不影响别的点；
  点名没登记报 `UnknownPoint`。
- **WritePoint**：
  - `RequestId` 是 ILCS 那条已签名的点位写入记录号。同一个号重发，回放原结论、不再写；同一个号换了点或值，报 `RequestConflict`。
  - 在驱动宿主里一次做完：核对（已登记、可写、不是控制信号、在范围内、类型对）→ 读当前值 → 写 → 回读 → 按点的容差比较。
  - 控制信号（启动、状态、复位、保持 / 终止、指令号、心跳、故障、就绪、联锁）任何时候都不能经 PointAccess 写，
    配置里写了 writable 也不行；要让设备动作请走 TaskExecution。
  - 设备在运行或保持中报 `DeviceBusy`。
  - ILCS 侧的权限、电子签名、排队和前置检查不变。驱动宿主这里的检查是纵深防御，不是替代。

## TaskExecution 1.1（参与自动流程的设备）

语义不变：按 ILCS 指令号提交、查询、保持、终止。回执 JSON 与 `http_json_v1` 是同一份：`command_id` 回显、`state`、
带时区的 `device_ts`、`quality`、`delivered`、`telemetry`、`error`。

1.1 的变化都向后兼容：
1. `SubmitTask` 声明了 `NotSupported`。1.0 没声明，sila2 会把它改成未定义错误，ILCS 只能按结果未知转人工。
2. 新增 `DeviceUnreachable`：动作之前就连不上设备，设备没动。`QueryTask` 返回它表示「现在问不到」，ILCS 下一轮再问。
   可能已经到达设备的请求不许报这个错。
3. `ContextJson` 增加 `station_id`、`material`，预留 `wells`。设备必须忽略不认识的键。
   矩阵条件的逐孔参数在 `ParametersJson.wells`（`{孔位: {参数: 值}}`，其余参数是缺省值）：设备服务要么自己按孔位执行，要么像驱动宿主的映射插件那样按孔位依次执行；回执的 `delivered.wells[孔位]` 按孔位回报，遥测点带 `well` 与这一孔的 `device_ts`（ILCS 据此关联样本、按各孔的时间记）。
4. `TaskType` 增加 `transfer`（转运指令）。
5. 新增属性 `TaskSupport`，内容如下。它从配置给，设备离线也能读。
   - 能力目录：每项能力的参数 JSON Schema、设备端程序；`*` 只给模拟器用；
   - 保持、终止、查询、去重四个支持标志；
   - 交接方式：`sync` 是提交时就知道接不接；`async` 是提交只是交接（例如写下启动沿），拒绝要之后查询才看得到。

| 上下文字段 | 什么时候带 | 含义 |
|---|---|---|
| `batch_id`、`step_index`、`step_id` | 总是 | 批次与步骤；续跑时设备据此找到被保持的作业 |
| `target_command_id` | 保持、终止、续跑 | 要控制的在途作业 |
| `station_id` | 总是 | ILCS 工位编号 |
| `method` | 步骤引用了设备方法 | `{id, code, version, name, program}`，设备按 `program` 选设备端程序 |
| `material` | 投料步骤 | `{name, unit, param}`：投 `params[param]` 这么多的 `name`；实际用量按这个名字写进 `delivered.materials` |
| `wells` | 预留 | 检测步骤覆盖的孔位 |

长任务仍然是「提交 + 按指令号查询」，不改用 SiLA 的可观察命令：可观察命令的执行编号由服务端生成，ILCS 如果在拿到编号前
崩了，就再也找不回那次执行。

## 错误与 ILCS 的处理

故障类别由 ILCS 按错误标识给，不靠报错文字。现在 `api/app/domain/exceptions.py` 按中文关键词归类，驱动挪到 ILCS 之外以后
不能再依赖它。

| 错误 | 出现在 | 设备动了吗 | ILCS 的结论 | 故障类别 |
|---|---|---|---|---|
| `InvalidParameters`、`DeviceBusy`、`NotSupported` | 提交、保持、终止 | 没动 | 明确失败 | 设备故障 |
| `Interlocked` | 提交 | 没动 | 明确失败 | 安全异常（只转人工） |
| `DeviceUnreachable` | 提交、保持、终止 | 没动 | 明确失败（是否允许自动重试 / 改派待评审） | 通信异常 |
| `DeviceUnreachable` | 查询、`DeviceInfo` | — | 暂时离线，下一轮再问 | 通信异常 |
| `UnknownPoint` … `RequestConflict` | 写点位 | 没写 | 写入记录「没有写」 | — |
| `WriteUnconfirmed` | 写点位 | 可能写了 | 写入记录「结果未知」，现场核对 | — |
| 未定义错误、回执不合规、超时、连接中断 | 任何调用 | 可能动了 | 结果未知，转人工核查 | 通信异常 |
| 令牌缺失或错误 | 任何调用 | 没动 | 明确失败，提示凭据问题 | 系统异常 |

新增定义错误时，同时补进这张表，写明设备动没动。

## 版本与兼容

- 全限定标识只带主版本（`ai.ses/ilcs/DeviceInfo/v1`）。次版本只做向后兼容的增量：新增属性、结构体元素、定义错误、说明。
  不兼容的改动升主版本（v2），过渡期服务端可以同时实现 v1 和 v2。
- ILCS 客户端（阶段 1）按已实现特性取能力：没有 DeviceInfo 时退回 `TaskExecution.DeviceIdentity`；没有 `TaskSupport` 时用工位
  登记的支持标志。

## 待评审

1. 鉴权只用令牌够不够；有没有现场必须双向 TLS。
2. 插件版本变了，是只记审计，还是也要求补验收。
3. 动作前 `DeviceUnreachable`（设备确实没收到）能不能像「指令没离开系统」一样允许自动重试或改派。
4. `Status` 要不要做成可订阅的属性来减少轮询；v1 先轮询。
5. 参数说明用 JSON Schema 的哪个版本、单位注解怎么写（暂定 2020-12、`unit` 字段）。
6. 点位读数要不要带设备时间；现在 `ObservedAt` 是驱动读到的时间。
7. 手动写点「结果未知」之后怎么核对。按同一个 `RequestId` 重发 `WritePoint` 不行：驱动宿主没收到过那一次时，重发会真的
   写下去。考虑加 `QueryWrite(RequestId)`：只查写入台账、不执行。
8. 服务器证书没带 SiLA 规定的服务器 UUID 扩展（OID 1.3.6.1.4.1.58583），sila2 客户端每次连接都警告。一个宿主一张证书
   装不下每台设备各自的 UUID；要带就得每台设备一张、由宿主的 CA 签发。
