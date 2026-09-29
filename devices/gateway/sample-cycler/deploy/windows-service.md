# 在设备旁的 Windows 工控机上运行

厂家 SDK 只有 Windows DLL 时，网关跑在设备旁的工控机上，ILCS 经 HTTPS 连它。

1. 装 Python 3.11（64 位，与厂家 DLL 位数一致），`pip install cryptography`（自签证书用；用现场签发的证书可不装）；
   经 pythonnet 调 .NET DLL 的再装 `pythonnet`。
2. 把仓库里的 `devices/gateway/ilcs_gateway/` 拷到 `C:\ilcs-gateway\sdk\ilcs_gateway\`、本模块目录拷到 `C:\ilcs-gateway\module\`，写一个厂家 SDK 的包装模块（提供 `connect()`，
   返回 `driver/vendor_sdk.py` 里 `VendorSdk` 那组方法），设环境变量 `VENDOR_SDK_MODULE=<包装模块名>`。
3. 先对着模拟接口跑一遍自测：`python -m pytest tests`（需要 ILCS 仓库的 `api/`，设 `ILCS_REPO` 指向仓库根目录）。
4. 用 NSSM 注册成服务（开机自启、崩溃自动拉起）：

   ```bat
   nssm install ilcs-gateway "C:\Python311\python.exe" "C:\ilcs-gateway\module\gateway.py" ^
       --device-id CYC-0231 --secrets C:\ilcs-gateway\secrets --state-dir C:\ilcs-gateway\state ^
       --host-name cycler-gw.lab.internal
   nssm set ilcs-gateway AppEnvironmentExtra PYTHONPATH=C:\ilcs-gateway\sdk VENDOR_SDK_MODULE=vendor_cycler
   nssm start ilcs-gateway
   ```

5. 防火墙只放行 ILCS 服务器访问 8443；`state` 目录在本机持久盘上，不要放临时目录——网关重启后要靠它按指令号回答查询。
6. 首次启动在 `secrets` 目录生成 `<设备编号>.crt` 与 `<设备编号>.token`：证书放进 ILCS 的凭据目录（适配器 `ca_file`），
   令牌同样放进凭据目录，适配器的 `credential_ref` 指向它。

网关不改业务判断，只把 SDK 调用包成 ILCS 契约：去重、台账、查询、回执丢失的处理都在 `devices/gateway/ilcs_gateway` 里。
