执行器入口为 `main.py`。它从业务库中的持久化指令队列取指令，按工位配置调用模拟适配器、
`http_json_v1` 真实 HTTPS 网关驱动或 `sila2_v1` 驱动，异步轮询长任务并写检查点；真实设备遥测不会用设定值补造。

默认按工位并发（`app/services/executor_runtime.py`，线程数 `ILCS_EXECUTOR_WORKERS`）：控制回路
（心跳、监控报警、流程推进）在主线程里每轮必跑，设备 I/O 按工位进线程池，一台设备卡住不拖慢别的
工位也不拖慢心跳。它 `LISTEN ilcs_queue`：新指令入队、新推进事件、转运完成时立即开始下一轮，
`ILCS_EXECUTOR_POLL_SEC` 只是兜底周期。`ILCS_EXECUTOR_MODE=serial` 退回逐条串行的旧回路。

契约（冻结草稿）：

- 当前从数据库持久化 `commands` 队列取指令，只取已到最早投递时刻、前置指令（转运）已完成的；
  执行门关闭时只取保持 / 终止。NATS JetStream 属于后续扩展，不是接真机前置条件
- 转运指令（`type=transfer`）投给承运工位，完成后按回执更新载具位置，等着它的设备动作才进候选
- 对账：设备实态 vs 最新检查点，不一致则批次 `fault`、指令 `unknown`
- 不自动重试 state=unknown 的指令
- `adapter_executions.command_id` 是唯一键，重复投递直接回放终态，不触发第二次动作
- 每小时按组织清理超过保留期且未被受控业务对象引用的暂存文件，清理动作写审计

运行：

```bash
cd ilcs
api/.venv/bin/python3.11 executor/main.py
```
