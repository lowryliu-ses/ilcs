# 样板设备模块：8 通道充放电柜（厂家 SDK 接口服务）

厂家只给了 Windows SDK 的充放电柜：本模块把 SDK 调用包成 ILCS 的网关契约，ILCS 侧用 `http_json_v1` 接入。
对应试点工位 ST-07（能力 `cap.test`，参数倍率 `rate` 0.01–10 C、充电截止电压 `vmax` 2.0–5.0 V）。

| 文件 | 内容 |
|---|---|
| `driver/vendor_sdk.py` | 厂家 SDK 的调用面（`VendorSdk`）与 `load_sdk()`：接真机只改这里 |
| `driver/device.py` | 真实接口：把 SDK 调用映射成 `ilcs_gateway.Device`（参数范围、程序、通道、急停、状态映射） |
| `simulator/fake_sdk.py` | 模拟接口：同一组方法的假 SDK，按时长完成、可注入故障 |
| `gateway.py` | 入口：`--simulate` 用模拟接口 |
| `profile.json` | ILCS 设备接入模板文件，导入后发布、套用到工位 |
| `tests/test_module.py` | 对模拟接口跑 ILCS 接入验收清单（含故障项目）、网关重启后按指令号查回、驱动的拒绝规则 |
| `deploy/` | Dockerfile、compose 片段、Windows 服务说明 |

```bash
api/.venv/bin/pytest devices/gateway/sample-cycler/tests          # 自测
python devices/gateway/sample-cycler/gateway.py --simulate --insecure --port 8443   # 本机联调
```

`--insecure` 是明文 HTTP，缺省只监听 127.0.0.1；现场一律 HTTPS + 令牌（不带令牌的 HTTPS 网关起不来）。

接真机：写一个厂家 SDK 的包装模块（提供 `connect()`，返回 `VendorSdk` 那组方法），设 `VENDOR_SDK_MODULE`，
去掉 `--simulate`；按 `deploy/windows-service.md` 注册成服务。测试照旧对着模拟接口跑。
