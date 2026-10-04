# 在接加热板的电脑上运行

网关跑在离加热板最近的那台电脑上（工控机、手套箱旁的 Windows / Linux 主机），每块板一条串口；ILCS 经 HTTPS 连网关。
计时在网关里：**网关不在，就没人到点停板子**，所以这台机器要稳、服务要开机自启。

## 1. 接线

每块板一条链路，三种接法都行，配置里按位置分别写：

| 接法 | 配置 `link` |
|---|---|
| 板子背面 RS 232（9 针）经 USB 转串口线；或板子的 USB 口（虚拟串口） | `{"kind": "serial", "port": "COM5"}`（Linux：`/dev/serial/by-id/…`） |
| 串口服务器（如 Moxa NPort），RFC 2217 模式 | `{"kind": "serial", "port": "rfc2217://192.168.10.31:4003"}` |
| 串口服务器，TCP 透明转发（Real COM 以外的 TCP Server 模式） | `{"kind": "tcp", "host": "192.168.10.31", "port": 4004}` |

串口参数缺省就是 NAMUR 的 9600 波特、7 数据位、偶校验、1 停止位、无流控，串口服务器那一侧也要设成这样。

- **位置号贴在实物上**：配置里位置 1–4 对应哪块板、哪个瓶位，贴标签。位置弄反了，瓶子会按别的瓶的参数加热。
- **COM 号要固定**：Windows 的 USB 串口重插、换口会变号。设备管理器 → 端口 → 属性 → 高级里给每条线固定 COM 号；
  Linux 用 `/dev/serial/by-id/` 下的名字，网关账号加进 `dialout` 组。
- 板子上 **安全温度旋钮** 拧到工艺需要的上限，配置的 `max_temp_c` 不要高于它（设定值超过它，板子会自己压低，
  网关回读发现后拒绝）。
- `sensor: external` 的位置要插 PT1000 外置探头、探头放进瓶里；没插探头就用 `plate`（报加热盘温度）。

## 2. 安装

1. 装 Python 3.11（64 位），`pip install -r requirements.txt`（pyserial 与 cryptography）。
2. 把仓库里的 `devices/gateway/ilcs_gateway/` 拷到 `C:\ilcs-gateway\sdk\ilcs_gateway\`、本模块目录拷到
   `C:\ilcs-gateway\module\`（Linux 用 `/opt/ilcs-gateway/…`）。
3. 写网关配置 `C:\ilcs-gateway\stirrer.json`（照 `config.example.json`）：`positions` 每块板一项；
   `ambient_c` 写手套箱 / 房间的温度（低于它的温度拒绝，等于它只搅拌不加热）；`programs` 登记 ILCS 设备方法里写的程序；
   `default_program` 给没带设备方法的指令（包括 ILCS 的接入验收）用；`auto_position` 平时是 `false`。
4. 先对着假加热板跑一遍自测：`python -m pytest tests`（模块自测不需要 ILCS；与 ILCS 的一致性测试要设 `ILCS_REPO` 指向 ILCS 仓库根目录，找不到 ILCS 会跳过——交付前要带上 ILCS 跑全）。
5. 只读地问一遍每块板（只发读命令，不会让板子动）：

   ```bat
   python C:\ilcs-gateway\module\gateway.py --config C:\ilcs-gateway\stirrer.json --check
   ```

   每个位置都要回型号和温度；回不上来先查线、COM 号、串口参数。

## 3. 注册成服务

Windows 用 NSSM（开机自启、崩溃自动拉起）。**停服务时给网关留时间把在跑的板停下**（缺省只等 1.5 秒）：

```bat
nssm install ilcs-ika "C:\Python311\python.exe" "C:\ilcs-gateway\module\gateway.py" ^
    --config C:\ilcs-gateway\stirrer.json --secrets C:\ilcs-gateway\secrets --state-dir C:\ilcs-gateway\state ^
    --host-name ika-stirrer-gw.lab.internal
nssm set ilcs-ika AppEnvironmentExtra PYTHONPATH=C:\ilcs-gateway\sdk
nssm set ilcs-ika AppStopMethodConsole 15000
nssm start ilcs-ika
```

Linux 用 systemd：`ExecStart=/usr/bin/python3 /opt/ilcs-gateway/module/gateway.py --config …`，
`Environment=PYTHONPATH=/opt/ilcs-gateway/sdk`，`Restart=always`，`TimeoutStopSec=20`（SIGTERM 后先停板再退出）。

- 防火墙只放行 ILCS 服务器访问 8443；串口服务器只给这台机器访问。
- `state` 目录在本机持久盘上：网关重启后要靠它按指令号回答查询（作业台账 + `runs/` 下每条作业的记录）。
- 首次启动在 `secrets` 目录生成 `<设备编号>.crt` 与 `<设备编号>.token`：证书和令牌放进 ILCS 的凭据目录，
  适配器的 `ca_file`、`credential_ref` 指向它们。

## 4. 网关停止、重启时板子怎样

- **停服务**（SIGTERM / Ctrl-C）：先把还在跑的板停下（关搅拌、关加热、读一次确认），这一步记成失败
  （「网关停止服务」），再退出。最多等 5 秒，等不到确认的，下次启动时接着停。
- **崩溃、断电后重启**：启动时把记录里还没结束的作业的板都停下、判失败（写明计划多久、重启前开始了多久）——**不续时**。
  要更新网关，挑没有作业的时候。
- **网关挂了没起来**：板子会一直按最后的设定值加热搅拌。要兜底就打开看门狗（`watchdog_sec`，模式 2：
  网关这么多秒不喂，设定值回落到 `watchdog_temp_c` / `watchdog_rpm`）——先在现场核对过（见模块 README「还没做」）再开。

## 5. 第一次接真机的动作级验收

ILCS 的验收指令不带设备方法，跑 `default_program`；参数是模板里写的 `temp 40、time 2、rpm 300、position 1`。
几个验收项目一个接一个跑，同一时刻只占位置 1。所以验收前：

- 位置 1 放一个**装水、带搅拌子的验收瓶**，验收期间别人别用这块板；
- 位置 1 的 `max_temp_c` 不低于 40，`ambient_c` 低于 40。

验收通过后不用改配置。生产上的步骤都引用设备方法；位置登记变了（换板、换线、换 COM 号）要重新跑只读级验收。
