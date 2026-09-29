# 设备模块

一台（一类）设备怎么接进 ILCS，打成一个目录交付：真实接口、模拟接口、ILCS 侧登记、测试与部署文件放在一起，
设备开发者不用碰 ILCS 代码、不用 ILCS 数据库就能独立调试，交付物进 ILCS 不改代码、不重启。

```
devices/modules/<厂家-型号>/
  driver/        真实接口：调厂家 SDK 或协议，只写启动、读状态、停止（能用映射驱动的设备没有这一层）
  simulator/     模拟接口：和厂家 SDK 同一组方法的「假 SDK」，带故障注入（丢回执、忙、联锁、失联）
  gateway.py     入口：--simulate 用模拟接口，否则连真实的厂家 SDK
  profile.json   ILCS 侧登记：设备接入模板文件（ilcs-device-template/1），在「工位与接入 → 接入模板」导入
  tests/         对模拟接口跑 ILCS 的接入验收清单，CI 里必须全过
  deploy/        Dockerfile、compose 片段、Windows 服务安装说明
  README.md
```

样板：[sample-cycler](sample-cycler/)（8 通道充放电柜，厂家只给 Windows SDK）。

## 两种模块

| 设备给的接口 | 模块里有什么 | ILCS 侧驱动 |
|---|---|---|
| 串口 / TCP 文本命令、Modbus 点表、OPC UA 节点、REST、中间库、天平 | 只有 `profile.json`（映射配置）+ 模拟设备 + 测试，不写代码 | 现有映射驱动（`line_command_v1` 等） |
| 厂家 SDK / DLL、私有协议、逻辑复杂 | `driver/` + `simulator/` + `gateway.py`，基于 `devices/sdk/ilcs_gateway` | `http_json_v1`（本模块起的网关） |

第二种的网关是一个独立服务：挂了只影响这一台（ILCS 判它失联、进待命列表），其他工位照常；升级只重启这个服务，ILCS 不动。

## 开发与自测（不需要 ILCS 数据库）

```bash
# 新建一个模块（从样板复制并替换名称、型号、能力与参数），测试开箱就能过
python scripts/new-device-module.py acme-vd80 --title "ACME 真空干燥箱" --model VD-80 --vendor ACME \
    --capability cap.vacuum_dry --param temp=60:180 --param vacuum=0.1:5

# 对模拟接口跑 ILCS 的接入验收清单（含故障项目）
api/.venv/bin/pytest devices/modules/acme-vd80/tests

# 手工联调：起模拟网关，另开一个终端用 ILCS 的验收命令对着它跑
python devices/modules/acme-vd80/gateway.py --simulate --insecure --port 8443
python scripts/device-acceptance.py --adapter my-adapter.json --allow-host --physical --faults
```

把 `driver/` 里对假 SDK 的调用换成真实 SDK 时，测试仍然对着模拟接口跑——真实接口与模拟接口实现同一组方法，
驱动代码只有一份。

## 交付与上线

1. **交付物**：模块目录（含 `tests/` 全过的记录）+ `profile.json`。
2. **ILCS 侧导入**：「工位与接入 → 接入模板」导入 `profile.json`，成草稿；核对后由另一个人签名发布
   （起草人不能发布本人起草的模板）。文件摘要对不上（导出后被改过）会被拒绝。
3. **部署网关**（第二种模块）：按 `deploy/` 起服务；证书与令牌放进 ILCS 的凭据目录；主机在 ILCS 的设备白名单网段里就不用改配置。
4. **工位套用模板**：「设备连接」里选模板、填这台设备的连接参数（地址、证书、设备编号），签名保存。
5. **接入验收**：保存后自动跑只读级；第一次接真实设备还要动作级（签名 + 现场批准人，DEC-02）。通过了工位才接指令，
   报告存档，带驱动、固件、配置与模板版本。

模板出新修订时不会自动推给工位：「接入模板」页签列出还在用旧修订的工位，逐台切换、重新验收。

## 模块必须守的规矩

由 `devices/sdk/ilcs_gateway` 保证、测试会查：

- 同一 ILCS 指令号重复提交只动作一次，回放原结论；
- 先落盘再动设备：网关任何时刻重启，都能按原指令号回答查询；
- 回执丢了宁可不回，不编造结论；不知道设备动没动就报结果未知；
- 设备明确拒绝（参数非法、不支持、联锁、忙）时设备确实没动；
- 模拟接口自报 `simulator: true`（正式环境会拒绝接入）。

驱动自己要守的：参数范围与 ILCS 工位能力极限一致；急停 / 联锁如实上报；厂家 SDK 的「不确定」错误不要改判成明确失败。
