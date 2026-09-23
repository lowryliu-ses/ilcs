执行器入口为 `main.py`。它轮询业务库中的持久化指令，按工位配置调用模拟适配器或
`http_json_v1` 真实 HTTPS 网关驱动，异步轮询长任务并写检查点；真实设备遥测不会用设定值补造。

契约（冻结草稿）：

- 当前从数据库持久化 `commands` 队列取指令；NATS JetStream 属于后续扩展，不是接真机前置条件
- 对账：设备实态 vs 最新检查点，不一致则批次 `fault`、指令 `unknown`
- 不自动重试 state=unknown 的指令
- `adapter_executions.command_id` 是唯一键，重复投递直接回放终态，不触发第二次动作
- 每小时按组织清理超过保留期且未被受控业务对象引用的暂存文件，清理动作写审计

运行：

```bash
cd ilcs
api/.venv/bin/python3.11 executor/main.py
```
