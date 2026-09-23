# PostgreSQL 本地隔离恢复演练记录

- 日期：2026-09-22
- 数据库：PostgreSQL 16（临时容器，仅绑定 `127.0.0.1`）
- 源库：`ilcs_test`
- 隔离恢复库：`ilcs_restore_test`
- 应用迁移版本：`0007_account_lifecycle`
- 范围：数据库逻辑备份、隔离恢复、全量数据一致性、应用启动兼容性

## 演练步骤与结果

1. 在源库从空库执行全部 Alembic 迁移并播种测试主数据。
2. 使用 `pg_dump -Fc` 生成自定义格式逻辑备份。
3. 新建独立恢复库，使用 `pg_restore --exit-on-error` 恢复；未改写源库。
4. 对恢复库执行 `scripts/migrate.py status`：当前版本与应用期待均为
   `0007_account_lifecycle`。
5. 对恢复库执行 `scripts/migrate.py verify`：组织归属、样本关联、库存流水与账面、
   历史审核状态、方案批准状态、人员和资产前置主数据核对全部通过。
6. 分别对源库和恢复库执行全量纯数据转储，去除 PostgreSQL 16 每次随机生成的
   `restrict/unrestrict` 标记后比较 SHA-256：

   ```text
   source   457832140c330e58ccfc0a7340fa0ebefaea1564c33a5924429d91cd73e286b0
   restored 457832140c330e58ccfc0a7340fa0ebefaea1564c33a5924429d91cd73e286b0
   ```

7. 使用恢复库进入 FastAPI 完整生命周期并请求 `/api/health`：HTTP 200，
   `status=ok`，当前/期待迁移版本一致。
8. 停止临时容器；容器使用 `--rm` 创建，停止后自动删除。

## 结论与边界

数据库备份可恢复、恢复数据与源数据一致，且当前应用版本可在恢复库上正常启动。本次是本机
隔离演练；附件打包、逐文件摘要、原服务状态恢复和目标机报告生成由
`scripts/backup-and-recovery-drill.sh` 实现。目标服务器仍需运行该脚本并留存其
`recovery-report.md`，本记录不能替代目标机的磁盘、权限、Compose 和真实数据验证。
