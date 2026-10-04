# ILCS 仓库约定

## 以什么为准

- 现状以代码为准，其次是 `README.md`、`ARCHITECTURE.md` 与 `docs/` 下的现行文档（索引见 `docs/README.md`）。
- `docs/archive/` 是历史需求、评审与验证证据，只作「当时为什么这样定」的背景，**不能当作现状引用**。
  和代码冲突时以代码为准；回答「某能力做到哪了」要查代码，不要引用差距梳理、评审里的旧结论。

## 目录

- `api/`：服务端（`app/adapters/` 是设备驱动：框架层 + `drivers/`）；`executor/`：执行器；`web/`：前端；`scripts/`：运维脚本。
- 设备那一侧（驱动宿主、契约、外部模拟设备、网关 SDK、设备模块、连接器）在独立仓库 `ilcs-devices`，和本仓库并排放（本机 `../ilcs-devices`）；测试与脚本按环境变量 `ILCS_DEVICES` 找它，见它的 `README.md`。
- `data/`、`secrets/`：运行状态（上传文件、执行器作业台账、备份；设备证书与令牌），不进仓库，部署同步时排除，不要删。

## 批量改文件

文件名大量是中文。拿 `git ls-files` 的清单批量处理时要用 `git -c core.quotepath=off ls-files -z`，
否则中文路径被转义成 `"docs/\346..."`，这些文件会被悄悄跳过。
