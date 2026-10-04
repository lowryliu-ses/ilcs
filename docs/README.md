# 文档

现状以代码为准，其次是仓库根目录的 [README](../README.md) 与 [ARCHITECTURE](../ARCHITECTURE.md)。本目录分两类：

## 现行文档（跟着代码维护）

| 文档 | 内容 |
|---|---|
| [设备适配器配置模板](设备适配器配置模板.md) | 选哪种驱动、每种驱动的配置项、设备接入模板、接入验收、设备模块 |
| [操作案例](操作案例.md) | 四个端到端演示案例与故障演练；`scripts/load-demo-cases.py` 按它导入 |
| [电解液配液线](电解液配液线.md) | C 公司电解液产线（模拟阶段）：工位与能力、配液模板的生成规则、导入配方表、`scripts/load-electrolyte-line.py`、占位项与接真机前要替换的东西 |
| [ProtoForge 联调全流程](ProtoForge联调全流程.md) | 本机 ProtoForge 三台设备（经驱动宿主）按多温度矩阵逐样本设定温度，跑通 SOP → 流程 → 方案 → 任务 → 排程 → 批次执行 → 复核 → 报告；`scripts/load-protoforge-flow.py` |
| [验收记录](acceptance-record.md) | 验收用例 AC-01 至 AC-40 与自动化测试的对应关系、只有手工证据的项 |
| [SOP 模板](SOP模板.md) | 受控 SOP 起草模板（SOP 页面有链接） |
| [上线前输入与授权清单](上线前输入与授权清单.md) | 正式上线前要由现场填写的事实与授权 |

## 归档（[archive/](archive/)，只作背景）

当时的需求、方案、评审与验证证据，每份开头标了日期。结论可能已经过时（例如差距梳理里列的缺口不少已经补上），
**不能当作现状引用**；要知道现在怎样，查代码。

| 类别 | 文档 |
|---|---|
| 需求与方案 | [实验室平台开发需求文档](archive/实验室平台开发需求文档.md)、[实验室平台修订方案](archive/实验室平台修订方案.md)（2026-09-21） |
| 对照与评审 | [PRD 对照差距梳理](archive/PRD对照差距梳理-2026-09-24.md)、[MADSci 对照评审](archive/MADSci对照评审-2026-09-28.md)、[核心链路评审](archive/reviews/)（三轮与整改方案，2026-09-26 至 09-28） |
| 设计记录 | [设备接入插件化](archive/设备接入插件化-2026-09-29.md) |
| 验证证据 | [Compose 本地部署验证](archive/compose-validation-local-2026-09-22.md)、[恢复演练](archive/recovery-drill-local-2026-09-22.md)、[目标机探测](archive/target-host-probe-2026-09-22.md)、[历史数据迁移报告](archive/migration-report.md)、[驱动宿主本机试点](archive/驱动宿主本机试点-2026-10-03.md)（2026-10-03） |
