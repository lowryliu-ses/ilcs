# 设备侧

ILCS 进程之外、设备那一侧的东西都在这里。ILCS 自己的驱动（主动去连设备的一端）不在这里，在
`api/app/adapters/`：框架层（驱动契约、回执解读、作业台账、注册表、驱动目录、接入验收）+ `drivers/`（每种协议一个驱动）。

| 目录 | 是什么 | 谁用 |
|---|---|---|
| [`contracts/`](contracts/) | 设备侧任务契约：设备要实现成什么样 ILCS 才接得上。`sila2/`（SiLA 2 特性）、`opcua/`（节点与方法）、`modbus/`（任务寄存器表） | 驱动与模拟设备读同一份定义；交给设备厂家 |
| [`simulators/`](simulators/README.md) | 外部模拟设备：独立进程、走真实协议，每种驱动都有；`pilot-devices.json` 是试点工位接到它们的连接配置 | `docker compose --profile pilot`、`scripts/configure-pilot-adapters.py`、测试 |
| [`gateway/`](gateway/README.md) | 设备网关：网关 SDK `ilcs_gateway`（厂家只给 SDK / DLL、私有协议的设备，用它包成 `http_json_v1` 网关，只写启动、读状态、停止）+ 设备模块（一台（一类）设备一个交付目录：驱动、假 SDK、`profile.json`、测试、部署文件；样板 `sample-cycler`） | 设备开发者；`scripts/new-device-module.py` 从样板生成新模块；HTTPS 网关模拟设备（`simulators/http_gateway`）也基于这个 SDK |
| [`connectors/`](connectors/result_files/README.md) | 设备侧连接器：`result_files/` 盯住检测软件的导出目录，把结果文件回传 ILCS（只取数，不启动设备） | `docker compose --profile results` |

证书、令牌这类运行时文件不放这里，放仓库根目录的 `secrets/`（不进仓库）：compose 挂进容器的是
`secrets/<sila|opcua|gateway|fleet|simctl>/`，本机直接跑模拟设备时缺省写到 `secrets/local/<类别>/`。

`simulators`、`connectors` 两个 Python 包以本目录为根（`from simulators.common.device import …`），和 `api/` 是
`app` 包的根一样；`gateway/` 是 `ilcs_gateway` 包的根。
