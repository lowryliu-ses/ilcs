# 在光谱仪 USB 插着的那台电脑上运行

网关要和光谱仪在同一台电脑上：python-seabreeze 经 USB 直接跟光谱仪说话。ILCS 经 HTTPS 连网关，网关不碰激光
（激光由外部钥匙开关与联锁控制，见 README「激光」）。Windows、Linux 都行；下面两种都写了。

1. 装 Python 3.11（64 位），`pip install -r requirements.txt`（python-seabreeze 带 numpy、pyusb，另有 cryptography）。
2. **USB 驱动 / 权限**：运行 python-seabreeze 自带的 `seabreeze_os_setup`——Linux 上装 udev 规则（要 sudo，装完重新插拔
   光谱仪），Windows 上装 USB 驱动。装过 OceanView 的 Windows 机器上，OceanView 的驱动可能和 seabreeze 的冲突，
   以 python-seabreeze 的安装说明为准。
3. **光谱仪同一时刻只能被一个程序打开**：网关运行时别开 OceanView；要用 OceanView 先停网关服务（网关退出时会放开 USB）。
4. 找序列号：

   ```bash
   python -c "from seabreeze.spectrometers import list_devices; print(list_devices())"
   ```

5. 把仓库里的 `devices/gateway/ilcs_gateway/` 拷到 `<网关目录>/sdk/ilcs_gateway/`、本模块目录拷到 `<网关目录>/module/`。
6. 写网关配置 `raman.json`（照 `config.example.json`）：
   - `spectrometer.serial`：上一步看到的序列号（USB 上只插一台也写上：插错设备就打不开，不会测错仪器）；
   - `laser.wavelength_nm`：激光器标称波长（铭牌或出厂报告）。拉曼位移按它换算，写错整条谱平移；
   - `programs`：键是 ILCS 设备方法里的「程序」，写积分时间预设；`default_program` 设成短积分的那个（接入验收跑它）；
   - `max_repeats`、`max_integration_ms`：和 ILCS 工位能力极限一致（`repeats`，登记了的话还有 `integration_ms`）；
   - `correct_dark_counts`、`correct_nonlinearity`：先关着；确认这台型号支持（有遮光像素、EEPROM 里有非线性系数）再打开，
     不支持时每次采谱都会失败并写明原因。
7. 先对着假光谱仪跑一遍自测：`python -m pytest module/tests`（需要 ILCS 仓库的 `api/`，设 `ILCS_REPO` 指向仓库根目录）。
8. 注册成服务（开机自启、崩溃自动拉起）。

   Windows（NSSM）：

   ```bat
   nssm install ilcs-raman "C:\Python311\python.exe" "C:\ilcs-gateway\module\gateway.py" ^
       --config C:\ilcs-gateway\raman.json --secrets C:\ilcs-gateway\secrets --state-dir C:\ilcs-gateway\state ^
       --host-name raman-gw.lab.internal
   nssm set ilcs-raman AppEnvironmentExtra PYTHONPATH=C:\ilcs-gateway\sdk
   nssm start ilcs-raman
   ```

   Linux（systemd，`/etc/systemd/system/ilcs-raman.service`；服务用户要能打开 USB 设备，按第 2 步的 udev 规则核对）：

   ```ini
   [Unit]
   Description=ILCS 拉曼光谱仪网关（seabreeze）
   After=network-online.target

   [Service]
   User=ilcs-gateway
   Environment=PYTHONPATH=/opt/ilcs-gateway/sdk
   ExecStart=/opt/ilcs-gateway/venv/bin/python /opt/ilcs-gateway/module/gateway.py \
       --config /etc/ilcs-gateway/raman.json --secrets /etc/ilcs-gateway/secrets \
       --state-dir /var/lib/ilcs-gateway --host-name raman-gw.lab.internal
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```

   在容器里跑真机要把 USB 设备映射进去，没试过；直接跑在主机上。
9. 防火墙只放行 ILCS 服务器访问 8443。`state` 目录在本机持久盘上——网关重启后要靠它按指令号回答查询、交出重启前
   采完的谱图。
10. 首次启动在 `secrets` 目录生成 `<设备编号>.crt` 与 `<设备编号>.token`：证书和令牌放进 ILCS 的凭据目录，
    适配器的 `ca_file`、`credential_ref` 指向它们。

## 第一次接真机的动作级验收

ILCS 的验收指令不带设备方法，跑的是配置里的 `default_program`；验收参数是模板里写的 `repeats: 1`。采谱不消耗样品、
光谱仪本身是被动的，所以验收对样品无害；但网关不知道激光开没开：

- 测量位放一个稳定的样品（或标准物），激光按现场规程打开、联锁确认后再跑；
- 激光没开时采到的只是暗谱，网关照样报完成——第一次验收看一眼回报的谱图上有没有拉曼峰。

验收通过后不用改配置。生产上的步骤都引用设备方法，按方法里的程序跑；换了光谱仪（序列号）要重新跑只读级验收。
