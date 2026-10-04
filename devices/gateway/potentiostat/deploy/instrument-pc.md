# 在工作站 USB 插着的那台电脑上运行

网关要和电化学工作站在同一台电脑上：经 USB 虚拟串口跟仪器说 MethodSCRIPT。ILCS 经 HTTPS 连网关。Windows、Linux 都行。

1. 装 Python 3.11（64 位），`pip install -r requirements.txt`（pyserial、cryptography；MethodSCRIPT 协议是 `driver/` 里自己写的，
   不用装 PalmSens 的 SDK 或 .NET）。
2. **串口**：EmStat4 是 USB CDC 设备，Windows 上设备管理器里显示成 `EmStat4 HR (COMx)` / `EmStat4 LR (COMx)`，Linux 上是
   `/dev/ttyACM0` 一类（服务用户要在 `dialout` 组里）；USB 上波特率这些设置不起作用。EmStat Pico 开发板走 FTDI 转串口
   （`USB Serial Port`），230400 波特、XON/XOFF（配置写 `"model": "EmStat Pico"` 时是缺省值）。只用 OEM 模块的 UART 时
   （EmStat4M），921600 波特、建议 `"rtscts": true`。看有哪些口：

   ```bash
   python -m serial.tools.list_ports -v
   ```

3. **串口同一时刻只能被一个程序打开**：网关运行时别开 PSTrace；要用 PSTrace 先停网关服务（网关退出时先终止仪器上的测量、
   再放开串口）。PSTrace 5.6 以上有 Connection viewer（未连接时双击「Not connected」），能看它和仪器之间收发的每一行，
   和网关日志对照着查问题很方便。
4. 把仓库里的 `devices/gateway/ilcs_gateway/` 拷到 `<网关目录>/sdk/ilcs_gateway/`、本模块目录拷到 `<网关目录>/module/`。
5. 写网关配置 `echem.json`（照 `config.example.json`）：
   - `model`：铭牌上的型号（`EmStat4 HR` / `EmStat4 LR` / `EmStat Pico` / `Nexus`）。网关连上后按仪器自报的设备类型核对，
     对不上就不接指令（HR ±6 V、LR ±3 V、Pico −1.7–2.0 V，写错型号会按错的范围核对参数）；
   - `backend.link.port`：上一步看到的 COM 口；
   - `cell`：这个通道上接的电池——`area_cm2`（电极面积，电流换算成 mA/cm²）、`cell_constant_per_cm`（电导池常数）。
     一台工作站换着接电导池和扣电时，把 `cell` 写在各自的程序里；
   - `limits`、`params`：和 ILCS 工位能力极限一致；
   - `programs`：键是 ILCS 设备方法里的「程序」；`default_program` 设成不加电位的开路电位短程序（接入验收跑它）。
6. 先对着假仪器跑一遍自测：`python -m pytest module/tests`（模块自测不需要 ILCS；与 ILCS 的一致性测试要设 `ILCS_REPO` 指向 ILCS 仓库根目录，找不到 ILCS 会跳过——交付前要带上 ILCS 跑全）。
7. 只读地问一遍仪器：

   ```bash
   python module/gateway.py --config echem.json --check
   ```

   打出设备类型（`es4_hr`）、固件、序列号、MethodSCRIPT 版本；`accepts_commands` 是 false 时看 `warning`（多半是型号写错）。
8. 注册成服务（开机自启、崩溃自动拉起）。

   Windows（NSSM）：

   ```bat
   nssm install ilcs-echem "C:\Python311\python.exe" "C:\ilcs-gateway\module\gateway.py" ^
       --config C:\ilcs-gateway\echem.json --secrets C:\ilcs-gateway\secrets --state-dir C:\ilcs-gateway\state ^
       --host-name echem-gw.lab.internal
   nssm set ilcs-echem AppEnvironmentExtra PYTHONPATH=C:\ilcs-gateway\sdk
   nssm start ilcs-echem
   ```

   Linux（systemd，`/etc/systemd/system/ilcs-echem.service`）：

   ```ini
   [Unit]
   Description=ILCS 电化学工作站网关（MethodSCRIPT）
   After=network-online.target

   [Service]
   User=ilcs-gateway
   SupplementaryGroups=dialout
   Environment=PYTHONPATH=/opt/ilcs-gateway/sdk
   ExecStart=/opt/ilcs-gateway/venv/bin/python /opt/ilcs-gateway/module/gateway.py \
       --config /etc/ilcs-gateway/echem.json --secrets /etc/ilcs-gateway/secrets \
       --state-dir /var/lib/ilcs-gateway --host-name echem-gw.lab.internal
   Restart=on-failure
   KillSignal=SIGTERM
   TimeoutStopSec=20

   [Install]
   WantedBy=multi-user.target
   ```

   在容器里跑真机要把 USB 设备映射进去，没试过；直接跑在主机上。
9. 防火墙只放行 ILCS 服务器访问 8443。`state` 目录在本机持久盘上——网关重启后要靠它按指令号回答查询、交出重启前
   测完的结果。
10. 首次启动在 `secrets` 目录生成 `<设备编号>.crt` 与 `<设备编号>.token`：证书和令牌放进 ILCS 的凭据目录，
    适配器的 `ca_file`、`credential_ref` 指向它们。

## 停服务、崩溃、拔线时仪器怎样

MethodSCRIPT 脚本是仪器自己执行的，网关不在了它照样往电池上加电位。所以：

- 停服务（SIGTERM / NSSM stop）：网关先发终止（`Z`），等仪器结束（脚本的 `on_finished:` 断开电池），再放开串口；
- 网关崩了、USB 拔了又插：下次连上仪器时先发 `Z` 停掉还在跑的测量，再做别的。那次测量的数据收不回来，判失败；
  连上之前 ILCS 查它一直是「在测」，不会被当成已经停了；
- 仪器运行时报错（如 `!0032` 电池严重过载）不执行 `on_finished:`：网关另发一个只有 `cell_off` 的脚本；这个也没确认时，
  错误里写明「请到现场核查电池是否断开」。

## 第一次接真机的动作级验收

ILCS 的验收指令不带设备方法，跑的是配置里的 `default_program`（开路电位：电池断开、只测电压，不加电位），验收参数为空。
验收前通道上接一个稳定的东西：电导池（装好电解液）、一颗扣电，或者 PalmSens 的 dummy cell / 一个电阻。

验收通过之后，每个测量程序在已知的样品上各跑一次再投产：

- `EIS-COND`：电导池里装 25 ℃ 的 0.1 mol/L KCl 标准液（12.88 mS/cm）测一次，用 K = σ × R_b 反算电导池常数，写进
  `cell_constant_per_cm`（厂家标的常数常有百分之几的偏差）；
- `LSV-ESW`：一颗新做的 Li | 电解液 | 不锈钢扣电，核对起扫电位就是静置后的开路电位、起始电位和截止电流停得对；
- `CV`、`CA`：看曲线方向与量程（`overload_points` 应为 0）。

换了仪器（序列号）要重新跑只读级验收。
