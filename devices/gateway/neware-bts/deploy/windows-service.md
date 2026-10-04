# 在 BTS 那台 Windows 机器上运行

网关要和 BTS 服务端在同一台机器上：启动命令里带的是工步文件的路径，由 BTS 去读；aurora-neware 发命令前
也会在本机核对文件在不在。ILCS 经 HTTPS 连网关，网关经本机 TCP（缺省 502）连 BTS。

1. **BTS 打开 API**：BTS 8.0「帮助 → 模式设置」里启用 API（可能要向 Neware 要激活码）。
2. 装 Python 3.11（64 位），`pip install -r requirements.txt`（aurora-neware 与 cryptography）。
3. 把仓库里的 `devices/gateway/ilcs_gateway/` 拷到 `C:\ilcs-gateway\sdk\ilcs_gateway\`、本模块目录拷到
   `C:\ilcs-gateway\module\`。
4. 写网关配置 `C:\ilcs-gateway\neware.json`（照 `config.example.json`）：
   - `channels`：ILCS 能用的通道（设备号-子设备号-通道号，BTS 里看，或 `neware status`）。**只列交给 ILCS 的通道**，
     人工在用的别列：网关不碰白名单外的通道；
   - `programs`：工步在 BTS 里编辑好、另存为工步文件，键是 ILCS 设备方法里的「程序」，`file` 是这台机器上的路径；
   - `data_dir`：BTS 把数据文件存到哪（结果文件接收器盯这个目录取数）；
   - `default_program`：没带设备方法的指令（包括 ILCS 的接入验收）跑哪个工步，设成一个很短、对电芯无害的（见下文）；
   - `auto_channel` 平时是 `false`：电池装在哪个通道由 ILCS 指定。
5. 先对着假 BTS 跑一遍自测：`python -m pytest tests`（模块自测不需要 ILCS；与 ILCS 的一致性测试要设 `ILCS_REPO` 指向 ILCS 仓库根目录，找不到 ILCS 会跳过——交付前要带上 ILCS 跑全）。
6. 用 NSSM 注册成服务（开机自启、崩溃自动拉起）：

   ```bat
   nssm install ilcs-neware "C:\Python311\python.exe" "C:\ilcs-gateway\module\gateway.py" ^
       --config C:\ilcs-gateway\neware.json --secrets C:\ilcs-gateway\secrets --state-dir C:\ilcs-gateway\state ^
       --host-name neware-gw.lab.internal
   nssm set ilcs-neware AppEnvironmentExtra PYTHONPATH=C:\ilcs-gateway\sdk
   nssm start ilcs-neware
   ```

7. 防火墙只放行 ILCS 服务器访问 8443；502 只给本机。`state` 目录在本机持久盘上——网关重启后要靠它按指令号回答查询。
8. 首次启动在 `secrets` 目录生成 `<设备编号>.crt` 与 `<设备编号>.token`：证书和令牌放进 ILCS 的凭据目录，
   适配器的 `ca_file`、`credential_ref` 指向它们。

## 第一次接真机的动作级验收

ILCS 的验收指令不带设备方法，跑的是配置里的 `default_program`；验收参数是模板里写的 `channel: 1`（白名单第一个通道）。
几个验收项目一个接一个跑，同一时刻只占这一个通道。所以验收前：

- `default_program` 指向一个很短、对电芯无害的工步（例如静置 10 秒，样例配置里的 `REST-10S`）；
- 白名单第一个通道放**假电池或空位**，验收期间别人别用它。

验收通过后不用改配置。生产上的步骤都引用设备方法，按方法里的程序跑；白名单变了要重新跑只读级验收。
