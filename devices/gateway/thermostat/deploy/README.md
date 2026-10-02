# 在接冷水机的电脑上运行

网关跑在离冷水机最近的那台电脑上（工控机、手套箱旁的 Windows / Linux 主机）；ILCS 经 HTTPS 连网关。
计时、到温判断、到点停板都在网关里：**网关不在，就没人停搅拌板、没人把冷浴改回待机温度**，所以这台机器要稳、服务要开机自启。

## 1. 接线

冷水机一条链路、每块搅拌板一条链路，配置里分别写。

| 冷水机 | 串口（`{"kind": "serial", "port": "COM3"}`） | 网口（`{"kind": "tcp", "host": …}`） | 冷水机上要设的 |
|---|---|---|---|
| Huber（Pilot ONE、CC-Pilot、Unistat Control） | RS232 9600 8N1、无握手（缺省） | Pilot ONE 网口，TCP 端口 8101（缺省） | 无（PB 命令点对点；RS485 总线挂多台要用 LAI 命令，本模块不支持） |
| Julabo | RS232 4800 7E1、**硬件握手 RTS/CTS**（缺省；新机型菜单里可改 9600，改了 link 里照写） | 有网口的机型照菜单写端口（`port` 必填） | **面板切到远程控制**（老机型开机时按住两个键切 `IOn`，显示 `rOFF` 才接受远程命令）；手册写大写命令的机型配 `"uppercase": true` |
| LAUDA（ECO、Variocool、PRO、Proline、Integral、LOOP） | RS232 9600 8N1（缺省），可带可不带 RTS/CTS | 网口模块（LRZ 921 等），TCP 端口 54321（出厂设置，缺省） | 接口模块的波特率 / IP 和 link 一致 |

- 串口服务器（如 Moxa NPort）：RFC 2217 模式写 `{"kind": "serial", "port": "rfc2217://192.168.10.31:4003"}`；
  TCP 透明转发写 `{"kind": "tcp", "host": "192.168.10.31", "port": 4004}`，串口服务器那一侧的串口参数要和冷水机一致。
- 搅拌板（IKA NAMUR）：9600 7E1、无流控（缺省），接法同 ika-stirrer；板子放在冷块上，**位置号贴在实物上**。
- **COM 号要固定**：Windows 的 USB 串口重插会变号，设备管理器里给每条线固定 COM 号；Linux 用 `/dev/serial/by-id/…`，
  网关账号加进 `dialout` 组。
- 冷水机的导热液、管路、冷块按工艺温度准备好；配置的 `min_c` / `max_c` 不能超出冷水机和导热液的范围
  （Huber 的设定值上下限 0x30 / 0x31 读得到时网关还会再核一次）。

## 2. 安装

1. 装 Python 3.11（64 位），`pip install -r requirements.txt`（pyserial 与 cryptography；只走网口时可以不装 pyserial）。
2. 把仓库里的 `devices/gateway/ilcs_gateway/` 拷到 `C:\ilcs-gateway\sdk\ilcs_gateway\`、本模块目录拷到
   `C:\ilcs-gateway\module\`（Linux 用 `/opt/ilcs-gateway/…`）。
3. 写网关配置 `C:\ilcs-gateway\thermostat.json`（照 `config.example.json`）：`chiller.kind` 选厂家、`link` 写线；
   `min_c` / `max_c` 写这一站接受的温度范围；`settle_sec`、`tolerance_c` 按工艺定；`after` 选作业之后冷水机怎样；
   `programs` 登记 ILCS 设备方法里写的程序；`default_program` 给没带设备方法的指令（包括 ILCS 的接入验收）用。
4. 先对着假设备跑一遍自测：`python -m pytest tests`（需要 ILCS 仓库的 `api/`，设 `ILCS_REPO` 指向仓库根目录）。
5. 只读地问一遍冷水机和每块板（只发读命令，不会让设备动）：

   ```bat
   python C:\ilcs-gateway\module\gateway.py --config C:\ilcs-gateway\thermostat.json --check
   ```

   冷水机要回浴温、设定值、启停；Julabo 要显示远程控制（不是「面板控制模式」）；有报警先在现场处理。

## 3. 注册成服务

Windows 用 NSSM（开机自启、崩溃自动拉起）。**停服务时给网关留时间把在跑的作业停下**：

```bat
nssm install ilcs-chill "C:\Python311\python.exe" "C:\ilcs-gateway\module\gateway.py" ^
    --config C:\ilcs-gateway\thermostat.json --secrets C:\ilcs-gateway\secrets --state-dir C:\ilcs-gateway\state ^
    --host-name thermostat-gw.lab.internal
nssm set ilcs-chill AppEnvironmentExtra PYTHONPATH=C:\ilcs-gateway\sdk
nssm set ilcs-chill AppStopMethodConsole 15000
nssm start ilcs-chill
```

Linux 用 systemd：`ExecStart=/usr/bin/python3 /opt/ilcs-gateway/module/gateway.py --config …`，
`Environment=PYTHONPATH=/opt/ilcs-gateway/sdk`，`Restart=always`，`TimeoutStopSec=20`。

- 防火墙只放行 ILCS 服务器访问 8443；串口服务器、冷水机网口只给这台机器访问。
- `state` 目录在本机持久盘上：网关重启后靠它按指令号回答查询（作业台账 + `runs/` 下每条作业的记录）。
- 首次启动在 `secrets` 目录生成 `<设备编号>.crt` 与 `<设备编号>.token`：放进 ILCS 的凭据目录，适配器的
  `ca_file`、`credential_ref` 指向它们。

## 4. 网关停止、重启时设备怎样

- **停服务**（SIGTERM / Ctrl-C）：先把在跑的作业停下（停板、按 `after` 处理冷水机），这一步记成失败（「网关停止服务」），
  再退出。最多等 5 秒，等不到确认的，下次启动时接着做。
- **崩溃、断电后重启**：启动时把记录里还没结束的作业停下、判失败（写明计划、开始了多久）——**不续做**。
- **网关挂了没起来**：冷水机照最后的设定值一直控温，搅拌板一直转。冷水机自己的看门狗（Huber 0x40、Julabo / LAUDA
  的接口超时）本模块没用，要兜底先在现场核对行为再加。
- **冷水机断电**：Huber、Julabo 用数据命令改的设定值不存盘，来电后回到面板上的值（Julabo 远程模式下来电不自动启动）；
  网关读到「重启过 / 停了 / 设定值变了」就把在跑的作业判失败。

## 5. 第一次接真机的动作级验收

ILCS 的验收指令不带设备方法，跑 `default_program`；参数是模板里写的 `temp 20、time 1`（`cap.thermostat`）。
几个验收项目一个接一个跑，每项都要到温（连续 `settle_sec` 秒在容差里）再保温 1 秒。所以验收前：

- 冷浴在面板上先调到 20 ℃ 附近、等它稳住，验收期间别人别动冷水机；`min_c` ≤ 20 ≤ `max_c`；
- ILCS 每个动作项目最多等 `ILCS_ACCEPTANCE_POLL_TIMEOUT_SEC`（缺省 180 秒）：要比「到温 + settle_sec + 1 秒」长，
  `settle_sec` 很长或冷浴离 20 ℃ 远时先调大它；
- 制冷搅拌（`cap.ely.stir`）不在模板的验收里：接好板以后，用一个装水、带搅拌子的验收瓶，在 ILCS 里手动下一条
  `temp 20、time 5、rpm 300、position 1` 的指令看一遍（板子到温后才转、5 秒后停）。

验收通过后不用改配置。冷水机、搅拌板换了线、换了 COM 号要重新跑只读级验收。
